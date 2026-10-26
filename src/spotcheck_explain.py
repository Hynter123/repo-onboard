#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 节点说明质量抽检（D5）
====================================

计划里 D5 有一条「抽检 20 条说明准不准」。这个脚本把它变成可复现的检查：
用本地 7B 模型对 20 个节点生成三行说明，然后自动核三项：

    1. 标识符校验是否通过（输出里出现的名字必须能在事实卡片里找到）
    2. 第 3 行是否引用了具体数字（防止"多个文件""一些"这类模糊表述）
    3. 是否出现模糊词（多个 / 一些 / 若干 / 可能）

这只能拦机器能拦的那部分；事实是否真的正确仍需人读一遍。

用法：
    python src/spotcheck_explain.py
    python src/spotcheck_explain.py --repos Delgan/loguru theskumar/python-dotenv --per-repo 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import explain as EX  # noqa: E402
import rank as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"

VAGUE = ("多个", "一些", "若干", "某些", "可能")
NUM = re.compile(r"\d+")


def check_text(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    third = lines[2] if len(lines) >= 3 else ""
    return {
        "lines": len(lines),
        "has_number": bool(NUM.search(third)),
        "vague": [w for w in VAGUE if w in text],
        "third": third[:80],
    }


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", nargs="+",
                    default=["tox-dev/pipdeptree", "theskumar/python-dotenv",
                             "jazzband/pip-tools", "Delgan/loguru"])
    ap.add_argument("--per-repo", type=int, default=5)
    ap.add_argument("--out", default="out/spotcheck_explain.json")
    args = ap.parse_args()

    ok, msg = EX.ollama_ready()
    if not ok:
        print(f"!! Ollama 未就绪：{msg}")
        return

    tasks_file = ROOT / "data" / "tasks.json"
    tasks = json.loads(tasks_file.read_text(encoding="utf-8")) if tasks_file.exists() else []

    rows = []
    for repo in args.repos:
        try:
            graph = R.load_graph(repo)
        except FileNotFoundError:
            print(f"跳过 {repo}（缺结构图）")
            continue

        rel_tasks = [t for t in tasks if t["repo"] == repo][:2]
        query = (rel_tasks[0]["issue_title"] + " " + (rel_tasks[0]["issue_body"] or "")[:400]
                 if rel_tasks else "理解这个仓库的主要结构")

        mods = R.modules(graph)
        comps = R.score_components(graph, repo, query)
        syms: dict[str, list[dict]] = {}
        for n in graph["nodes"]:
            if n["type"] in ("function", "method", "class"):
                syms.setdefault(R.rel_of(n["id"]), []).append(n)

        fan_in, depends, tested, dependents = EX.build_file_relations(graph)
        fused = R.fuse(comps)
        ranked = sorted(fused, key=lambda nid: -fused[nid])[: args.per_repo]

        print(f"\n[{repo}]  任务：{query[:60]}")
        for nid in ranked:
            card = EX.fact_card(graph, nid, mods, syms, fan_in, depends, tested, dependents)
            prompt = EX.TEMPLATE.format(
                path=card["path"], fan_in=card["fan_in"],
                depends_on=", ".join(card["depends_on"]) or "（无）",
                depended_by=", ".join(card["depended_by"]) or "（无）",
                symbols=", ".join(f"{s['name']}（被调用{s['in_degree']}次）"
                                  for s in card["symbols"]) or "（无）",
                is_test=card["is_test"], is_vendor=card["is_vendor"],
                tested=card["tested"], score=card["score"], task=query)
            try:
                text = EX.gen(prompt, EX.SYSTEM)
            except Exception as e:
                text = f"（生成失败：{type(e).__name__}）"
            c = EX.verify(text, card)
            q = check_text(text)
            row = {"repo": repo, "path": card["path"], "verified": c["verified"],
                   "unknown": c["unknown_identifiers"], "text": text, **q}
            rows.append(row)
            flag = "✓" if c["verified"] else "✗"
            print(f"  {flag} {card['path']:<40} 数字={q['has_number']} "
                  f"模糊={','.join(q['vague']) or '-'}")

    if not rows:
        print("没有样本")
        return

    n = len(rows)
    ok_cnt = sum(1 for r in rows if r["verified"])
    num_cnt = sum(1 for r in rows if r["has_number"])
    vague_cnt = sum(1 for r in rows if r["vague"])
    three = sum(1 for r in rows if r["lines"] >= 3)

    print("\n" + "=" * 62)
    print(f"抽检汇总（{n} 条）")
    print("=" * 62)
    print(f"  标识符校验通过      {ok_cnt}/{n}  ({ok_cnt / n:.1%})")
    print(f"  第 3 行含具体数字   {num_cnt}/{n}  ({num_cnt / n:.1%})")
    print(f"  出现模糊词          {vague_cnt}/{n}")
    print(f"  输出至少三行        {three}/{n}")
    bad = [r for r in rows if not r["verified"]]
    if bad:
        print("\n  未通过校验的样本（需人工看一眼）：")
        for r in bad[:5]:
            print(f"    {r['repo']} :: {r['path']}  可疑标识符={r['unknown']}")

    OUT.mkdir(exist_ok=True)
    (ROOT / args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    print(f"\n明细 → {args.out}")


if __name__ == "__main__":
    main()
