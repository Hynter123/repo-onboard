# RepoOnboard — Python repository structure map

RepoOnboard turns a public Python repository into a browsable structure map and ranks likely files for a task description. The six repositories in the original working copy are optional prebuilt examples; this release draft omits their derived payloads and accepts repositories entered by URL.

## Run locally

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

4. Open `http://127.0.0.1:8765` and enter a public GitHub or Gitee repository URL.

The service downloads and statically parses Python files; it does not execute repository code. Current limits: public GitHub/Gitee only, at least 3 Python files, archive ≤30 MB, expanded archive ≤512 MB / 50,000 entries, ≤4,000 Python files, ≤400,000 non-empty Python lines, and a 90-second response wait. Non-Python projects and private repositories are unsupported.

## Open data included

`data/benchmark_sources.json` contains source URLs and issue/PR numbers only. `data/metrics_summary.json` contains aggregate results. Neither file includes issue bodies, PR text, source code, or per-task changed-file labels. The metadata and aggregate summary are licensed under CC BY 4.0 as stated in `data/DATASET_LICENSE.md`; that license does not cover linked GitHub content.

The original raw task file is intentionally not included because it contains third-party issue text and task-level labels whose redistribution rights have not been established. This summary is informative; exact reproduction of the reported benchmark requires the excluded per-task data.

## Licensing

Original RepoOnboard source code is offered under the MIT License in LICENSE. Data files in data/ use the scope and terms in data/DATASET_LICENSE.md. Browser assets under web/vendor/ retain their upstream terms; see THIRD_PARTY_NOTICES.md and THIRD_PARTY_LICENSES.md. The Markmap bundle version and complete transitive inventory are not recoverable from the supplied files.
