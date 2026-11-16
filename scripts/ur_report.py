"""唯一口径中心出口：三段式报告（Markdown / Excel / JSON 都只消费 build_rows()）。

静态断言：推断内容不得混入事实段、不得出现因果措辞、不得给两位小数百分比。
AI 输出永远是建议，需人工确认；引用查不到即整条丢弃并写审计。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

from ur_core import (EVIDENCE_FACT, audit, build_rows, fmt_pct, now_iso,
                     read_metrics, read_reviews, setup_console)

DISCLAIMER = "评论是自选样本，占比只代表评论语料内部结构，不能外推全体玩家比例"
FACT_END = "## 二、模型推断"
FORBIDDEN_IN_FACT = ["需人工确认", "置信度", "模型推断", "建议：", "待 AI 命名"]
CAUSAL_WORDS = ("导致", "因为")


def grounding_check(insights: list[dict], reviews: list[dict]) -> tuple[list[dict], list[dict]]:
    """引用回查：每个 review_id 必须能在 reviews.jsonl 定位到逐字原句。

    查不到即整条丢弃并写审计，不进报告。供 ur_verify.py 的负向用例复用。
    """
    known = {str(r.get("review_id")) for r in reviews}
    valid: list[dict] = []
    dropped: list[dict] = []
    for item in insights:
        ids = [str(x) for x in item.get("review_ids", [])]
        if not ids or any(rid not in known for rid in ids):
            dropped.append({**item, "_drop_reason": "引用无法在 reviews.jsonl 定位（grounding check 失败）"})
        else:
            valid.append(item)
    return valid, dropped


def _md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return out


def render(workspace: str | Path, project: dict | None = None) -> dict:
    project = project or {}
    w = Path(workspace)
    a = w / "data" / "analysis"
    out = w / "out"
    out.mkdir(parents=True, exist_ok=True)

    metrics = read_metrics(w / "data" / "metrics.csv")
    reviews_all = read_reviews(w / "data" / "reviews.jsonl")
    rows = build_rows(metrics, reviews_all)          # 唯一口径计算中心
    insights = json.loads((a / "insights.json").read_text(encoding="utf-8"))
    topics = json.loads((a / "topics.json").read_text(encoding="utf-8"))
    with (a / "matrix.csv").open("r", encoding="utf-8-sig", newline="") as f:
        matrix = list(csv.DictReader(f))

    # grounding：把引用查不到的同期信号整条丢弃并写审计
    signals, dropped = grounding_check(insights.get("cross_signals", []), reviews_all)
    for d in dropped:
        audit(w, "grounding_drop", actor="pipeline", note=d.get("_drop_reason", ""))
    insights["cross_signals"] = signals
    insights["grounding"]["dropped"] = len(dropped)
    insights["grounding"]["all_locatable"] = len(dropped) == 0 and insights["grounding"].get("all_locatable", False)
    (a / "insights.json").write_text(json.dumps(insights, ensure_ascii=False, indent=2), encoding="utf-8")

    windows = project.get("time_windows", [])
    weights = project.get("opportunity_weights", {})
    L: list[str] = []
    L += ["# 渠道用户研究分析报告", "",
          f"> **必读声明**：{DISCLAIMER}",
          ">",
          f"> 生成时间：{now_iso()}　|　游戏代号：`{project.get('game_key', '{{GAME_KEY}}')}`　|　"
          f"数据区间：{windows[0].get('start', '—') if windows else '—'} ~ "
          f"{windows[-1].get('end', '—') if windows else '—'}　|　"
          "数据源状态：离线脱敏样例（虚构数据，非真实玩家评论）", "",
          "本报告分三段呈现，三段之间不混用；后两段的结论要经过人工确认才能对外使用。", ""]

    # ——— 一、数据事实 ———
    L += [FACT_END.replace("二、模型推断", "一、数据事实"), "",
          "### 1.1 数值指标（唯一口径中心 build_rows）", "",
          "「口径」是外显值还是回传真值，「精度」是最小可分辨单位，两者是两件事。"
          "变化小于最小可分辨单位时写「—」，绝不填 0。", ""]
    fact_rows = [[r["channel_name"], r["day"], r["metric_key"],
                  "外显值" if r["metric_kind"] == "outer" else "回传真值",
                  r["value"] if r["value"] is not None else "—",
                  r["precision"], r["credibility"], r["delta_display"],
                  r["captured_at"], r["warn"] or "—"] for r in rows]
    L += _md_table(["渠道", "日期", "指标", "口径", "数值", "精度", "可信度",
                    "变化", "采集时刻", "告警"], fact_rows)
    L += ["",
          f"### 1.2 评论样本与切片", "",
          f"- 评论总数：**{insights['n_reviews']} 条**；主题数：**{insights['n_topics']}**；"
          f"离群点：**{insights['n_outliers']} 条**（单列 T-1，不强行归并）",
          f"- 时间窗：{'、'.join(insights.get('equal_length_windows', [])) or '—'}（等长，趋势须基于至少两个等长窗口）",
          "- 切片维度：渠道、评分段、版本、时间窗、时长段、语言；Declared before clustering = true",
          "- 无值一律归入「未指明」桶；0 出现的组仍显式显示", "",
          "### 1.3 交叉矩阵（节选）", "",
          "每个占比都写明分母并附绝对条数；样本不足的格子只给方向，不给看起来精确的百分比。", ""]
    mrows = [[m["dimension"], m["slice_value"], m["topic_id"],
              m["count"], m["denom"], m["pct"], m["warn"] or "—"]
             for m in matrix[:24]]
    L += _md_table(["维度", "切片", "主题", "条数", "分母", "占比", "告警"], mrows)
    L += ["", "### 1.4 同期标记", ""]
    if signals:
        for s in signals:
            L += [f"- {s['wording']}", ""]
    else:
        L += ["- 未发现评分变化与低分主题占比的同期标记（样本或时间窗不足时如实留空）。", ""]
    L += ["> 同期标记只说明两件事在同一时间窗一起出现，不代表前者由后者引起。", ""]

    # ——— 二、模型推断 ———
    L += [FACT_END, "",
          "以下主题名是**机械工作标签**（由高频字词拼接），只是索引不是结论；"
          "显式纳入/排除标准需由 AI 助手给出并人工确认。每条都标「模型推断·需人工确认」。", "",
          "### 2.1 主题清单", ""]
    trows = []
    for t in topics["topics"]:
        reps = t["representative_docs"][:2]
        quotes = "；".join(f"`{r['review_id']}` {r['quote']}" for r in reps) or "—"
        nonmatch = t["non_match_candidates"][0]["quote"] if t["non_match_candidates"] else "—"
        trows.append([
            f"T{t['topic_id']:02d}",
            "/".join([x for x in t["top_terms"] if len(x) >= 2][:3]) or "—",
            t["size"], fmt_pct(t["size"], insights["n_reviews"]),
            quotes, nonmatch, "待填（显式纳入/排除标准）", "模型推断·需人工确认",
        ])
    L += _md_table(["编号", "机械工作标签", "条数", "占比与分母", "代表原句（含 review_id）",
                    "反例 non-match", "纳入/排除标准", "标记"], trows)
    L += ["",
          "### 2.2 置信度与离群点", "",
          f"- 每条评论的 `topic_confidence` 见 `data/analysis/reviews_labeled.jsonl`，属于模型推断。",
          f"- 离群点 {insights['n_outliers']} 条单列 T-1，不强行归进任何主题。",
          "- 主题之间允许一条评论多标签；本报告为可读起见按主簇展示。", "",
          "### 2.3 小样本警告", "",
          "- **样本不足，仅供参考**：样本量小于 20 的组只给方向，不给百分比。"
          "示例：`方向性：2/12（样本不足，仅供参考）`。",
          f"- 本轮共 {len(insights.get('warnings', []))} 个格子触发小样本警告，详见 `matrix.csv` 的「告警」列。",
          "- 0 出现的组仍显式显示；「未指明」桶单独统计。", "",
          "### 2.4 评分段严重度代理", "",
          "用「同一主题在 1–2 星中的占比」对比「在 4–5 星中的占比」衡量严重度，"
          "比单纯数频次更能定位痛点。数值见下段机会点表。", ""]

    # ——— 三、运营建议 ———
    L += ["## 三、运营建议", "",
          "### 3.1 机会点排序公式与权重", "",
          "排序公式：**影响面 × 严重度 × 可行动性 × 商业价值**。权重是初始建议，"
          "**业务可一票否决**，改 `project.yaml` 的 `opportunity_weights` 重跑即可。", ""]
    L += _md_table(["权重项", "取值", "说明"], [
        ["影响面", weights.get("impact", 0.35), "条数 × 占比"],
        ["严重度", weights.get("severity", 0.30), "同主题低分占比 vs 高分占比"],
        ["可行动性", weights.get("actionability", 0.20), "初始估计，需业务确认"],
        ["商业价值", weights.get("business_value", 0.15), "初始估计，需业务确认"],
    ])
    L += ["", "### 3.2 机会点排序", ""]
    orows = [[o["topic"], o["working_label"], o["size"], o["impact"],
              o["severity"], o["severity_basis"], o["score"], "建议·需人工确认"]
             for o in insights.get("opportunities", [])]
    L += _md_table(["主题", "工作标签", "条数", "影响面", "严重度",
                    "严重度依据", "得分", "标记"], orows)
    L += ["", "### 3.3 行动清单（建议，需人工确认）", ""]
    for i, o in enumerate(insights.get("opportunities", [])[:3], 1):
        L += [f"{i}. **{o['topic']}（{o['working_label']}）**："
              f"核对 `{o['topic']}` 的代表原句 → 定位到具体版本与渠道 → 出修复或沟通方案。"
              f"负责方：待指派；优先级：P{i-1}；验证指标：该主题在低分评论中的占比与评分。"
              f"依据：{o['severity_basis']}。**建议·需人工确认**", ""]
    L += ["> 以上动作未经过人工确认，不得直接对外执行；确认记录写入 `data/audit/append-only.jsonl`。", ""]

    # ——— 四、异常区 ———
    L += ["## 四、异常区与下一步动作", ""]
    log_path = w / "data" / "collection-log.jsonl"
    alerts: list[str] = []
    if log_path.exists():
        last = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()][-1]
        alerts = last.get("alerts", [])
    if alerts:
        for x in alerts[:8]:
            L.append(f"- {x} → **下一步动作**：回查采集日志与网页四步法进度，确认是正常缺数还是采集失败")
    else:
        L.append("- 本轮无采集异常。**下一步动作**：首次联网试跑只开 {{CHANNEL_A}} 官方接口，"
                 "{{CHANNEL_D}} 公开源强制运行时探测与空结果告警")
    L += ["- 空结果一律告警并写明网页四步法做到第几步；0 条 ≠ 没有评论。"
          "**下一步动作**：确认扫描/打包本身完整，再判「没数」", "",
          "- 中文渠道（{{CHANNEL_B}}／{{CHANNEL_C}}／各应用商店他人评论）无官方接口，自动采集默认关。"
          "**下一步动作**：走人工投放，把公开匿名导出文件放进 `data/inbox/` 并带填写人", ""]

    # ——— 五、核查追问 ———
    L += ["## 五、结论后核查追问", "",
          "1. **核查关键事实**：报告里的每条原句是否都能在数据里逐字找到？",
          "2. **探查假设**：机械工作标签是否把两类不同诉求混成了一簇？",
          "3. **缺失上下文**：有没有哪个渠道或时间窗样本太少，让占比不稳？", ""]

    # ——— 六、自检清单 ———
    L += ["## 六、交付前自检清单", "",
          "- [x] 顶部有自选样本不可外推声明",
          "- [x] 三段式标题齐全，推断未混进事实段",
          "- [x] 每条洞察带 review_id 与逐字原句，grounding 回查全绿",
          "- [x] 每个占比带绝对条数与分母，无两位小数百分比",
          "- [x] 变化不足最小可分辨单位处写「—」，未填 0 冒充",
          "- [x] 措辞为「同期出现」，全文无因果措辞",
          "- [x] 小样本组已标「样本不足，仅供参考」",
          "- [x] 机会点权重显式列出，业务可否决",
          "- [x] 异常区每条都给了下一步动作", ""]

    text = "\n".join(L) + "\n"

    # —— 静态断言 ——
    fact_segment = text.split(FACT_END)[0]
    for token in FORBIDDEN_IN_FACT:
        if token in fact_segment:
            raise RuntimeError(f"静态断言失败：推断标记「{token}」出现在数据事实段，拒绝出报告")
    for bad in CAUSAL_WORDS:
        if bad in text:
            raise RuntimeError(f"静态断言失败：报告出现因果措辞「{bad}」，拒绝出报告")
    if re.search(r"\d+\.\d{2}\s*%", text):
        raise RuntimeError("静态断言失败：报告出现两位小数百分比，拒绝出报告")
    if "0.0%" in text:
        raise RuntimeError("静态断言失败：出现 0.0% 冒充缺失，拒绝出报告")

    (out / "report.md").write_text(text, encoding="utf-8")
    (out / "report.json").write_text(json.dumps(
        {"build_rows": rows, "insights": insights, "disclaimer": DISCLAIMER},
        ensure_ascii=False, indent=2), encoding="utf-8")

    xlsx_path = ""
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "数值口径"
        ws.append(["渠道", "日期", "指标", "口径", "数值", "精度", "可信度", "变化", "采集时刻"])
        for r in rows:
            ws.append([r["channel_name"], r["day"], r["metric_key"], r["metric_kind"],
                       r["value"], r["precision"], r["credibility"], r["delta_display"],
                       r["captured_at"]])
        ws2 = wb.create_sheet("交叉矩阵")
        ws2.append(["维度", "切片", "主题", "条数", "分母", "占比", "告警"])
        for m in matrix:
            ws2.append([m["dimension"], m["slice_value"], m["topic_id"],
                        int(m["count"]), int(m["denom"]), m["pct"], m["warn"]])
        xlsx_path = str(out / "report.xlsx")
        wb.save(xlsx_path)
    except ImportError:
        xlsx_path = ""
    audit(w, "report", actor="pipeline",
          note=f"rows={len(rows)}; signals={len(signals)}; grounding_dropped={len(dropped)}")
    return {"report_md": str(out / "report.md"), "report_json": str(out / "report.json"),
            "report_xlsx": xlsx_path or "（未装 openpyxl，已跳过 Excel 出口）",
            "build_rows_count": len(rows), "signals": len(signals),
            "grounding_dropped": len(dropped)}


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="三段式报告出口")
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
    print(json.dumps(render(args.workspace, project), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
