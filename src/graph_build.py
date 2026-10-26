#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · D2/D3 结构图构建器
================================

只用 Python 标准库 `ast` 解析，零第三方依赖（networkx 可选，用于中心性）。

产出二棵树 + 四类边：

    树：contains（真父子：module → class → function/method）
        dir      （目录树，兄弟关系在这里）
    边：imports  （文件 → 文件）
        calls    （函数 → 函数，跨文件尽力解析）
        inherits （类 → 基类）
        tests    （测试文件 → 被测模块）

设计原则（写进报告的那句话）：
    **骨架由 AST 确定性生成，不经过模型 —— 所以它永远不会"编"。**

用法：
    python src/graph_build.py data/repos/tox-dev__pipdeptree -o out/graph_pipdeptree.json
    python src/graph_build.py data/repos/xxx --no-centrality
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict, deque
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", ".eggs", "node_modules",
             "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "site-packages",
             # 文档与示例目录：typer 的 docs_src 有 304 个示例脚本，
             # 不排除会把结构图彻底变成噪音（核心包其实只有 32 个文件）
             "docs", "docs_src", "doc", "examples", "samples", ".github"}


# ==========================================================================
# 1. 源码树扫描：找到真正的"包根"
# ==========================================================================
def find_source_root(root: Path) -> Path:
    """src-layout 还是 flat-layout？返回 import 路径的起点目录。"""
    for cand in (root / "src", root / "lib", root):
        if not cand.is_dir():
            continue
        # 该目录下是否存在带 __init__.py 的子包
        for child in cand.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                return cand
        # 或者自身就是包（根目录有 __init__.py）
        if (cand / "__init__.py").exists():
            return cand.parent
    return root


def iter_py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name == "setup.py" or p.name == "conftest.py":
            continue
        yield p


def module_name_of(path: Path, source_root: Path, root: Path) -> str:
    """计算 import 路径名。不在 source_root 下的文件（如 docs/conf.py）退回仓库根。"""
    try:
        rel = path.relative_to(source_root).with_suffix("")
    except ValueError:
        try:
            rel = path.relative_to(root).with_suffix("")
        except ValueError:
            return ""
        # 仓库根下的散落脚本不参与 import 解析
        if rel.name == "conf":
            return ""
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


# ==========================================================================
# 2. 符号抽取（含作用域栈 → 得到真父子关系）
# ==========================================================================
class SymbolCollector(ast.NodeVisitor):
    def __init__(self, rel_path: str):
        self.rel = rel_path
        self.scope: list[str] = []          # 例如 ["MyClass"]
        self.symbols: list[dict] = []
        self.imports: list[dict] = []       # {"module": str, "names": [..], "level": int}
        self.calls: list[dict] = []         # {"from": qualname, "to": name_hint, "lineno": int}
        self.bases: list[dict] = []         # {"cls": qualname, "base": name}

    # ---- 作用域 ----
    def _qual(self, name: str) -> str:
        return ".".join(self.scope + [name])

    def _add(self, name: str, kind: str, node):
        self.symbols.append({
            "id": f"sym:{self.rel}::{self._qual(name)}",
            "type": kind,
            "label": name,
            "qualname": self._qual(name),
            "path": self.rel,
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", None),
            "parent": (f"sym:{self.rel}::{'.'.join(self.scope)}" if self.scope
                       else f"mod:{self.rel}"),
            "doc": (ast.get_docstring(node) or "").strip().split("\n")[0][:200],
            "loc": (getattr(node, "end_lineno", node.lineno) - node.lineno + 1),
        })

    def visit_ClassDef(self, node: ast.ClassDef):
        self._add(node.name, "class", node)
        for b in node.bases:
            if isinstance(b, ast.Name):
                self.bases.append({"cls": self._qual(node.name), "base": b.id})
            elif isinstance(b, ast.Attribute):
                self.bases.append({"cls": self._qual(node.name), "base": b.attr})
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        kind = "method" if self.scope else "function"
        # 顶层嵌套函数也按 function 处理
        self._add(node.name, kind, node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import):
        for a in node.names:
            self.imports.append({"module": a.name, "names": [], "level": 0})
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        self.imports.append({
            "module": node.module or "",
            "names": [a.name for a in node.names],
            "level": node.level or 0,
        })
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        f = node.func
        cur = ".".join(self.scope) if self.scope else "<module>"
        if isinstance(f, ast.Name):
            self.calls.append({"from": cur, "to": f.id, "lineno": node.lineno})
        elif isinstance(f, ast.Attribute):
            self.calls.append({"from": cur, "to": f.attr, "lineno": node.lineno})
        self.generic_visit(node)


