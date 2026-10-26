#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · LLM 零样本基线
=============================

回答一个绕不开的问题：**"既然大模型能读仓库，为什么还需要你这套结构化检索？"**

做法：让本地 `qwen2.5-coder:7b` 直接读「仓库地图 + issue 原文」，让它自己选 Top-5 文件，
再用与主方案**完全相同**的 `eval_one` 算 Recall@5 / Top-1 / MRR / 子图覆盖。

公平性设计（这几条决定了这个对比有没有说服力）：

| 维度 | 处理 |
|---|---|
| 候选池 | 与主方案相同：非测试的 Python 模块 |
| 输入信息 | 每个文件的「路径 + 主要符号 + 开头摘要」，信息量对标我们的四路信号 |
| 任务文本 | 与主方案完全相同的 issue 标题 + 正文（截断规则一致） |
| 采样 | `temperature=0`，固定 `seed`，减少随机性 |
| 评分 | 复用 `evaluate.eval_one`，指标定义与主方案一字不差 |
| 局限 | 只测了本地 7B（无 API 预算）；**前沿模型未测，这是本基线的已知限制** |

输出：
    out/llm_baseline.json    逐条结果 + 原始回答（可审计）
    out/metrics.json         把该基线合并进 baselines（保留其它字段）

用法：
    python src/llm_baseline.py                # 全量 30 条
    python src/llm_baseline.py --limit 5      # 先试 5 条
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate as E  # noqa: E402
import rank as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
OLLAMA = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"
PROMPT_BUDGET = 7000          # 字符预算，超出就压缩摘要

LABEL = "LLM 零样本（本地 7B）"

PROMPT = """你在帮开发者定位代码。下面是一个 Python 仓库的文件清单。

每个文件一行，格式为：路径 || 主要符号 || 文件开头摘要

===== 仓库文件清单 =====
{repo_map}
===== 清单结束 =====

开发者的任务描述：
{issue}

请从上面的清单里选出**最可能需要修改的 5 个文件**，按可能性从高到低排序。

要求：
- 只输出 5 行，每行一个完整路径（从清单里原样复制）
- 不要编号、不要解释、不要输出其它任何内容
- 如果不足 5 个有把握的，就输出你有把握的那些"""


# ==========================================================================
def repo_symbols(idx: E.RepoIndex) -> dict[str, list[str]]:
    """模块 nid → 顶层符号名列表（按被调次数降序）。"""
    from collections import defaultdict
    by_path: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for n in idx.graph["nodes"]:
        if n["type"] not in ("function", "method", "class"):
            continue
        rel = R.rel_of(n["id"])
        pin = n.get("metrics", {}).get("in_degree", 0)
        by_path[rel].append((pin, n["label"]))
    out: dict[str, list[str]] = {}
    for path, items in by_path.items():
        nid = idx.nid_of_path.get(path)
        if nid:
            out[nid] = [name for _, name in sorted(items, key=lambda x: (-x[0], x[1]))]
    return out


def build_repo_map(idx: E.RepoIndex) -> str:
    """把候选文件渲染成"仓库地图"文本（路径 + 符号 + 摘要）。"""
    text_of = dict(zip(idx.ids, idx.texts))
    syms_of = repo_symbols(idx)

    entries = []
    for nid in idx.ids:
        m = idx.mods[nid].get("metrics", {})
        if m.get("is_test"):
            continue                      # 与主方案候选池一致
        path = idx.path_of[nid]
        syms = syms_of.get(nid, [])[:3]
        head = re.sub(r"\s+", " ", (text_of.get(nid) or "")).strip()[:170]
        entries.append((path, ", ".join(syms) or "-", head))

    # 预算不够就整体缩短摘要，避免"只给前几个文件摘要"造成的不公平
    rm = ""
    for cut in (170, 110, 60, 0):
        rm = "\n".join(f"{p} || {s} || {h[:cut] or '-'}" for p, s, h in entries)
        if len(rm) <= PROMPT_BUDGET:
            return rm
    return rm


def ask_ollama(prompt: str, timeout: int = 180) -> str:
    import requests
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "seed": 42, "num_predict": 200, "top_p": 1.0},
    }
    r = requests.post(OLLAMA, json=payload, timeout=timeout)
    r.raise_for_status()
    return (r.json() or {}).get("response", "")


