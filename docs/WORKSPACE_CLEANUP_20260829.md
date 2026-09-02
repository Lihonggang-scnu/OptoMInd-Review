# Workspace cleanup record — 2026-08-29

This is a reversible maintenance record for the mainline workspace. It does
not authorize deletion, reset, commit, or upload.

## Archived

The following explicitly generated scratch families were moved from the
repository root to:

```text
archive/mainline-test-scratch-20260829/
```

- `.pytest_cache/`;
- `.pytest-basetemp-*` test fixtures;
- `.tmp-*-tests/` test scratch directories;
- `dom-review-mat-tmp-*/` and `material-grounding-tmp-*/` temporary material
  directories;
- root `build/` and `out/` generated output.

The move covered 87 directories, 20,101 files, and 1,072,244,594 bytes. The
operation was a same-volume move, not a destructive delete, so the archived
material remains available for historical debugging. The archive itself is
local-only and ignored by the source release rules.

## Intentionally preserved

The following were not moved because they may be active, persistent, or user
owned:

- `api_keys/` — preserved exactly and not read by the release-preparation
  scripts;
- `OptoMind-TMM-Article-Handoff-20260813/` — preserved exactly and excluded
  from this mainline;
- the three complete E2E trees under `outputs/research_harness_e2e/`;
- `data/`, `database/`, `literature_workspace/`, `user_fulltexts/`, and the
  long-term local cache;
- `.venv/`, `.uv-cache-review/`, `optomind_desktop/node_modules/`, and native
  desktop `target/` output;
- `tmp/`, `_docx_qa_submission_template_20260828/`, and existing archive
  history because their ownership or future use was not unambiguous.

The release rules now ignore package-manager and native build directories while
leaving `replay/` visible as a portable deliverable. No persistent research
asset was deleted or relocated by this cleanup.
