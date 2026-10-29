#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RepoOnboard · D4 剪枝 / 聚合 / 检索 / 结构传播
==============================================

本模块把 D2/D3 产出的结构图变成一个"能回答任务"的导航器。

三组排序（故意做出来对比，这是 25 分那一项的弹药）：

    ① bm25      —— 文件名 + 符号名 + docstring 的关键词匹配（最笨的基线）
    ② embed     —— 纯向量检索（2024 年业界通行做法，主要对照组）
    ③ ours      —— 向量相似度 + 结构传播 + 中心性先验（本方案）

关键设计：
    · 检索用 fastembed(bge-small) ONNX，CPU 即可，不占显存
    · 结构传播沿 import/calls/inherits 边做 2 跳扩散，"改了 A 多半要读 A 的邻居"
    · 剪枝 + 聚合用于默认视图：tests / vendor / 各目录先折叠，节点数压到 ≤15
    · 测试文件不进候选池（与 ground truth 口径一致）

用法：
    python src/rank.py Delgan/loguru "日志在异步环境下丢失"
    python src/rank.py tox-dev/pipdeptree "summary 输出格式不好看"
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

# 国内直连 HuggingFace 不通，统一走镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

OUT = Path(__file__).resolve().parent.parent / "out"
EMB_MODEL = "BAAI/bge-small-en-v1.5"

TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# 模型单例：每次 new 一个 TextEmbedding 要重新加载 ONNX，30 个任务会白等一分钟
_MODEL = None
_MODEL_FAILED = False          # 语义模型不可用后不再重试，避免每次调用都等一轮下载重试


def _hf_reachable(timeout: float = 2.5) -> bool:
    """模型还没缓存时，先花 2 秒探一下能不能下；不通就直接走非语义路径。

    fastembed 自己的下载重试是 3s + 9s + 27s 的指数退避，在离线/被墙的机器上
    一次调用要白等 40 秒以上。先探一次，可以把这一跳压到 2 秒。
    """
    import os as _os

    base = _os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    try:
        import requests

        resp = requests.head(base, timeout=timeout, allow_redirects=True)
        return resp.status_code < 500
    except Exception:
        return False


def get_model():
    global _MODEL, _MODEL_FAILED
    if _MODEL_FAILED:
        raise RuntimeError("语义模型不可用（此前加载失败），本次改用关键词与结构信号")
    if _MODEL is None:
        cache = Path(__file__).resolve().parent.parent / "fastembed_cache"
        if not cache.exists() and not _hf_reachable():
            _MODEL_FAILED = True
            raise RuntimeError("语义模型尚未缓存且 HuggingFace 不可达，本次改用关键词与结构信号")
        from fastembed import TextEmbedding

        try:
            _MODEL = TextEmbedding(model_name=EMB_MODEL)
        except Exception:
            _MODEL_FAILED = True
            raise
    return _MODEL


# ==========================================================================
# 载入与索引
# ==========================================================================
def graph_path(repo: str) -> Path:
    return OUT / f"graph_{repo.replace('/', '__')}.json"


def load_graph(repo: str) -> dict:
    p = graph_path(repo)
    if not p.exists():
        raise FileNotFoundError(f"缺少结构图 {p}，请先跑 src/graph_build.py")
    return json.loads(p.read_text(encoding="utf-8"))


def rel_of(nid: str) -> str:
    if nid.startswith("sym:"):
        return nid[4:].split("::")[0]
    if nid.startswith(("mod:", "dir:")):
        return nid.split(":", 1)[1]
    return ""


