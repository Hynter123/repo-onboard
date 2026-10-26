#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · D1 数据集构建器
=============================

目标：从真实 GitHub 历史里，构造「任务 → 必读文件」的客观评测集。

核心思想（这是本方案指标不靠人工标注的关键）：
    每一条 "issue → 已合并 PR" 对应关系里，
    **PR 实际改动的代码文件** 就是「完成这个任务必须读懂/改动的文件」。
    这是维护者自己写下的 ground truth，不存在标注争议。

网络方案（重要）：
    本机 github.com:443 不可达，但 api.github.com 与 codeload.github.com 可达。
    因此全程**不使用 git**：元数据走 REST API，源码走 codeload 的 zip 包。
    不需要代理，也不需要 git 命令行。

三个子命令：
    check    : 查元数据 + 下 zip + 实测代码行数，判断是否适合入集
    fetch    : 只下载源码 zip（后续 AST 分析要用）
    build    : 抓 issue→PR 配对，产出 data/tasks.json

用法：
    set GITHUB_TOKEN=ghp_xxx
    python src/scrape_tasks.py check --repos repos.txt
    python src/scrape_tasks.py build --repos repos.txt --per-repo 5 --out data/tasks.json

配额：
    未认证 60 次/小时（基本不可用）；认证后 5000 次/小时。
    抓 8 个仓库 x 5 个任务约消耗 150~350 次。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import requests

# Windows 控制台默认 GBK，中文/符号会崩，强制 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# HuggingFace 直连不通，统一走镜像（fastembed 也会用到）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPO_CACHE = DATA / "repos"

ISSUE_REF = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)", re.IGNORECASE
)

META_SUFFIX = {".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini",
               ".json", ".lock", ".png", ".jpg", ".svg", ".in"}
META_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "makefile",
              "requirements.txt", ".gitignore", "license", "changelog.md"}

SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", ".eggs", "node_modules",
             "__pycache__", ".mypy_cache", ".pytest_cache", ".tox",
             # 与 graph_build 保持一致：文档/示例目录不算代码量
             "docs", "docs_src", "doc", "examples", "samples"}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def make_session(token: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "repoonboard-dataset-builder",
    })
    s.trust_env = False          # 直连；本机系统代理会干扰
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def gh(session, url: str, params: dict | None = None, tries: int = 3):
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"    ! 网络异常 {type(e).__name__}，重试", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code == 200:
            remain = int(r.headers.get("X-RateLimit-Remaining", "9999"))
            if remain < 15:
                reset = int(r.headers.get("X-RateLimit-Reset", "0"))
                wait = max(0, reset - int(time.time())) + 2
                print(f"    ! 配额仅剩 {remain}，等待 {wait}s", file=sys.stderr)
                time.sleep(min(wait, 900))
            return r.json()

        if r.status_code in (403, 429):
            reset = int(r.headers.get("X-RateLimit-Reset", "0"))
            wait = max(5, min(reset - int(time.time()) + 2, 180))
            print(f"    ! 限流 {r.status_code}，等 {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        if r.status_code == 404:
            return None

        time.sleep(1.5 * (attempt + 1))
    return None


def paginate(session, url: str, params: dict, max_pages: int = 5):
    out, page = [], 1
    while page <= max_pages:
        data = gh(session, url, dict(params, per_page=100, page=page))
        if not data:
            break
        out.extend(data)
        if len(data) < 100:
            break
        page += 1
    return out


def rate_report(session):
    j = gh(session, f"{API}/rate_limit")
    if not j:
        return
    core = j.get("resources", {}).get("core", {})
    print(f"[配额] 剩余 {core.get('remaining')}/{core.get('limit')}  "
          f"（重置于 {time.strftime('%H:%M', time.localtime(core.get('reset', 0)))}）")


# --------------------------------------------------------------------------
# zip 下载（替代 git clone）
# --------------------------------------------------------------------------
def default_branch(session, repo: str) -> str:
    j = gh(session, f"{API}/repos/{repo}")
    return (j or {}).get("default_branch") or "main"


def download_zip(session, repo: str, branch: str, dest: Path,
                 max_mb: int = 40) -> bool:
    """下载 codeload zip 并解压到 dest（去掉顶层目录）。"""
    if dest.exists() and any(dest.iterdir()):
        return True
    url = f"{CODELOAD}/{repo}/zip/refs/heads/{branch}"
    try:
        r = session.get(url, timeout=120)
        if r.status_code != 200:
            print(f"    ! zip 下载失败 {r.status_code}", file=sys.stderr)
            return False
        if len(r.content) > max_mb * 1024 * 1024:
            print(f"    ! zip 超过 {max_mb}MB，跳过", file=sys.stderr)
            return False
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            top = z.namelist()[0].split("/")[0]
            tmp = dest.parent / (dest.name + "__tmp")
            z.extractall(tmp)
            src = tmp / top
            if src.exists():
                for item in src.iterdir():
                    target = dest / item.name
                    if target.exists():
                        continue
                    item.replace(target)
            _rmtree(tmp)
        return True
    except Exception as e:
        print(f"    ! zip 异常 {type(e).__name__}: {e}", file=sys.stderr)
        return False


def _rmtree(p: Path):
    if not p.exists():
        return
    for c in p.rglob("*"):
        try:
            c.unlink() if c.is_file() else None
        except OSError:
            pass
    try:
        for c in sorted(p.rglob("*"), key=lambda x: -len(x.parts)):
            if c.is_dir():
                c.rmdir()
        p.rmdir()
    except OSError:
        pass


# --------------------------------------------------------------------------
# 文件分类
# --------------------------------------------------------------------------
def is_test_file(path: str) -> bool:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    return ("/test" in p or p.startswith("test") or name.startswith("test_")
            or name.endswith("_test.py") or "tests/" in p)


def is_meta_file(path: str) -> bool:
    p = path.lower()
    suffix = "." + p.rsplit(".", 1)[-1] if "." in p else ""
    return (suffix in META_SUFFIX
            or p.startswith(("docs/", "doc/", ".github/", "changes/"))
            or p.rsplit("/", 1)[-1] in META_NAMES)


def split_files(files: list[str]):
    code, tests, meta = [], [], []
    for f in files:
        if not f.endswith(".py"):
            meta.append(f)
        elif is_test_file(f):
            tests.append(f)
        elif is_meta_file(f):
            meta.append(f)
        else:
            code.append(f)
    return code, tests, meta


def py_loc(path: Path) -> tuple[int, int]:
    total, n = 0, 0
    for p in path.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        n += 1
        total += sum(1 for line in text.splitlines() if line.strip())
    return total, n


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------
def do_check(args):
    session = prepare_session(args)
    rate_report(session)
    DATA.mkdir(parents=True, exist_ok=True)
    REPO_CACHE.mkdir(parents=True, exist_ok=True)

    rows = []
    for repo in read_repos(args.repos):
        j = gh(session, f"{API}/repos/{repo}")
        if not j:
            rows.append((repo, "查询失败", "-", "-", "-", "-", "✗"))
            continue
        branch = j.get("default_branch") or "main"
        size_kb = j.get("size") or 0
        dest = REPO_CACHE / repo.replace("/", "__")
        ok_dl = download_zip(session, repo, branch, dest)
        if not ok_dl:
            rows.append((repo, f"{size_kb/1024:.1f}MB", "下载失败", "-", "-", "-", "✗"))
            continue
        loc, nfiles = py_loc(dest)
        has_tests = (dest / "tests").is_dir() or (dest / "test").is_dir()
        has_ci = (dest / ".github" / "workflows").is_dir()
        ok = args.min_loc <= loc <= args.max_loc
        rows.append((repo, f"{size_kb/1024:.1f}MB", f"{loc:,}", str(nfiles),
                     "✓" if has_tests else "✗",
                     "✓" if has_ci else "✗",
                     "✓" if ok else "✗ 规模越界"))

    print("\n{:<32}{:>9}{:>8}{:>7}{:>7}{:>5}{:>12}".format(
        "仓库", "体积", "代码行", "py文件", "有测试", "CI", "入集"))
    print("-" * 82)
    for r in rows:
        print("{:<32}{:>9}{:>8}{:>7}{:>7}{:>5}{:>12}".format(*r))
    good = sum(1 for r in rows if r[-1].startswith("✓"))
    print(f"\n可入集 {good} / {len(rows)}   标准：{args.min_loc} ≤ 代码行 ≤ {args.max_loc}")


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def do_fetch(args):
    session = prepare_session(args)
    for repo in read_repos(args.repos):
        branch = default_branch(session, repo)
        dest = REPO_CACHE / repo.replace("/", "__")
        ok = download_zip(session, repo, branch, dest)
        loc, n = py_loc(dest) if ok else (0, 0)
        print(f"{'OK ' if ok else 'ERR'} {repo:<32} {loc:>7} 行 / {n:>4} 文件")


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def build_for_repo(session, repo: str, per_repo: int, max_files: int,
                   verbose: bool) -> list[dict]:
    tasks, seen = [], set()
    pulls = paginate(session, f"{API}/repos/{repo}/pulls",
                     {"state": "closed", "sort": "updated", "direction": "desc"},
                     max_pages=4)
    print(f"  {repo}: {len(pulls)} 个已关闭 PR")

    for pr in pulls:
        if len(tasks) >= per_repo:
            break
        if not pr.get("merged_at"):
            continue
        m = ISSUE_REF.search(pr.get("body") or "")
        if not m:
            continue
        issue_no = int(m.group(1))
        if issue_no in seen:
            continue

        issue = gh(session, f"{API}/repos/{repo}/issues/{issue_no}")
        if not issue or "pull_request" in issue:
            continue
        title = (issue.get("title") or "").strip()
        if not title:
            continue

        files = [f["filename"] for f in paginate(
            session, f"{API}/repos/{repo}/pulls/{pr['number']}/files", {}, max_pages=2)]
        if not files or len(files) > max_files:
            continue
        code, tests, meta = split_files(files)
        if not code:
            continue

        seen.add(issue_no)
        tasks.append({
            "id": f"{repo}#{issue_no}",
            "repo": repo,
            "pr_number": pr["number"],
            "issue_number": issue_no,
            "issue_title": title,
            "issue_body": (issue.get("body") or "").strip()[:4000],
            "pr_title": (pr.get("title") or "").strip(),
            "gt_files": code,
            "gt_test_files": tests,
            "gt_meta_files": meta,
            "n_files_total": len(files),
            "merged_at": pr.get("merged_at"),
        })
        if verbose:
            print(f"    ✓ #{issue_no} → PR #{pr['number']}  {title[:46]}  [{len(code)}]")
    return tasks


def do_build(args):
    session = prepare_session(args)
    rate_report(session)
    all_tasks = []
    for repo in read_repos(args.repos):
        print(f"\n[{repo}]")
        try:
            all_tasks.extend(build_for_repo(session, repo, args.per_repo,
                                            args.max_files, args.verbose))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  ! {repo} 失败：{type(e).__name__}: {e}", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(all_tasks, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 66)
    print(f"共 {len(all_tasks)} 条任务 → {out}")
    by: dict[str, int] = {}
    for t in all_tasks:
        by[t["repo"]] = by.get(t["repo"], 0) + 1
    for r, n in by.items():
        print(f"  {r:<32} {n:>3}")
    if len(all_tasks) < 15:
        print("\n⚠ 任务数偏少（目标 20~30）：多加仓库 / 调大 --per-repo / 换 PR 规范的项目")


# --------------------------------------------------------------------------
def prepare_session(args):
    token = (getattr(args, "token", None)
             or os.environ.get("GITHUB_TOKEN")
             or os.environ.get("GH_TOKEN"))
    if not token:
        print("!! 没有 GITHUB_TOKEN，未认证只有 60 次/小时，抓不到东西。", file=sys.stderr)
        print("   Settings → Developer settings → Personal access tokens (classic)"
              " → 勾 public_repo（只读）", file=sys.stderr)
        if not getattr(args, "allow_no_token", False):
            sys.exit(1)
    else:
        print(f"[认证] 使用 token（...{token[-4:]}）")
    return make_session(token)


def read_repos(path: str) -> list[str]:
    repos = []
    # utf-8-sig：吃掉 PowerShell 写文件时带的 BOM，否则仓库名会带 \ufeff
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip().lstrip("\ufeff")
        if line and "/" in line and not line.startswith("//"):
            repos.append(line)
    return repos


def main():
    ap = argparse.ArgumentParser(description="RepoOnboard 数据集构建器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repos", default="repos.txt")
        p.add_argument("--token", default=None)
        p.add_argument("--allow-no-token", action="store_true")

    c = sub.add_parser("check", help="下 zip + 实测规模")
    common(c)
    c.add_argument("--min-loc", type=int, default=500)
    c.add_argument("--max-loc", type=int, default=15000)
    c.set_defaults(func=do_check)

    f = sub.add_parser("fetch", help="只下载源码")
    common(f)
    f.set_defaults(func=do_fetch)

    b = sub.add_parser("build", help="抓 issue→PR 产出 tasks.json")
    common(b)
    b.add_argument("--per-repo", type=int, default=5)
    b.add_argument("--max-files", type=int, default=20)
    b.add_argument("--out", default="data/tasks.json")
    b.add_argument("--verbose", action="store_true")
    b.set_defaults(func=do_build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