def parse_paths(answer: str, valid: list[str]) -> list[str]:
    """从模型回答里抽出路径。模型常加编号/引号/反引号，按最长匹配容错。"""
    by_norm = {p.replace("\\", "/").strip(): p for p in valid}
    picked, seen = [], set()
    for line in (answer or "").splitlines():
        raw = line.strip().strip("`'\"").lstrip("-*0123456789. 、)").strip()
        raw = raw.strip("`'\"").replace("\\", "/")
        if not raw:
            continue
        hit = None
        if raw in by_norm:
            hit = by_norm[raw]
        else:
            for p in valid:                      # 容忍后缀缺失
                if raw.endswith(p) or p.endswith(raw):
                    hit = p
                    break
        if hit and hit not in seen:
            seen.add(hit)
            picked.append(hit)
        if len(picked) >= 5:
            break
    return picked


def ranking_from(picked: list[str], idx: E.RepoIndex) -> dict:
    """把 LLM 的排序转成 eval_one 需要的 {nid: score}：命中的按序递减，其余 0。"""
    rank: dict[str, float] = {nid: 0.0 for nid in idx.ids}
    for i, path in enumerate(picked):
        nid = idx.nid_of_path.get(path)
        if nid:
            rank[nid] = 1.0 - i * 0.02
    return rank


# ==========================================================================
def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    args = ap.parse_args()

    tasks = json.loads((ROOT / "data" / "tasks.json").read_text(encoding="utf-8"))
    if args.limit:
        tasks = tasks[:args.limit]

    idxs: dict[str, E.RepoIndex] = {}
    maps: dict[str, str] = {}
    for t in tasks:
        repo = t["repo"]
        if repo not in idxs:
            print(f"  索引 {repo} …")
            idxs[repo] = E.RepoIndex(repo)
            maps[repo] = build_repo_map(idxs[repo])
            print(f"       候选 {sum(1 for n in idxs[repo].ids if not idxs[repo].mods[n].get('metrics',{}).get('is_test'))} 个文件，"
                  f"地图 {len(maps[repo])} 字符")

    rows, raw = [], []
    t_all = time.time()
    for i, t in enumerate(tasks, 1):
        idx = idxs[t["repo"]]
        issue = (t["issue_title"] + "\n" + (t["issue_body"] or "")[:800]).strip()
        prompt = PROMPT.format(repo_map=maps[t["repo"]], issue=issue)

        t0 = time.time()
        try:
            answer = ask_ollama(prompt)
        except Exception as e:
            print(f"  [{i:>2}/{len(tasks)}] {t['id']}  ⚠ 调用失败 {type(e).__name__}：{e}")
            answer = ""
        dt = time.time() - t0

        valid = [idx.path_of[n] for n in idx.ids]
        picked = parse_paths(answer, valid)
        r = E.eval_one(idx, ranking_from(picked, idx), t["gt_files"], k=5)
        rows.append(r)
        raw.append({"id": t["id"], "repo": t["repo"], "answer": answer,
                    "picked": picked, "gt": t["gt_files"], "seconds": round(dt, 1),
                    "metrics": {k: round(v, 3) for k, v in r.items()}})
        print(f"  [{i:>2}/{len(tasks)}] {t['id']:<28} R@5={r['recall@5']:.2f} "
              f"Top1={r['top1']:.0f} MRR={r['mrr']:.2f}  选了 {len(picked)} 个  {dt:.0f}s")

    agg = E.aggregate(rows)
    el = time.time() - t_all
    print("\n" + "=" * 66)
    print(f"  {LABEL}：n={len(rows)}")
    print(f"    Recall@5={agg['recall@5']:.3f}  Top-1={agg['top1']:.3f}  "
          f"MRR={agg['mrr']:.3f}  子图覆盖={agg['subgraph']:.3f}")
    print(f"    总耗时 {el / 60:.1f} 分钟（平均 {el / max(len(rows), 1):.1f} 秒/条）")
    print("=" * 66)

    OUT.mkdir(exist_ok=True)
    (OUT / "llm_baseline.json").write_text(json.dumps({
        "label": LABEL, "model": MODEL, "n": len(rows),
        "temperature": 0, "seed": 42, "prompt_budget": PROMPT_BUDGET,
        "aggregate": {k: round(v, 4) for k, v in agg.items()},
        "seconds_total": round(el, 1),
        "rows": raw,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  明细 → out/llm_baseline.json")

    # 合并进 metrics.json（不覆盖其它字段）
    mf = OUT / "metrics.json"
    if mf.exists() and not args.limit:
        m = json.loads(mf.read_text(encoding="utf-8"))
        m.setdefault("baselines", {})[LABEL] = {k: round(v, 4) for k, v in agg.items()}
        m.setdefault("llm_baseline_meta", {})["model"] = MODEL
        m["llm_baseline_meta"]["n"] = len(rows)
        m["llm_baseline_meta"]["seconds_total"] = round(el, 1)
        mf.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已合并进 out/metrics.json 的 baselines[\"{LABEL}\"]")


if __name__ == "__main__":
    main()
