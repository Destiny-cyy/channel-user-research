"""一键串起 MVP 离线闭环：采集 → 清洗 → 聚类 → 量化 → 报告 → 验收。

默认 --offline，零网络、零第三方依赖、零外部 API key，任何 Python 3.11+ 可直接试跑。
任一阶段失败即停并打印原因，不自动补救。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date
from pathlib import Path

from ur_core import audit, setup_console, sha256_tree

SKILL_ROOT = Path(__file__).parent.parent
DEFAULT_FIXTURES = SKILL_ROOT / "examples" / "fixtures"

DEFAULT_PROJECT = {
    "game_key": "{{GAME_KEY}}",
    "display_name": "示例游戏（脱敏虚构样例）",
    "time_windows": [
        {"id": "W1", "start": "2026-10-01", "end": "2026-10-14"},
        {"id": "W2", "start": "2026-10-15", "end": "2026-10-28"},
    ],
    "privacy": {"redact_pii": True},
    "report": {"audience": "运营 / 发行", "include_pii": False},
    "analysis": {
        "n_clusters": 5, "min_topic_docs": 3,
        "small_sample_threshold": 20, "indicative_threshold": 50,
        "equal_length_windows_required": True, "outlier_topic_id": -1,
    },
    "opportunity_weights": {
        "impact": 0.35, "severity": 0.30,
        "actionability": 0.20, "business_value": 0.15,
        "note": "权重之和 1.0；主观设定，业务可一票否决",
    },
}

DEFAULT_OFFLINE_CONFIG = {
    "version": 1,
    "defaults": {
        "rate_limit_qps": 0.3, "random_jitter": True, "max_pages": 5,
        "page_size": 100, "retry_on": [429, 500, 502, 503, 504],
        "snapshot": True, "redact_pii": True, "enabled_default": False,
        "empty_result_is_alert": True,
    },
    # 离线模式不启用任何真实渠道：数据全部来自脱敏 fixtures，天然零联网
    "channels": [],
    "slices": ["channel", "rating_band", "version", "time_window", "playtime_band", "lang"],
}


def _load_project(path: str) -> dict:
    if not path or not Path(path).exists():
        return DEFAULT_PROJECT
    try:
        import yaml
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        merged = dict(DEFAULT_PROJECT)
        merged.update(loaded)
        return merged
    except ImportError:
        print("[提示] 未安装 PyYAML，--project 已忽略，使用内置默认配置（离线闭环零依赖）")
        return DEFAULT_PROJECT


def run(workspace: Path, fixtures: Path, project: dict, *, day: str,
        offline: bool = True, n_clusters: int = 5, emit_brief: bool = True,
        config_path: str = "") -> dict:
    from ur_analyze import analyze
    from ur_clean import clean
    from ur_cluster import cluster
    from ur_collect import collect, load_config
    from ur_report import render
    from ur_verify import verify

    cfg = load_config(config_path) if (config_path and Path(config_path).exists()) else DEFAULT_OFFLINE_CONFIG
    workspace.mkdir(parents=True, exist_ok=True)
    before = sha256_tree(SKILL_ROOT)
    stages: list[dict] = []

    def _stage(name: str, fn) -> None:
        try:
            result = fn()
            stages.append({"stage": name, "ok": True, "summary": result})
            print(f"[OK] {name}")
        except Exception as exc:
            stages.append({"stage": name, "ok": False, "error": str(exc),
                           "trace": traceback.format_exc(limit=3)})
            print(f"[STOP] {name} 失败：{exc}", file=sys.stderr)
            raise

    _stage("A 采集", lambda: collect(cfg, workspace, day, offline=offline,
                                     fixtures_dir=fixtures, project=project))
    _stage("B 清洗归一", lambda: clean(workspace, project))
    _stage("C 轻量聚类", lambda: cluster(workspace, n_clusters, emit_brief))
    _stage("D 量化与交叉", lambda: analyze(workspace, project))
    _stage("E 三段式报告", lambda: render(workspace, project))

    after = sha256_tree(SKILL_ROOT)
    report_path = workspace / "out" / "report.md"
    verification = verify(workspace, SKILL_ROOT, fixtures, report_path, before, after)
    stages.append({"stage": "F 验收", "ok": verification["all_passed"],
                   "summary": {"all_passed": verification["all_passed"]}})

    summary = {
        "workspace": str(workspace), "skill_root": str(SKILL_ROOT),
        "offline": offline, "day": day, "stages": stages,
        "verification": verification,
        "deliverables": {
            "report_md": str(report_path),
            "report_json": str(workspace / "out" / "report.json"),
            "report_xlsx": str(workspace / "out" / "report.xlsx"),
            "verification_json": str(workspace / "out" / "verification.json"),
            "llm_brief": str(workspace / "out" / "llm_brief.md"),
        },
    }
    (workspace / "out" / "pipeline-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    audit(workspace, "pipeline", actor="pipeline",
          note=f"offline={offline}; all_passed={verification['all_passed']}")
    return summary


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="渠道用户研究一键流水线")
    ap.add_argument("--workspace", default="", help="工作区根目录（产物写入 <workspace>/data 与 <workspace>/out）")
    ap.add_argument("--out", default="", help="输出目录；给了则以其父目录为工作区（兼容 <工作区>/out 写法）")
    ap.add_argument("--config", default="", help="channels.yaml（离线可省略）")
    ap.add_argument("--project", default="", help="project.yaml（离线可省略，用内置默认）")
    ap.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    ap.add_argument("--day", default=date.today().isoformat())
    ap.add_argument("--live", action="store_true", help="联网真实采集（默认离线）")
    ap.add_argument("--n-clusters", type=int, default=5)
    ap.add_argument("--no-brief", action="store_true")
    args = ap.parse_args()

    if args.out:
        workspace = Path(args.out).parent
    elif args.workspace:
        workspace = Path(args.workspace)
    else:
        workspace = Path.cwd() / "channel-user-research-demo"

    project = _load_project(args.project)
    offline = not args.live
    print(f"工作区：{workspace}")
    print(f"模式：{'离线脱敏样例（零联网）' if offline else '联网真实采集'}")
    try:
        summary = run(workspace, Path(args.fixtures), project, day=args.day,
                      offline=offline, n_clusters=args.n_clusters,
                      emit_brief=not args.no_brief, config_path=args.config)
    except Exception:
        print("[STOP] 流水线已停止，未自动补救。请按上面的失败原因排查后重跑。", file=sys.stderr)
        return 1
    passed = summary["verification"]["all_passed"]
    print("\n=== 验收结果 ===")
    for name, chk in summary["verification"]["checks"].items():
        print(f"  {'PASS' if chk['passed'] else 'FAIL'}  {name}")
    print(f"\n全部通过：{passed}")
    print(f"报告：{summary['deliverables']['report_md']}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