# ==========================================================================
# 3. 构建图
# ==========================================================================
def pagerank(nodes: list[str], edges: list[tuple[str, str]],
             d: float = 0.85, iters: int = 50, tol: float = 1e-8) -> dict[str, float]:
    """自实现 PageRank（幂迭代），不依赖 networkx/scipy。

    实现要点：阻尼系数 0.85；悬挂节点（出度为 0）的权重均分给全图；
    迭代到 L1 变化 < tol 或达上限。返回值已按最大值归一到 (0, 1]。
    """
    idx = {n: i for i, n in enumerate(nodes)}
    N = len(nodes)
    if N == 0:
        return {}
    outdeg = [0] * N
    incoming: list[list[int]] = [[] for _ in range(N)]
    for u, v in edges:
        iu, iv = idx.get(u), idx.get(v)
        if iu is None or iv is None or iu == iv:
            continue
        outdeg[iu] += 1
        incoming[iv].append(iu)

    pr = [1.0 / N] * N
    for _ in range(iters):
        dangling = sum(pr[i] for i in range(N) if outdeg[i] == 0)
        base = (1.0 - d) / N + d * dangling / N
        new = [base] * N
        for v in range(N):
            s = 0.0
            for u in incoming[v]:
                s += pr[u] / outdeg[u]
            new[v] += d * s
        delta = sum(abs(new[i] - pr[i]) for i in range(N))
        pr = new
        if delta < tol:
            break

    mx = max(pr) or 1.0
    return {n: pr[idx[n]] / mx for n in nodes}