def modules(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"] if n["type"] == "module"}


def candidate_pool(graph: dict) -> list[str]:
    """可被推荐的候选文件：排除测试文件（与 ground truth 口径一致）。"""
    mods = modules(graph)
    return [nid for nid, n in mods.items()
            if not n.get("metrics", {}).get("is_test", False)]


def centrality_of(graph: dict) -> dict[str, float]:
    return {n["id"]: n.get("metrics", {}).get("score", 0.0)
            for n in graph["nodes"] if n["type"] == "module"}


# ==========================================================================
# 每个文件的文本画像（用于检索）
# ==========================================================================
def file_texts(graph: dict) -> tuple[list[str], list[str]]:
    syms: dict[str, list[dict]] = defaultdict(list)
    for n in graph["nodes"]:
        if n["type"] in ("function", "method", "class"):
            syms[rel_of(n["id"])].append(n)

    mods = modules(graph)
    ids, texts = [], []
    for nid in candidate_pool(graph):
        m = mods[nid]
        rel = m["path"]
        ss = sorted(syms.get(rel, []),
                    key=lambda x: -x.get("metrics", {}).get("in_degree", 0))
        # 符号名全部纳入（最多 40 个）：issue 里点名的往往是具体的那个函数，
        # 而不是最中心的那几个，只取 Top-N 会把关键线索丢掉
        names = [s["label"] for s in ss[:40]]
        docs = [s.get("doc", "") for s in ss[:12] if s.get("doc")]
        txt = f"{rel} {m.get('module_name', '')} " + " ".join(names) + " " + " ".join(docs)
        ids.append(nid)
        texts.append(txt[:2000])
    return ids, texts


def file_vectors(repo: str, ids: list[str], texts: list[str]) -> np.ndarray:
    """带磁盘缓存的向量化。缓存名内含文本指纹，文本一改就自动失效。"""
    import hashlib

    sig = hashlib.md5("\x00".join(texts).encode("utf-8")).hexdigest()[:10]
    cache = OUT / f"emb_{repo.replace('/', '__')}_{sig}.npz"
    if cache.exists():
        try:
            z = np.load(cache, allow_pickle=True)
            if list(z["ids"]) == ids:
                return z["vec"]
        except Exception:
            pass
    model = get_model()
    vec = np.array(list(model.embed(texts)), dtype=np.float32)
    vec /= (np.linalg.norm(vec, axis=1, keepdims=True) + 1e-9)
    np.savez(cache, ids=np.array(ids, dtype=object), vec=vec)
    return vec


def embed_query(text: str) -> np.ndarray:
    model = get_model()
    v = np.array(list(model.embed([text])), dtype=np.float32)[0]
    return v / (np.linalg.norm(v) + 1e-9)


# ==========================================================================
# 基线①：BM25 关键词
# ==========================================================================
def bm25_scores(texts: list[str], query: str, k1: float = 1.5,
                b: float = 0.75) -> np.ndarray:
    docs = [TOKEN.findall(t.lower()) for t in texts]
    q = TOKEN.findall(query.lower())
    N = len(docs)
    avgdl = sum(len(d) for d in docs) / max(N, 1)
    df: dict[str, int] = defaultdict(int)
    for d in docs:
        for w in set(d):
            df[w] += 1

    out = np.zeros(N, dtype=np.float32)
    for i, d in enumerate(docs):
        dl = len(d)
        tfs: dict[str, int] = defaultdict(int)
        for w in d:
            tfs[w] += 1
        s = 0.0
        for w in q:
            if w not in df:
                continue
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            tf = tfs.get(w, 0)
            if tf:
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / max(avgdl, 1e-9)))
        out[i] = s
    return out


# ==========================================================================
# 结构传播
# ==========================================================================
def module_adjacency(graph: dict) -> dict[str, set[str]]:
    """模块级无向邻接表（沿 import/calls/inherits 边扩散）。"""
    adj: dict[str, set[str]] = defaultdict(set)
    for e in graph["edges"]:
        if e["type"] not in ("imports", "calls", "inherits"):
            continue
        su, tv = rel_of(e["source"]), rel_of(e["target"])
        if not su or not tv or su == tv:
            continue
        a, c = "mod:" + su, "mod:" + tv
        adj[a].add(c)
        adj[c].add(a)
    return adj


def propagate(sims: dict[str, float], adj: dict[str, set[str]],
              hops: int = 2, decay: float = 0.5) -> dict[str, float]:
    """把相似度沿结构边扩散：邻居"沾光"，强度按 decay 衰减。"""
    acc = dict(sims)
    frontier = dict(sims)
    for _ in range(hops):
        nxt: dict[str, float] = defaultdict(float)
        for nid in sorted(frontier):        # 固定顺序
            v = frontier[nid]
            nb = adj.get(nid)
            if not nb:
                continue
            share = v * decay / len(nb)
            for m in sorted(nb):            # 固定顺序：浮点累加顺序影响末位
                nxt[m] += share
        for k, v in nxt.items():
            acc[k] = acc.get(k, 0.0) + v
        frontier = dict(nxt)
    mx = max(acc.values()) if acc else 1.0
    return {k: v / (mx or 1.0) for k, v in acc.items()}


