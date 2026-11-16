"""生成 4 份脱敏虚构样例，保证离线闭环可复现、可审计。

全部内容为**虚构**，不含任何真实平台数据、真实评论或真实用户标识。
重跑：python examples/fixtures/generate_fixtures.py
固定随机种子 20261112，重跑结果一致（便于 sha256 指纹验收）。
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(20261112)
HERE = Path(__file__).parent
GAME = "{{GAME_KEY}}"

CHANNELS = [
    ("channel_a_reviews", "{{CHANNEL_A}} 公开评论"),
    ("channel_b_manual", "{{CHANNEL_B}}"),
    ("channel_c_manual", "{{CHANNEL_C}}"),
]
WINDOWS = [("W1", "2026-10-01", "2026-10-14"), ("W2", "2026-10-15", "2026-10-28")]

# 主题词汇刻意重复，保证字符 n-gram + KMeans 在纯 CPU 下能分开
TOPIC_TEXT = {
    "crash": [
        "一进游戏就闪退，根本进不去，优化太差了",
        "打到一半直接崩溃掉线，进度全没了，闪退太频繁",
        "更新之后频繁闪退崩溃，手机发烫还掉线",
        "每次加载都崩溃闪退，重连也连不上，玩不了",
        "闪退掉线崩溃三连，这个版本稳定性太差",
    ],
    "visual": [
        "画面很精致，美术风格特别喜欢，光影舒服",
        "建模好看，场景细节到位，画风很治愈",
        "美术和配色都很棒，截图当壁纸都行",
        "画面表现超出预期，角色建模很用心",
        "视觉效果不错，光影和材质看着很舒服",
    ],
    "gameplay": [
        "玩法挺有意思，操作手感还能再调调",
        "核心循环不错，就是操作有点别扭，手感偏重",
        "关卡设计有想法，玩法深度够，希望能加难度选项",
        "战斗手感流畅，玩法节奏偏慢，建议优化",
        "玩法新鲜但引导太少，新手容易懵",
    ],
    "payment": [
        "定价有点贵，内购氪金压力大，性价比一般",
        "礼包太多，氪金点密集，希望出个买断",
        "付费设计劝退，月卡和战令叠加太贵了",
        "价格偏高，氪金才能玩得舒服，不太满意",
        "充值档位不合理，定价再亲民一点就好了",
    ],
    "service": [
        "充值不到账，客服一直不回，账号问题没人管",
        "客服响应太慢，工单三天没处理，体验很差",
        "账号被误封，申诉没回应，客服找不到人",
        "客服态度敷衍，充值问题拖了一周",
        "工单石沉大海，客服渠道形同虚设",
    ],
}
RATING_BY_TOPIC = {
    "crash": lambda: random.choice([1, 1, 2]),
    "visual": lambda: random.choice([4, 5, 5]),
    "gameplay": lambda: random.choice([3, 3, 4]),
    "payment": lambda: random.choice([2, 2, 3]),
    "service": lambda: random.choice([1, 2, 2]),
}
# W2 里「闪退/崩溃」明显变多 → 低分评论中该主题占比上升
PLAN = {
    "W1": {"crash": 2, "visual": 4, "gameplay": 3, "payment": 2, "service": 1},
    "W2": {"crash": 5, "visual": 3, "gameplay": 2, "payment": 1, "service": 1},
}

METRIC_FIELDS = [
    "record_id", "record_type", "game_key", "channel_key", "channel_name",
    "day", "captured_at", "source", "source_url", "status", "precision",
    "raw", "warn", "error", "note", "snapshot_path", "ingest_batch",
    "compliance_tag", "metric_key", "metric_value", "metric_unit", "metric_kind",
]


def _date_in(start: str, end: str) -> str:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return (s + timedelta(days=random.randint(0, (e - s).days))).isoformat()


def gen_metrics() -> list[dict]:
    rows = []
    base = {"channel_a_reviews": 3600000, "channel_b_manual": 2380000, "channel_c_manual": 600000}
    for ch_key, ch_name in CHANNELS:
        for wid, start, end in WINDOWS:
            for i, d in enumerate((start, end)):
                # 预约数：万级一位，precision=1000（只能看趋势，不算精确增量）
                val = base[ch_key] + (0 if wid == "W1" else 12000) + i * 3000
                rows.append({
                    "record_id": f"metric:{ch_key}:{d}:reservations:outer",
                    "record_type": "metric", "game_key": GAME,
                    "channel_key": ch_key, "channel_name": ch_name,
                    "day": d, "captured_at": f"{d}T09:00:00",
                    "source": "虚构样例·渠道公开页", "status": "ok",
                    "precision": 1000, "raw": f"{val / 10000:.1f}万",
                    "note": "万级一位，仅看趋势", "ingest_batch": "fixture",
                    "compliance_tag": "public_anonymous",
                    "metric_key": "reservations", "metric_value": val,
                    "metric_unit": "人", "metric_kind": "outer",
                })
                # 评分：埋渠道 A（channel_a_reviews）从 4.5 下滑到 4.1
                rating = ({"W1": [4.5, 4.5], "W2": [4.3, 4.1]}[wid][i]
                          if ch_key == "channel_a_reviews" else 4.2)
                rows.append({
                    "record_id": f"metric:{ch_key}:{d}:rating:outer",
                    "record_type": "metric", "game_key": GAME,
                    "channel_key": ch_key, "channel_name": ch_name,
                    "day": d, "captured_at": f"{d}T09:00:00",
                    "source": "虚构样例·渠道公开页", "status": "ok",
                    "precision": 0.1, "raw": f"{rating:.1f}",
                    "note": "", "ingest_batch": "fixture",
                    "compliance_tag": "public_anonymous",
                    "metric_key": "rating", "metric_value": rating,
                    "metric_unit": "分", "metric_kind": "outer",
                })
    return rows


def gen_reviews() -> list[dict]:
    out, seq = [], 0
    for ch_key, ch_name in CHANNELS:
        for wid, start, end in WINDOWS:
            for topic, n in PLAN[wid].items():
                for _ in range(n):
                    seq += 1
                    rid = f"R{seq:04d}"
                    rating = RATING_BY_TOPIC[topic]()
                    d = _date_in(start, end)
                    out.append({
                        "record_id": f"{ch_key}:{rid}", "record_type": "review",
                        "game_key": GAME, "channel_key": ch_key,
                        "channel_name": ch_name, "day": d,
                        "captured_at": f"{d}T10:00:00",
                        "source": "虚构样例·玩家评论", "source_url": "",
                        "status": "ok", "precision": 0,
                        "raw": "", "warn": "", "error": "", "note": "",
                        "snapshot_path": "", "ingest_batch": "fixture",
                        "compliance_tag": "public_anonymous",
                        "review_id": rid,
                        "review_text": random.choice(TOPIC_TEXT[topic]),
                        "rating": rating, "rating_max": 5,
                        "review_time": f"{d}T20:00:00",
                        "app_version": "1.0.0" if wid == "W1" else "1.1.0",
                        "playtime_hours": round(random.uniform(0.5, 60), 1),
                        "lang": "zh", "country": "CN",
                        "votes_up": random.randint(0, 40),
                        "developer_replied": False, "is_early_access": False,
                        "labels": [], "topic_id": None,
                        "topic_confidence": None, "evidence_kind": "数据事实",
                        "_fixture_topic": topic, "_fixture_window": wid,
                    })
    return out


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    metrics = gen_metrics()
    with (HERE / "metrics_sample.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=METRIC_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in METRIC_FIELDS} for r in metrics)

    reviews = gen_reviews()
    with (HERE / "reviews_sample.jsonl").open("w", encoding="utf-8") as f:
        for r in reviews:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    snapshot = {
        "_comment": "脱敏后的公开接口响应副本样例；虚构内容，非真实采集",
        "request_url": "https://example.invalid/api/reviews?game_id=<redacted>&limit=100",
        "method": "GET", "http_status": 200, "captured_at": "2026-10-20T09:00:00",
        "payload": {"results": [{"recommendationid": "R0001", "review": "虚构评论文本",
                                 "voted_up": True, "votes_up": 3}]},
    }
    (HERE / "raw_snapshot_sample.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    expected = {
        "_comment": "一键试跑的期望断言，供 ur_verify.py 核对",
        "min_reviews": 60,
        "min_metrics": 20,
        "min_topic_count": 3,
        "report_required_headings": ["数据事实", "模型推断", "运营建议"],
        "report_forbidden_phrases": ["导致", "因为"],
        "required_disclaimer_fragment": "不能外推全体玩家比例",
        "required_warning_fragment": "样本不足",
        "cross_matrix_min_cells": 6,
        "grounding_negative_case": {
            "fake_review_id": "FAKE-ID-DOES-NOT-EXIST",
            "must_be_dropped": True,
        },
        "forbid_two_decimal_pct": True,
        "forbid_zero_as_missing": True,
    }
    (HERE / "expected_summary.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"生成完成：metrics={len(metrics)} 行, reviews={len(reviews)} 条 + 快照 + 期望断言")


if __name__ == "__main__":
    main()
