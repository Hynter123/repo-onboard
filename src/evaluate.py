#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · D5 评测
=====================

任务：给定一句真实 issue 描述，预测该读哪些文件。
评测口径：**已合并 PR 真实改动的文件**作为 ground truth —— 不含任何人工标注，
         因此不存在"标注主观性"这一质疑点。

三个基线 + 完整消融 + 5 折交叉验证的选择流程估计。

用法：
    python src/evaluate.py
    python src/evaluate.py --no-cv        # 只看全量消融
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rank as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"

KEYMAP = {"keyword": "kw", "embedding": "vec", "symbol": "sym"}
BASELINES = [("keyword", "① BM25 关键词"), ("embedding", "② 纯向量检索"),
             ("symbol", "③ 仅符号链接")]


# ==========================================================================
class RepoIndex:
    def __init__(self, repo: str):
        self.repo = repo
        self.graph = R.load_graph(repo)
        self.ids, self.texts = R.file_texts(self.graph)
        self.vecs = R.file_vectors(repo, self.ids, self.texts)
        self.cent = R.centrality_of(self.graph)
        self.adj = R.module_adjacency(self.graph)
        self.mods = R.modules(self.graph)
        self.view, self.total_files = R.default_view(self.graph)
        self.path_of = {nid: self.mods[nid]["path"] for nid in self.ids}
        self.nid_of_path = {v: k for k, v in self.path_of.items()}

    def components(self, query: str) -> dict:
        return R.score_components(self.graph, self.repo, query)


# ==========================================================================
def eval_one(idx: RepoIndex, ranking: dict, gt_files: list[str], k: int = 5) -> dict:
    ordered = [(nid, sc) for nid, sc in
               sorted(ranking.items(), key=lambda kv: (-kv[1], kv[0]))
               if nid in idx.path_of]
    ranked_paths = [idx.path_of[nid] for nid, _ in ordered]
    ranked_nids = [nid for nid, _ in ordered]

    gt = set(gt_files)
    reachable = gt & set(idx.path_of.values())
    topk = set(ranked_paths[:k])

    recall = len(topk & reachable) / len(gt) if gt else 0.0
    top1 = 1.0 if (ranked_paths and ranked_paths[0] in gt) else 0.0
    mrr = 0.0
    for i, p in enumerate(ranked_paths, 1):
        if p in gt:
            mrr = 1.0 / i
            break

    sg_paths = {idx.path_of[n] for n in R.subgraph_of(idx.graph, ranked_nids[:k], hops=1)
                if n in idx.path_of}
    cover = len(sg_paths & reachable) / len(gt) if gt else 0.0

    return {"recall@5": recall, "top1": top1, "mrr": mrr,
            "subgraph": cover, "reachable": len(reachable) / len(gt) if gt else 0.0}


def aggregate(rows: list[dict]) -> dict:
    keys = ["recall@5", "top1", "mrr", "subgraph", "reachable"]
    return {k: (sum(r[k] for r in rows) / len(rows) if rows else 0.0) for k in keys}


def run_method(idxs, tasks, method: str, k: int = 5) -> list[dict]:
    rows = []
    for t in tasks:
        idx = idxs[t["repo"]]
        comps = t["_comps"]
        if method in KEYMAP:
            ranking = comps[KEYMAP[method]]
        else:
            ranking = R.fuse(comps, method)      # method 即预设名
        rows.append(eval_one(idx, ranking, t["gt_files"], k))
    return rows


def objective(m: dict) -> float:
    return 0.5 * m["recall@5"] + 0.3 * m["mrr"] + 0.2 * m["top1"]


