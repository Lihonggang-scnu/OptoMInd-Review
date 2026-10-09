# Provisional author-supply audit

This is a free, provisional audit of
`alias_recovered_live/DRAFT_GUIDE.json`. It is not a final GUIDE acceptance:
the guide lifecycle is blocked because the paid continuation repeated N1 with
`repeated_read_request:P0212:explicit_new_reread_reason_required`, so no
`GUIDE.json` exists.

The provisional candidate file SHA-256 is
`e08230cf4a2420ec7699228bde23bed7ac6de65078af9db4a5e4915a0b8d5946`.
The lifecycle receipt is `alias_recovered_live/GUIDE_RESULT.json` with
`status=blocked`, `complete=false`, one paid maker call, and the repeated-read
issue above.

## Audit result

The existing constructor path was used for all 29 writing units:

- `validate_guide`
- `compile_guided_materials`
- `build_author_payload(pack, guide, chapter_or_unit, accepted_body_markdown, reread_atoms=())`
- `guided_body_writer._messages`

Every unit used an explicit empty preceding-body snapshot:

```json
{
  "kind": "explicit_empty_preceding_body",
  "accepted_body_markdown": "",
  "continuity_claim": false
}
```

The audit passed with zero provider calls:

- 29 expected and 29 constructed writing units
- 95 short content addresses mapped to 95 canonical task IDs exactly once
- 0 automatically recovered units
- Full content-task wrappers exact, including tables and unknown fields
- Original content-unit contexts exact
- Task and context source evidence present
- Source identities exact
- Scientific atom IDs and the author-payload scientific projection values exact
- Tokenizer sizes recorded for every unit
- No continuity claim

Summary receipt:

`author_supply_check_provisional_final/AUTHOR_SUPPLY_CHECK.json`

The same directory contains each unit's `PAYLOAD.json` and `MESSAGES.json`.
The report records the per-unit payload/message paths, source unions, context
source unions, material counts, tokenizer sizes, hashes, and empty-body
snapshots.

Observed report SHA-256:

`108a76bf9c1f1a606441f067492942ff912f8a89df147fdc46f50dd0bba1e722`

## Formal BODY preview

The official free preview was attempted with the existing CLI and Plus config,
without `--run`:

```text
C:\Anaconda\python.exe -X utf8 scripts/upgrade3/guided_body_writer.py --manifest F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json --guide F:\OptoMind-Review-2\outputs\guide_content_units_acceptance_20261010\alias_recovered_live\DRAFT_GUIDE.json --config config\guided_body_writer\plus_first.json --plus-only --tokenizer F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json --output F:\OptoMind-Review-2\outputs\guide_content_units_acceptance_20261010\guided_body_preview_provisional
```

It produced a valid free preview at
`guided_body_preview_provisional/runs/20261009T171722Z-1051ab86f6`:

- `status=preview`, `complete=false`
- first planned author stage: `author_001_unit_001` (Ch1)
- 0 model calls and 0 paid dispatches
- measured first author prompt: 144,338 tokens; reserved estimate: 169,851
- Plus profile: `qwen3.5-plus`, 16,384 thinking budget, 49,152 output tokens
- no body output was generated; this is a planning preview only

The preview does not promote the provisional draft or alter its blocked
lifecycle state.

Preview receipt:

`guided_body_preview_provisional/runs/20261009T171722Z-1051ab86f6/FULL_BODY_RESULT.json`


