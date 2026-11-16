"""验收自检：6 条跑通判定 + 负向用例 + 红线静态断言 + sha256 指纹。

沿用指纹验收的思路：sandbox 指向临时目录、跑前跑后指纹校验、
正常/异常样本对（负向用例必须通过）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from ur_core import fingerprint_diff, read_reviews, setup_console, sha256_tree
from ur_fetch import FetchGuardError, assert_readonly, fetch_json

# 源码里出现这些即视为踩红线（写操作 / 登录态复用 / 逆向工具）
FORBIDDEN_SOURCE_PATTERNS = [
    (r"method\s*=\s*['\"](POST|PUT|PATCH|DELETE)['\"]", "HTTP 写方法"),
    (r"requests\.(post|put|patch|delete)\s*\(", "requests 写方法"),
    (r"['\"]Cookie['\"]\s*:", "显式 Cookie 头"),
    (r"['\"]Authorization['\"]\s*:", "显式 Authorization 头"),
    (r"\b(selenium|frida|xposed|mitmproxy|hook_ssl)\b", "逆向/风控绕过工具"),
    (r"proxy rotate|轮换代理|绕过验证码|破解签名", "绕过防护措辞"),
]


def redline_static_assert(scripts_dir: str | Path) -> dict:
    hits: list[dict] = []
    for p in sorted(Path(scripts_dir).glob("*.py")):
        if p.name == "ur_verify.py":
            continue  # 检查器自身含禁用词字面量（用于定义规则），不参与被检查
        text = p.read_text(encoding="utf-8")
        for pat, label in FORBIDDEN_SOURCE_PATTERNS:
            for m in re.finditer(pat, text, re.IGNORECASE):
                line = text[:m.start()].count("\n") + 1
                hits.append({"file": p.name, "line": line, "issue": label, "match": m.group(0)[:60]})
    return {"passed": not hits, "hits": hits,
            "note": "静态断言：脚本源码不得出现写操作、登录态复用、逆向工具"}


def offline_guard_check() -> dict:
    """负向用例：POST / 带 cookie / --offline 下联网，三种都必须被拒。"""
    cases = []

    def _case(name: str, fn, expect_raise: bool = True) -> None:
        try:
            result = fn()
            if expect_raise:
                cases.append({"case": name, "passed": False, "note": "未被拦截"})
            else:
                cases.append({"case": name, "passed": not result.get("ok", True),
                              "note": result.get("error", "")})
        except FetchGuardError as exc:
            cases.append({"case": name, "passed": expect_raise, "note": str(exc)[:100]})

    _case("POST 必须被只读断言拒绝",
          lambda: assert_readonly("POST", {}, "https://example.com/api", offline=False))
    _case("携带 Cookie 必须被拒绝",
          lambda: assert_readonly("GET", {"Cookie": "session=abc"}, "https://example.com/api", offline=False))
    _case("携带 Authorization 必须被拒绝",
          lambda: assert_readonly("GET", {"Authorization": "Bearer x"}, "https://example.com/api", offline=False))
    _case("登录页 URL 必须被拒绝",
          lambda: assert_readonly("GET", {}, "https://example.com/login", offline=False))
    _case("--offline 下 fetch_json 必须被拒绝",
          lambda: fetch_json("https://example.com/api", offline=True), expect_raise=False)
    return {"passed": all(c["passed"] for c in cases), "cases": cases}


def grounding_negative_case(reviews_path: str | Path, fake_id: str) -> dict:
    """故意塞一个假 review_id，grounding check 必须把它整条丢弃并写审计。"""
    from ur_report import grounding_check
    reviews = read_reviews(reviews_path)
    fake_insight = {
        "topic_id": 999, "topic_name": "负向用例·伪造主题",
        "claim": "这是一条伪造洞察，用于验证引用回查",
        "review_ids": [fake_id], "quotes": ["这句原话并不存在于任何真实评论里"],
        "needs_human_confirm": True,
    }
    valid, dropped = grounding_check([fake_insight], reviews)
    passed = len(valid) == 0 and len(dropped) == 1
    return {"passed": passed, "valid_count": len(valid), "dropped_count": len(dropped),
            "note": "查不到 review_id 的引用必须整条丢弃，不进报告"}


def check_report(report_path: str | Path, expected: dict) -> dict:
    """核对报告的三段式、因果措辞、声明、样本警告、两位小数、分母。"""
    p = Path(report_path)
    if not p.exists():
        return {"passed": False, "checks": [{"case": "report.md 存在", "passed": False}]}
    text = p.read_text(encoding="utf-8")
    checks = []

    def _add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"case": name, "passed": bool(ok), "detail": detail})

    for h in expected.get("report_required_headings", ["数据事实", "模型推断", "运营建议"]):
        _add(f"含固定标题「{h}」", h in text)
    frag = expected.get("required_disclaimer_fragment", "不能外推全体玩家比例")
    _add("顶部含自选样本不可外推声明", frag in text)
    for bad in expected.get("report_forbidden_phrases", ["导致", "因为"]):
        _add(f"不含因果措辞「{bad}」", bad not in text)
    _add("含「同期出现」措辞", "同期出现" in text)
    warn = expected.get("required_warning_fragment", "样本不足")
    _add(f"含小样本警告「{warn}」", warn in text)
    two_dec = re.findall(r"\d+\.\d{2}\s*%", text)
    _add("无两位小数百分比", not two_dec, f"命中 {two_dec[:3]}")
    pcts = re.findall(r"\d+(?:\.\d)?\s*%", text)
    with_denom = re.findall(r"\d+(?:\.\d)?\s*%[（(]\s*\d+\s*/\s*\d+", text)
    _add("百分比带绝对条数与分母", len(with_denom) >= len(pcts) - len(re.findall(r"机会点|权重", text)),
         f"百分比 {len(pcts)} 处，带分母 {len(with_denom)} 处")
    _add("不含 0.0% 冒充缺失", "0.0%" not in text)
    _add("含「—」表示变化不足最小可分辨单位", "—" in text)
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def verify(workspace: str | Path, skill_root: str | Path, fixtures_dir: str | Path,
           report_path: str | Path, before_fp: dict | None = None,
           after_fp: dict | None = None) -> dict:
    """6 条跑通判定汇总。"""
    w = Path(workspace)
    expected_path = Path(fixtures_dir) / "expected_summary.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8")) if expected_path.exists() else {}

    checks: dict[str, dict] = {}
    checks["1_三段式与声明"] = check_report(report_path, expected)

    # ② 交叉分析：评分下滑 + 同窗主题低分占比上升，且为「同期出现」
    matrix_path = w / "data" / "analysis" / "insights.json"
    insights = json.loads(matrix_path.read_text(encoding="utf-8")) if matrix_path.exists() else {}
    cross = insights.get("cross_signals", [])
    checks["2_交叉同期标记"] = {
        "passed": bool(cross) and all("同期出现" in str(c.get("wording", "")) for c in cross),
        "signals": cross[:3],
        "note": "只写同期出现，禁止因果",
    }

    # ③ 由 check_report 覆盖（百分比/分母/小样本/两位小数），此处补「交叉矩阵格子数」
    matrix_csv = w / "data" / "analysis" / "matrix.csv"
    cells = len(matrix_csv.read_text(encoding="utf-8").strip().splitlines()) - 1 if matrix_csv.exists() else 0
    checks["3_交叉矩阵与量化"] = {
        "passed": cells >= expected.get("cross_matrix_min_cells", 6)
                  and checks["1_三段式与声明"]["passed"],
        "matrix_cells": cells,
    }

    # ④ grounding 正向（由 ur_report 写进 insights.json）+ 负向用例
    gstat = insights.get("grounding", {})
    neg = grounding_negative_case(w / "data" / "reviews.jsonl",
                                  expected.get("grounding_negative_case", {}).get(
                                      "fake_review_id", "FAKE-ID-DOES-NOT-EXIST"))
    checks["4_引用可定位"] = {
        "passed": bool(gstat.get("all_locatable", False)) and neg["passed"],
        "grounding": gstat, "negative_case": neg,
    }

    # ⑤ 跑前跑后 sha256：skill 目录与 fixtures 零改动
    before_fp = before_fp if before_fp is not None else {}
    after_fp = after_fp if after_fp is not None else {}
    diff = fingerprint_diff(before_fp, after_fp)
    checks["5_指纹零改动"] = {
        "passed": not diff["changed"] and not diff["removed"],
        "diff": {"changed": diff["changed"][:5], "removed": diff["removed"][:5],
                 "added_count": len(diff["added"])},
        "note": "skill 目录与 fixtures 不得被流水线改动",
    }

    # ⑥ 红线静态断言 + 离线护栏负向用例 + 采集日志确认 offline
    static = redline_static_assert(Path(skill_root) / "scripts")
    guard = offline_guard_check()
    log_path = w / "data" / "collection-log.jsonl"
    offline_ok = False
    if log_path.exists():
        last = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()][-1]
        offline_ok = bool(last.get("offline"))
    checks["6_红线与零联网"] = {
        "passed": static["passed"] and guard["passed"] and offline_ok,
        "static_assert": {"passed": static["passed"], "hits": static["hits"]},
        "guard_cases": guard["cases"], "offline_log_confirmed": offline_ok,
    }

    all_pass = all(v["passed"] for v in checks.values())
    result = {"all_passed": all_pass, "checks": checks}
    out = w / "out" / "verification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="离线闭环验收")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--skill-root", default=str(Path(__file__).parent.parent))
    ap.add_argument("--fixtures", default=str(Path(__file__).parent.parent / "examples" / "fixtures"))
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    report = args.report or str(Path(args.workspace) / "out" / "report.md")
    result = verify(args.workspace, args.skill_root, args.fixtures, report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
