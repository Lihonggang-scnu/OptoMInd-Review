# Runtime alignment checkpoint

This directory is the external, reviewable runtime record for the 2026-10-13
Article/Review functional trial. It deliberately lives outside both source
repositories and contains no editable-install metadata.

## Interpreter ownership

| Profile | Interpreter | Source roots | Purpose |
| --- | --- | --- | --- |
| Review | `D:\OptoMind-Article-3\local-preparation\envs\review\Scripts\python.exe` | `OptoMInd-Review` via the existing `optomind_review_source.pth` | ordinary literature review and Review CLI checks |
| Article/Torch | `D:\OptoMind-Article-3\local-integration\20261013\envs\tensor\Scripts\python.exe` | Article `code` and `veritmm` via the existing `optomind_article_source.pth` | native Article imports and CPU Torch evaluator checks |
| Bridge | `bridge-venv\Scripts\python.exe` | the explicit roots in `profiles\bridge-source.pth` | narrow cross-repo import/transport check only |

The two application environments are intentionally separate. Article and
Review both advertise the distribution name `optomind`; neither environment
installs that distribution, so their source roots cannot be accidentally
combined through simultaneous editable installs. The bridge uses source paths
only and has no `optomind*.dist-info` entry.

The bridge is created from the tensor interpreter with system packages and
its persistent `.pth` explicitly adds the tensor and Review site-package
roots plus the three source roots. Trial scripts do not modify `sys.path` or
set `PYTHONPATH`. The paid transport's `module4/runtime.py` remains loaded by
exact file path and is not invoked by this checkpoint.

## Dependency policy

The four source manifests carry the compatible bounds:

- `matplotlib>=3.10.8,<3.12` in both projects;
- `json-repair>=0.63.5,<1` in both projects;
- Review's optional graph profile accepts tested `igraph` 1.x,
  `leidenalg` 0.12.x, and `pyarrow` 26.x;
- Review's research requirements do not carry pytest at runtime. Pytest is in
  `requirements\test-only.txt` as an external test profile.

The Article evaluator profile also declares the native import surface used by
the tensor check: OpenAI, PyYAML, requests, json-repair, matplotlib, and CPU
Torch. Optional institutional, graph, and differentiable features remain
outside the native profile.

`requirements\*.txt` records alignment pins observed in the existing tested
environments. It is an audit/profile record, not an instruction to reinstall
the active tensor environment.

## Acceptance

Run `audit\native_acceptance.ps1` from any directory. It clears
`PYTHONPATH`, changes each child process to `%WINDIR%`, checks native imports,
checks the neutral CLI help paths, runs `uv pip check`, and records focused
freezes under `audit\results\`. It performs no API, GPU, paid, or full
regression operation.

