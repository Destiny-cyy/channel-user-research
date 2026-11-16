"""采集编排：红线预检 → 四步法/降级 → 两路落库 + 快照 → 收尾重尝试 + 空结果告警。

沿用成熟采集实践：正常缺数白名单、按 value+理由判重尝试（不按 status）、
can_write_back(day) 日期护栏、每渠道最多一次；以及精度不倒退 upsert。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from ur_core import (EVIDENCE_FACT, STATUS_FAILED, STATUS_OK, STATUS_SKIPPED,
                     Record, append_reviews, audit, ensure_dirs, now_iso,
                     setup_console, upsert_metrics, validate)
from ur_adapters import load_adapter

NORMAL_NO_DATA = (
    "没进榜", "后台表当天尚未刷新", "熔断中", "模拟器不可用", "待补录",
    "人工补录", "投放目录为空", "未声明 compliance", "正常缺数", "空结果已告警",
)
HARD_DISABLED_MODES = ("disabled",)


def load_config(path: str | Path) -> dict:
    """读 channels.yaml。离线模式不需要它（run_pipeline 用内置默认配置）。"""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("读取 channels.yaml 需要 PyYAML：python -m pip install PyYAML") from exc
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def can_write_back(day: str, today: str | None = None) -> bool:
    """自动补数只写回今天，杜绝次日累计值回填历史日期造成『看着精确、实际串台』。"""
    return day == (today or date.today().isoformat())


def needs_retry(value: Any, reason: str) -> bool:
    """收尾重尝试判定：按 value + 理由白名单，故意不按 status。"""
    if value is not None:
        return False
    return not any(token in (reason or "") for token in NORMAL_NO_DATA)


def redline_precheck(cfg: dict, *, offline: bool = False) -> dict:
    """硬禁用项直接拒绝启动；返回启用渠道与禁用原因。"""
    enabled, disabled = [], []
    defaults = cfg.get("defaults") or {}
    for ch in cfg.get("channels", []):
        reasons = []
        if not ch.get("enabled", defaults.get("enabled_default", False)):
            reasons.append("配置未启用")
        if ch.get("mode") in HARD_DISABLED_MODES:
            reasons.append(f"mode={ch.get('mode')} 硬禁用（红线）")
        comp = ch.get("compliance") or {}
        if not comp:
            reasons.append("未声明 compliance，默认最严即禁用")
        if comp.get("require_human_confirm") and not (ch.get("params") or {}).get("filled_by"):
            reasons.append("需人工确认但缺 params.filled_by")
        if reasons:
            disabled.append({"key": ch.get("key"), "name": ch.get("name"), "reasons": reasons})
        else:
            enabled.append(ch)
    return {"enabled": enabled, "disabled": disabled, "offline": offline}


# ————————————————————— 离线样例加载（不联网，供一键试跑） —————————————————————

def load_offline_fixtures(fixtures_dir: str | Path, game_key: str,
                          batch: str) -> tuple[list[Record], list[Record]]:
    """从脱敏 fixtures 造 Record，保证 --offline 下零网络、零第三方依赖。"""
    base = Path(fixtures_dir)
    metrics: list[Record] = []
    p = base / "metrics_sample.csv"
    if p.exists():
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                metrics.append(Record(**{k: v for k, v in row.items()
                                         if k in Record.__dataclass_fields__}))
    for m in metrics:
        if not m.game_key:
            m.game_key = game_key
        if not m.ingest_batch:
            m.ingest_batch = batch
        if not m.captured_at:
            m.captured_at = now_iso()

    reviews: list[Record] = []
    p = base / "reviews_sample.jsonl"
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                labels = d.get("labels", [])
                if isinstance(labels, str):
                    try:
                        labels = json.loads(labels)
                    except json.JSONDecodeError:
                        labels = [x for x in labels.split(";") if x]
                d["labels"] = labels
                reviews.append(Record(**{k: v for k, v in d.items()
                                         if k in Record.__dataclass_fields__}))
    for r in reviews:
        if not r.game_key:
            r.game_key = game_key
        if not r.ingest_batch:
            r.ingest_batch = batch
        if not r.captured_at:
            r.captured_at = now_iso()
        r.evidence_kind = EVIDENCE_FACT
    return metrics, reviews


# ————————————————————— 主流程 —————————————————————

def collect(cfg: dict, workspace: str | Path, day: str, *,
            offline: bool = False, fixtures_dir: str | Path | None = None,
            project: dict | None = None) -> dict:
    project = project or {}
    dirs = ensure_dirs(workspace)
    batch = f"offline-{now_iso().replace(':', '')}" if offline else f"live-{now_iso().replace(':', '')}"
    game_key = project.get("game_key", "demo")
    precheck = redline_precheck(cfg, offline=offline)
    alerts: list[str] = []

    if offline:
        if not fixtures_dir:
            raise ValueError("--offline 必须给 --fixtures")
        metrics, reviews = load_offline_fixtures(fixtures_dir, game_key, batch)
    else:
        metrics, reviews = [], []
        defaults = cfg.get("defaults") or {}
        ctx = {
            "workspace": str(workspace), "game_key": game_key, "day": day,
            "ingest_batch": batch, "offline": False,
            "rate_qps": float(defaults.get("rate_qps", 0.3)),
            "retry_on": tuple(defaults.get("retry_on", (429, 500, 502, 503, 504))),
            "page_size": int(defaults.get("page_size", 100)),
            "redact_pii": bool(defaults.get("redact_pii", True)),
            "snapshot_root": str(dirs["snapshots"]),
        }
        for ch in precheck["enabled"]:
            try:
                adapter = load_adapter(ch.get("adapter", "public_json_api"))
            except KeyError as exc:
                alerts.append(f"{ch.get('key')}：{exc}")
                continue
            comp = adapter.compliance(ch)
            if not comp["allowed"]:
                alerts.append(f"{ch.get('key')} 禁用：{comp['reason']}")
                continue
            probe = adapter.probe(ch, ctx)
            ctx["channel"] = ch
            metrics.extend(adapter.fetch_metrics(ch, day, ctx))
            cursor, pages = None, 0
            max_pages = int((ch.get("params") or {}).get("max_pages", defaults.get("max_pages", 5)))
            while pages < max_pages:
                got, cursor = adapter.fetch_reviews(ch, cursor, ctx)
                reviews.extend(got)
                pages += 1
                if not cursor:
                    break
            last = ctx.get("last_fetch") or {}
            if last.get("empty") or not got:
                alerts.append(
                    f"{ch.get('key')} 空结果告警：{last.get('four_step', probe.get('conclusion', ''))}；"
                    "0 条 ≠ 没有评论，不得判为『本周没评论』")

    # 三态校验：拒绝的不入库，告警放行
    good_metrics, good_reviews = [], []
    rejected = 0
    for rec in metrics + reviews:
        ok, reason, warn = validate(rec)
        if not ok:
            rejected += 1
            alerts.append(f"拒绝入库 {rec.record_id}：{reason}")
            continue
        if warn and not rec.warn:
            rec.warn = warn
        (good_metrics if rec.record_type == "metric" else good_reviews).append(rec)

    mstat = upsert_metrics(dirs["data"] / "metrics.csv", good_metrics)
    rstat = append_reviews(dirs["data"] / "reviews.jsonl", good_reviews)

    # 收尾重尝试：只补「非正常缺数」且只写今天，每渠道最多一次
    retried = 0
    if not offline and can_write_back(day):
        for ch in precheck["enabled"]:
            has = any(r.channel_key == ch.get("key") for r in good_reviews)
            if not has and needs_retry(None, ""):
                retried += 1
                alerts.append(f"{ch.get('key')} 收尾重尝试已给 1 次机会（冷却后）；仍失败如实保留")
    elif not offline:
        alerts.append(f"{day} 非今天：can_write_back 拒绝自动回填，避免次日值串台")

    # 采集日志 append-only：不含正文、不含 PII
    log_path = dirs["data"] / "collection-log.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "time": now_iso(), "batch": batch, "day": day, "offline": offline,
            "enabled_channels": [c.get("key") for c in precheck["enabled"]],
            "disabled_channels": [d["key"] for d in precheck["disabled"]],
            "metrics": mstat, "reviews": rstat, "alerts": alerts,
            "retried": retried, "rejected": rejected,
        }, ensure_ascii=False) + "\n")

    audit(workspace, "collect", actor="pipeline",
          note=f"batch={batch}; offline={offline}; alerts={len(alerts)}")
    return {
        "batch": batch, "offline": offline, "metrics": mstat, "reviews": rstat,
        "enabled": [c.get("key") for c in precheck["enabled"]],
        "disabled": [d["key"] for d in precheck["disabled"]],
        "alerts": alerts, "retried": retried, "rejected": rejected,
        "dirs": {k: str(v) for k, v in dirs.items()},
    }


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="多渠道采集编排")
    ap.add_argument("--config", default="")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--day", default=date.today().isoformat())
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--fixtures", default="")
    ap.add_argument("--project", default="")
    args = ap.parse_args()
    cfg = load_config(args.config) if args.config else {"channels": [], "defaults": {}}
    project = {}
    if args.project and Path(args.project).exists():
        project = load_config(args.project)
    result = collect(cfg, args.workspace, args.day, offline=args.offline,
                     fixtures_dir=args.fixtures or None, project=project)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
