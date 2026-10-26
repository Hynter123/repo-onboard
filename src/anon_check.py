#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 匿名与合规自检（D7）
==================================

规则要求：作品的**技术报告、演示视频、答辩 PPT 及其他评审材料**中
不得出现参赛团队所在学校名称、学校 LOGO 与指导教师信息。

这个脚本做机器能查的那一半（文字与代码里的机构痕迹）；
视频画面、截图里的校徽/校服/背景这类只能人眼看，脚本会在最后提醒。

用法：
    python src/anon_check.py
    python src/anon_check.py --extra "南京大学,东南大学"     # 补充你自己的学校名
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 扫描范围：项目里会进入评审材料的文本类文件
SCAN_INCLUDE = {".py", ".md", ".html", ".js", ".css", ".json", ".txt", ".yaml", ".yml"}
SCAN_SKIP_DIRS = {"data/repos", "out", ".venv", "web/data", "web/vendor",
                  "__pycache__", ".git", "node_modules"}

# 规则：机构类字样
SCHOOL_PAT = re.compile(
    r"(大学|学院|University|College|校徽|校名|指导教师|指导老师|学号|班级|辅导员)")
EMAIL_PAT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 常见高校域名（常见的会命中；自己的校名可用 --extra 补）
EDU_PAT = re.compile(r"\.edu(\.cn)?\b", re.I)

# 本脚本自身：里面必然含匹配规则的字样
SKIP_FILES = {"src/anon_check.py"}

# 白名单：正文里确实需要出现的词（规则引述、自检说明、合规声明）
WHITELIST_LINES = (
    "不得出现", "匿名", "校徽", "学校名称", "指导教师信息", "含学校名",
    "不涉及学校", "既有成果", "规则要求", "自检", "SCHOOL_PAT", "EDU_PAT",
)


def iter_files():
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SCAN_INCLUDE:
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        if any(rel.startswith(d) or f"/{d}" in rel for d in SCAN_SKIP_DIRS):
            continue
        yield p, rel


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", default="", help="补充要查的机构名，逗号分隔")
    args = ap.parse_args()
    extras = [w.strip() for w in args.extra.split(",") if w.strip()]

    hits: list[tuple[str, int, str, str]] = []

    for path, rel in iter_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(w and w in line for w in WHITELIST_LINES):
                continue
            reasons = []
            if SCHOOL_PAT.search(line):
                reasons.append("机构字样")
            # URL 里的 @（GitHub 讨论链接等）不是邮箱，跳过整行含 http 的
            if EMAIL_PAT.search(line) and "http" not in line:
                reasons.append("邮箱")
            if EDU_PAT.search(line) and "http" not in line:
                reasons.append("edu 域名")
            for w in extras:
                if w in line:
                    reasons.append(f"自定义:{w}")
            if reasons:
                hits.append((rel, i, "、".join(sorted(set(reasons))), line.strip()[:110]))

    print("=" * 78)
    print("RepoOnboard 匿名与合规自检")
    print("=" * 78)

    if not hits:
        print("\n✅ 文本类材料中未发现学校名称 / 校徽 / 指导教师 / edu 邮箱等痕迹。")
    else:
        print(f"\n⚠ 发现 {len(hits)} 处需要人工确认的位置：\n")
        cur = None
        for rel, ln, reason, text in hits:
            if rel != cur:
                print(f"[{rel}]")
                cur = rel
            print(f"  L{ln:<4} ({reason})  {text}")

    # 额外检查：git 提交邮箱
    print("\n" + "-" * 78)
    print("git 提交邮箱检查：")
    git_cfg = ROOT / ".git" / "config"
    if not (ROOT / ".git").exists():
        print("  （本项目未初始化 git；若日后 git init，请确认 user.email 不含学校域名）")
    else:
        import subprocess
        out = subprocess.run(["git", "config", "user.email"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
        if not out:
            print("  ⚠ 未设置 user.email")
        elif EDU_PAT.search(out) or "@" in out and any(
                s in out for s in ("edu", "ac.")):
            print(f"  ⚠ user.email = {out} —— 含教育机构痕迹，发布前请改")
        else:
            print(f"  ✅ user.email = {out}")

    print("\n" + "-" * 78)
    print("脚本查不到、必须你人眼确认的部分：")
    print("  □ 演示视频画面里有没有校徽、校服、校名横幅、教室背景")
    print("  □ web/index.html 顶栏与页脚是否出现机构名（当前只有作品名）")
    print("  □ 报告/截图里有没有带学校信息的浏览器书签栏、桌面文件名")
    print("  □ 答辩 PPT（晋级后提交）同样适用本规则")
    print()


if __name__ == "__main__":
    main()
