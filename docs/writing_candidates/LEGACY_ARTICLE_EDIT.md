# Optional full-article edit

`scripts/upgrade3/legacy_unit_writer.py --article-edit` adds one whole-article coordination request using the existing `article_text_editor` prompt and local edit contract. It runs after every unit of the supplied full book is complete and any enabled quality stages have finished. Selecting a subset explicitly skips it. A completed quality assessment with retained questions permits editing; unfinished quality work does not. Existing quality and citation pending states remain visible.

Use the same `--ledger`, `--budget-scope` and `--budget-limit 60` as the author and quality runs. The editor uses Qwen3.5 Plus, 16384 thinking tokens, 24576 answer tokens, streaming, and no automatic retries. Preview is free. The default remains disabled, and existing fixture/recording APIs remain available.

The route stores stages at `article_edit/<input-hash>/`, retaining prior candidates when a recovered quality stage changes the assembled body. Identical input reuses the same stage without another call; the author cache is unaffected. Each stage writes `EDIT_IDENTITY.json`, an exact-byte `ORIGINAL_HANDLES.md` snapshot, and `full/REQUEST.json`, messages, profile, estimate and `RAW_RESPONSE.json`. Matching saved RAW or SSE can recover without constructing a provider. Input/profile changes are rejected within the same stage directory. Incomplete or invalid responses remain pending and retain their raw evidence; another ordinary resume cannot buy a replacement call.

Edits target unique spans of one original snapshot. Overlapping or missing targets are reported, and a replacement cannot become another edit's target. An unrelated occurrence of a replacement does not prove a missing target was previously edited. Repeating completed edits remains a no-op. Malformed proposal rows are explicit errors. Title edits may make task-like headings concise while retaining numbering, subject and scope; the editor receives no experiment-specific scientific issue list.

The original assembly remains unchanged. `CANDIDATE_HANDLES.md` retains the editorial candidate; `TEXT_EDIT_REPORT.json` records proposals, applied/skipped changes, unresolved questions and pending problems. Source identity additions/removals are reported rather than forced to preserve a reference count. A separate `NUMBERING_INPUT_HANDLES.md` excludes the old reference section, then the existing delivery citation stage generates `numbered/MANUSCRIPT_READER.md` and a new `REFERENCES.json` from the original formal identity catalog. Numbering follows final first appearance. Editing and transport completion are not scientific acceptance.

Offline checks:

```powershell
python -m pytest tests/test_article_edit_live.py tests/test_unit_realization.py tests/test_legacy_unit_route.py tests/test_legacy_unit_route_review.py -q
```
