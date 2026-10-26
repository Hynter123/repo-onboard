#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 节点说明生成（本地开源小模型）
==========================================

设计原则（这条要写进报告，是"AI 用得克制"的证据）：
    **事实全部由 AST 静态分析给出，模型只负责把事实组织成人话。**

    给模型的输入是一个"事实卡片"，包含：文件路径、被几个文件依赖、被哪些文件依赖、
    包含的关键符号与其被调用次数、是否有测试覆盖、是否内嵌第三方代码。
    提示词明确要求"只使用给定事实，不得编造未出现的函数名或行为"。
    出稿后再做一次校验：若生成文本里出现了事实卡片中不存在的标识符，标记为待复核。

模型：qwen2.5-coder:7b-instruct-q4_K_M（Ollama 本地推理，8GB 显存可跑，全程离线）

用法：
    python src/explain.py Delgan/loguru "异步环境下日志丢失"
    python src/explain.py --check          # 只检查 Ollama 与模型是否就绪
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rank as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"

ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


# ==========================================================================
def ollama_ready(model: str = MODEL) -> tuple[bool, str]:
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=5)
        if r.status_code != 200:
            return False, f"Ollama 返回 {r.status_code}"
        tags = [m["name"] for m in r.json().get("models", [])]
        if not any(t.startswith(model.split(":")[0]) for t in tags):
            return False, f"已装模型：{tags or '（无）'}；需要 {model}"
        return True, "ok"
    except Exception as e:
        return False, f"连不上 Ollama（{type(e).__name__}）：先运行 `ollama serve`"


