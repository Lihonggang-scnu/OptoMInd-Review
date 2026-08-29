# Staged Publication Handoff

`optomind_research/runtime/staged_publication_handoff.py` builds the portable
publication handoff for the staged full-manuscript workflow. It is deterministic,
model-free, and network-free.

## Input contract

All inputs are explicit paths:

- accepted reviewed manuscript Markdown (`STAGED_COMPLETE_REVIEW_EN.md`);
- newer staged conclusion, introduction, and abstract JSON artifacts;
- publication metadata catalog (`PUBLICATION_METADATA_CATALOG.json`);
- optional publication metadata audit;
- final visual package (`FINAL_VISUAL_PACKAGE.json`);
- commander work order or an equivalent structured section source;
- publication article metadata JSON (title/authors/keywords/date);
- output directory, optional project root, and optional run id.

The accepted reviewed body is the authority for the eight scientific sections.
The staged artifacts are the authority for Abstract, Introduction, and
Conclusion. The commander/section source is the structural authority for
scientific section order.

## Outputs

The builder writes:

- `FINAL_REVIEW_EN.md`
- `FINAL_VISUAL_PACKAGE.json`
- `visual_assets/`
- `section_coverage/sections/ALL/SECTION_SOURCE_LEDGER.json`
- `PUBLICATION_METADATA_CATALOG.json`
- `PUBLICATION_METADATA_AUDIT.json`
- `publication_metadata.json`
- `REVIEW_BLUEPRINT.json`
- `REVIEW_CONTENT_PACKAGE.json`

The content package uses `research_harness.content_package.v1`, with
`source_run_dir="."`, `publication_eligible=false`, and portable relative paths.

## Safety rules

- Front-matter citation identity mismatch fails closed.
- Duplicate or missing commander section IDs fail closed.
- Malformed required JSON artifacts fail closed.
- Visual asset paths that escape the project root or visual package directory
  fail closed.
- Missing/rejected visual assets fail open and are audited; they are never
  fabricated.
- Scientific body prose is not rewritten. Only a duplicate consecutive visible
  chapter heading (`## Title` followed by `# Title`) is removed.
- Bibliography fields stay empty when unresolved. The builder never emits
  `1900`, fake authors, or silent placeholder metadata.

## CLI

```powershell
python scripts/build_staged_publication_handoff.py `
  --reviewed-manuscript outputs/staged_frontmatter_r35_20260815_live_v1/STAGED_COMPLETE_REVIEW_EN.md `
  --conclusion-artifact outputs/staged_frontmatter_r35_20260815_live_v1/staged_conclusion.json `
  --introduction-artifact outputs/staged_frontmatter_r35_20260815_live_v1/staged_introduction.json `
  --abstract-artifact outputs/staged_frontmatter_r35_20260815_live_v1/staged_abstract.json `
  --metadata-catalog outputs/publication_metadata_r35_20260815_online_v2/PUBLICATION_METADATA_CATALOG.json `
  --visual-package outputs/staged_visual_factory_r35_20260815_v1/FINAL_VISUAL_PACKAGE.json `
  --commander outputs/global_commander_enhanced_r33_20260815_live_v2/global_commander_work_order.json `
  --publication-metadata path/to/publication_metadata.json `
  --project-root . `
  --output-dir outputs/staged_publication_handoff `
  --run-id staged-publication-handoff-v1
```

Exit code `0` prints a compact JSON summary; `2` prints `REFUSED: <reason>` for
unsafe or malformed inputs.
