"""清洗归一：三态校验、review_id 去重、空结果告警、脱敏、先定 slice。

B7「先定 slice」是关键纪律：切片维度必须在聚类之前声明，防止「先聚类再找差异」。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ur_core import (EVIDENCE_FACT, Record, audit, now_iso, read_metrics,
                     read_reviews, redact, setup_console, validate)

DEFAULT_DIMENSIONS = ["channel", "rating_band", "version", "time_window", "playtime_band", "lang"]
UNSPECIFIED = "未指明"


def _rating_band(rating: float | None, rating_max: float | None) -> str:
    if rating is None or rating_max in (None, 0):
        return UNSPECIFIED
    try:
        r = float(rating) / float(rating_max) * 5.0
    except (TypeError, ValueError, ZeroDivisionError):
        return UNSPECIFIED
    if r <= 2:
        return "low(1-2星)"
    if r < 4:
        return "mid(3星)"
    return "high(4-5星)"


def _playtime_band(hours: float | None) -> str:
    if hours is None:
        return UNSPECIFIED
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return UNSPECIFIED
    if h < 2:
        return "new(<2h)"
    if h <= 20:
        return "mid(2-20h)"
    return "veteran(>20h)"


def _time_window(day: str, windows: list[dict]) -> str:
    for w in windows:
        if w.get("start", "") <= day <= w.get("end", ""):
            return w.get("id", UNSPECIFIED)
    return UNSPECIFIED


def slice_of(r: dict, windows: list[dict]) -> dict:
    """结构层维度（客观字段，最可靠）先于聚类定义。"""
    return {
        "channel": r.get("channel_key") or UNSPECIFIED,
        "rating_band": _rating_band(r.get("rating"), r.get("rating_max")),
        "version": (r.get("app_version") or UNSPECIFIED),
        "time_window": _time_window(str(r.get("day", "")), windows),
        "playtime_band": _playtime_band(r.get("playtime_hours")),
        "lang": (r.get("lang") or UNSPECIFIED),
    }


def clean(workspace: str | Path, project: dict | None = None,
          slices_cfg: list[str] | None = None, redact_pii: bool = True) -> dict:
    project = project or {}
    w = Path(workspace)
    analysis = w / "data" / "analysis"
    analysis.mkdir(parents=True, exist_ok=True)
    windows = project.get("time_windows", [])
    dimensions = slices_cfg or DEFAULT_DIMENSIONS
    alerts: list[str] = []

    raw_reviews = read_reviews(w / "data" / "reviews.jsonl")
    raw_metrics = read_metrics(w / "data" / "metrics.csv")
    if not raw_reviews:
        alerts.append("空结果告警：reviews.jsonl 无数据。0 条 ≠ 没有评论，请检查采集日志与四步法进度")
    if not raw_metrics:
        alerts.append("空结果告警：metrics.csv 无数据。不得判为『本周没有指标』")

    seen: set[str] = set()
    clean_reviews: list[dict] = []
    rejected = duplicate = 0
    for r in raw_reviews:
        rec = Record.from_dict(r)
        ok, reason, warn = validate(rec)
        if not ok:
            rejected += 1
            continue
        rid = str(rec.review_id)
        if rid in seen:
            duplicate += 1
            continue
        seen.add(rid)
        row = rec.review_row()
        if not isinstance(row.get("labels"), list):
            row["labels"] = []
        row["evidence_kind"] = EVIDENCE_FACT
        row["_slices"] = slice_of(row, windows)
        # 脱敏只作用于快照/日志字段；评论逐字原句 review_text 保持不动
        row["raw"] = redact(str(row.get("raw", "")), redact_pii)
        if warn:
            row["warn"] = (row.get("warn", "") + "；" + warn).strip("；")
        clean_reviews.append(row)

    out_path = analysis / "reviews_clean.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for row in clean_reviews:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    slices_doc = {
        "declared_before_clustering": True,
        "dimensions": dimensions,
        "time_windows": windows,
        "equal_length_required": bool(project.get("analysis", {}).get("equal_length_windows_required", True)),
        "buckets": {
            "rating_band": ["low(1-2星)", "mid(3星)", "high(4-5星)", UNSPECIFIED],
            "playtime_band": ["new(<2h)", "mid(2-20h)", "veteran(>20h)", UNSPECIFIED],
        },
        "note": "无值一律归入「未指明」桶；0 出现的组仍要显式显示",
        "created_at": now_iso(),
    }
    (analysis / "slices.json").write_text(
        json.dumps(slices_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    audit(w, "clean", actor="pipeline",
          note=f"reviews={len(clean_reviews)}; rejected={rejected}; duplicate={duplicate}")
    return {
        "reviews_in": len(raw_reviews), "reviews_out": len(clean_reviews),
        "metrics_in": len(raw_metrics), "rejected": rejected, "duplicate": duplicate,
        "alerts": alerts, "slices": slices_doc,
        "clean_path": str(out_path),
    }


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="清洗归一与先定 slice")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--project", default="")
    args = ap.parse_args()
    project = {}
    if args.project and Path(args.project).exists():
        try:
            import yaml
            project = yaml.safe_load(Path(args.project).read_text(encoding="utf-8")) or {}
        except ImportError:
            pass
    print(json.dumps(clean(args.workspace, project), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
