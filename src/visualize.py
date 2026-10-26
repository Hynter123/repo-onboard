#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 结构图可视化（出报告图用）
========================================

两张图：
    view  —— 默认视图（剪枝+聚合后）：上百个文件压成 ≤15 个节点。
             这是"可读性"的直接证据，也是视频里最该给评委看的一屏。
    graph —— 完整依赖关系图（只建议给小仓库用，否则就是意大利面）。

纯静态出图（matplotlib + networkx spring_layout，固定随机种子保证可复现），
不依赖前端，报告和 PPT 里直接能用。

用法：
    python src/visualize.py view  Delgan/loguru
    python src/visualize.py graph theskumar/python-dotenv
    python src/visualize.py view  tox-dev/pipdeptree -o out/view_pipdeptree.png
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib import font_manager

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rank as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"


def setup_cjk_font():
    """Windows 自带的中文字体，不装额外字体。否则标签会全变成方块。"""
    cands = ["Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "SimSun",
             "Noto Sans CJK SC", "Source Han Sans SC"]
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in cands:
        if name in have:
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            return name
    # 字体表里没有就直接从系统路径注册
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
              r"C:\Windows\Fonts\simsun.ttc"):
        if Path(p).exists():
            try:
                font_manager.fontManager.addfont(p)
                name = font_manager.FontProperties(fname=p).get_name()
                plt.rcParams["font.family"] = name
                plt.rcParams["axes.unicode_minus"] = False
                return name
            except Exception:
                continue
    return None


setup_cjk_font()

NODE_COLORS = {
    "核心文件": "#e8590c",
    "其余源码": "#f0b27a",
    "（测试目录）": "#adb5bd",
    "（内嵌第三方）": "#b197fc",
    "（辅助脚本）": "#74c0fc",
}


def _short(path: str, maxlen: int = 26) -> str:
    """标签尽量短，只留最后两段路径。"""
    parts = path.split("/")
    s = "/".join(parts[-2:]) if len(parts) > 2 else path
    return s if len(s) <= maxlen else "…" + s[-(maxlen - 1):]


def build_view_graph(graph: dict, max_nodes: int = 15) -> tuple[nx.Graph, dict]:
    """把文件级图折叠成默认视图的节点与边。"""
    view, total = R.default_view(graph, max_nodes=max_nodes)

    file2node: dict[str, int] = {}
    for i, v in enumerate(view):
        for m in v.get("members", [v["representative"]]):
            file2node[m] = i

    G = nx.Graph()
    for i, v in enumerate(view):
        G.add_node(i, label=v["cluster"] if v["cluster"] != "核心文件"
                   else _short(v["representative"]),
                   cluster=v["cluster"], count=v["file_count"])

    # 聚合文件级边到视图节点之间，并按边类型计数
    agg: dict[tuple[int, int], Counter] = defaultdict(Counter)
    for e in graph["edges"]:
        if e["type"] not in ("imports", "calls", "inherits"):
            continue
        a = file2node.get(R.rel_of(e["source"]))
        b = file2node.get(R.rel_of(e["target"]))
        if a is None or b is None or a == b:
            continue
        agg[(min(a, b), max(a, b))][e["type"]] += 1

    for (a, b), c in agg.items():
        G.add_edge(a, b, weight=sum(c.values()), types=dict(c))

    return G, {"view": view, "total": total, "file2node": file2node}


