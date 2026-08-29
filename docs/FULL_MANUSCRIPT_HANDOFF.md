# Unified Full-Manuscript Handoff

Deterministic, metadata-only builder for the eight enhanced chapter assets.
It never rewrites chapter prose or evidence.

## Input manifest

```json
{
  "project_root": ".",
  "sections": [
    {
      "section_id": "S01",
      "enhanced_asset_dir": "outputs/chapter_asset_enhancer_s01_...",
      "authoritative_input_packet": "outputs/.../input_packet.json",
      "source_old_draft": "outputs/.../SECTION_DRAFT_EN.md"
    }
  ]
}
```

Required enhanced files per section: `ENHANCED_CHAPTER.md`,
`CHAPTER_ARGUMENT_PLAN.json`, `CLAIM_TO_PARAGRAPH_MAP.json`,
`EXPLANATION_BLOCKS.json`, `EXPLANATORY_CITATION_LEDGER.json`,
`ENHANCEMENT_REPORT.json`.  `BLOCK_SCIENTIFIC_REVIEW.json` and
`LEGACY_GAP_AUDIT.json` are optional and fail-open.

## Outputs

- `UNIFIED_MANUSCRIPT_HANDOFF.json` — per-section envelopes with
  project-relative paths, sha256 digests, title/id, word count, chapter
  status, core packet digest (`role=core_evidence`), explanatory ledger
  digest (`trust_boundary=background_explanation_only`), reviewer notes
  status, provenance, and a deterministic relocation-safe `input_fingerprint`.
- `HANDOFF_METADATA_REPAIR_REPORT.json` — title repairs, section-id
  reconciliation diagnostics, hard defects, and aggregate counts.

## Metadata repair rules

- Section titles are recovered from the manifest, `CHAPTER_ARGUMENT_PLAN`,
  or the enhanced chapter heading; unrecoverable titles are recorded.
- Section-id mismatches between the manifest and enhanced JSON files are
  blocking (never guessed).
- Missing optional review files are recorded as fail-open.
- Missing/empty required files fail with an actionable error.
- Visible `[REF:...]` markers are checked against the union of core packet
  paper identities and explanatory ledger identities; unknown markers are
  recorded as hard defects without rewriting prose.
- The input fingerprint depends on file contents, schema, section order,
  repair notes, hard defects, and aggregate counts, never on absolute paths,
  so the same content under another project root yields the same fingerprint.
- Repeated runs with an unchanged fingerprint reuse the existing package.

## CLI

```powershell
py -3.11 scripts/build_full_manuscript_handoff.py `
  --manifest handoff_manifest.json `
  --output-dir outputs/full_manuscript_handoff_v1
```
