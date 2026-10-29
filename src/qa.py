#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 仓库速答（固定问题集）
====================================

为什么不再做自由问答
--------------------
自由问答里，只要问题超出「核心文件 / 测试 / 任务定位」这三类，
模型就只能给套话；而模型一旦编得顺口，反而比不答更糟。所以这里换一个思路：

    把「能答对的问题」做成一份**清单**，每个问题对应一种**确定的结构计算**，
    答案在分析阶段就算好（随 payload 一起返回），前端点选即出。

三条约束（改这个文件前先看）：
    1. 每条回答里的路径、数字必须能在结构图里回查到，不许出现「大概 / 通常」式结论；
    2. 问题数量宁少勿多：答不出来的问题不要放进清单；
    3. 不依赖模型——本地 Qwen 只用于「按任务描述找文件」这一步的可选润色。

对外结构
--------
    build(...) -> {
      "groups":    [{"key", "title"}],
      "questions": [{"id", "group", "title", "desc", "answer", "bullets",
                     "files": [{"path", "label", "reason"}],
                     "variants": [...]  # 可选：同一问题的不同文件视角
                    }]
    }
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from html import escape
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rank as R  # noqa: E402

DEP_TYPES = ("imports", "calls", "inherits")
GROUPS = [
    ("overview", "结构总览"),
    ("reading", "从哪读起"),
    ("deps", "依赖关系"),
    ("quality", "测试与来源"),
    ("task", "按任务定位"),
]
ENTRY_HINT = ("__main__", "cli", "main", "app", "runner", "program",
              "console", "entry", "cmd", "server")


# ==========================================================================
# 小工具
# ==========================================================================
def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}%" if b else "0%"


def _c(text) -> str:
    """把路径包成 <code> 并转义——回答在前端走 v-html，仓库里一个恶意文件名就能注入。"""
    return f"<code>{escape(str(text))}</code>"


def _files(rows, limit: int = 6) -> list[dict]:
    return [{"path": p, "label": _base(p), "reason": r} for p, r in rows[:limit]]


def _join(paths, limit: int = 3) -> str:
    paths = [escape(str(p)) for p in paths]
    if not paths:
        return ""
    head = "；".join(paths[:limit])
    return head + (f" 等 {len(paths)} 个" if len(paths) > limit else "")


def _dep_graph(graph: dict, keep: set[str]) -> tuple[dict, dict]:
    """文件级依赖边（不含测试边）：返回 (out 邻接权重, 每条边的类型计数)。"""
    out: dict[str, Counter] = defaultdict(Counter)
    types: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for edge in graph.get("edges", []):
        if edge.get("type") not in DEP_TYPES:
            continue
        a, b = R.rel_of(edge.get("source", "")), R.rel_of(edge.get("target", ""))
        if not a or not b or a == b or a not in keep or b not in keep:
            continue
        weight = 2 if edge["type"] == "calls" else 1
        out[a][b] += weight
        types[(a, b)][edge["type"]] += 1
    return out, types


