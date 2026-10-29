#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 仓库分析核心
==========================

把「源码目录 → 前端需要的完整 payload」这件事集中在一个地方，
供两个入口共用：

    · `src/build_web.py`  预生成 6 个示例仓库（离线包）
    · `src/server.py`     用户在页面上输入任意仓库地址，实时解析

payload 结构（前端 `app.js` 依赖）：
    {
      slug, name, stats, total_files,
      nodes, edges, view, mindmap, paths,
      tasks: [ {id, issue_title, query, top5, sub, gt} ]
    }
"""

from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_build as GB  # noqa: E402
import qa  # noqa: E402
import rank as R  # noqa: E402

EDGE_TYPES = ("imports", "calls", "inherits", "tests")
VIEW_EDGE_TYPES = ("imports", "calls", "inherits")


# ==========================================================================
# 图 → 前端可用的文件级结构
# ==========================================================================
def file_level_graph(graph: dict) -> tuple[list[dict], list[dict], dict]:
    mods = R.modules(graph)
    nodes = []
    for nid, n in mods.items():
        m = n.get("metrics", {})
        nodes.append({
            "id": n["path"],
            "label": n["path"].rsplit("/", 1)[-1],
            "module": n.get("module_name", ""),
            "score": m.get("score", 0.0),
            "fan_in": m.get("fan_in", 0),
            "pagerank": m.get("pagerank", 0.0),
            "is_test": bool(m.get("is_test")),
            "is_aux": bool(m.get("is_aux")),
            "is_vendor": bool(m.get("is_vendor")),
        })
    nodes.sort(key=lambda x: (-x["score"], x["id"]))

    # 同名消歧：invoke 里同时有 invoke/context.py 与 invoke/parser/context.py
    base_count = Counter(n["label"] for n in nodes)
    for n in nodes:
        if base_count[n["label"]] > 1:
            parts = n["id"].split("/")
            n["label"] = "/".join(parts[-2:]) if len(parts) > 1 else n["label"]

    pairs: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for e in graph["edges"]:
        if e["type"] not in EDGE_TYPES:
            continue
        a, b = R.rel_of(e["source"]), R.rel_of(e["target"])
        if not a or not b or a == b:
            continue
        pairs[(a, b)][e["type"]] += 1

    edges = [{"source": a, "target": b, "weight": int(sum(c.values())), "types": dict(c)}
             for (a, b), c in sorted(pairs.items())]

    syms: dict[str, list[dict]] = defaultdict(list)
    for n in graph["nodes"]:
        if n["type"] in ("function", "method", "class"):
            syms[R.rel_of(n["id"])].append({
                "name": n["label"], "kind": n["type"],
                "in": n.get("metrics", {}).get("in_degree", 0),
            })
    for n in nodes:
        ss = sorted(syms.get(n["id"], []), key=lambda x: (-x["in"], x["name"]))
        n["symbols"] = ss[:6]

    return nodes, edges, graph.get("stats", {})


def mindmap_md(graph: dict, name: str, max_files_per_dir: int = 8) -> str:
    """生成思维导图用的 markdown（markmap 渲染层级）。"""
    mods = R.modules(graph)

    def flags(nid: str) -> dict:
        return mods[nid].get("metrics", {})

    pool = [nid for nid in R.candidate_pool(graph)
            if not flags(nid).get("is_vendor") and not flags(nid).get("is_aux")]

    by_dir: dict[str, list[str]] = defaultdict(list)
    for nid in pool:
        rel = mods[nid]["path"]
        d = rel.rsplit("/", 1)[0] if "/" in rel else "."
        by_dir[d].append(rel.rsplit("/", 1)[-1])

    folded: Counter = Counter()
    for n in mods.values():
        m = n.get("metrics", {})
        if m.get("is_test"):
            folded["（测试目录）"] += 1
        elif m.get("is_vendor"):
            folded["（内嵌第三方）"] += 1
        elif m.get("is_aux"):
            folded["（辅助脚本）"] += 1

    lines = [f"# {name}"]
    for d in sorted(by_dir, key=lambda x: (-len(by_dir[x]), x)):
        files = sorted(by_dir[d])
        lines.append(f"## {d}/　（{len(files)} 个文件）" if d != "." else "## （仓库根）")
        for f in files[:max_files_per_dir]:
            lines.append(f"### {f}")
        if len(files) > max_files_per_dir:
            lines.append(f"### …另有 {len(files) - max_files_per_dir} 个文件")
    for k in ("（测试目录）", "（内嵌第三方）", "（辅助脚本）"):
        if folded.get(k):
            lines.append(f"## {k}　{folded[k]} 个文件")
    return "\n".join(lines)


def compact_view_layout(view: list[dict], edges: list[dict]) -> list[dict]:
    """Use the saved graph layout, then pack disconnected components together."""
    graph = nx.Graph()
    graph.add_nodes_from(range(len(view)))
    file_to_view: dict[str, int] = {}
    for i, item in enumerate(view):
        for path in item.get("members", [item.get("representative", "")]):
            if path:
                file_to_view[path] = i

    # Tests are useful for task evidence, but their links make the architecture view noisy.
    for edge in edges:
        source = file_to_view.get(edge["source"])
        target = file_to_view.get(edge["target"])
        if source is None or target is None or source == target:
            continue
        weight = sum(int((edge.get("types") or {}).get(kind, 0)) for kind in VIEW_EDGE_TYPES)
        if weight <= 0:
            continue
        left, right = sorted((source, target))
        if graph.has_edge(left, right):
            graph[left][right]["weight"] += weight
        else:
            graph.add_edge(left, right, weight=weight)

    if not view:
        return []
    try:
        positions = nx.kamada_kawai_layout(graph)
    except Exception:
        positions = nx.spring_layout(graph, seed=42, k=1.6, iterations=400, weight="weight")

    # Layout engines can put disconnected components very far apart. Preserve each
    # component's internal shape, but repack the components around the largest one.
    components = sorted(
        nx.connected_components(graph),
        key=lambda component: (-len(component), min(component)),
    )
    if len(components) > 1:
        packed: dict[int, tuple[float, float]] = {}
        main = components[0]

        def centered(component: set[int]) -> dict[int, tuple[float, float]]:
            cx = sum(float(positions[i][0]) for i in component) / len(component)
            cy = sum(float(positions[i][1]) for i in component) / len(component)
            return {i: (float(positions[i][0]) - cx, float(positions[i][1]) - cy)
                    for i in component}

        main_points = centered(main)
        main_radius = max(
            (x * x + y * y) ** 0.5 for x, y in main_points.values()
        ) or 1.0
        main_scale = 0.42 / main_radius
        for i, (x, y) in main_points.items():
            packed[i] = (x * main_scale, y * main_scale)

        satellites = components[1:]
        for index, component in enumerate(satellites):
            local = centered(component)
            radius = max((x * x + y * y) ** 0.5 for x, y in local.values())
            local_scale = min(1.0, 0.14 / radius) if radius else 1.0
            ring = index // 8
            slot = index % 8
            slots = min(8, len(satellites) - ring * 8)
            orbit = 0.42 + min(radius * local_scale, 0.14) + 0.08 + ring * 0.12
            angle = -3.141592653589793 / 2 + 2 * 3.141592653589793 * slot / max(1, slots)
            cx, cy = orbit * math.cos(angle), orbit * math.sin(angle)
            for i, (x, y) in local.items():
                packed[i] = (cx + x * local_scale, cy + y * local_scale)

        extent = max(max(abs(x), abs(y)) for x, y in packed.values()) or 1.0
        positions = {i: (x / extent, y / extent) for i, (x, y) in packed.items()}
    else:
        cx = sum(float(positions[i][0]) for i in range(len(view))) / len(view)
        cy = sum(float(positions[i][1]) for i in range(len(view))) / len(view)
        centered_positions = {
            i: (float(positions[i][0]) - cx, float(positions[i][1]) - cy)
            for i in range(len(view))
        }
        extent = max(max(abs(x), abs(y)) for x, y in centered_positions.values()) or 1.0
        positions = {i: (x / extent, y / extent) for i, (x, y) in centered_positions.items()}

    return [{"x": float(positions[i][0]), "y": float(positions[i][1])}
            for i in range(len(view))]


def _symbol_names(graph: dict) -> dict[str, list[str]]:
    """文件 → 出现最多的符号名（按被调用次数倒序），供路径步骤展示。"""
    rows: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for n in graph.get("nodes", []):
        if n.get("type") in ("function", "method", "class"):
            rel = R.rel_of(n.get("id", ""))
            if rel:
                rows[rel].append((int(n.get("metrics", {}).get("in_degree", 0) or 0),
                                  n.get("label", "")))
    return {rel: [name for _, name in sorted(items, key=lambda x: (-x[0], x[1]))[:3]]
            for rel, items in rows.items()}


def extract_paths(graph: dict, max_paths: int = 3, max_len: int = 6) -> list[dict]:
    """抽取「主干路径」：从入口文件沿最强依赖边走 3~6 个文件。

    每条路径除了节点顺序，还带每一步的角色 / 扇入 / 关键符号，
    以及相邻两步之间靠哪些边连起来（调用、导入还是继承）——
    这些字段是前端「主干路径」页画流程图的依据，去掉会让页面退化成一行文件名。
    """
    mods = R.modules(graph)
    pool = {nid for nid in R.candidate_pool(graph)
            if not mods[nid].get("metrics", {}).get("is_vendor")
            and not mods[nid].get("metrics", {}).get("is_aux")}

    out_edges: dict[str, set[str]] = defaultdict(set)
    weight: Counter = Counter()
    fan_in: Counter = Counter()
    kinds: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for e in graph["edges"]:
        if e["type"] not in ("imports", "calls", "inherits"):
            continue
        na = "mod:" + R.rel_of(e["source"])
        nb = "mod:" + R.rel_of(e["target"])
        if na not in pool or nb not in pool or na == nb:
            continue
        out_edges[na].add(nb)
        weight[(na, nb)] += 2 if e["type"] == "calls" else 1
        fan_in[nb] += 1
        kinds[(na, nb)][e["type"]] += 1

    ENTRY_HINT = ("__main__", "cli", "main", "app", "runner", "program",
                  "console", "entry", "cmd")

    def entry_score(nid: str) -> float:
        base = mods[nid]["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        s = 5.0 if any(h in base for h in ENTRY_HINT) else 0.0
        s += min(len(out_edges.get(nid, ())), 8) * 0.5
        s -= min(fan_in.get(nid, 0), 10) * 0.8
        s += mods[nid].get("metrics", {}).get("score", 0.0)
        return s

    cands = sorted(pool, key=entry_score, reverse=True)[:10]
    symbols = _symbol_names(graph)

    # 「枢纽」按本仓库的扇入分布定，不写死阈值：各仓库规模差太多。
    # 这里用**去重后的文件数**（metrics.fan_in），而不是两个文件之间有多少条边；
    # 否则同一份 payload 里「被 26 个文件依赖」和速答页的「被 4 个文件依赖」会互相打脸。
    fans = {nid: int(mods[nid].get("metrics", {}).get("fan_in", 0) or 0) for nid in pool}
    fan_values = sorted(fans.values()) or [0]
    hub_cut = max(4, fan_values[int(len(fan_values) * 0.75)] if len(fan_values) > 3 else 4)

    def role_of(index: int, last: int, nid: str) -> str:
        fan = fans.get(nid, 0)
        if index == 0:
            return "入口"
        if fan >= hub_cut:
            return "枢纽"
        if index == last and fan <= 1:
            return "叶子"
        if mods[nid].get("metrics", {}).get("score", 0.0) >= 0.30:
            return "核心"
        return "模块"

    paths, covered = [], set()
    for start in cands:
        if len(paths) >= max_paths:
            break
        chain, cur = [start], start
        while len(chain) < max_len:
            nxt = [(b, weight[(cur, b)]) for b in out_edges.get(cur, ()) if b not in chain]
            if not nxt:
                break
            nxt.sort(key=lambda it: (-(it[1] - 0.45 * min(fan_in.get(it[0], 0), 10)), it[0]))
            cur = nxt[0][0]
            chain.append(cur)
        if len(chain) < 3:
            continue
        if len(set(chain) & covered) / len(chain) > 0.5:
            continue
        covered |= set(chain)
        last = len(chain) - 1
        steps = []
        for index, nid in enumerate(chain):
            node = mods[nid]
            metrics = node.get("metrics", {})
            path = node["path"]
            step = {
                "path": path,
                "label": path.rsplit("/", 1)[-1],
                "role": role_of(index, last, nid),
                "fan_in": fans.get(nid, 0),
                "fan_out": len(out_edges.get(nid, ())),
                "score": round(float(metrics.get("score", 0.0) or 0.0), 4),
                "symbols": symbols.get(path, [])[:2],
                "via": None,
            }
            if index > 0:
                counts = kinds.get((chain[index - 1], nid), Counter())
                step["via"] = {
                    "calls": int(counts.get("calls", 0)),
                    "imports": int(counts.get("imports", 0)),
                    "inherits": int(counts.get("inherits", 0)),
                    "total": int(sum(counts.values())),
                }
            steps.append(step)

        entry_base = mods[chain[0]]["path"].rsplit("/", 1)[-1]
        end_base = mods[chain[-1]]["path"].rsplit("/", 1)[-1]
        paths.append({
            "index": len(paths) + 1,
            "label": f"{entry_base} → {end_base}",
            "entry": mods[chain[0]]["path"],
            "nodes": [mods[n]["path"] for n in chain],
            "steps": steps,
        })

    # 同一个文件常被多条路径复用——前端要据此提示「这几条路径在这里汇合」
    for path in paths:
        for step in path["steps"]:
            step["in_paths"] = [{"index": other["index"], "label": other["label"]}
                                for other in paths if step["path"] in other["nodes"]]
    return paths


def summarize_paths(paths: list[dict]) -> dict:
    """主干路径的整体统计（页面顶部那排数字）。"""
    files: Counter = Counter()
    for path in paths:
        for step in path["steps"]:
            files[step["path"]] += 1
    return {
        "paths": len(paths),
        "files": len(files),
        "entries": len({p["entry"] for p in paths}),
        "shared": [{"path": p, "count": c} for p, c in files.most_common() if c > 1],
    }


# ==========================================================================
# 完整 payload
# ==========================================================================
def payload(root: Path, name: str, slug: str | None = None,
            task: str | None = None, with_centrality: bool = True) -> dict:
    """源码目录 → 前端 payload。task 非空时额外算一次「任务定位」。"""
    slug = slug or name.replace("/", "__")
    graph = GB.build(root, with_centrality=with_centrality)
    out = payload_from_graph(graph, name, slug, task=task)
    out["_graph"] = graph            # 供调用方继续使用（不写盘，序列化前 pop 掉）
    return out


def payload_from_graph(graph: dict, name: str, slug: str | None = None,
                       task: str | None = None) -> dict:
    """已经建好图时用这个，避免重复解析。"""
    slug = slug or name.replace("/", "__")
    nodes, edges, stats = file_level_graph(graph)
    label_of = {n["id"]: n["label"] for n in nodes}
    view, total = R.default_view(graph)

    paths = extract_paths(graph)
    for p in paths:
        p["labels"] = [label_of.get(n, n.rsplit("/", 1)[-1]) for n in p["nodes"]]

    out = {
        "slug": slug,
        "name": name,
        "stats": stats,
        "total_files": total,
        "nodes": nodes,
        "edges": edges,
        "view": [{"cluster": v["cluster"], "count": v["file_count"],
                  "rep": v["representative"], "score": v.get("score", 0),
                  "members": v.get("members", [])} for v in view],
        "view_layout": compact_view_layout(view, edges),
        "mindmap": mindmap_md(graph, name),
        "paths": paths,
        "path_summary": summarize_paths(paths),
        "qa": qa.build(graph, nodes, edges, paths, stats),
        "label_of": label_of,
        "tasks": [],
    }

    if task and task.strip():
        out["tasks"] = [rank_task(graph, slug, task.strip(), label_of)]

    return out


def rank_task(graph: dict, slug: str, query: str, label_of: dict,
              preset: str = "I 两阶段召回15") -> dict:
    """给定一句任务描述，算出 Top-5 与 1 跳子图（无 ground truth）。"""
    mods = R.modules(graph)
    comps = R.score_components(graph, slug, query)
    fused = R.fuse(comps, preset)
    ranked = R._ranked(fused)[:5]
    top5 = [{"path": mods[nid]["path"], "score": round(sc, 4)} for nid, sc in ranked]

    seeds = {nid for nid, _ in ranked}
    sub = R.subgraph_of(graph, list(seeds), hops=1) & set(comps["ids"])
    sub_paths = sorted(mods[nid]["path"] for nid in sub)

    return {
        "id": "自定义任务",
        "issue_title": query,
        "query": query,
        "top5": top5,
        "sub": sub_paths,
        "gt": [],                 # 任意仓库没有 ground truth
        "synthetic": True,
    }
