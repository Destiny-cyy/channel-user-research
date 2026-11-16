"""唯一量化中心：占比、五套交叉矩阵、严重度代理、趋势、机会点排序。

所有百分比一律走 ur_core.fmt_pct()（分母 + 绝对条数 + 小样本警告 + 永不两位小数）。
交叉结论只写「同期出现」，绝不写因果。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from ur_core import (EVIDENCE_FACT, audit, fmt_pct, now_iso, read_metrics,
                     setup_console)

DIMENSIONS = ["channel", "rating_band", "version", "time_window", "playtime_band", "lang"]
MATRIX_FIELDS = ["dimension", "slice_value", "topic_id", "working_label",
                 "count", "denom", "pct", "warn", "evidence_kind"]
UNSPECIFIED = "未指明"


def _working_label(topic: dict) -> str:
    """机械工作标签，由高频字词拼接；不是结论，正式命名需 AI + 人工确认。"""
    terms = [t for t in topic.get("top_terms", []) if len(t) >= 2][:3]
    return "/".join(terms) if terms else f"T{topic['topic_id']:02d}"


def _load(workspace: Path) -> tuple[list[dict], dict, list[dict], list[dict]]:
    a = workspace / "data" / "analysis"
    reviews = [json.loads(x) for x in (a / "reviews_labeled.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    topics = json.loads((a / "topics.json").read_text(encoding="utf-8"))
    all_reviews = [json.loads(x) for x in (workspace / "data" / "reviews.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    metrics = read_metrics(workspace / "data" / "metrics.csv")
    return reviews, topics, all_reviews, metrics


def build_matrix(reviews: list[dict], topics: dict) -> tuple[list[dict], dict]:
    """五套交叉矩阵：维度×主题，每格带 count/denom/占比或方向性。"""
    label_of = {t["topic_id"]: _working_label(t) for t in topics["topics"]}
    tid_list = [t["topic_id"] for t in topics["topics"]]
    rows: list[dict] = []
    warnings: list[str] = []

    for dim in DIMENSIONS:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in reviews:
            sl = r.get("_slices") or {}
            groups[str(sl.get(dim, UNSPECIFIED))].append(r)
        for value in sorted(groups):
            members = groups[value]
            denom = len(members)
            for tid in tid_list:
                count = sum(1 for r in members if r.get("topic_id") == tid)
                pct = fmt_pct(count, denom)
                warn = "样本不足，仅供参考" if denom < 20 else ""
                if warn and dim in ("channel", "time_window"):
                    warnings.append(f"{dim}={value} × T{tid:02d}：{warn}")
                rows.append({
                    "dimension": dim, "slice_value": value,
                    "topic_id": f"T{tid:02d}", "working_label": label_of[tid],
                    "count": count, "denom": denom, "pct": pct,
                    "warn": warn, "evidence_kind": EVIDENCE_FACT,
                })
            # 0 出现的组仍要显式显示（count=0 也写一行），离群点单列
            out_count = sum(1 for r in members if r.get("topic_id") == -1)
            rows.append({
                "dimension": dim, "slice_value": value, "topic_id": "T-1",
                "working_label": "离群点（不强行归并）", "count": out_count,
                "denom": denom, "pct": fmt_pct(out_count, denom),
                "warn": "样本不足，仅供参考" if denom < 20 else "",
                "evidence_kind": EVIDENCE_FACT,
            })
    return rows, {"warnings": warnings}


def _low_share(rows: list[dict], tid: int, window: str | None = None) -> tuple[int, int]:
    """同主题在 1–2 星（low）中的条数与分母，用作严重度代理。"""
    for r in rows:
        if r["dimension"] != "rating_band" or r["topic_id"] != f"T{tid:02d}":
            continue
        if not r["slice_value"].startswith("low"):
            continue
        return int(r["count"]), int(r["denom"])
    return (0, 0)


def _high_share(rows: list[dict], tid: int) -> tuple[int, int]:
    for r in rows:
        if (r["dimension"] == "rating_band" and r["topic_id"] == f"T{tid:02d}"
                and r["slice_value"].startswith("high")):
            return int(r["count"]), int(r["denom"])
    return (0, 0)


def _topic_window_count(reviews: list[dict], tid: int, window: str,
                        low_only: bool = True) -> tuple[int, int]:
    members = [r for r in reviews
               if (r.get("_slices") or {}).get("time_window") == window
               and (not low_only or str((r.get("_slices") or {}).get("rating_band", "")).startswith("low"))]
    return (sum(1 for r in members if r.get("topic_id") == tid), len(members))


def _rating_drop(metrics: list[dict], windows: list[dict]) -> list[dict]:
    """找评分下滑的渠道：比较两个等长时间窗的末值，变化须大于最小可分辨单位。"""
    if len(windows) < 2:
        return []
    w1, w2 = windows[0], windows[1]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for m in metrics:
        if m.get("metric_key") == "rating":
            grouped[m.get("channel_key", "")].append(m)
    drops = []
    for channel, items in grouped.items():
        def last_of(win: dict) -> float | None:
            cand = [x for x in items if win.get("start", "") <= x.get("day", "") <= win.get("end", "")]
            if not cand:
                return None
            try:
                return float(sorted(cand, key=lambda x: x.get("day", ""))[-1]["metric_value"])
            except (ValueError, KeyError, TypeError):
                return None
        v1, v2 = last_of(w1), last_of(w2)
        if v1 is None or v2 is None:
            continue
        precision = 0.1
        for x in items:
            try:
                precision = float(x.get("precision") or 0.1)
                break
            except (TypeError, ValueError):
                pass
        if abs(v2 - v1) < precision:
            continue
        if v2 < v1:
            drops.append({"channel": channel, "from": v1, "to": v2,
                          "w1": w1.get("id"), "w2": w2.get("id"),
                          "name": items[0].get("channel_name", channel)})
    return drops


def cross_signals(reviews: list[dict], topics: dict, metrics: list[dict],
                  windows: list[dict]) -> list[dict]:
    """评分下滑 ↔ 低分主题占比上升：只写「同期出现」，绝不写因果。"""
    drops = _rating_drop(metrics, windows)
    if len(windows) < 2 or not drops:
        return []
    w1, w2 = windows[0].get("id"), windows[1].get("id")
    signals = []
    for d in drops:
        best = None
        for t in topics["topics"]:
            tid = t["topic_id"]
            c1, n1 = _topic_window_count(reviews, tid, str(w1))
            c2, n2 = _topic_window_count(reviews, tid, str(w2))
            if n1 == 0 or n2 == 0:
                continue
            r1, r2 = c1 / n1, c2 / n2
            rise = r2 - r1
            if best is None or rise > best["rise"]:
                best = {"tid": tid, "label": _working_label(t), "rise": rise,
                        "c1": c1, "n1": n1, "c2": c2, "n2": n2,
                        "w1_pct": fmt_pct(c1, n1), "w2_pct": fmt_pct(c2, n2)}
        if not best or best["rise"] <= 0:
            continue
        reps = [r["review_id"] for r in next(
            (t["representative_docs"] for t in topics["topics"] if t["topic_id"] == best["tid"]), [])][:3]
        signals.append({
            "channel": d["channel"], "channel_name": d["name"],
            "metric": "rating", "from": d["from"], "to": d["to"],
            "window_from": w1, "window_to": w2,
            "topic": f"T{best['tid']:02d}", "working_label": best["label"],
            "w1": best["w1_pct"], "w2": best["w2_pct"],
            "review_ids": reps,
            "wording": (f"「{d['name']}」评分 {d['from']:.1f} → {d['to']:.1f} 与"
                        f"「{best['label']}」主题在低分评论中占比上升同期出现"
                        f"（{w1} {best['w1_pct']} → {w2} {best['w2_pct']}；同期出现，非因果）"),
            "evidence_kind": "数据事实 + 模型推断（工作标签为机械生成，待 AI 命名与人工确认）",
        })
    return signals


def opportunities(rows: list[dict], topics: dict, weights: dict,
                  total: int) -> list[dict]:
    """机会点排序：影响面 × 严重度 × 可行动性 × 商业价值，权重显式可否决。"""
    wi = float(weights.get("impact", 0.35))
    ws = float(weights.get("severity", 0.30))
    wa = float(weights.get("actionability", 0.20))
    wb = float(weights.get("business_value", 0.15))
    out = []
    for t in topics["topics"]:
        tid = t["topic_id"]
        size = int(t.get("size", 0))
        impact = min(1.0, (size / max(total, 1)) * 2)
        lc, ln = _low_share(rows, tid)
        hc, hn = _high_share(rows, tid)
        low_rate = lc / max(ln, 1)
        high_rate = hc / max(hn, 1)
        severity = min(1.0, max(0.0, (low_rate - high_rate) + 0.5))
        actionability = 0.6   # 初始估计，需业务确认（权重与估计值都写进报告）
        business_value = 0.5  # 初始估计，需业务确认
        score = wi * impact + ws * severity + wa * actionability + wb * business_value
        out.append({
            "topic": f"T{tid:02d}", "working_label": _working_label(t),
            "size": size, "impact": round(impact, 3),
            "severity": round(severity, 3),
            "severity_basis": f"低分占比 {fmt_pct(lc, ln)} vs 高分占比 {fmt_pct(hc, hn)}",
            "actionability": actionability, "business_value": business_value,
            "score": round(score, 3),
            "weights": {"impact": wi, "severity": ws,
                        "actionability": wa, "business_value": wb},
            "needs_human_confirm": True,
        })
    return sorted(out, key=lambda x: -x["score"])


def grounding_check_local(insights: dict, all_reviews: list[dict]) -> dict:
    """引用回查：insights 里出现的每个 review_id 必须能在 reviews.jsonl 定位到逐字原句。"""
    known = {str(r.get("review_id")): str(r.get("review_text", "")) for r in all_reviews}
    checked = dropped = 0
    for sig in insights.get("cross_signals", []):
        for rid in sig.get("review_ids", []):
            checked += 1
            if str(rid) not in known:
                dropped += 1
    return {"all_locatable": dropped == 0, "checked": checked, "dropped": dropped,
            "note": "查不到 review_id 的引用整条丢弃并写审计，不进报告"}


def analyze(workspace: str | Path, project: dict | None = None) -> dict:
    project = project or {}
    w = Path(workspace)
    a = w / "data" / "analysis"
    reviews, topics, all_reviews, metrics = _load(w)
    if not reviews:
        raise RuntimeError("空结果告警：reviews_labeled.jsonl 无数据，量化终止")

    rows, warn_doc = build_matrix(reviews, topics)
    with (a / "matrix.csv").open("w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=MATRIX_FIELDS)
        wr.writeheader()
        wr.writerows(rows)

    windows = project.get("time_windows", [])
    weights = project.get("opportunity_weights", {})
    signals = cross_signals(reviews, topics, metrics, windows)
    opps = opportunities(rows, topics, weights, len(reviews))
    grounding = grounding_check_local({"cross_signals": signals}, all_reviews)

    insights = {
        "created_at": now_iso(),
        "sample_disclaimer": "评论是自选样本，占比只代表评论语料内部结构，不能外推全体玩家比例",
        "wording_rule": "只写「同期出现」，禁止因果措辞",
        "n_reviews": len(reviews), "n_topics": len(topics["topics"]),
        "n_outliers": len(topics.get("outliers", [])),
        "cross_signals": signals,
        "opportunities": opps,
        "opportunity_formula": "影响面 × 严重度 × 可行动性 × 商业价值（权重见每条 weights，业务可一票否决）",
        "severity_proxy": "同主题在 1–2 星占比 vs 4–5 星占比",
        "equal_length_windows": [x.get("id") for x in windows],
        "warnings": warn_doc["warnings"],
        "grounding": grounding,
        "note": "工作标签由高频字词机械生成，不是结论；正式主题命名需 AI 助手给出显式纳入/排除标准并人工确认",
    }
    (a / "insights.json").write_text(json.dumps(insights, ensure_ascii=False, indent=2), encoding="utf-8")
    audit(w, "analyze", actor="pipeline",
          note=f"matrix_cells={len(rows)}; cross_signals={len(signals)}; grounding_dropped={grounding['dropped']}")
    return {"matrix_cells": len(rows), "cross_signals": len(signals),
            "opportunities": len(opps), "warnings": len(warn_doc["warnings"]),
            "grounding": grounding, "insights_path": str(a / "insights.json")}


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="唯一量化中心")
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
    print(json.dumps(analyze(args.workspace, project), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