def _test_coverage(graph: dict) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(被测模块 -> 测试文件集合, 测试文件 -> 被测模块集合)，均来自 tests 边。"""
    by_module: dict[str, set[str]] = defaultdict(set)
    by_test: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        if edge.get("type") != "tests":
            continue
        test = R.rel_of(edge.get("source", ""))
        module = R.rel_of(edge.get("target", ""))
        if test and module and test != module:
            by_module[module].add(test)
            by_test[test].add(module)
    return by_module, by_test


def _classify(nodes: list[dict]) -> dict[str, list[dict]]:
    """按「测试 > 内嵌第三方 > 辅助 > 核心」的优先级分类。

    三个标记并不互斥（有仓库把 vendored 的测试也放在 tests/ 下），
    不按优先级去重会让各块数字加起来超过文件总数。
    """
    buckets: dict[str, list[dict]] = {"core": [], "test": [], "vendor": [], "aux": []}
    for node in nodes:
        if node["is_test"]:
            buckets["test"].append(node)
        elif node["is_vendor"]:
            buckets["vendor"].append(node)
        elif node["is_aux"]:
            buckets["aux"].append(node)
        else:
            buckets["core"].append(node)
    return buckets


# ==========================================================================
# 每个问题
# ==========================================================================
def q_overview(ctx: dict) -> dict:
    stats, nodes = ctx["stats"], ctx["nodes"]
    total = len(nodes)
    buckets = _classify(nodes)
    core = buckets["core"]
    vendor, aux = buckets["vendor"], buckets["aux"]
    tests = buckets["test"]
    symbols = int(stats.get("symbols") or 0)
    dirs = int(stats.get("dirs") or 0)
    edges = len(ctx["edges"])
    calls = int(stats.get("calls_matched") or 0)

    answer = (
        f"这个仓库共 {total} 个 Python 文件、{dirs} 个目录、{symbols} 个符号；"
        f"结构图里留下 {edges} 条文件级依赖边，其中解析到 {calls} 处跨文件调用。"
        f"按文件角色拆开看，真正需要读的主干代码是 {len(core)} 个（{_pct(len(core), total)}），"
        f"其余是测试、内嵌第三方和辅助脚本。"
    )
    bullets = [f"核心源码 {len(core)} 个（{_pct(len(core), total)}）"]
    for label, count in (("测试文件", len(tests)), ("内嵌第三方", len(vendor)),
                         ("辅助脚本 / 文档示例", len(aux))):
        if count:
            bullets.append(f"{label} {count} 个（{_pct(count, total)}）")
    if len(bullets) == 1:
        bullets.append("没有测试、内嵌第三方或辅助脚本——全部是主干代码")
    rows = [(n["id"], f"结构得分 {n['score']:.3f}，被 {n['fan_in']} 个文件依赖")
            for n in ctx["core_sorted"][:3]]
    return {"id": "overview", "group": "overview", "title": "这个仓库有多大、由什么组成",
            "desc": "规模、目录、核心/测试/第三方占比",
            "answer": answer, "bullets": bullets, "files": _files(rows)}


def q_entry(ctx: dict) -> dict:
    paths = ctx["paths"]
    in_pool = ctx["core"]
    out = ctx["out"]

    scored = []
    for node in in_pool:
        nid = node["id"]
        base = _base(nid).rsplit(".", 1)[0].lower()
        hint = any(h in base for h in ENTRY_HINT)
        scored.append((5.0 if hint else 0.0, len(out.get(nid, ())), -int(node["fan_in"]), nid))
    scored.sort(reverse=True)
    picks = [s[3] for s in scored if s[0] > 0 or s[1] >= 3][:4]
    if paths:
        picks = [paths[0]["entry"]] + [p for p in picks if p != paths[0]["entry"]]
    picks = list(dict.fromkeys(picks))[:3]

    if not picks:
        return {"id": "entry", "group": "overview", "title": "程序从哪里开始执行",
                "desc": "找入口文件", "answer": "这个仓库没有明显的入口文件迹象："
                "既没有 __main__/cli/main 之类的命名，也没有明显的扇出节点。"
                "它更像一个被别的项目 import 的库。", "bullets": [], "files": []}

    first = ctx["nodes_by_id"].get(picks[0], {})
    fan_out = len(out.get(picks[0], ()))
    why = ("文件名符合入口特征（main / cli / __main__），而且是主干路径的起点"
           if any(h in _base(picks[0]).rsplit(".", 1)[0].lower() for h in ENTRY_HINT)
           else "它向外依赖的文件最多，是主干路径的起点")
    answer = (f"入口看 {_c(picks[0])}：{why}。"
              f"它依赖 {fan_out} 个文件，被 {first.get('fan_in', 0)} 个文件依赖。")
    if paths:
        answer += f"从它出发能连出 {len(paths)} 条主干路径（见「主干路径」页签）。"
    bullets = [f"{nid}：扇出 {len(out.get(nid, ()))}，被依赖 "
               f"{ctx['nodes_by_id'].get(nid, {}).get('fan_in', 0)}"
               for nid in picks]
    rows = [(nid, f"扇出 {len(out.get(nid, ()))}，被 "
                  f"{ctx['nodes_by_id'].get(nid, {}).get('fan_in', 0)} 个文件依赖")
            for nid in picks]
    return {"id": "entry", "group": "overview", "title": "程序从哪里开始执行",
            "desc": "按命名与扇出找入口", "answer": answer, "bullets": bullets, "files": _files(rows)}


def q_layers(ctx: dict) -> dict:
    nodes = ctx["nodes"]
    if not nodes:
        return {"id": "layers", "group": "overview", "title": "这个仓库分成哪几块",
                "desc": "按目录划分", "answer": "没有可用于划分的文件。",
                "bullets": [], "files": []}

    total = len(nodes)
    # 去掉 src/、lib/ 这一层，让真正的顶层包名露出来
    roots = Counter(n["id"].split("/")[0] for n in nodes if "/" in n["id"])
    prefix = ""
    if roots:
        name, count = roots.most_common(1)[0]
        if name in ("src", "lib") and count / total >= 0.8:
            prefix = name + "/"

    groups: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        rest = node["id"][len(prefix):] if prefix and node["id"].startswith(prefix) else node["id"]
        key = rest.split("/")[0] + "/" if "/" in rest else "（根目录）"
        groups[key].append(node)

    def core_count(members: list[dict]) -> int:
        return len(_classify(members)["core"])

    ordered = sorted(groups.items(), key=lambda kv: (-core_count(kv[1]), -len(kv[1]), kv[0]))
    core_total = sum(core_count(m) for _, m in ordered) or 1
    bullets, rows = [], []
    for key, members in ordered[:6]:
        buckets = _classify(members)
        core = sorted(buckets["core"], key=lambda n: (-n["score"], n["id"]))
        tests, vendor, aux = len(buckets["test"]), len(buckets["vendor"]), len(buckets["aux"])
        extra = "、".join(part for part in (
            f"测试 {tests}" if tests else "", f"第三方 {vendor}" if vendor else "",
            f"辅助 {aux}" if aux else "") if part)
        bullets.append(f"{key} — 共 {len(members)} 个文件（核心 {len(core)}"
                       + (f"，{extra}" if extra else "") + "）")
        if core:
            rows.append((core[0]["id"], f"{key} 里最核心的文件（得分 {core[0]['score']:.3f}）"))
        elif members:
            rows.append((members[0]["id"], f"{key} 的代表文件"))

    top_key, top_members = ordered[0]
    answer = (f"{total} 个文件分布在 {len(ordered)} 个顶层目录。"
              f"主干代码集中在 {_c(top_key)}（{core_count(top_members)} 个核心文件，"
              f"占主干代码的 {_pct(core_count(top_members), core_total)}）")
    rest = ordered[1:4]
    if rest:
        answer += "；其余主要是 " + "、".join(
            f"{_c(key)}（{len(members)} 个）" for key, members in rest)
    if len(ordered) > 4:
        answer += f"，另有 {len(ordered) - 4} 个更小的目录"
    answer += "。"
    if prefix:
        answer += f"（统计时已去掉公共前缀 {_c(prefix)}）"
    return {"id": "layers", "group": "overview", "title": "这个仓库分成哪几块",
            "desc": "按顶层目录统计规模", "answer": answer, "bullets": bullets,
            "files": _files(rows, 6)}


def q_core(ctx: dict) -> dict:
    core = ctx["core_sorted"][:5]
    if not core:
        return {"id": "core", "group": "reading", "title": "最该先读的几个文件",
                "desc": "按结构重要性排序", "answer": "没有识别到主干文件。",
                "bullets": [], "files": []}
    answer = (
        "按「被依赖 + PageRank」算出的结构重要性排序，前 "
        f"{len(core)} 个文件是 {_join([n['id'] for n in core], 5)}。"
        f"第 1 名 {core[0]['id']} 被 {core[0]['fan_in']} 个文件依赖，是全仓库的汇聚点。"
    )
    bullets = []
    rows = []
    for i, node in enumerate(core, 1):
        syms = "/".join(s["name"] for s in node.get("symbols", [])[:3]) or "—"
        bullets.append(f"{i}. {node['id']} — 被 {node['fan_in']} 个文件依赖，"
                       f"PageRank {node['pagerank']:.4f}，符号 {syms}")
        rows.append((node["id"], f"被 {node['fan_in']} 个文件依赖 · 结构得分 {node['score']:.3f} "
                                 f"· 关键符号 {syms}"))
    return {"id": "core", "group": "reading", "title": "最该先读的几个文件",
            "desc": "按被依赖关系和 PageRank 排", "answer": answer,
            "bullets": bullets, "files": _files(rows, 5)}


def q_order(ctx: dict) -> dict:
    paths = ctx["paths"]
    if not paths:
        return q_core(ctx) | {"id": "order", "title": "想读懂它，按什么顺序读",
                              "desc": "合并主干路径得到阅读顺序"}
    seen, order = set(), []
    for path in paths:
        for step in path["steps"]:
            if step["path"] not in seen:
                seen.add(step["path"])
                order.append(step)
    answer = (
        (f"把 {len(paths)} 条主干路径去掉重复，得到一条 {len(order)} 步的阅读顺序。"
         if len(paths) > 1 else
         f"这个仓库只抽出 1 条主干路径，共 {len(order)} 步，按它往下读即可。")
        + "不必严格按顺序读，但沿着它读不会在工具函数里绕圈。"
    )
    bullets = [f"{i}. {step['path']}（{step['role']}）" for i, step in enumerate(order, 1)]
    rows = [(step["path"], f"{step['role']} · 被 {step['fan_in']} 个文件依赖 · 出现在第 "
                            f"{', '.join(str(p['index']) for p in step['in_paths'])} 条路径")
            for step in order]
    return {"id": "order", "group": "reading", "title": "想读懂它，按什么顺序读",
            "desc": "主干路径去重后的阅读序列", "answer": answer,
            "bullets": bullets, "files": _files(rows, 10)}


def q_hubs(ctx: dict) -> dict:
    ranked = sorted(ctx["core"], key=lambda n: (-int(n["fan_in"]), n["id"]))[:5]
    if not ranked:
        return {"id": "hubs", "group": "reading", "title": "哪些文件是枢纽",
                "desc": "被最多文件依赖的文件", "answer": "没有识别到主干文件。",
                "bullets": [], "files": []}
    answer = (
        f"被依赖最多的是 {ranked[0]['id']}（{ranked[0]['fan_in']} 个文件）。"
        f"前 {len(ranked)} 个文件加起来被 "
        f"{sum(int(n['fan_in']) for n in ranked)} 条依赖指向——改动它们的影响面最大，"
        "读它们能最快建立整体概念。"
    )
    bullets = [f"{n['id']} — 被 {n['fan_in']} 个文件依赖" for n in ranked]
    rows = [(n["id"], f"被 {n['fan_in']} 个文件依赖 · PageRank {n['pagerank']:.4f}") for n in ranked]
    return {"id": "hubs", "group": "reading", "title": "哪些文件是枢纽",
            "desc": "被依赖最多、改动风险最高", "answer": answer,
            "bullets": bullets, "files": _files(rows, 5)}


def q_deps(ctx: dict) -> dict:
    """一个文件的上游 / 下游；对若干核心文件各算一份。"""
    out = ctx["out"]
    inn: dict[str, list[str]] = defaultdict(list)
    for a, targets in out.items():
        for b in targets:
            inn[b].append(a)

    # 先展示「既依赖别人、又被别人依赖」的文件：这类文件的上下游最能说明结构
    focus = sorted(ctx["core_sorted"][:12],
                   key=lambda n: (-(int(bool(out.get(n["id"]))) + int(bool(inn.get(n["id"])))),
                                  -n["score"], n["id"]))[:8]
    focus = [n["id"] for n in focus]
    if not focus:
        return {"id": "deps", "group": "deps", "title": "某个文件依赖谁、又被谁依赖",
                "desc": "上下游清单", "answer": "没有识别到主干文件。",
                "bullets": [], "files": []}

    variants = []
    for path in focus:
        ups = sorted(inn.get(path, []))
        downs = sorted(out.get(path, ()))
        if not ups and not downs:
            continue
        body = []
        if downs:
            body.append(f"它依赖 {len(downs)} 个文件：{_join(downs, 4)}")
        else:
            body.append("它不依赖仓库内其他文件")
        if ups:
            body.append(f"有 {len(ups)} 个文件依赖它：{_join(ups, 4)}")
        else:
            body.append("没有其他文件依赖它")
        rows = [(p, "它依赖的下游") for p in downs[:4]] + [(p, "依赖它的上游") for p in ups[:4]]
        variants.append({
            "key": path, "label": path,
            "answer": _c(path) + "：" + "；".join(body) + "。",
            "bullets": [], "files": _files(rows, 8),
        })

    if not variants:
        return {"id": "deps", "group": "deps", "title": "某个文件依赖谁、又被谁依赖",
                "desc": "上下游清单", "answer": "这些核心文件之间没有可显示的依赖关系。",
                "bullets": [], "files": []}

    first = variants[0]
    return {"id": "deps", "group": "deps", "title": "某个文件依赖谁、又被谁依赖",
            "desc": "选一个文件看上下游", "answer": first["answer"],
            "bullets": [], "files": first["files"], "variants": variants}


def q_cycles(ctx: dict) -> dict:
    graph = nx.DiGraph()
    graph.add_nodes_from(ctx["core_set"])
    for a, targets in ctx["out"].items():
        for b in targets:
            if b in ctx["core_set"]:
                graph.add_edge(a, b)
    groups = [sorted(c) for c in nx.strongly_connected_components(graph) if len(c) > 1]
    groups.sort(key=lambda c: (-len(c), c[0]))

    if not groups:
        edges = sum(len(v) for v in ctx["out"].values())
        return {"id": "cycles", "group": "deps", "title": "有没有循环依赖",
                "desc": "检测文件级环",
                "answer": "文件级依赖图里没有循环依赖（没有任何一组文件互相强连通）。"
                          "这意味着可以按 import 的拓扑顺序从头读到尾，不会出现 A 依赖 B、B 又依赖 A 的死结。",
                "bullets": [f"检查范围：{len(ctx['core_set'])} 个主干文件、{edges} 条依赖边（不含测试边）"],
                "files": []}

    total = sum(len(g) for g in groups)
    answer = (f"发现 {len(groups)} 组循环依赖，涉及 {total} 个文件。"
              "循环依赖会让 import 顺序变敏感、也让单文件测试变难，是重构的常见起点。")
    bullets, rows = [], []
    for group in groups[:3]:
        sub = graph.subgraph(group)
        try:
            cycle = [a for a, _ in nx.find_cycle(sub)]
            chain = " → ".join(cycle + [cycle[0]])
        except Exception:
            chain = " → ".join(group)
        bullets.append(f"{len(group)} 个文件互指：{chain}")
        for path in group[:4]:
            rows.append((path, f"属于 {len(group)} 个文件的循环依赖组"))
    more = len(groups) - len(bullets)
    if more > 0:
        bullets.append(f"另有 {more} 组更小的环（略）")
    return {"id": "cycles", "group": "deps", "title": "有没有循环依赖",
            "desc": "文件级环检测", "answer": answer, "bullets": bullets,
            "files": _files(rows, 8)}


def q_tests(ctx: dict) -> dict:
    by_module, by_test = ctx["by_module_tests"], ctx["by_test"]
    tests = [n for n in ctx["nodes"] if n["is_test"]]
    core_set = ctx["core_set"]
    if not tests:
        return {"id": "tests", "group": "quality", "title": "测试在哪、覆盖了什么",
                "desc": "测试文件与被测模块", "answer": "这个仓库里没有识别到测试文件。",
                "bullets": [], "files": []}

    ranked_tests = sorted(tests, key=lambda n: (-len(by_test.get(n["id"], ())), n["id"]))
    covered_core = [p for p in core_set if by_module.get(p)]
    uncovered = [n for n in ctx["core_sorted"] if not by_module.get(n["id"])]

    answer = (f"共 {len(tests)} 个测试文件。按 <code>tests</code> 边（测试 import 了哪个模块）统计，"
              f"有 {len(covered_core)} 个主干模块被测试直接引用，"
              f"占主干代码的 {_pct(len(covered_core), len(core_set))}。")
    bullets = [f"{n['id']} — 关联 {len(by_test.get(n['id'], ()))} 个被测模块"
               for n in ranked_tests[:3]]
    if uncovered:
        bullets.append("没有被任何测试直接引用的主干文件（前 3）：" + "、".join(
            n["id"] for n in uncovered[:3]))
    rows = [(n["id"], f"关联 {len(by_test.get(n['id'], ()))} 个被测模块")
            for n in ranked_tests[:5]]
    return {"id": "tests", "group": "quality", "title": "测试在哪、覆盖了什么",
            "desc": "测试文件与被测模块的对应", "answer": answer,
            "bullets": bullets, "files": _files(rows, 5)}


def q_vendor(ctx: dict) -> dict:
    vendor = [n for n in ctx["nodes"] if n["is_vendor"]]
    total = len(ctx["nodes"])
    if not vendor:
        return {"id": "vendor", "group": "quality", "title": "哪些代码不是原创的",
                "desc": "内嵌第三方识别", "answer": "没有检测到内嵌第三方代码："
                "仓库里没有出现典型的上游包目录（如 vendor/、_click/ 之类）。",
                "bullets": [], "files": []}
    dirs = Counter(n["id"].rsplit("/", 1)[0] if "/" in n["id"] else "." for n in vendor)
    top_dirs = "、".join(f"{_c(str(d) + '/')}（{c} 个）" for d, c in dirs.most_common(3))
    answer = (f"有 {len(vendor)} 个文件属于内嵌第三方（占全部 {_pct(len(vendor), total)}），"
              f"主要集中在 {top_dirs}。这些代码不是本仓库写的，读源码时应该跳过，"
              "分析里也已经按内嵌第三方降权。")
    bullets = [f"{d}/ — {c} 个文件" for d, c in dirs.most_common(4)]
    rows = [(n["id"], "内嵌第三方，已降权") for n in
            sorted(vendor, key=lambda n: n["id"])[:6]]
    return {"id": "vendor", "group": "quality", "title": "哪些代码不是原创的",
            "desc": "内嵌第三方清单", "answer": answer, "bullets": bullets,
            "files": _files(rows, 6)}


def q_task(ctx: dict) -> dict:
    answer = (
        "输入一句话描述要改的事，会按「稠密向量 + BM25 + AST 符号 + 结构传播」四路检索融合，"
        "返回 Top-5 文件以及每个文件被选中的依据；示例仓库另外内置了评测用的真实 issue，"
        "可以点开对比「推荐」和「已合并 PR 的真实改动」。"
    )
    return {"id": "task", "group": "task", "title": "说一句要改的事，帮我找文件",
            "desc": "四路检索 → Top-5 文件", "answer": answer,
            "bullets": [], "files": [], "kind": "task"}


# ==========================================================================
# 入口
# ==========================================================================
def build(graph: dict, nodes: list[dict], edges: list[dict], paths: list[dict],
          stats: dict) -> dict:
    """把结构事实整理成固定问题集（随 payload 一起返回给前端）。"""
    by_id = {n["id"]: n for n in nodes}
    core = [n for n in nodes if not (n["is_test"] or n["is_vendor"] or n["is_aux"])]
    core.sort(key=lambda n: (-n["score"], n["id"]))
    core_sorted = core
    core_set = {n["id"] for n in core}
    out, _types = _dep_graph(graph, core_set)
    by_module_tests, by_test = _test_coverage(graph)

    ctx = {
        "nodes": nodes, "nodes_by_id": by_id, "edges": edges, "stats": stats,
        "paths": paths, "core": core, "core_sorted": core_sorted, "core_set": core_set,
        "out": out, "by_module_tests": by_module_tests, "by_test": by_test,
    }

    builders = [q_overview, q_entry, q_layers, q_core, q_order, q_hubs,
                q_deps, q_cycles, q_tests, q_vendor, q_task]
    questions = []
    for builder in builders:
        try:
            item = builder(ctx)
        except Exception as exc:                      # 单个问题失败不能拖垮整页
            item = {"id": builder.__name__, "group": "overview",
                    "title": builder.__name__, "desc": "",
                    "answer": f"这个问题在当前仓库上没能算出来（{type(exc).__name__}）。",
                    "bullets": [], "files": []}
        if item.get("answer"):
            questions.append(item)

    return {"groups": [{"key": k, "title": t} for k, t in GROUPS],
            "questions": questions,
            "note": "所有回答都由 AST 结构事实计算，可在结构图里逐条核对。"}
