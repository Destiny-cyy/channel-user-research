"""轻量本地聚类：字符 n-gram TF-IDF → KMeans。

sklearn 可用时自动增强；不可用时走**纯标准库兜底**，保证离线闭环零第三方依赖也能跑通。
纯 CPU、无 GPU、不申请任何外部 API key。主题命名交会话自带 LLM（--emit-brief 生成任务包）。
离群点单列 -1，不强行归并；保留 representative_docs 供证据链回溯。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from ur_core import audit, now_iso, setup_console

SEED = 20261112
OUTLIER_TOPIC_ID = -1


def _char_ngrams(text: str, sizes: tuple[int, ...] = (2, 3)) -> list[str]:
    """中文按字符 n-gram，零依赖；不依赖分词器。"""
    clean = re.sub(r"[\s\W_]+", "", str(text), flags=re.UNICODE)
    grams: list[str] = []
    for size in sizes:
        for i in range(len(clean) - size + 1):
            grams.append(clean[i:i + size])
    return grams or [clean[:2]]


def _build_vectors(texts: list[str]) -> tuple[list[dict[str, float]], list[str]]:
    counters = [Counter(_char_ngrams(t)) for t in texts]
    df: Counter = Counter()
    for c in counters:
        df.update(c.keys())
    keep = {g for g, n in df.items() if n >= 2}   # 去掉只出现一次的噪声
    n_docs = len(texts)
    idf = {g: math.log((n_docs + 1) / (df[g] + 1)) + 1 for g in keep}
    vecs: list[dict[str, float]] = []
    for c in counters:
        v = {g: (1 + math.log(tf)) * idf[g] for g, tf in c.items() if g in keep}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({g: x / norm for g, x in v.items()})
    return vecs, sorted(keep)


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(g, 0.0) for g, x in a.items())


def _kmeans_stdlib(vecs: list[dict[str, float]], k: int,
                   iters: int = 15) -> tuple[list[int], list[dict[str, float]]]:
    rng = random.Random(SEED)
    k = max(1, min(k, len(vecs)))
    centers: list[dict[str, float]] = [dict(vecs[rng.randrange(len(vecs))])]
    while len(centers) < k:
        best, best_sim = None, 2.0
        for v in vecs:
            s = max(_cos(v, c) for c in centers)
            if s < best_sim:
                best_sim, best = s, v
        centers.append(dict(best or vecs[0]))
    labels = [-1] * len(vecs)
    for _ in range(iters):
        changed = False
        for i, v in enumerate(vecs):
            sims = [_cos(v, c) for c in centers]
            j = max(range(k), key=lambda x: sims[x])
            if labels[i] != j:
                labels[i] = j
                changed = True
        for j in range(k):
            members = [vecs[i] for i in range(len(vecs)) if labels[i] == j]
            if not members:
                continue
            agg: Counter = Counter()
            for m in members:
                for g, x in m.items():
                    agg[g] += x
            norm = math.sqrt(sum(x * x for x in agg.values())) or 1.0
            centers[j] = {g: x / norm for g, x in agg.items()}
        if not changed:
            break
    return labels, centers


def _cluster_vectors(texts: list[str], k: int) -> dict:
    """sklearn 优先，标准库兜底。两条路径都返回统一结构。"""
    try:
        from sklearn.cluster import KMeans
        from sklearn.feature_extraction.text import TfidfVectorizer
        matrix = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=2)
        X = matrix.fit_transform(texts)
        model = KMeans(n_clusters=max(1, min(k, len(texts))), random_state=SEED, n_init=10)
        labels = model.fit_predict(X).tolist()
        terms = matrix.get_feature_names_out()
        center_terms = []
        for j in range(model.n_clusters):
            order = model.cluster_centers_[j].argsort()[::-1][:10]
            center_terms.append([str(terms[i]) for i in order])
        sims = [1.0] * len(texts)
        method = "sklearn(char_wb 2-4 + KMeans)"
    except ImportError:
        vecs, _ = _build_vectors(texts)
        labels, centers = _kmeans_stdlib(vecs, k)
        center_terms = []
        for c in centers:
            center_terms.append([g for g, _ in sorted(c.items(), key=lambda x: -x[1])[:10]])
        sims = [max((_cos(v, c) for c in centers), default=0.0) for v in vecs]
        method = "stdlib(字符 n-gram TF-IDF + KMeans 兜底，零第三方依赖)"
    # 离群点：与所有簇质心相似度都过低，单列 -1，不强行归并
    for i, s in enumerate(sims):
        if s < 0.06:
            labels[i] = OUTLIER_TOPIC_ID
    return {"labels": labels, "top_terms": center_terms, "method": method, "sims": sims}


def cluster(workspace: str | Path, n_clusters: int = 5,
            emit_brief: bool = False) -> dict:
    w = Path(workspace)
    analysis = w / "data" / "analysis"
    src = analysis / "reviews_clean.jsonl"
    reviews = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not reviews:
        raise RuntimeError("空结果告警：reviews_clean.jsonl 无数据；0 条 ≠ 没有评论，请回查采集日志")

    texts = [str(r.get("review_text", "")) for r in reviews]
    result = _cluster_vectors(texts, n_clusters)
    labels = result["labels"]

    groups: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        groups.setdefault(int(lab), []).append(i)

    topics = []
    for tid in sorted(k for k in groups if k != OUTLIER_TOPIC_ID):
        idxs = groups[tid]
        ranked = sorted(idxs, key=lambda i: -result["sims"][i])
        reps = [{"review_id": reviews[i].get("review_id", ""),
                 "quote": reviews[i].get("review_text", ""),
                 "channel": reviews[i].get("channel_key", ""),
                 "rating": reviews[i].get("rating")} for i in ranked[:5]]
        # 反例候选：离该簇最远的其他簇样本，供 AI 写 non-matches
        others = [i for i, lab in enumerate(labels) if lab != tid and lab != OUTLIER_TOPIC_ID]
        others.sort(key=lambda i: result["sims"][i])
        non_matches = [{"review_id": reviews[i].get("review_id", ""),
                        "quote": reviews[i].get("review_text", "")} for i in others[:2]]
        topics.append({
            "topic_id": tid, "size": len(idxs),
            "top_terms": result["top_terms"][tid] if tid < len(result["top_terms"]) else [],
            "representative_docs": reps, "non_match_candidates": non_matches,
            "proposed_name": "", "inclusion_criteria": "", "exclusion_criteria": "",
            "needs_human_confirm": True,
            "note": "主题名与纳入标准由 AI 助手命名（模型推断，需人工确认）；脚本不编造主题名",
        })

    outliers = [{"review_id": reviews[i].get("review_id", ""),
                 "quote": reviews[i].get("review_text", "")}
                for i in groups.get(OUTLIER_TOPIC_ID, [])]

    doc = {
        "method": result["method"], "created_at": now_iso(),
        "n_clusters_requested": n_clusters, "topics": topics,
        "outlier_topic_id": OUTLIER_TOPIC_ID, "outliers": outliers,
        "n_reviews": len(reviews),
        "note": "离群点单列 -1；主题名留空，交会话自带 LLM 按显式纳入/排除标准命名",
    }
    (analysis / "topics.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    labeled = analysis / "reviews_labeled.jsonl"
    with labeled.open("w", encoding="utf-8") as f:
        for i, r in enumerate(reviews):
            r["topic_id"] = int(labels[i])
            r["topic_confidence"] = round(float(result["sims"][i]), 4)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    brief_path = ""
    if emit_brief:
        brief_path = str(_emit_brief(w, doc))
    audit(w, "cluster", actor="pipeline",
          note=f"method={result['method']}; topics={len(topics)}; outliers={len(outliers)}")
    return {"method": result["method"], "topics": len(topics),
            "outliers": len(outliers), "reviews": len(reviews),
            "topics_path": str(analysis / "topics.json"),
            "labeled_path": str(labeled), "brief_path": brief_path}


def _emit_brief(w: Path, doc: dict) -> Path:
    """生成给 AI 助手的主题命名任务包（脚本不调外部 API、不要 key）。"""
    out = w / "out" / "llm_brief.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 主题命名任务包（AI 建议，需人工确认）",
        "",
        "> 本文件由 `ur_cluster.py --emit-brief` 生成，全部引文来自脱敏样例或真实采集的逐字原句。",
        "> 请按 `assets/llm_prompt_template.md` 给出：显式纳入标准 / 排除标准 / 代表引文 / 反例 / score rationale。",
        "> ⛔ 禁止编造 review_id 与原句；主题名只是索引不是事实；输出永远是「建议」，需人工确认。",
        "",
        f"聚类方式：{doc['method']}；评论数：{doc['n_reviews']}；主题数：{len(doc['topics'])}；离群点：{len(doc['outliers'])} 条（单列 -1）。",
        "",
    ]
    for t in doc["topics"]:
        lines += [f"## T{t['topic_id']:02d}（{t['size']} 条）", "",
                  f"- 高频字词：{'、'.join(t['top_terms'][:8])}", "",
                  "代表原句（逐字，带 review_id）：", ""]
        for r in t["representative_docs"]:
            lines.append(f"- `{r['review_id']}`（{r['channel']}，{r['rating']}星）：{r['quote']}")
        lines += ["", "反例候选（说明为何不算这个主题）：", ""]
        for r in t["non_match_candidates"]:
            lines.append(f"- `{r['review_id']}`：{r['quote']}")
        lines += ["", "待 AI 填写：", "",
                  "| 字段 | 内容 |", "| --- | --- |",
                  "| proposed_name | （待填，只是索引） |",
                  "| inclusion_criteria | （待填，显式纳入标准） |",
                  "| exclusion_criteria | （待填，显式排除标准） |",
                  "| rationale | （待填，score rationale） |",
                  "| confidence | （待填） |",
                  "| needs_human_confirm | true |", ""]
    lines += ["## 结论后核查追问", "",
              "1. 核查关键事实：这些原句是否真的都在数据里？",
              "2. 探查假设：主题命名是否把两类不同诉求混成一簇？",
              "3. 缺失上下文：有没有哪个渠道/时间窗样本太少导致结论不稳？", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="轻量本地聚类")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--n-clusters", type=int, default=5)
    ap.add_argument("--emit-brief", action="store_true")
    args = ap.parse_args()
    print(json.dumps(cluster(args.workspace, args.n_clusters, args.emit_brief),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
