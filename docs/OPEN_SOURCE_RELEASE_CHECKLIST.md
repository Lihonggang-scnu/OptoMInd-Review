# Open-source release checklist

Status: release candidate prepared locally; GitHub upload is intentionally
blocked until the owner gives explicit approval.

## Scope lock

- [ ] Mainline only: `optomind_research/`, `config/`, `prompts/`, `schemas/`,
  `scripts/`, `tests/`, `optomind_ui/`, `optomind_desktop/`, root entry files,
  and the release-facing documentation.
- [ ] Preserve `OptoMind-TMM-Article-Handoff-20260813` exactly as it is. It is
  not part of this cleanup, review, artifact index, or upload candidate.
- [ ] Preserve the `api_keys/` directory layout and its pre-created provider
  filenames. A local live-run copy may contain private values temporarily, but
  every credential file must be cleared to zero bytes before public staging;
  never publish a non-empty value.
- [ ] No GitHub remote, push, tag, or commit is performed by this preparation
  pass.

## Public source boundary

Keep:

- the canonical `run_review_harness.py` entry point;
- the mainline packages and their regression tests;
- model and provider configuration templates without secret values;
- `replay/`, which is the portable, read-only three-run interface;
- `artifacts/e2e/`, which contains the final publication layer for the three
  historical runs;
- `docs/MAINLINE_FILE_MAP.md`, this checklist, and the E2E artifact index.

Keep local-only:

- non-empty files under `api_keys/`, `.env*`, certificates, and any other
  credential values. The directory and reviewed empty placeholder files may be
  retained as part of the public setup layout;
- `data/`, `database/`, `literature_workspace/`, `user_fulltexts/`, and raw
  local caches;
- `outputs/` raw run trees and downloaded papers. Their complete file list and
  hashes are recorded without duplicating the multi-gigabyte tree into the
  source upload;
- `.venv/`, `node_modules/`, native `target/` output, scratch directories, and
  `archive/` maintenance material;
- the separate TMM handoff line named above.

## Evaluator setup gate

The following should be true on a clean clone before a live run:

- [ ] Python 3.11+ is available.
- [ ] `requirements-research.txt` installs successfully.
- [ ] The repository-root `api_keys/` directory and its pre-created filenames
  are present. Fill `qwen-api-key.txt` for a live run; one key per line is
  accepted and forms one shared Qwen pool. Do not create or rename files.
- [ ] Optional provider files are present only when the evaluator wants the
  corresponding higher-rate backend. Their absence must not prevent the
  normal public/local fallback path.
- [ ] `python run_review_harness.py --help` exits successfully with Python 3.11+.
- [ ] `python -m pytest -q` passes the source regression suite.
- [ ] `xelatex` and `latexmk` are available if PDF output is required. Without
  them, the normal non-strict run still preserves the review text and LaTeX
  source.

Recommended first live command:

```powershell
py -3.11 run_review_harness.py `
  --question "写一篇关于规模化光子计算的文献综述，阐述从可编程集成光子芯片到 AI 加速与光互连" `
  --execution-profile private_study `
  --no-research-plan `
  --auto-confirm-query-plan `
  --output-root outputs/research_harness_e2e
```

The run directory records `HARNESS_STATE.json`, `HARNESS_COST.json`, the
delivery gate, stage ledgers, citation maps, visual audits, and publication
outputs. Resume with `--run-dir <existing-run-directory>` after a recoverable
interruption.

## Pre-upload checks

- [ ] Run `python scripts/build_release_candidate.py` to refresh the three
  portable publication snapshots and the complete raw-artifact hash index.
- [ ] Run `python scripts/verify_public_release.py`.
- [ ] Before staging, clear every file under `api_keys/` and verify that all
  credential files are zero bytes. The directory and filenames remain in the
  release, but no live key value may be staged.
- [ ] Confirm the verification report finds no private branch path,
  machine-specific absolute path, or raw key value in `artifacts/` and
  `replay/`. The verifier does not read credential contents.
- [ ] Because `.gitignore` protects `api_keys/` during normal development,
  explicitly stage only the reviewed empty placeholders; never use a broad
  recursive add for the final upload.
- [ ] Confirm all three English PDFs, their source `.tex`/bibliography files,
  the Chinese companion PDFs, and the final quality/visual/style reports are
  present in `artifacts/e2e/`.
- [ ] Review `docs/E2E_ARTIFACTS_INDEX.md` and the three original run trees.
- [ ] Use `docs/PUBLIC_UPLOAD_ALLOWLIST.md` to exclude internal memory,
  work-order, and historical log folders from the eventual upload.
- [ ] Review `git status` manually, especially any pre-existing dirty files;
  this checklist does not authorize resetting, deleting, or committing them.
- [ ] Only after the owner explicitly approves, select the intended files,
  create a commit, and upload to GitHub.

## Why the raw trees are not copied into the source upload

The three original runs are complete and remain at their existing paths. They
include downloaded papers, intermediate caches, large evidence ledgers, and
runtime traces; together they are multi-gigabyte research records and can
contain publisher-controlled source material. The release candidate therefore
keeps them intact locally, records every file and SHA-256 value, and publishes
the final manuscripts, source packages, audit reports, and static replay as a
small portable layer. This preserves reproducibility without pretending that
a Git repository is a suitable mirror for all raw acquisition data.
