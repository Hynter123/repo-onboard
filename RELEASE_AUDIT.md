# Release audit notes

This release contains the RepoOnboard application, required browser assets, a link-only benchmark index, and aggregate metrics. It excludes virtual environments, model weights and caches, raw benchmark text and task labels, precomputed repository payloads, downloaded repository source trees, graph caches, Python bytecode, local IDE settings, and internal handoff/development logs.

## Checks recorded for this package

- data/benchmark_sources.json contains 30 records with repository names, issue/PR numbers, and URLs only.
- data/metrics_summary.json contains aggregate values; it does not contain per-task changed-file labels.
- web/data/index.json is an empty runtime index.
- No raw task text, repository snapshot, model file, or Python cache is included.
- Source code, dataset metadata, and third-party browser assets have separate license scopes documented in the repository.

## Remaining license detail

The supplied Markmap browser bundles do not record their exact versions or a full transitive dependency inventory. THIRD_PARTY_NOTICES.md lists identifiable components and explicitly records this gap. The @gera2ld/jsx-dom bundle banner/package metadata say ISC while its published package LICENSE says MIT; both license texts and the discrepancy are recorded. The link-only benchmark index and aggregate values are offered under CC BY 4.0; that license does not cover linked upstream content. Review upstream issue/PR links and local authorship before using this package in a formal submission.