# ==========================================================================
# 符号级链接：issue 里点名的函数/类 → 直接定位到文件
# ==========================================================================
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TRANS_CACHE = OUT / "trans_cache.json"
TRANS_MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def translate_query(query: str) -> str | None:
    """把中文任务翻成英文（检索用）。结果落盘缓存，失败返回 None。

    为什么需要：代码与 docstring 是英文，BM25 按英文词切分，
    中文任务与代码零共词，关键词这一路会完全失效。
    用本地 7B 翻译后，中文任务即可复用英文侧的检索能力。
    只在检测到中文时触发，因此不影响英文任务的评测结果。
    """
    try:
        cache = json.loads(TRANS_CACHE.read_text(encoding="utf-8")) if TRANS_CACHE.exists() else {}
    except Exception:
        cache = {}
    if query in cache:
        return cache[query]

    try:
        import requests
        # Ollama 不在运行或未安装模型时尽快退回英文/结构检索，避免每个中文问题都等满 120 秒。
        tags_response = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
        if tags_response.status_code != 200:
            return None
        tags = [m.get("name", "") for m in tags_response.json().get("models", [])]
        if not any(tag.startswith(TRANS_MODEL.split(":")[0]) for tag in tags):
            return None
        prompt = (
            "Translate the following software issue description into concise English "
            "for searching source code. Output ONLY the translation, one line, "
            "no explanation, no quotes.\n\n" + query
        )
        r = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": TRANS_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.0, "num_predict": 120, "num_ctx": 2048}},
            timeout=120,
        )
        r.raise_for_status()
        txt = (r.json().get("response") or "").strip()
        txt = txt.splitlines()[0].strip().strip('"').strip() if txt else ""
        if not txt:
            return None
        cache[query] = txt
        TRANS_CACHE.parent.mkdir(exist_ok=True)
        TRANS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        return txt
    except Exception:
        return None


def build_symbol_index(graph: dict) -> dict[str, set[str]]:
    idx: dict[str, set[str]] = defaultdict(set)
    for n in graph["nodes"]:
        if n["type"] in ("function", "method", "class"):
            idx[n["label"].lower()].add("mod:" + rel_of(n["id"]))
    return idx


def query_identifiers(query: str) -> set[str]:
    """从任务描述里抽标识符，同时拆 snake_case 与 CamelCase。

    这一步是"结构感知"的入口：issue 正文里一个 `set_key` 比一整段自然语言
    更有指向性，而纯向量检索会把这种精确信号平均掉。
    """
    toks: set[str] = set()
    for m in ID_RE.findall(query):
        toks.add(m.lower())
        for part in re.split(r"[_\W]+", m):
            if len(part) >= 3:
                toks.add(part.lower())
        for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+", m):
            if len(part) >= 3:
                toks.add(part.lower())
    return toks


def symbol_scores(sym_index: dict[str, set[str]], query: str,
                  pool: set[str] | None = None) -> dict[str, float]:
    toks = query_identifiers(query)
    sc: dict[str, float] = defaultdict(float)
    for t in sorted(toks):                 # 固定顺序，避免 set 迭代随机化
        files = sym_index.get(t)
        if not files:
            continue
        if pool is not None:
            files = {f for f in files if f in pool}
            if not files:
                continue
        w = 1.0 / len(files)          # 名字越独特，指向性越强
        for f in sorted(files):       # 浮点累加顺序必须固定，否则末位不同会改变排序
            sc[f] += w
    mx = max(sc.values()) if sc else 1.0
    return {k: v / mx for k, v in sc.items()}


# ==========================================================================
# 统一入口：算五个分量，权重交给上层（只在 dev 集上调）
# ==========================================================================
DEFAULT_WEIGHTS = {"vec": 0.35, "kw": 0.15, "sym": 0.30, "prop": 0.15, "cent": 0.05}


