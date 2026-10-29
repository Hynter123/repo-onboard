# Release audit notes

This release contains the RepoOnboard application, required browser assets, a link-only benchmark index, and aggregate metrics. It excludes virtual environments, model weights and caches, raw benchmark text and task labels, precomputed repository payloads, downloaded repository source trees, graph caches, Python bytecode, local IDE settings, and internal handoff/development logs.

## Checks recorded for this package

- data/benchmark_sources.json contains 30 records with repository names, issue/PR numbers, and URLs only.
- data/metrics_summary.json contains aggregate values; it does not contain per-task changed-file labels.
- web/data/index.json is an empty runtime index.
- The interactive flow provides four tabs: mind map, dependency graph (compact / all files), key paths, and a structure Q&A page.
- The Q&A page lists 11 fixed questions whose answers are computed from AST facts by `src/qa.py` at analysis time and shipped inside the analysis payload; no model or extra request is involved, and every path and count can be traced back to the structure graph.
- Qwen/Ollama is optional, runs locally, and is used only to rephrase the evidence for the free-text “find files for this task” question; without it the same top-5 files and structured reasons are returned.
- `src/chat.py` uses only AST-derived file, symbol, dependency, and test-link facts; it does not send repository source to a hosted model.
- No raw task text, repository snapshot, model file, or Python cache is included.
- Project source, data, and third-party assets have separate license scopes documented in the repository.
- Markmap browser assets are verified as version 0.18.12 by byte-for-byte comparison with upstream; all 81 Markmap dependency components have version and license entries in THIRD_PARTY_COMPONENTS.json.
- The G6 browser bundle and source map are the official AntV 4.8.23 files; their hashes, 39 source-mapped package-version records, and the top-level G6 license record are in THIRD_PARTY_COMPONENTS.json.

## Remaining review notes

The @gera2ld/jsx-dom 2.2.2 npm metadata and bundle banner declare ISC, while the published package LICENSE says MIT. Both license texts and the conflicting declarations are recorded. The link-only benchmark index and aggregate values are offered under CC BY 4.0; that license does not cover linked upstream content. Review authorship and the other vendor notices before formal submission.
