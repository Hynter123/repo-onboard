#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 预生成示例仓库数据
================================

把 `data/repos/` 下已下载的 6 个示例仓库转成前端可直接 fetch 的 JSON：

    web/data/index.json      仓库清单 + 指标摘要 + 节点说明
    web/data/<slug>.json     每个仓库的完整 payload（含真实任务）

注：现在输出 **JSON** 而不是 JS。页面由 `src/server.py` 通过 http 提供，
`fetch` 可用；之前的 `.js` 包装只是让 `file://` 能用的临时做法。

用法：
    python src/build_web.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
import graph_build as GB  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
WEB = ROOT / "web"
DATA_DIR = WEB / "data"


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default=str(ROOT / "repos_final.txt"))
    args = ap.parse_args()

    repos = [ln.split("#", 1)[0].strip()
             for ln in Path(args.repos).read_text(encoding="utf-8-sig").splitlines()]
    repos = [r for r in repos if "/" in r]

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    tasks_file = ROOT / "data" / "tasks.json"
    all_tasks = json.loads(tasks_file.read_text(encoding="utf-8")) if tasks_file.exists() else []

    repo_meta = []
    for repo in repos:
        slug = repo.replace("/", "__")
        root = ROOT / "data" / "repos" / slug
        if not root.is_dir():
            print(f"  跳过 {repo}（缺源码目录）")
            continue

        graph = GB.build(root)
        data = analyze.payload_from_graph(graph, repo, slug)
        label_of = data.pop("label_of", {})

        mine = [t for t in all_tasks if t["repo"] == repo]
        tasks = []
        for t in mine:
            query = (t["issue_title"] + " " + (t["issue_body"] or "")[:800]).strip()
            entry = analyze.rank_task(graph, slug, query, label_of)
            entry["id"] = t["id"]
            entry["issue_title"] = t["issue_title"]
            entry["gt"] = t["gt_files"]
            entry["synthetic"] = False
            tasks.append(entry)
        data["tasks"] = tasks

        (DATA_DIR / f"{slug}.json").write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        repo_meta.append({
            "slug": slug, "name": repo,
            "nodes": len(data["nodes"]), "edges": len(data["edges"]),
            "total_files": data["total_files"], "view_nodes": len(data["view"]),
            "paths": len(data["paths"]), "tasks": len(tasks),
        })
        size_kb = (DATA_DIR / (slug + ".json")).stat().st_size / 1024
        print(f"  {repo:<28} 文件 {len(data['nodes']):>4}  边 {len(data['edges']):>5}  "
              f"路径 {len(data['paths'])}  任务 {len(tasks):>2}  → {slug}.json ({size_kb:.0f} KB)")

    metrics = {}
    mf = OUT / "metrics.json"
    if mf.exists():
        m = json.loads(mf.read_text(encoding="utf-8"))
        metrics = {"baselines": m.get("baselines", {}), "ours": m.get("ours", {}),
                   "cv": m.get("cv"), "best_preset": m.get("best_preset")}

    explains: dict[str, str] = {}
    for f in OUT.glob("explain_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("results", []):
            explains[f"{d['repo']}::{r['path']}"] = r.get("text", "")

    index = {"repos": repo_meta, "metrics": metrics, "explains": explains}
    (DATA_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\n  → data/index.json ({(DATA_DIR / 'index.json').stat().st_size / 1024:.0f} KB)")
    print("完成。启动：python src/server.py  然后打开 http://127.0.0.1:8765")


if __name__ == "__main__":
    main()