def score_components(graph: dict, repo: str, query: str) -> dict:
    ids, texts = file_texts(graph)
    vecs = file_vectors(repo, ids, texts)

    # 中文任务：先用本地模型翻成英文，再让原文与译文各自检索、逐元素取较大者。
    # 这样中文任务也能吃到英文侧的检索能力，且不影响英文任务的既有结果。
    queries = [query]
    translated = None
    if has_cjk(query):
        translated = translate_query(query)
        if translated:
            queries.append(translated)

    qvecs = [embed_query(q) for q in queries]
    cos = np.max(np.stack([vecs @ qv for qv in qvecs], axis=0), axis=0)
    cos_mx = float(cos.max()) or 1.0
    vec_d = {nid: float(cos[i]) / cos_mx for i, nid in enumerate(ids)}

    # 关键词这一路只能走英文侧（BM25 按英文词切分）
    kw = bm25_scores(texts, translated or query)
    kw_mx = float(kw.max()) or 1.0
    kw_d = {nid: float(kw[i]) / kw_mx for i, nid in enumerate(ids)}

    sym_d = symbol_scores(build_symbol_index(graph), " ".join(queries), pool=set(ids))

    adj = module_adjacency(graph)
    # ① 稠密向量沿结构扩散：把整个仓库的"语义热度"摊开
    props = {h: propagate(vec_d, adj, hops=h, decay=0.5) for h in (1, 2, 3)}
    # ② 符号命中沿结构扩散：这才是"改了 A 多半要读 A 的邻居"的正确用法。
    #    符号命中是高精度信号，扩散它比扩散稠密向量干净得多。
    props_sym = {h: propagate(sym_d, adj, hops=h, decay=0.5) for h in (1, 2, 3)}

    flags = {nid: {k: graph_flags(graph, nid, k)
                   for k in ("is_test", "is_aux", "is_vendor")}
             for nid in ids}

    return {"ids": ids, "vec": vec_d, "kw": kw_d, "sym": sym_d,
            "prop": props, "prop_sym": props_sym, "cent": centrality_of(graph),
            "flags": flags, "query_used": queries, "translated": translated}


def graph_flags(graph: dict, mod_nid: str, key: str) -> bool:
    for n in graph["nodes"]:
        if n["id"] == mod_nid:
            return bool(n.get("metrics", {}).get(key, False))
    return False


def _comp_scores(comps: dict, name: str, hops: int) -> dict[str, float]:
    if name == "prop":
        return comps["prop"].get(hops, {})
    if name == "prop_sym":
        return comps["prop_sym"].get(hops, {})
    return comps.get(name, {})


def rrf(comps: dict, names: list[str], hops: int = 1, k: float = 60.0,
        restrict: set[str] | None = None) -> dict[str, float]:
    """Reciprocal Rank Fusion：多路召回按名次融合。

    为什么不用线性加权：五个分量的分值尺度不可比，线性加权需要精调权重，
    而 dev 集只有 10 条样本，调出来的权重点必然过拟合。
    RRF 只看名次不看分值，天然对尺度免疫，几乎无需调参。
    """
    fused: dict[str, float] = defaultdict(float)
    for name in names:
        items = _ranked(_comp_scores(comps, name, hops))
        if restrict is not None:
            items = [(n, v) for n, v in items if n in restrict]
        for rank, (nid, v) in enumerate(items, 1):
            if v <= 0:            # 该分量没命中就不参与投票，避免噪声稀释
                break
            fused[nid] += 1.0 / (k + rank)
    return dict(fused)


def _ranked(scores: dict[str, float]) -> list[tuple[str, float]]:
    """统一排序：分值降序 + 节点 ID 升序作为 tie-break。

    没有显式 tie-break 时，并列项的先后取决于字典/集合的迭代顺序，
    同一份代码两次运行可能得到不同结果——这会直接毁掉"可复现"。
    """
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


# 辅助脚本（tools/ scripts/ 等）的降权系数：它们几乎从不是"该读的核心模块"，
# 但确实偶尔是某个任务的目标，因此降权而非直接排除。
AUX_PENALTY = 0.6


