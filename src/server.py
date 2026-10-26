#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · 本地解析服务
==========================

为什么必须有它：浏览器不能自己下载仓库、也不能跑 Python 的 AST 分析。
要"输入任意仓库地址 → 解析"，就必须有一个本地服务。

提供两个能力：
    1. 静态托管 web/（页面与离线依赖库）
    2. POST /api/analyze {url, task} → 下载仓库（zip）→ AST 建图 → 返回 payload

设计约束（都是踩过的）：
    · github.com:443 不可达，只有 codeload.github.com 可达 → 用 codeload 的 zip
    · gitee.com 直连可用 → 同时支持国内仓库
    · 只读解析：绝不执行目标仓库的任何代码
    · 有上限：zip ≤ 30MB、展开 ≤ 512MB / 50,000 项、py 文件 ≤ 4000、总行数 ≤ 40 万
    · zip 解压前校验路径和规模；HTTP 请求体与任务描述也有长度上限

用法：
    python src/server.py              # 默认 127.0.0.1:8765，自动开浏览器
    python src/server.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlparse, unquote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
EXTERNAL = ROOT / "data" / "repos_external"
CACHE = ROOT / "out" / "web_cache"

LIMIT_ZIP_MB = 30
LIMIT_PY_FILES = 4000
LIMIT_PY_LINES = 400_000
LIMIT_SECONDS = 90
LIMIT_EXTRACTED_BYTES = 512 * 1024 * 1024
LIMIT_ARCHIVE_ENTRIES = 50_000
MAX_REQUEST_BYTES = 256 * 1024
MAX_TASK_CHARS = 2_000

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".woff2": "font/woff2", ".map": "application/json"}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "RepoOnboard/1.0"})
SESSION.trust_env = False          # 绕开系统代理，直连
ANALYZE_LOCK = threading.Lock()    # 避免并发下载/建图争抢资源并覆盖缓存


class UnsafeArchiveError(ValueError):
    """压缩包路径或展开规模不安全，不能通过备用通道绕过。"""


class DownloadLimitError(ValueError):
    """下载内容超过服务端资源上限。"""


def _is_within(root: Path, path: Path) -> bool:
    """按路径组件判断包含关系，避免字符串前缀把 sibling 误当成子目录。"""
    root_s = os.path.normcase(str(root.resolve()))
    path_s = os.path.normcase(str(path.resolve()))
    try:
        return os.path.commonpath((root_s, path_s)) == root_s
    except ValueError:
        return False

_URL_RE = re.compile(
    r"^(?:https?://)?(?P<host>github\.com|gitee\.com)[/:]"
    r"(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)"
    r"(?:/(?:tree|blob)/(?P<branch>[^/\s]+).*)?$", re.I)
_SHORT_RE = re.compile(r"^(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)$")


