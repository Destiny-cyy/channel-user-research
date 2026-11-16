"""统一数据契约 + 落库内核。

所有脚本只依赖本模块，不许各定义一套字段、各算一套口径。
设计来源：渠道数值采集的成熟工程做法——精度量纲/原始值保真/三通道状态、
原子写/精度不倒退、三态校验、
sha256 指纹验收。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"

# —— 列结构定稿：加维度优先塞 note / labels / topic_*，不要加列 ——
COMMON_FIELDS = [
    "record_id", "record_type", "game_key", "channel_key", "channel_name",
    "day", "captured_at", "source", "source_url", "status", "precision",
    "raw", "warn", "error", "note", "snapshot_path", "ingest_batch",
    "compliance_tag",
]
METRIC_FIELDS = ["metric_key", "metric_value", "metric_unit", "metric_kind"]
REVIEW_FIELDS = [
    "review_id", "review_text", "rating", "rating_max", "review_time",
    "app_version", "playtime_hours", "lang", "country", "votes_up",
    "developer_replied", "is_early_access", "labels", "topic_id",
    "topic_confidence", "evidence_kind",
]
METRICS_CSV_FIELDS = COMMON_FIELDS + METRIC_FIELDS
REVIEWS_JSONL_FIELDS = COMMON_FIELDS + REVIEW_FIELDS

STATUS_OK, STATUS_FAILED, STATUS_SKIPPED = "ok", "failed", "skipped"
EVIDENCE_FACT, EVIDENCE_INFER, EVIDENCE_ADVICE = "数据事实", "模型推断", "运营建议"


def setup_console() -> None:
    """Windows 控制台 GBK 打不出中文时统一切 UTF-8。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Record:
    """一表两型的唯一契约。数值行用 metric_row()，评论行用 review_row()。"""
    record_id: str = ""
    record_type: str = "metric"
    game_key: str = ""
    channel_key: str = ""
    channel_name: str = ""
    day: str = ""
    captured_at: str = field(default_factory=now_iso)
    source: str = ""
    source_url: str = ""
    status: str = STATUS_OK
    precision: float = 0
    raw: str = ""
    warn: str = ""
    error: str = ""
    note: str = ""
    snapshot_path: str = ""
    ingest_batch: str = ""
    compliance_tag: str = "public_anonymous"

    # 数值专用
    metric_key: str = ""
    metric_value: Any = None
    metric_unit: str = ""
    metric_kind: str = "outer"   # outer=外显值 / real=回传真值

    # 评论专用
    review_id: str = ""
    review_text: str = ""
    rating: Any = None
    rating_max: Any = None
    review_time: str = ""
    app_version: str = ""
    playtime_hours: Any = None
    lang: str = ""
    country: str = ""
    votes_up: int = 0
    developer_replied: bool = False
    is_early_access: bool = False
    labels: list[str] = field(default_factory=list)
    topic_id: int | None = None
    topic_confidence: float | None = None
    evidence_kind: str = EVIDENCE_FACT

    def metric_row(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        return {k: d.get(k) for k in METRICS_CSV_FIELDS}

    def review_row(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        return {k: d.get(k) for k in REVIEWS_JSONL_FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


# ——————————————————————————— 精度与可信度 ———————————————————————————

def _prec(value: Any) -> float:
    """脏精度当最差（最大）处理，保证不会被误判成更精确。"""
    try:
        v = float(value)
        return v if v >= 0 else 9999.0
    except (TypeError, ValueError):
        return 9999.0


def credibility(precision: Any) -> str:
    """精度 → 可信度三档文案（由精度量纲机械映射，不掺主观判断）。"""
    p = _prec(precision)
    if p <= 1:
        return "精确·可算增量"
    if p <= 1000:
        return "万级一位·仅趋势"
    return "万级整数·仅量级"


def change_is_meaningful(delta: float | None, precision: Any) -> bool:
    """变化量小于最小可分辨单位 → 不可汇报，报告写「—」绝不写 0。"""
    if delta is None:
        return False
    return abs(float(delta)) >= _prec(precision)


def confidence_band(n: int) -> str:
    """样本量分档。n<20 不给百分比，n<50 仅供参考，n>=50 才给一位小数。"""
    if n < 20:
        return "sample_too_small"
    if n < 50:
        return "indicative"
    return "reportable"


def fmt_pct(count: int, denom: int) -> str:
    """统一占比格式：永不两位小数，永远带绝对条数与分母。

    n 指「该组样本量」（分母 denom），不是某主题的命中条数：
    一组只有 12 条评论时，即便某主题占 8% 也不该给出看起来精确的百分比。
    """
    if denom is None or denom <= 0:
        return "—（无样本）"
    band = confidence_band(denom)
    if band == "sample_too_small":
        return f"方向性：{count}/{denom}（样本不足，仅供参考）"
    pct = count * 100.0 / denom
    if band == "indicative":
        return f"{round(pct)}%（{count}/{denom}，仅供参考）"
    return f"{pct:.1f}%（{count}/{denom}）"


# ——————————————————————————— 三态校验 ———————————————————————————

def validate(rec: Record) -> tuple[bool, str, str]:
    """返回 (是否入库, 拒绝原因, 告警提示)。告警不拦数据，只进报告异常区。"""
    warn = ""
    if not rec.record_id:
        return False, "缺少 record_id", warn
    if rec.record_type not in ("metric", "review"):
        return False, f"record_type 非法：{rec.record_type}", warn
    if rec.status not in (STATUS_OK, STATUS_FAILED, STATUS_SKIPPED):
        return False, f"status 非法：{rec.status}", warn
    if rec.compliance_tag not in ("public_anonymous", "manual_confirmed"):
        return False, f"compliance_tag 非法：{rec.compliance_tag}", warn
    if rec.status == STATUS_FAILED and not rec.error:
        return False, "status=failed 必须带 error（采不到必须明说，绝不伪装成功）", warn
    if rec.status == STATUS_SKIPPED and not (rec.error or rec.note):
        warn = "skipped 未写明原因，建议补齐四步法进度"
    if rec.compliance_tag == "manual_confirmed" and not rec.note:
        warn = "自有账号/后台取数须在 note 里带填写人"

    if rec.record_type == "metric":
        if not rec.metric_key:
            return False, "数值行缺少 metric_key", warn
        if rec.metric_value not in (None, ""):
            try:
                v = float(rec.metric_value)
                if v < 0:
                    return False, "metric_value 为负数", warn
            except (TypeError, ValueError):
                return False, f"metric_value 非数字：{rec.metric_value}", warn
        elif rec.status == STATUS_OK:
            warn = (warn + "；" if warn else "") + "状态 ok 但数值为空，请确认是正常缺数还是采集失败"
        if rec.metric_kind not in ("outer", "real"):
            return False, f"metric_kind 非法：{rec.metric_kind}", warn
        if _prec(rec.precision) >= 9999:
            warn = (warn + "；" if warn else "") + "precision 缺失或非法，按最差精度处理"
    else:
        if not rec.review_id:
            return False, "评论行缺少 review_id（引用无法定位）", warn
        if not rec.review_text:
            return False, "评论行缺少逐字原句 review_text", warn
        if rec.topic_id == -1:
            warn = (warn + "；" if warn else "") + "离群点单列，不强行归并"
    return True, "", warn


# ——————————————————————————— 脱敏 ———————————————————————————

_PII_PATTERNS = [
    (re.compile(r"1[3-9]\d{9}"), "<手机号>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<邮箱>"),
    (re.compile(r"(?<![\w])@[\w\u4e00-\u9fa5_-]{2,20}"), "<@ID>"),
    (re.compile(r"(?<![\w])\d{8,}(?![\w])"), "<长数字ID>"),
]


def redact(text: str, enabled: bool = True) -> str:
    """脱敏默认开。评论原句仍逐字保留在 reviews.jsonl，此处只作用于快照/日志。"""
    if not enabled or not text:
        return text or ""
    out = str(text)
    for pat, repl in _PII_PATTERNS:
        out = pat.sub(repl, out)
    return out


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ——————————————————————————— 落库（原子写 + 精度不倒退） ———————————————————————————

def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def atomic_write_csv(path: str | Path, rows: Iterable[dict], fieldnames: list[str]) -> None:
    """先写 .tmp 再 os.replace。Windows 上被 Excel 占用时明确报错，不静默覆盖。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: _cell(row.get(k)) for k in fieldnames})
    try:
        os.replace(tmp, path)
    except OSError as exc:
        raise RuntimeError(
            f"原子写失败（文件可能被 Excel/预览面板占用，WinError {getattr(exc, 'winerror', '?')}）：{path}。"
            "已保留 .tmp，未覆盖原文件；请关闭占用程序后重跑。"
        ) from exc


def read_metrics(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _metric_key(row: dict) -> tuple:
    return (row.get("day", ""), row.get("channel_key", ""),
            row.get("metric_key", ""), row.get("metric_kind", ""))


def upsert_metrics(path: str | Path, records: list[Record]) -> dict:
    """同一 (日期,渠道,指标,口径) 重跑时，旧值更精确则保留旧值并备注留痕。"""
    path = Path(path)
    index: dict[tuple, dict] = {_metric_key(r): r for r in read_metrics(path)}
    added = replaced = kept = rejected = 0
    for rec in records:
        ok, reason, warn = validate(rec)
        if not ok:
            rejected += 1
            continue
        if warn and not rec.warn:
            rec.warn = warn
        row = rec.metric_row()
        key = _metric_key(row)
        if key in index:
            old = index[key]
            if _prec(old.get("precision")) < _prec(row.get("precision")):
                parts = [p for p in (old.get("note", ""),
                                     f"已保留当天精确值（本次来源精度较低：{row.get('source', '')}）") if p]
                old["note"] = "；".join(parts)
                kept += 1
                continue
            index[key] = row
            replaced += 1
        else:
            index[key] = row
            added += 1
    rows = sorted(index.values(), key=_metric_key)
    atomic_write_csv(path, rows, METRICS_CSV_FIELDS)
    return {"total": len(rows), "added": added, "replaced": replaced,
            "kept_precise": kept, "rejected": rejected}


def read_reviews(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def append_reviews(path: str | Path, records: list[Record]) -> dict:
    """append-only，按 review_id 去重，绝不覆盖已有原文。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {r.get("review_id") for r in read_reviews(path)}
    added = dup = rejected = 0
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            ok, reason, warn = validate(rec)
            if not ok:
                rejected += 1
                continue
            if rec.review_id in existing:
                dup += 1
                continue
            if warn and not rec.warn:
                rec.warn = warn
            f.write(json.dumps(rec.review_row(), ensure_ascii=False) + "\n")
            existing.add(rec.review_id)
            added += 1
    return {"added": added, "duplicate": dup, "rejected": rejected, "total": len(existing)}


# ——————————————————————————— 证据链 / 审计 / 指纹 ———————————————————————————

def save_snapshot(root: str | Path, channel_key: str, day: str,
                  payload: dict, redact_pii: bool = True) -> str:
    """原始响应副本：只增不改，含请求参数、时间戳、HTTP 状态、脱敏后内容 + 哈希指纹。"""
    safe_channel = re.sub(r"[^\w\u4e00-\u9fa5-]", "_", channel_key)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    text = redact(text, redact_pii)
    digest = sha256_text(text)
    path = Path(root) / safe_channel / day / f"{digest[:16]}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return str(path)


def audit(workspace: str | Path, action: str, *, actor: str = "system",
          input_fingerprint: str = "", output_fingerprint: str = "",
          human_confirmed: bool = False, note: str = "") -> None:
    """append-only 审计轨迹：谁、何时、做了什么、确认了什么。"""
    path = Path(workspace) / "data" / "audit" / "append-only.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": now_iso(), "action": action, "actor": actor,
        "input_sha256": input_fingerprint, "output_sha256": output_fingerprint,
        "human_confirmed": human_confirmed, "note": note,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def sha256_tree(root: str | Path, skip_parts: tuple[str, ...] = ("__pycache__", ".tmp", "out")) -> dict:
    """跑前跑后指纹：确认 skill 目录与 fixtures 零改动。"""
    root = Path(root)
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in skip_parts or part.endswith(".tmp") or part.endswith(".pyc") for part in rel.parts):
            continue
        try:
            result[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
    return result


def fingerprint_diff(before: dict, after: dict) -> dict:
    """返回新增/修改/删除清单。"""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    return {"added": added, "removed": removed, "changed": changed}


def ensure_dirs(workspace: str | Path) -> dict[str, Path]:
    w = Path(workspace)
    dirs = {
        "data": w / "data",
        "analysis": w / "data" / "analysis",
        "snapshots": w / "data" / "snapshots",
        "inbox": w / "data" / "inbox",
        "audit": w / "data" / "audit",
        "out": w / "out",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


# ——————————————————————————— 报告口径中心 ———————————————————————————

def build_rows(metrics: list[dict], reviews: list[dict]) -> list[dict]:
    """唯一口径计算中心：所有出口（md/xlsx/json）只消费它的返回值，不许各算一套。

    每行 = 一个渠道一个指标在一个日期的展示口径，含可信度文案与「变化是否可汇报」判定。
    """
    rows: list[dict] = []
    grouped: dict[tuple, list[dict]] = {}
    for m in metrics:
        grouped.setdefault((m.get("channel_key", ""), m.get("metric_key", ""),
                            m.get("metric_kind", "")), []).append(m)
    for (channel, metric_key, kind), items in sorted(grouped.items()):
        items.sort(key=lambda r: r.get("day", ""))
        prev = None
        for r in items:
            try:
                value = float(r["metric_value"]) if r.get("metric_value") not in (None, "") else None
            except (TypeError, ValueError):
                value = None
            delta = None
            if prev is not None and value is not None:
                delta = value - prev
            precision = r.get("precision", 1)
            rows.append({
                "channel_key": channel,
                "channel_name": r.get("channel_name", channel),
                "day": r.get("day", ""),
                "metric_key": metric_key,
                "metric_kind": kind,             # 口径 ≠ 精度，两列分开
                "metric_unit": r.get("metric_unit", ""),
                "value": value,
                "precision": precision,
                "credibility": credibility(precision),
                "delta": delta if change_is_meaningful(delta, precision) else None,
                "delta_display": (f"{delta:+.1f}" if change_is_meaningful(delta, precision) else "—"),
                "status": r.get("status", ""),
                "source": r.get("source", ""),
                "captured_at": r.get("captured_at", ""),
                "warn": r.get("warn", ""),
                "note": r.get("note", ""),
            })
            if value is not None:
                prev = value
    return rows
