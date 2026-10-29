# RepoOnboard · 开源仓库结构地图

> 2026 第八届全球校园人工智能算法精英大赛 · 算法主题赛「AI+开源」· 方向二（AI 开发工具与开源协作）
> 参赛团队：**龙骨（Keel）**　团队编号：**AIC-2026-44614864**

输入一个公开的 Python 仓库地址，RepoOnboard 在本地把它变成一张**可交互的结构地图**：
思维导图看目录、依赖图看谁依赖谁、主干路径给出"按什么顺序读"，
再用一页 11 个问题的**结构速答**回答核心文件、入口、枢纽、循环依赖、测试覆盖等问题。

- 结构完全由 Python `ast` 静态分析生成（**只读解析，不执行目标仓库的任何代码**）
- 语义来自本地小模型（`bge-small` ONNX），可选地用本地 `Qwen2.5-Coder-7B`（Ollama）组织文字
- **全程离线**：目标仓库源码不出本机，模型不上云

## 快速运行（Windows / Python 3.10+）

```powershell
git clone https://github.com/Hynter123/repo-onboard.git
cd repo-onboard
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src\server.py      # 或双击 启动.bat
```

浏览器打开 <http://127.0.0.1:8765>，粘贴任意公开的 GitHub / Gitee Python 仓库地址即可。
解析通常 2–5 秒（视仓库大小与网络），全部四个页签立刻可用。
更详细的分步说明（含常见问题）见 [教师运行说明.md](教师运行说明.md)。

**首次使用「按任务找文件」会下载语义模型**（`bge-small`，约 63 MB，20–30 秒），
之后走本地缓存。如果这台机器连不上 HuggingFace，会**自动降级**为"关键词 + 符号 + 结构"
检索，并在回答里明确提示"当前语义检索不可用"——其余功能不受影响。
国内网络可以先设镜像再启动：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
```

**可选的本地 AI**（只有「按任务找文件」这一组会用到，不装也能用）：

```powershell
ollama pull qwen2.5-coder:7b-instruct-q4_K_M
ollama serve
```

## 这份仓库里有什么 / 没有什么

| 有 | 说明 |
|---|---|
| `src/` | 14 个 Python 脚本：AST 建图、四路检索、RRF 融合、速答、本地服务、评测 |
| `web/` | 单页前端（Vue + G6 + markmap），第三方库全部离线打包在 `web/vendor/` |
| `data/` | 只有**来源链接索引**（`benchmark_sources.json`）与**汇总指标**（`metrics_summary.json`） |
| `教师运行说明.md` | 给评审的分步运行说明 |

| 没有（原因） | |
|---|---|
| `data/repos/`、`data/tasks.json` | 被分析仓库的源码与上游 issue 正文，再分发权未确认，不随仓库分发 |
| `fastembed_cache/`、模型权重 | 由使用者按其自身许可证下载 |
| 技术报告 / 演示视频 | 通过大赛报名系统提交，不在本仓库内 |

因此 **`python src/evaluate.py` 的评测复现需要自备那 6 个仓库与任务标签**；
只跑交互界面（`src/server.py`）不需要任何本地数据集。

## 复现指标（需自备数据集）

```powershell
.\.venv\Scripts\python.exe src\evaluate.py      # 4 指标 / 12 预设消融 / 5 折 CV
.\.venv\Scripts\python.exe src\llm_baseline.py  # LLM 零样本基线（需 Ollama）
```

评测口径：ground truth = **已合并 PR 实际改动的文件**（维护者自己写下的答案，不含人工标注）。

## 许可

- 源码：MIT（见 [LICENSE](LICENSE)）
- `data/` 下的来源链接索引与汇总指标：CC BY 4.0（见 `data/DATASET_LICENSE.md`）
- 第三方前端资源与模型：各自上游许可证，清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、
  [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)、[THIRD_PARTY_COMPONENTS.json](THIRD_PARTY_COMPONENTS.json)

---

# English (release notes)

## RepoOnboard — Python repository structure map (English)

RepoOnboard analyzes a public Python repository and shows a mind map, an interactive file dependency graph, key dependency paths, and a structure Q&A page. The dependency graph has compact and all-file modes. The Q&A tab offers 11 fixed questions (size and layout, entry point, core files, reading order, hubs, a file's up/downstream, dependency cycles, test coverage, vendored code) whose answers are computed from AST facts at analysis time — no model is involved, so every number is traceable. One group, "find files for this task", uses the four-signal retrieval and can optionally be rephrased by a local Qwen model. It uses AST-based static analysis and never executes imported repository code. The six repositories in the original working copy are omitted from this release; enter a repository URL to analyze one.

### Run locally

For a Chinese quick start, see [教师运行说明.md](教师运行说明.md).

1. Install Python 3.10 or later.
2. Create an environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. Start the service from this directory:

   ```powershell
   .\.venv\Scripts\python.exe src\server.py
   ```

4. Open `http://127.0.0.1:8765`, enter a public GitHub or Gitee repository URL, and select **解析项目**.
5. Browse the mind map, dependency graph, or key paths. The mind map is the default view. The graph opens in compact mode; choose **全部文件** to see every file node and dependency edge. Open the **问答** tab and pick a question from the left-hand list; the answer and its evidence files appear immediately. The last group takes a free-text task description and returns the top-5 files. For rephrased Chinese answers in that group, optionally install Ollama and the Qwen model described in [教师运行说明.md](教师运行说明.md); without it the same top-5 files and structured reasons are still returned.

The service downloads and statically parses Python files; it does not execute repository code. Current limits: public GitHub/Gitee only, at least 3 Python files, archive ≤30 MB, expanded archive ≤512 MB / 50,000 entries, ≤4,000 Python files, ≤400,000 non-empty Python lines, and a 90-second analysis wait. Non-Python projects and private repositories are unsupported. Q&A uses local repository facts; when configured, Ollama runs the Qwen model on the same computer. No source code is sent to a hosted AI service.

### Open data included

`data/benchmark_sources.json` contains source URLs and issue/PR numbers only. `data/metrics_summary.json` contains aggregate results. Neither file includes issue bodies, PR text, source code, or per-task changed-file labels. The metadata and aggregate summary are licensed under CC BY 4.0 as stated in `data/DATASET_LICENSE.md`; that license does not cover linked GitHub content.

The original raw task file is intentionally not included because it contains third-party issue text and task-level labels whose redistribution rights have not been established. This summary is informative; exact reproduction of the reported benchmark requires the excluded per-task data.

### Licensing

Original RepoOnboard source code is offered under the MIT License in LICENSE. Data files in data/ use the scope and terms in data/DATASET_LICENSE.md. Browser assets under web/vendor/ retain their upstream terms; see THIRD_PARTY_NOTICES.md and THIRD_PARTY_LICENSES.md. Markmap bundles are verified as version 0.18.12, and the AntV G6 bundle is verified as version 4.8.23. Versioned bundled-component license inventories for both are recorded in THIRD_PARTY_COMPONENTS.json.