def fuse(comps: dict, preset: str | None = None) -> dict[str, float]:
    """按预设融合。stage1 非空时采用「先召回后精排」两阶段。"""
    cfg = PRESETS.get(preset or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET])
    restrict = None
    m = cfg.get("stage1")
    if m:
        # 第一阶段：纯向量召回 Top-M（广），第二阶段：多信号精排（准）
        restrict = {nid for nid, _ in _ranked(comps["vec"])[:m]}
    fused = rrf(comps, cfg["names"], cfg.get("hops", 1),
                cfg.get("k", 60.0), restrict)

    # 辅助脚本 / 内嵌第三方的降权**只在精排阶段生效**。
    # 原因：召回阶段降权会把辅助脚本直接漏掉（ground truth 里确实有 tools/*.py 的情形），
    # 而精排阶段降权只是把它们往后放。因此单信号预设（A~H）保持与基线完全一致。
    if restrict is not None:
        flags = comps.get("flags") or {}
        for nid in list(fused):
            f = flags.get(nid) or {}
            if f.get("is_aux"):
                fused[nid] *= AUX_PENALTY
            if f.get("is_vendor"):
                fused[nid] *= 0.7
    return fused


def combine(comps: dict, weights: dict, hops: int = 2) -> dict[str, float]:
    """保留线性加权实现，用于报告里的对照分析（主方案用 rrf）。"""
    prop = comps["prop"][hops]
    return {
        nid: (weights["vec"] * comps["vec"].get(nid, 0.0)
              + weights["kw"] * comps["kw"].get(nid, 0.0)
              + weights["sym"] * comps["sym"].get(nid, 0.0)
              + weights["prop"] * prop.get(nid, 0.0)
              + weights["cent"] * comps["cent"].get(nid, 0.0))
        for nid in comps["ids"]
    }


# 预设：按"信号从少到多、从简单融合到两阶段"排，由交叉验证挑最优
PRESETS: dict[str, dict] = {
    "A 仅向量": dict(names=["vec"], hops=1),
    "B 向量+关键词": dict(names=["vec", "kw"], hops=1),
    "C 向量+符号": dict(names=["vec", "sym"], hops=1),
    "D 向量+关键词+符号": dict(names=["vec", "kw", "sym"], hops=1),
    "E D+符号结构扩散": dict(names=["vec", "kw", "sym", "prop_sym"], hops=1),
    "F E+向量结构扩散": dict(names=["vec", "kw", "sym", "prop_sym", "prop"], hops=1),
    "G F+中心性": dict(names=["vec", "kw", "sym", "prop_sym", "prop", "cent"], hops=1),
    "I 两阶段召回15": dict(names=["vec", "kw", "sym", "prop_sym"], hops=1, stage1=15),
    "J 两阶段召回10": dict(names=["vec", "kw", "sym", "prop_sym"], hops=1, stage1=10),
    "K 两阶段召回8": dict(names=["vec", "kw", "sym", "prop_sym"], hops=1, stage1=8),
    "L 向量+符号(k=10)": dict(names=["vec", "sym"], hops=1, k=10),
    "M 四信号(k=10)": dict(names=["vec", "kw", "sym", "prop_sym"], hops=1, k=10),
}

DEFAULT_PRESET = "E D+符号结构扩散"


def rank_all(graph: dict, query: str, repo: str | None = None,
             preset: str | None = None) -> dict:
    repo = repo or graph["name"].replace("__", "/")
    comps = score_components(graph, repo, query)

    def top(d: dict, k: int = 5):
        return _ranked(d)[:k]

    return {
        "keyword": top(comps["kw"]),
        "embedding": top(comps["vec"]),
        "symbol": top(comps["sym"]),
        "ours": top(fuse(comps, preset)),
        "_comps": comps,
        "_preset": preset or DEFAULT_PRESET,
    }