def parse_repo(text: str) -> tuple[str, str, str | None]:
    """解析用户输入 → (host, 'owner/repo', branch)。失败抛 ValueError。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("请先填入仓库地址")
    s = s.split("#")[0].split("?")[0].rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]

    m = _URL_RE.match(s)
    if m:
        host = m.group("host").lower()
        repo = f"{m.group('owner')}/{m.group('repo')}"
        repo = re.sub(r"\.git$", "", repo)
        return host, repo, m.group("branch")
    m = _SHORT_RE.match(s)
    if m:
        return "github.com", f"{m.group('owner')}/{m.group('repo')}", None
    raise ValueError("看不懂这个地址。支持：https://github.com/owner/repo、"
                     "https://gitee.com/owner/repo，或直接 owner/repo")


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """解压前校验路径和展开规模，并逐块写入以限制磁盘占用。"""
    dest = dest.resolve()
    infos = zf.infolist()
    if len(infos) > LIMIT_ARCHIVE_ENTRIES:
        raise UnsafeArchiveError(f"压缩包文件数超过 {LIMIT_ARCHIVE_ENTRIES:,}，已中止")
    declared_size = sum(info.file_size for info in infos)
    if declared_size > LIMIT_EXTRACTED_BYTES:
        limit_mb = LIMIT_EXTRACTED_BYTES // (1024 * 1024)
        raise DownloadLimitError(f"压缩包展开后超过 {limit_mb}MB，暂不解析这么大的项目")

    targets: set[str] = set()
    extracted = 0
    for info in infos:
        name = info.filename
        posix_name = name.replace("\\", "/")
        posix_path = PurePosixPath(posix_name)
        windows_path = PureWindowsPath(name)
        if (not name or "\x00" in name or posix_path.is_absolute()
                or windows_path.is_absolute() or windows_path.drive
                or any(part == ".." for part in posix_path.parts)):
            raise UnsafeArchiveError("压缩包内含非法路径，已中止")

        target = (dest.joinpath(*posix_path.parts)).resolve()
        if not _is_within(dest, target):
            raise UnsafeArchiveError("压缩包内含非法路径，已中止")
        if target == dest and not info.is_dir():
            raise UnsafeArchiveError("压缩包内含非法路径，已中止")

        target_key = os.path.normcase(str(target))
        if target_key in targets:
            raise UnsafeArchiveError("压缩包内含重复路径，已中止")
        targets.add(target_key)

        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as source, target.open("wb") as output:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                extracted += len(chunk)
                if extracted > LIMIT_EXTRACTED_BYTES:
                    limit_mb = LIMIT_EXTRACTED_BYTES // (1024 * 1024)
                    raise DownloadLimitError(
                        f"压缩包展开后超过 {limit_mb}MB，暂不解析这么大的项目")
                output.write(chunk)


def _default_branch(host: str, repo: str) -> str | None:
    """向平台 API 询问默认分支，避免瞎猜 main/master。"""
    try:
        if host == "gitee.com":
            r = SESSION.get(f"https://gitee.com/api/v5/repos/{repo}", timeout=20)
            if r.status_code == 200:
                return (r.json() or {}).get("default_branch")
        else:
            r = SESSION.get(f"https://api.github.com/repos/{repo}", timeout=20)
            if r.status_code == 200:
                return (r.json() or {}).get("default_branch")
    except requests.RequestException:
        pass
    return None


def clone_with_git(host: str, repo: str, branch: str | None, dest: Path) -> str:
    """git clone 兜底通道。

    为什么需要两条路：本机 github.com 的 git 端口不通但 codeload 可达；
    gitee.com 反过来——匿名 zip 下载返回 405，而 git clone 正常。
    两条通道互补，谁通走谁。
    """
    import subprocess

    url = f"https://{host}/{repo}.git"
    tmp = dest.parent / (dest.name + "__git")
    shutil.rmtree(tmp, ignore_errors=True)
    cmd = ["git", "clone", "--depth", "1", "--quiet"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [url, str(tmp)]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="echo")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("git clone 超时（180 秒）")
    if p.returncode != 0 or not tmp.is_dir():
        msg = (p.stderr or p.stdout or "").strip().splitlines()
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("git clone 失败：" + (msg[-1][:140] if msg else "未知原因"))

    branch_name = branch or "default"
    head = tmp / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            branch_name = ref.rsplit("/", 1)[-1]
    except OSError:
        pass

    dest.mkdir(parents=True, exist_ok=True)
    for item in list(tmp.iterdir()):
        if item.name == ".git":
            continue
        shutil.move(str(item), str(dest / item.name))
    shutil.rmtree(tmp, ignore_errors=True)
    return branch_name


def download_repo(host: str, repo: str, branch: str | None, dest: Path) -> str:
    """下载并解压源码，返回实际用的分支名。"""
    if dest.exists() and any(dest.iterdir()):
        return branch or "cached"

    candidates: list[str] = []
    if branch:
        candidates.append(branch)
    db = _default_branch(host, repo)
    if db:
        candidates.append(db)
    candidates += ["main", "master"]
    # 去重保序
    seen, branches = set(), []
    for b in candidates:
        if b and b not in seen:
            seen.add(b)
            branches.append(b)

    errors: list[str] = []
    for br in branches:
        if host == "gitee.com":
            url = f"https://gitee.com/{repo}/repository/archive/{br}.zip"
        else:
            url = f"https://codeload.github.com/{repo}/zip/refs/heads/{br}"
        try:
            r = SESSION.get(url, timeout=90, stream=True)
        except requests.RequestException as e:
            errors.append(f"{br}: 网络错误 {type(e).__name__}")
            continue
        if r.status_code != 200:
            errors.append(f"{br}: HTTP {r.status_code}")
            r.close()
            continue

        buf = io.BytesIO()
        total = 0
        try:
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > LIMIT_ZIP_MB * 1024 * 1024:
                    raise DownloadLimitError(
                        f"仓库压缩包超过 {LIMIT_ZIP_MB}MB，暂不解析这么大的项目")
                buf.write(chunk)
        finally:
            r.close()

        tmp = dest.parent / (dest.name + "__tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
                _safe_extract(zf, tmp)
        except (UnsafeArchiveError, DownloadLimitError):
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        except zipfile.BadZipFile:
            shutil.rmtree(tmp, ignore_errors=True)
            errors.append(f"{br}: 下载到的不是有效 zip")
            continue
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise UnsafeArchiveError(f"压缩包解压失败（{type(e).__name__}），已中止") from e

        tops = [p for p in tmp.iterdir() if p.is_dir()]
        dest.mkdir(parents=True, exist_ok=True)
        src = tops[0] if len(tops) == 1 else tmp
        for item in list(src.iterdir()):
            shutil.move(str(item), str(dest / item.name))
        shutil.rmtree(tmp, ignore_errors=True)
        return br

    raise ValueError("下载失败，尝试过的分支：" + "；".join(errors or ["（无）"]))


def scan_python(root: Path) -> tuple[int, int]:
    """返回 (py 文件数, 非空行数)，用于规模上限校验。"""
    skip = {".git", ".venv", "venv", "node_modules", "build", "dist", ".eggs",
            "__pycache__", ".tox", ".mypy_cache", ".pytest_cache"}
    files = lines = 0
    for p in root.rglob("*.py"):
        if any(part in skip for part in p.parts):
            continue
        files += 1
        if files > LIMIT_PY_FILES:
            return files, lines
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                lines += sum(1 for ln in f if ln.strip())
        except OSError:
            continue
    return files, lines


def analyze_url(text: str, task: str | None, refresh: bool = False) -> dict:
    t0 = time.time()
    host, repo, branch = parse_repo(text)
    slug = repo.replace("/", "__")
    branch_key = hashlib.sha256((branch or "default").encode("utf-8")).hexdigest()[:10]
    dest = EXTERNAL / (slug if branch is None else f"{slug}__{branch_key}")
    if refresh and dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    # 两条下载通道：zip 优先（快），失败退回 git clone（Gitee 只能走这条）
    try:
        used_branch = download_repo(host, repo, branch, dest)
    except (UnsafeArchiveError, DownloadLimitError):
        raise
    except ValueError as zip_err:
        try:
            used_branch = clone_with_git(host, repo, branch, dest)
        except ValueError as git_err:
            raise ValueError(f"{zip_err}；备用通道也失败 —— {git_err}")

    n_files, n_lines = scan_python(dest)
    if n_files < 3:
        raise ValueError(f"这个仓库里只有 {n_files} 个 Python 文件，"
                         f"看起来不是 Python 项目（本项目目前只支持 Python）")
    if n_lines > LIMIT_PY_LINES:
        raise ValueError(f"Python 代码量 {n_lines:,} 行，超过上限 {LIMIT_PY_LINES:,} 行，"
                         f"解析会太慢，暂不支持")

    CACHE.mkdir(parents=True, exist_ok=True)
    task_hash = hashlib.sha256((task or "").strip().encode("utf-8")).hexdigest()[:16]
    cache_file = CACHE / f"{slug}__{branch_key}__{task_hash}.json"
    if cache_file.exists() and not refresh:
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["_cached"] = True
                data["_elapsed"] = round(time.time() - t0, 2)
                return data
        except (OSError, json.JSONDecodeError):
            pass

    payload = analyze.payload(dest, repo, slug, task=task)
    payload.pop("_graph", None)
    payload.pop("label_of", None)
    payload["_source"] = {"host": host, "repo": repo, "branch": used_branch,
                          "py_files": n_files, "py_lines": n_lines}
    payload["_elapsed"] = round(time.time() - t0, 2)
    payload["_cached"] = False

    tmp_cache = cache_file.with_suffix(".tmp")
    tmp_cache.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    os.replace(tmp_cache, cache_file)
    return payload


# ==========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "RepoOnboard"

    def log_message(self, fmt, *args):      # 简化日志
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- 静态文件 ----------
    def _send_file(self, path: Path):
        if not path.is_file():
            self.send_error(404, "Not Found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if path.suffix in (".html", ".js", ".json")
                         else "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        rel = unquote(u.path)
        if rel in ("/", ""):
            return self._send_file(WEB / "index.html")
        target = (WEB / rel.lstrip("/")).resolve()
        if not _is_within(WEB, target):
            self.send_error(403, "Forbidden")
            return
        self._send_file(target)

    # ---------- 分析接口 ----------
    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/analyze":
            self.send_error(404)
            return
        try:
            raw_length = self.headers.get("Content-Length")
            n = int(raw_length) if raw_length is not None else 0
            if n <= 0:
                return self._json({"error": "请求体不能为空"}, 400)
            if n > MAX_REQUEST_BYTES:
                return self._json({"error": "请求体过大"}, 413)
            raw_body = self.rfile.read(n)
            if len(raw_body) != n:
                return self._json({"error": "请求体未完整接收"}, 400)
            body = json.loads(raw_body)
        except (ValueError, RecursionError):
            return self._json({"error": "请求体不是合法 JSON"}, 400)

        if not isinstance(body, dict):
            return self._json({"error": "请求内容必须是 JSON 对象"}, 400)
        url = body.get("url", "")
        task_value = body.get("task")
        refresh = bool(body.get("refresh"))
        if not isinstance(url, str) or (task_value is not None and not isinstance(task_value, str)):
            return self._json({"error": "url 和 task 必须是文本"}, 400)
        task = task_value.strip() if task_value is not None else ""
        task = task or None
        if task and len(task) > MAX_TASK_CHARS:
            return self._json({"error": f"任务描述不能超过 {MAX_TASK_CHARS} 个字符"}, 400)
        if not ANALYZE_LOCK.acquire(blocking=False):
            return self._json({"error": "已有仓库正在解析，请稍后再试"}, 429)

        holder: dict = {}

        def work():
            try:
                holder["ok"] = analyze_url(url, task, refresh)
            except Exception as e:
                holder["err"] = str(e) if isinstance(e, ValueError) else \
                    f"{type(e).__name__}: {e}"
            finally:
                ANALYZE_LOCK.release()

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(LIMIT_SECONDS)
        if th.is_alive():
            return self._json({"error": f"等待解析超过 {LIMIT_SECONDS} 秒；后台任务可能仍在运行。"
                                        f"建议换小一点的仓库。"}, 504)
        if "err" in holder:
            return self._json({"error": holder["err"]}, 400)
        return self._json(holder["ok"], 200)

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    EXTERNAL.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"RepoOnboard 服务已启动：{url}")
    print("  页面：输入任意 GitHub / Gitee 仓库地址即可解析")
    print("  停止：Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