def draw_view(repo: str, out: Path, max_nodes: int = 15):
    graph = R.load_graph(repo)
    G, meta = build_view_graph(graph, max_nodes)
    view, total = meta["view"], meta["total"]

    fig, ax = plt.subplots(figsize=(13, 9))
    # kamada_kawai 比 spring 分离度更好，且完全确定（无需随机种子）
    try:
        pos = nx.kamada_kawai_layout(G)
    except Exception:
        pos = nx.spring_layout(G, seed=42, k=1.6, iterations=400)

    colors = [NODE_COLORS.get(G.nodes[n]["cluster"], "#dee2e6") for n in G]
    sizes = []
    for n in G:
        d = G.nodes[n]
        sizes.append(320 if d["cluster"] == "核心文件" else min(220 + d["count"] * 4, 1400))

    widths = [min(0.6 + G[u][v]["weight"] * 0.12, 4.0) for u, v in G.edges()]

    nx.draw_networkx_edges(G, pos, ax=ax, width=widths, alpha=0.40,
                           edge_color="#868e96")
    nx.draw_networkx_nodes(G, pos, ax=ax, node_color=colors, node_size=sizes,
                           edgecolors="white", linewidths=1.8)

    # 标签放到节点下方，加白底，避免压住节点和边
    for n in G:
        x, y = pos[n]
        ax.annotate(G.nodes[n]["label"], xy=(x, y), xytext=(0, -14),
                    textcoords="offset points", ha="center", va="top",
                    fontsize=8.5, color="#212529",
                    bbox=dict(boxstyle="round,pad=0.18", fc="white",
                              ec="none", alpha=0.75))

    ax.set_title(f"RepoOnboard 默认视图 · {repo}\n"
                 f"{total} 个 Python 文件  →  {len(view)} 个节点"
                 f"（压缩 {total / max(len(view), 1):.1f}×）",
                 fontsize=14)
    ax.axis("off")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return total, len(view)


def draw_full_graph(repo: str, out: Path):
    graph = R.load_graph(repo)
    mods = R.modules(graph)
    # candidate_pool 返回节点 ID，这里统一换成「路径 → 中心性得分」
    score_of = {mods[nid]["path"]: mods[nid].get("metrics", {}).get("score", 0.0)
                for nid in R.candidate_pool(graph)}
    pool = set(score_of)

    G = nx.DiGraph()
    for path, sc in score_of.items():
        G.add_node(path, score=sc)

    for e in graph["edges"]:
        if e["type"] not in ("imports", "calls", "inherits"):
            continue
        a, b = R.rel_of(e["source"]), R.rel_of(e["target"])
        if a in pool and b in pool and a != b:
            G.add_edge(a, b)

    fig, ax = plt.subplots(figsize=(11, 8))
    pos = nx.spring_layout(G, seed=42, k=0.9, iterations=400)
    scores = [G.nodes[n].get("score", 0.0) for n in G]
    mx = max(scores) or 1.0

    nx.draw_networkx_edges(G, pos, ax=ax, width=0.7, alpha=0.3,
                           edge_color="#adb5bd", arrows=True,
                           arrowsize=6, connectionstyle="arc3,rad=0.06")
    nx.draw_networkx_nodes(
        G, pos, ax=ax,
        node_size=[180 + 900 * (s / mx) for s in scores],
        node_color=scores, cmap="OrRd", edgecolors="white", linewidths=1.2,
        vmin=0, vmax=mx)
    nx.draw_networkx_labels(G, pos, ax=ax,
                            labels={n: _short(n, 22) for n in G},
                            font_size=7.5)

    ax.set_title(f"Full dependency graph · {repo}  "
                 f"({G.number_of_nodes()} files, {G.number_of_edges()} edges)",
                 fontsize=13)
    ax.axis("off")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return G.number_of_nodes(), G.number_of_edges()


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["view", "graph"])
    ap.add_argument("repo")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--max-nodes", type=int, default=15)
    args = ap.parse_args()

    slug = args.repo.replace("/", "__")
    if args.mode == "view":
        out = Path(args.out) if args.out else OUT / f"view_{slug}.png"
        total, n = draw_view(args.repo, out, args.max_nodes)
        print(f"{args.repo}: {total} 文件 → {n} 节点  →  {out}")
    else:
        out = Path(args.out) if args.out else OUT / f"graph_{slug}.png"
        n, e = draw_full_graph(args.repo, out)
        print(f"{args.repo}: {n} 节点 / {e} 边  →  {out}")


if __name__ == "__main__":
    main()