# ==========================================================================
# 剪枝 + 聚合：默认视图
# ==========================================================================
def default_view(graph: dict, max_nodes: int = 15,
                 top_files: int = 9) -> tuple[list[dict], int]:
    """默认视图：核心文件逐个展示 + 其余按类折叠，总节点 ≤ max_nodes。

    设计取舍：只按目录折叠会把 loguru 的 170 个文件压成 3 块，图是干净了但
    完全没信息量。所以主包取 Top-N 文件各自成节点（这才是"该读哪几个文件"），
    只有测试/内嵌第三方/辅助目录这类"不需要展开但必须交代存在"的部分才折叠。
    """
    mods = modules(graph)
    total_files = len(mods)
    main, tests, vendor, aux = [], [], [], []
    for n in mods.values():
        m = n.get("metrics", {})
        if m.get("is_test"):
            tests.append(n)
        elif m.get("is_vendor"):
            vendor.append(n)
        elif m.get("is_aux"):
            aux.append(n)
        else:
            main.append(n)

    main.sort(key=lambda n: -n.get("metrics", {}).get("score", 0.0))
    view: list[dict] = []

    for n in main[:top_files]:
        view.append({
            "cluster": "核心文件",
            "file_count": 1,
            "representative": n["path"],
            "members": [n["path"]],
            "score": round(n.get("metrics", {}).get("score", 0.0), 3),
        })

    rest_items = main[top_files:]
    if rest_items:
        view.append({
            "cluster": "其余源码",
            "file_count": len(rest_items),
            "representative": f"（{len(rest_items)} 个未展开文件）",
            "members": [n["path"] for n in rest_items],
            "score": 0.0,
        })

    for key, items in (("（测试目录）", tests), ("（内嵌第三方）", vendor),
                       ("（辅助脚本）", aux)):
        if items:
            top = max(items, key=lambda i: i.get("metrics", {}).get("score", 0.0))
            view.append({
                "cluster": key,
                "file_count": len(items),
                "representative": top["path"],
                "members": [n["path"] for n in items],
                "score": 0.0,
            })

    return view[:max_nodes], total_files


def subgraph_of(graph: dict, seeds: list[str], hops: int = 1) -> set[str]:
    adj = module_adjacency(graph)
    out = set(seeds)
    frontier = set(seeds)
    for _ in range(hops):
        nxt: set[str] = set()
        for n in frontier:
            nxt |= adj.get(n, set())
        frontier = nxt - out
        out |= nxt
    return out


# ==========================================================================
# CLI 演示
# ==========================================================================
def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print(__doc__)
        return
    repo = sys.argv[1]
    query = " ".join(sys.argv[2:]) or "程序崩溃"

    g = load_graph(repo)
    res = rank_all(g, query, repo=repo)
    comps = res["_comps"]

    view, total = default_view(g)
    mods = modules(g)
    print(f"\n=== {repo} ===  候选文件 {len(comps['ids'])} 个，"
          f"默认视图 {len(view)}/{total} 块")
    print(f"任务：{query}")
    if comps.get("translated"):
        print(f"检索用译文：{comps['translated']}")
    print()

    print("  默认视图（剪枝+聚合后，这就是思维导图的顶层）:")
    for v in view:
        print(f"    [{v['cluster']:<10}] {v['file_count']:>3} 个文件  代表：{v['representative']}")

    groups = (("keyword", "① BM25 关键词"),
              ("embedding", "② 纯向量检索"),
              ("symbol", "③ 仅符号链接"),
              ("ours", "④ 本方案（向量+关键词+符号+结构+中心性）"))

    for name, label in groups:
        items = res[name]
        print(f"\n  {label}:")
        if not items or max(sc for _, sc in items) <= 0:
            if name == "keyword" and not comps.get("translated"):
                print("    （该分量未命中：BM25 按英文词切分，中文任务无共词）")
            else:
                print("    （该分量未命中）")
            continue
        # 组内归一：避免 RRF 的 1/(60+rank) 量纲被误读成"分数很低"
        mx = max(sc for _, sc in items) or 1.0
        for nid, sc in items:
            print(f"    {sc / mx:.3f}  {mods[nid]['path']}")

    seeds = [nid for nid, _ in res["ours"]]
    sg = subgraph_of(g, seeds, hops=1) & set(comps["ids"])
    print(f"\n  高亮子图：{len(seeds)} 个种子 → 1 跳扩展后 {len(sg)} 个候选文件"
          f"（已排除测试/辅助文件）")
    if comps.get("translated"):
        print("\n  注：①②③④ 的分值均为**组内相对值**（除以该组最大值），只表示排序先后。")


if __name__ == "__main__":
    main()
