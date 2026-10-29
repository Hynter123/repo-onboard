# RepoOnboard — Python repository structure map

RepoOnboard analyzes a public Python repository and shows a mind map, an interactive file dependency graph, key dependency paths, and a structure Q&A page. The dependency graph has compact and all-file modes. The Q&A tab offers 11 fixed questions (size and layout, entry point, core files, reading order, hubs, a file's up/downstream, dependency cycles, test coverage, vendored code) whose answers are computed from AST facts at analysis time — no model is involved, so every number is traceable. One group, “find files for this task”, uses the four-signal retrieval and can optionally be rephrased by a local Qwen model. It uses AST-based static analysis and never executes imported repository code. The six repositories in the original working copy are omitted from this release; enter a repository URL to analyze one.

## Run locally

For a Chinese Windows quick start, see [教师运行说明.md](教师运行说明.md).

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

## Open data included

`data/benchmark_sources.json` contains source URLs and issue/PR numbers only. `data/metrics_summary.json` contains aggregate results. Neither file includes issue bodies, PR text, source code, or per-task changed-file labels. The metadata and aggregate summary are licensed under CC BY 4.0 as stated in `data/DATASET_LICENSE.md`; that license does not cover linked GitHub content.

The original raw task file is intentionally not included because it contains third-party issue text and task-level labels whose redistribution rights have not been established. This summary is informative; exact reproduction of the reported benchmark requires the excluded per-task data.

## Licensing

Original RepoOnboard source code is offered under the MIT License in LICENSE. Data files in data/ use the scope and terms in data/DATASET_LICENSE.md. Browser assets under web/vendor/ retain their upstream terms; see THIRD_PARTY_NOTICES.md and THIRD_PARTY_LICENSES.md. Markmap bundles are verified as version 0.18.12, and the AntV G6 bundle is verified as version 4.8.23. Versioned bundled-component license inventories for both are recorded in THIRD_PARTY_COMPONENTS.json.