def gen(prompt: str, system: str = "", temperature: float = 0.2,
        timeout: int = 120) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": 260},
    }
    if system:
        payload["system"] = system
    r = requests.post(f"{OLLAMA}/api/generate", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json().get("response", "").strip()


# ==========================================================================
def build_file_relations(graph: dict):
    """文件级 fan-in（**去重后的文件数**）/ 依赖表 / 测试覆盖集合。

    注意：fan-in 必须按「依赖它的不同文件数」统计，而不是边数——
    按边数统计会把"被 3 个文件调用 20 次"说成"被 20 个文件依赖"。
    """
    mods = R.modules(graph)
    path_of = {nid: n["path"] for nid, n in mods.items()}
    dependents: dict[str, set[str]] = {}     # 文件 → 依赖它的文件集合
    depends: dict[str, set[str]] = {}        # 文件 → 它依赖的文件集合
    tested: set[str] = set()

    for e in graph["edges"]:
        a = path_of.get("mod:" + R.rel_of(e["source"]))
        b = path_of.get("mod:" + R.rel_of(e["target"]))
        if not a or not b or a == b:
            continue
        if e["type"] == "tests":
            tested.add(b)
            continue
        if e["type"] in ("imports", "calls", "inherits"):
            dependents.setdefault(b, set()).add(a)
            depends.setdefault(a, set()).add(b)

    fan_in = {k: len(v) for k, v in dependents.items()}
    return fan_in, depends, tested, dependents


def fact_card(graph: dict, mod_nid: str, mods: dict, syms: dict,
              fan_in: dict, depends: dict, tested: set,
              dependents: dict) -> dict:
    """从结构图里抽出一张"事实卡片"，全部是静态分析得到的硬事实。"""
    m = mods[mod_nid]
    rel = m["path"]
    ss = sorted(syms.get(rel, []),
                key=lambda x: -x.get("metrics", {}).get("in_degree", 0))[:8]
    metrics = m.get("metrics", {})
    return {
        "path": rel,
        "module_name": m.get("module_name", ""),
        "fan_in": fan_in.get(rel, 0),
        "depends_on": sorted(depends.get(rel, set()))[:6],
        "depended_by": sorted(dependents.get(rel, set()))[:5],
        "symbols": [{"name": s["label"], "kind": s["type"],
                     "in_degree": s.get("metrics", {}).get("in_degree", 0),
                     "doc": (s.get("doc") or "")[:120]} for s in ss],
        "is_test": metrics.get("is_test", False),
        "is_vendor": metrics.get("is_vendor", False),
        "score": metrics.get("score", 0.0),
        "tested": rel in tested,          # ← 真实计算，不让模型猜
    }


SYSTEM = (
    "你是一个代码库导览助手。你只能使用用户给出的『事实』作答，"
    "不得编造任何未在事实中出现的函数名、类名、文件路径或行为。"
    "如果事实不足以说明某一点，就直接写『事实未覆盖』。"
    "输出严格三行，每行不超过 40 个汉字，不要编号以外的任何修饰。"
)

TEMPLATE = """事实：
- 文件：{path}
- 被 {fan_in} 个不同文件依赖
- 它依赖：{depends_on}
- 依赖它的文件：{depended_by}
- 关键符号：{symbols}
- 是否测试文件：{is_test}；是否内嵌第三方：{is_vendor}
- 是否有测试覆盖它：{tested}
- 结构重要性得分（0~1，按 PageRank+扇入归一）：{score}

用户的任务：{task}

请输出三行，每行只陈述事实里写明的内容：
1. 这个文件负责什么（只能根据上面的符号名与路径推断）
2. 谁在用它（必须引用"依赖它的文件"里的具体文件名）
3. 改动它的风险（必须引用"被 N 个不同文件依赖"中的数字 N，以及"是否有测试覆盖它"的取值，
   不得使用"多个""一些"这类模糊说法）"""


def verify(text: str, card: dict) -> dict:
    """校验：生成文本里出现的标识符是否都能在事实卡片里找到。"""
    allowed = {s["name"].lower() for s in card["symbols"]}
    for p in [card["path"]] + card.get("depends_on", []) + card.get("depended_by", []):
        allowed |= set(re.split(r"[_./]", p.lower()))     # 路径拆成片段
        allowed.add(p.lower())
        allowed.add(Path(p).stem.lower())
    allowed |= {"事实", "未覆盖"}
    unknown = []
    for tok in ID_RE.findall(text):
        low = tok.lower()
        if low in allowed or len(tok) < 4:
            continue
        if any(low in a or a in low for a in allowed if len(a) > 2):
            continue
        unknown.append(tok)
    return {"unknown_identifiers": sorted(set(unknown))[:6],
            "verified": not unknown}


def explain_repo(repo: str, task: str, top_k: int = 5) -> dict:
    graph = R.load_graph(repo)
    mods = R.modules(graph)
    comps = R.score_components(graph, repo, task)

    syms: dict[str, list[dict]] = {}
    for n in graph["nodes"]:
        if n["type"] in ("function", "method", "class"):
            syms.setdefault(R.rel_of(n["id"]), []).append(n)

    fan_in, depends, tested, dependents = build_file_relations(graph)
    fused = R.fuse(comps)                       # 只算一次，避免排序时反复重算
    ranked = sorted(fused, key=lambda nid: -fused[nid])[:top_k]

    out = []
    for nid in ranked:
        card = fact_card(graph, nid, mods, syms, fan_in, depends, tested, dependents)
        prompt = TEMPLATE.format(
            path=card["path"], fan_in=card["fan_in"],
            depends_on=", ".join(card["depends_on"]) or "（无）",
            depended_by=", ".join(card["depended_by"]) or "（无）",
            symbols=", ".join(f"{s['name']}（被调用{s['in_degree']}次）"
                              for s in card["symbols"]) or "（无）",
            is_test=card["is_test"], is_vendor=card["is_vendor"],
            tested=card["tested"], score=card["score"], task=task)
        try:
            text = gen(prompt, SYSTEM)
        except Exception as e:
            text = f"（生成失败：{type(e).__name__}）"
        out.append({"nid": nid, "path": card["path"], "facts": card,
                    "text": text, "check": verify(text, card)})

    return {"repo": repo, "task": task,
            "results": out}


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?")
    ap.add_argument("task", nargs="?", default="理解这个仓库的主要结构")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    ok, msg = ollama_ready()
    if args.check or not args.repo:
        print(f"Ollama 就绪：{ok}  ({msg})")
        return
    if not ok:
        print(f"!! {msg}")
        return

    res = explain_repo(args.repo, args.task, args.top_k)
    OUT.mkdir(exist_ok=True)
    slug = args.repo.replace("/", "__")
    p = OUT / f"explain_{slug}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== {args.repo} ===\n任务：{args.task}\n")
    for r in res["results"]:
        f = r["facts"]
        print(f"● {r['path']}   (被 {f['fan_in']} 文件依赖, 得分 {f['score']})")
        for line in r["text"].splitlines():
            if line.strip():
                print(f"    {line.strip()}")
        c = r["check"]
        print(f"    [校验] {'通过' if c['verified'] else '待复核: ' + ', '.join(c['unknown_identifiers'])}")
        print()
    print(f"→ {p}")


if __name__ == "__main__":
    main()