# ==========================================================================
def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(ROOT / "data" / "tasks.json"))
    ap.add_argument("--no-cv", action="store_true")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    tasks.sort(key=lambda t: t["id"])
    for t in tasks:
        t["query"] = (t["issue_title"] + " " + (t["issue_body"] or "")[:800]).strip()

    repos = sorted({t["repo"] for t in tasks})
    print(f"任务 {len(tasks)} 条，仓库 {len(repos)} 个")

    idxs: dict[str, RepoIndex] = {}
    for r in repos:
        print(f"  建索引 {r} ...", end="", flush=True)
        idxs[r] = RepoIndex(r)
        print(f" {len(idxs[r].ids)} 个候选文件")

    print("\n【压缩率】默认视图（剪枝+聚合）:")
    total_all = sum(i.total_files for i in idxs.values())
    view_all = sum(len(i.view) for i in idxs.values())
    for r, idx in idxs.items():
        print(f"  {r:<28} {idx.total_files:>4} 文件 → {len(idx.view):>2} 节点"
              f"   压缩 {idx.total_files / max(len(idx.view), 1):.1f}×")
    print(f"  {'合计':<28} {total_all:>4} 文件 → {view_all:>2} 节点"
          f"   压缩 {total_all / max(view_all, 1):.1f}×")

    print("\n计算分数分量 ...")
    for t in tasks:
        t["_comps"] = idxs[t["repo"]].components(t["query"])

    # ---------- 全量消融 ----------
    print("\n" + "=" * 74)
    print(f"全量结果（{len(tasks)} 条任务；k={args.k}）")
    print("=" * 74)
    print(f"{'方法 / 预设':<26}{'Recall@5':>10}{'Top-1':>9}{'MRR':>9}{'子图覆盖':>10}")
    print("-" * 74)

    results = {}
    for name, label in BASELINES:
        m = aggregate(run_method(idxs, tasks, name, args.k))
        results[name] = m
        print(f"{label:<24}{m['recall@5']:>11.3f}{m['top1']:>9.3f}"
              f"{m['mrr']:>9.3f}{m['subgraph']:>11.3f}")
    print("-" * 74)

    presets = {}
    for p in R.PRESETS:
        m = aggregate(run_method(idxs, tasks, p, args.k))
        presets[p] = m
        print(f"  {p:<24}{m['recall@5']:>11.3f}{m['top1']:>9.3f}"
              f"{m['mrr']:>9.3f}{m['subgraph']:>11.3f}")

    best_preset = max(presets, key=lambda p: objective(presets[p]))
    results["ours"] = presets[best_preset]
    print("-" * 74)
    print(f"最优预设：{best_preset}")

    base, ours = results["embedding"], results["ours"]
    print("\n【结论】本方案 vs 纯向量检索:")
    for m in ("recall@5", "top1", "mrr", "subgraph"):
        d = ours[m] - base[m]
        rel = d / base[m] if base[m] else 0.0
        print(f"  {m:<10} {base[m]:.3f} → {ours[m]:.3f}"
              f"   ({'+' if d >= 0 else ''}{d:.3f}, {'+' if rel >= 0 else ''}{rel:.1%})")
    print(f"  ground truth 可达率 {ours['reachable']:.1%}")

    # ---------- 5 折 CV：诚实估计"选择流程"的泛化能力 ----------
    cv = None
    if not args.no_cv:
        print("\n" + "=" * 74)
        print(f"{args.folds} 折交叉验证：每折在其余折上挑预设，再到留出折上评估")
        print("=" * 74)
        rows_all, picked = [], []
        for f in range(args.folds):
            train = [t for i, t in enumerate(tasks) if i % args.folds != f]
            held = [t for i, t in enumerate(tasks) if i % args.folds == f]
            cand = {p: aggregate(run_method(idxs, train, p, args.k)) for p in R.PRESETS}
            bp = max(cand, key=lambda p: objective(cand[p]))
            hm = aggregate(run_method(idxs, held, bp, args.k))
            picked.append(bp)
            rows_all.extend(run_method(idxs, held, bp, args.k))
            print(f"  折{f + 1}: 选 {bp:<24} 留出 n={len(held):>2}  "
                  f"R@5={hm['recall@5']:.3f}  Top1={hm['top1']:.3f}  MRR={hm['mrr']:.3f}")
        cv = aggregate(rows_all)
        from collections import Counter
        print(f"\n  被选中的预设：{dict(Counter(picked))}")
        print(f"  CV 汇总（等价于全部 {len(tasks)} 条各评估一次）:")
        print(f"    Recall@5={cv['recall@5']:.3f}  Top-1={cv['top1']:.3f}  "
              f"MRR={cv['mrr']:.3f}  子图覆盖={cv['subgraph']:.3f}")

    # ---------- 按仓库拆解 ----------
    print("\n【按仓库】Recall@5（纯向量 vs 最优预设）:")
    by_repo: dict[str, list[dict]] = {}
    for t in tasks:
        by_repo.setdefault(t["repo"], []).append(t)
    for r, ts in by_repo.items():
        a = aggregate(run_method(idxs, ts, best_preset, args.k))["recall@5"]
        b = aggregate(run_method(idxs, ts, "embedding", args.k))["recall@5"]
        flag = "↑" if a > b else ("↓" if a < b else "=")
        print(f"  {r:<28} n={len(ts):>2}  向量 {b:.2f} → 本方案 {a:.2f}  {flag}")

    # ---------- 落盘 ----------
    OUT.mkdir(exist_ok=True)

    # LLM 零样本基线由 src/llm_baseline.py 单独跑（要调 Ollama，耗时不稳），
    # 这里只读取它的结果一并写入，保证 metrics.json 始终是"全量"的。
    llm = None
    lf = OUT / "llm_baseline.json"
    if lf.exists():
        try:
            d = json.loads(lf.read_text(encoding="utf-8"))
            if d.get("n") == len(tasks):
                llm = d
        except Exception:
            llm = None

    baselines_out = {k: {m: round(v, 4) for m, v in a.items()}
                     for k, a in results.items() if k != "ours"}
    llm_meta = None
    if llm:
        baselines_out[llm["label"]] = llm["aggregate"]
        llm_meta = {"model": llm.get("model"), "n": llm.get("n"),
                    "temperature": llm.get("temperature"),
                    "seconds_total": llm.get("seconds_total")}

    (OUT / "metrics.json").write_text(json.dumps({
        "n_tasks": len(tasks), "k": args.k,
        "baselines": baselines_out,
        "presets": {p: {m: round(v, 4) for m, v in a.items()} for p, a in presets.items()},
        "best_preset": best_preset,
        "ours": {m: round(v, 4) for m, v in results["ours"].items()},
        "cv": {m: round(v, 4) for m, v in cv.items()} if cv else None,
        "llm_baseline_meta": llm_meta,
        "compression": {r: {"files": i.total_files, "view_nodes": len(i.view)}
                        for r, i in idxs.items()},
        "compression_total": {"files": total_all, "view_nodes": view_all},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 柱状图 ----------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        metrics = ["recall@5", "top1", "mrr", "subgraph"]
        titles = ["Recall@5", "Top-1 hit", "MRR", "Subgraph cov."]
        groups = [("keyword", "BM25 keyword"), ("embedding", "Dense retrieval"),
                  ("symbol", "Symbol link only"),
                  ("llm", "LLM zero-shot (7B)"), ("ours", "Ours (fused)")]
        x = np.arange(len(metrics))
        width = 0.16
        fig, ax = plt.subplots(figsize=(10, 4.8))
        colors = ["#adb5bd", "#4c8bf5", "#40c057", "#7048e8", "#e8590c"]
        for i, (key, lab) in enumerate(groups):
            if key == "llm":
                if not llm:
                    continue
                vals = [llm["aggregate"][m] for m in metrics]
            else:
                vals = [results[key][m] for m in metrics]
            bars = ax.bar(x + (i - 2) * width, vals, width, label=lab, color=colors[i])
            for b, v in zip(bars, vals):
                ax.text(b.get_x() + b.get_width() / 2, v + 0.012, f"{v:.2f}",
                        ha="center", fontsize=7.5)
        ax.set_xticks(x)
        ax.set_xticklabels(titles)
        ax.set_ylabel("score")
        ax.set_ylim(0, 1.08)
        ax.set_title(f"RepoOnboard · issue-to-file localization (n={len(tasks)})")
        ax.legend(frameon=False, ncol=5, fontsize=8, loc="upper center")
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(OUT / "metrics.png", dpi=170)
        print(f"\n图表 → {OUT / 'metrics.png'}")
    except Exception as e:
        print(f"\n（图表生成失败：{e}）")


if __name__ == "__main__":
    main()