def build(root: Path, with_centrality: bool = True) -> dict:
    root = root.resolve()
    source_root = find_source_root(root)
    py_files = list(iter_py_files(root))

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    files_meta: dict[str, dict] = {}
    collectors: dict[str, SymbolCollector] = {}
    skipped: list[dict] = []

    # 3.1 文件节点 + 目录树
    dirs_seen: set[str] = set()

    def add_dir_tree(rel: Path):
        parts = rel.parts[:-1]
        parent = "dir:."
        for i, part in enumerate(parts):
            cur = "dir:" + "/".join(parts[: i + 1])
            if cur not in dirs_seen:
                dirs_seen.add(cur)
                nodes[cur] = {"id": cur, "type": "dir",
                              "label": part, "path": "/".join(parts[: i + 1]),
                              "parent": parent}
                edges.append({"source": parent, "target": cur, "type": "contains"})
            parent = cur

    for p in py_files:
        rel = p.relative_to(root).as_posix()
        rel_path = Path(rel)
        add_dir_tree(rel_path)

        mod_id = f"mod:{rel}"
        parent_dir = "dir:" + ("/".join(rel_path.parts[:-1]) or ".")
        nodes[mod_id] = {
            "id": mod_id, "type": "module", "label": rel_path.stem or rel,
            "path": rel, "parent": parent_dir,
            "module_name": module_name_of(p, source_root, root),
            "is_test": rel.startswith("tests/") or "/tests/" in rel or rel_path.name.startswith("test_"),
        }
        if parent_dir in nodes:
            edges.append({"source": parent_dir, "target": mod_id, "type": "contains"})

        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"), filename=str(p))
        except SyntaxError as e:
            skipped.append({"path": rel, "error": f"SyntaxError: {e.msg} (line {e.lineno})"})
            continue

        col = SymbolCollector(rel)
        col.visit(tree)
        collectors[rel] = col
        files_meta[rel] = {"module_name": nodes[mod_id]["module_name"], "is_test": nodes[mod_id]["is_test"]}

        for s in col.symbols:
            nodes[s["id"]] = s
            if s["parent"] in nodes:
                edges.append({"source": s["parent"], "target": s["id"], "type": "contains"})
        if col.symbols:
            edges.append({"source": mod_id, "target": f"sym:{rel}::{col.symbols[0]['qualname']}",
                          "type": "contains"}) if False else None  # 已由 parent 覆盖

    # 3.2 模块名 → 文件 索引（用于解析 import）
    mod_index: dict[str, str] = {}
    for rel, meta in files_meta.items():
        mn = meta["module_name"]
        if mn:
            mod_index[mn] = rel
    # 仓库内的顶层包名，用于区分「内部 import」和「stdlib/第三方」
    pkg_roots = {mn.split(".")[0] for mn in mod_index if mn}

    def resolve_module(name: str, from_rel: str, level: int) -> str | None:
        """把 import 的模块名解析成仓库内的文件。返回 rel path 或 None。"""
        if level and level > 0:
            base = files_meta[from_rel]["module_name"].split(".")
            if not files_meta[from_rel]["is_test"]:
                base = base[:-1]
            base = base[: len(base) - (level - 1)] if level > 1 else base
            name = ".".join(base + ([name] if name else []))
        if name in mod_index:
            return mod_index[name]
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in mod_index:
                return mod_index[cand]
        return None

    # 三类计数：内部 import 成功 / 外部（stdlib+第三方，不计为失败）/ 内部未解析
    imp_internal = imp_external = imp_unresolved = 0

    for rel, col in collectors.items():
        for imp in col.imports:
            if not imp["level"]:
                root_pkg = (imp["module"] or "").split(".")[0]
                if root_pkg and root_pkg not in pkg_roots:
                    imp_external += 1          # 外部依赖，正常，不算失败
                    continue
            target = resolve_module(imp["module"], rel, imp["level"])
            if target and target != rel:
                edges.append({"source": f"mod:{rel}", "target": f"mod:{target}",
                              "type": "imports"})
                imp_internal += 1
            elif target == rel:
                pass
            else:
                imp_unresolved += 1

    # 3.3 调用边：函数名 → 候选符号（同文件优先，其次全仓唯一同名）
    name_to_syms: dict[str, list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        if n["type"] in ("function", "method", "class"):
            name_to_syms[n["label"]].append(nid)

    call_total = call_matched = call_known = 0
    local_of: dict[str, dict[str, str]] = defaultdict(dict)
    for rel, col in collectors.items():
        for s in col.symbols:
            local_of[rel][s["label"]] = s["id"]

    for rel, col in collectors.items():
        for c in col.calls:
            call_total += 1
            src = (f"sym:{rel}::{c['from']}" if c["from"] != "<module>"
                   else f"mod:{rel}")
            if src not in nodes:
                continue
            cand = local_of[rel].get(c["to"])
            if cand or name_to_syms.get(c["to"]):
                call_known += 1          # 被调名在本仓库符号表里出现过
            if not cand:
                pool = name_to_syms.get(c["to"], [])
                if len(pool) == 1:
                    cand = pool[0]
                elif pool:
                    same_dir = [x for x in pool
                                if x.split("::")[0].split(":", 1)[1].rsplit("/", 1)[0]
                                == rel.rsplit("/", 1)[0]]
                    cand = same_dir[0] if same_dir else pool[0]
            if cand and cand != src:
                edges.append({"source": src, "target": cand, "type": "calls"})
                call_matched += 1

    # 3.4 继承边
    inherit_ok = 0
    for rel, col in collectors.items():
        for b in col.bases:
            cand = local_of[rel].get(b["base"])
            if not cand:
                pool = [x for x in name_to_syms.get(b["base"], []) if nodes[x]["type"] == "class"]
                cand = pool[0] if pool else None
            if cand:
                edges.append({"source": f"sym:{rel}::{b['cls']}", "target": cand,
                              "type": "inherits"})
                inherit_ok += 1

    # 3.5 测试边：tests/ 下的文件 → 它 import 的模块
    test_edges = 0
    for rel, meta in files_meta.items():
        if not meta["is_test"]:
            continue
        for e in list(edges):
            if e["type"] == "imports" and e["source"] == f"mod:{rel}":
                edges.append({"source": f"mod:{rel}", "target": e["target"], "type": "tests"})
                test_edges += 1

    # 3.6 去重
    dedup, seen = [], set()
    for e in edges:
        key = (e["source"], e["target"], e["type"])
        if key in seen or e["source"] == e["target"]:
            continue
        seen.add(key)
        dedup.append(e)

    # 3.7 中心性：自实现 PageRank，只用 import/calls/inherits 三类有向边
    stats = {
        "files": len(files_meta),
        "symbols": sum(1 for n in nodes.values() if n["type"] in ("function", "method", "class")),
        "dirs": sum(1 for n in nodes.values() if n["type"] == "dir"),
        "edges": len(dedup),
        "imports_internal": imp_internal,
        "imports_external": imp_external,
        "imports_unresolved": imp_unresolved,
        "calls_total": call_total,
        "calls_known": call_known,
        "calls_matched": call_matched,
        "inherit_edges": inherit_ok,
        "test_edges": test_edges,
        "parse_skipped": len(skipped),
        "source_root": str(source_root.relative_to(root)) or ".",
    }
    if with_centrality:
        # ---- 关键设计：中心性算在「文件级图」上，而不是符号级 ----
        # 原因：符号级图极其稀疏且碎片化（一个文件内部自成一个小组件），
        # PageRank 在孤立小分量上会被归一化虚高——辅助脚本 tools/xxx.py
        # 里两三个函数互调就能冲到第一名，而真正的核心模块反而被压下去。
        # 文件级图（模块之间靠 import/跨文件调用相连）连通性好得多，排序才可信。
        import math

        def _rel_of(nid: str) -> str:
            if nid.startswith("sym:"):
                return nid[4:].split("::")[0]
            if nid.startswith(("mod:", "dir:")):
                return nid.split(":", 1)[1]
            return ""

        def _is_test(nid: str) -> bool:
            return files_meta.get(_rel_of(nid), {}).get("is_test", False)

        # 辅助目录：参与建图，但不参与"该读什么"的主排序
        AUX_PREFIX = ("tools/", "scripts/", "examples/", "docs/", "doc/",
                      "benchmarks/", "ci/", ".github/")

        def _is_aux(rel: str) -> bool:
            return rel.lower().startswith(AUX_PREFIX)

        # vendor：第三方代码被复制进仓库。两种形态都要认：
        #   ① invoke/vendor/yaml/...          —— 明确的 vendor 目录
        #   ② typer/_click/utils.py           —— 顶层下划线命名的 fork（内嵌整个 click）
        # 它们结构上确实很"中心"，但对理解本项目毫无帮助，必须降权。
        VENDOR_HINTS = {"click", "yaml", "urllib3", "requests", "six", "attr",
                        "attrs", "toml", "certifi", "colorama", "idna",
                        "chardet", "pytz", "dateutil", "setuptools", "vendor",
                        "vendored", "thirdparty", "third_party"}

        def _is_vendor(rel: str) -> bool:
            low = rel.lower()
            parts = low.split("/")
            if len(parts) < 3:
                return False
            # 关键：先跳过"仓库自己的包根"，再看中间层。
            #   src/requests/api.py   → 从第 3 段开始 → 不命中（否则整个仓库被误判成 vendor）
            #   typer/_click/utils.py → 从第 2 段开始 → 命中 _click
            #   invoke/vendor/yaml/x  → 从第 2 段开始 → 命中 vendor
            start = 2 if (parts[0] in ("src", "lib") and len(parts) > 2) else 1
            for seg in parts[start:-1]:
                if seg.lstrip("_") in VENDOR_HINTS:
                    return True
            return False

        mod_ids = [n["id"] for n in nodes.values() if n["type"] == "module"]
        mod_edges = []
        for e in dedup:
            if e["type"] not in ("imports", "calls", "inherits"):
                continue
            su, tv = _rel_of(e["source"]), _rel_of(e["target"])
            if not su or not tv or su == tv:
                continue
            if _is_test(e["source"]) or _is_test(e["target"]):
                continue
            mod_edges.append(("mod:" + su, "mod:" + tv))
        mod_edges = list(dict.fromkeys(mod_edges))

        mpr = pagerank(mod_ids, mod_edges)
        fan_in: dict[str, int] = defaultdict(int)
        for _u, v in mod_edges:
            fan_in[v] += 1
        max_fin = max(fan_in.values()) if fan_in else 1
        log_max = math.log1p(max_fin) or 1.0

        for nid in mod_ids:
            rel = nid.split(":", 1)[1]
            p = mpr.get(nid, 0.0)
            f = math.log1p(fan_in.get(nid, 0)) / log_max if max_fin > 1 else 0.0
            base = 0.55 * p + 0.45 * f
            vendor = _is_vendor(rel)
            nodes[nid]["metrics"] = {
                "pagerank": round(p, 4),
                "fan_in": fan_in.get(nid, 0),
                # 55% 全局结构权重 + 45% 扇入（fan-in 对分量大小不敏感，抗虚高）
                "score": round(base * (0.35 if vendor else 1.0), 4),
                "base_score": round(base, 4),
                "is_test": files_meta.get(rel, {}).get("is_test", False),
                "is_aux": _is_aux(rel),
                "is_vendor": vendor,
            }
        stats["vendor_files"] = sum(
            1 for nid in mod_ids if nodes[nid]["metrics"]["is_vendor"])
        stats["test_files"] = sum(
            1 for nid in mod_ids if nodes[nid]["metrics"]["is_test"])

        # 符号级只保留 in_degree（不做 PageRank，避免同样的碎片化问题）
        sym_in: dict[str, int] = defaultdict(int)
        for e in dedup:
            if e["type"] == "calls":
                sym_in[e["target"]] += 1
        for nid, n in nodes.items():
            if n["type"] in ("function", "method", "class"):
                rel = _rel_of(nid)
                n["metrics"] = {
                    "in_degree": sym_in.get(nid, 0),
                    "file_score": nodes.get("mod:" + rel, {}
                                            ).get("metrics", {}).get("score", 0.0),
                }
        stats["centrality"] = "文件级 PageRank(自实现) + 扇入(logn 归一)"
    else:
        for n in nodes.values():
            n["metrics"] = {}
        stats["centrality"] = "skipped"

    return {
        "root": str(root),
        "name": root.name,
        "stats": stats,
        "skipped": skipped,
        "nodes": list(nodes.values()),
        "edges": dedup,
    }


# ==========================================================================
def main():
    ap = argparse.ArgumentParser(description="RepoOnboard 结构图构建器")
    ap.add_argument("repo", help="本地仓库路径")
    ap.add_argument("-o", "--out", default=None, help="输出 json 路径")
    ap.add_argument("--no-centrality", action="store_true")
    args = ap.parse_args()

    root = Path(args.repo)
    if not root.is_dir():
        sys.exit(f"目录不存在：{root}")

    g = build(root, with_centrality=not args.no_centrality)
    out = Path(args.out) if args.out else Path("out") / f"graph_{root.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")

    s = g["stats"]
    print(f"\n=== {g['name']} ===")
    print(f"  源根: {s['source_root']}")
    print(f"  文件 {s['files']}  符号 {s['symbols']}  目录 {s['dirs']}  边 {s['edges']}")
    print(f"  import：内部成功 {s['imports_internal']}  外部(忽略) {s['imports_external']}"
          f"  内部未解析 {s['imports_unresolved']}")
    known = s["calls_known"] or 1
    print(f"  调用：总计 {s['calls_total']}  符号表内 {s['calls_known']}"
          f"  已连边 {s['calls_matched']}  (符号表内命中率 {s['calls_matched']/known:.0%})")
    print(f"  继承边 {s['inherit_edges']}  测试边 {s['test_edges']}  跳过文件 {s['parse_skipped']}")
    print(f"  中心性: {s.get('centrality')}")
    print(f"  → {out}")

    # 打印文件级 Top-10：这才是"入场导航"真正要排序的东西
    mods = [n for n in g["nodes"] if n["type"] == "module"
            and n.get("metrics", {}).get("score", 0) > 0]
    mods.sort(key=lambda n: n["metrics"]["score"], reverse=True)
    if mods:
        print("\n  文件级 Top-10（核心模块应排在前面，辅助脚本靠后）:")
        for n in mods[:10]:
            m = n["metrics"]
            tag = "  [测试]" if m.get("is_test") else ("  [辅助]" if m.get("is_aux") else "")
            print(f"    score {m['score']:.3f}  fan-in {m['fan_in']:>2}  "
                  f"{n['path']}{tag}")


if __name__ == "__main__":
    main()
