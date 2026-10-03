# WO03 local material lookup: P0004 PDAC microbiota route

## Result

P0004 is present in the local SQLite index with full text and card material. The row is:

- `paper_id`: `00d3d83d6571a7d9c15adbb84e0c371ce46d15a4`
- `source_handle`: `P0004`
- title: `Roles of microbiota in pancreatic cancer development and treatment`
- year/DOI: `2024` / `10.1080/19490976.2024.2320280`
- `material_depth`: `fulltext`; `identity_status`: `pool_identity`; `pool_action`: `already_in_pool`
- `card_path`: `<LOCAL_ONLY_PAPER_CARDS_ROOT>\20260922_microbiome_batch\reading\cards\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\ff1ff79c0b843ce0db21e51a7032e870\attempt-01\PAPER_READING_CARD.json`
- `snapshot_path`: `<LOCAL_ONLY_PAPER_CARDS_ROOT>\20260922_microbiome_batch\materials\00d3d83d6571a7d9c15adbb84e0c371ce46d15a4\snapshot-5be0c4b8670f4d78bd64a766`

It has 320 indexed segments (292 `document_block` plus 28 card/directed metadata segments). `paper_terms.card_terms` includes `Immune checkpoint inhibitors (anti-PD-1, anti-CTLA-4)`, `Antibiotic-mediated microbial depletion`, and TME/immune terms.

## Observed triage behavior

The stored live triage at `runs/live/retrieval/rebind_same_answered_need/local_triage/Ne675fc8ee99c6a9a6b37/LOCAL_TRIAGE.json` reports `decision=external_research`, `local_reading.paper_contexts=[P0597,P0087,P0479]`, and has no `known_paper_handles` in its serialized gap (`existing_handles=[]`). The same triage's selected passage handles are P0597, P0087, and P0479. This is a retrieval ranking/cap effect, not an absent P0004 record.

Reproduced read-only through `prepare_local_reading()` with the stored question and defaults (`top_papers=6`, `passages_per_paper=2`): candidates selected in the six-paper cutoff were P0597, P0087, P0479, P0151, P0583, and P0081; P0004 was outside that cutoff. Raising only `top_papers` to 100 returns P0004. With the narrower ordinary query `microbiota depletion anti-PD1 pancreatic cancer mouse models`, `search()` returns P0004 at rank 9 (top_papers=100, one passage/paper); dropping `anti-PD1` to `microbiota depletion pancreatic cancer mouse models` returns P0004 at rank 7. With the exact stored gap question and top_papers=100, P0004 is rank 7; the default top-six cap therefore excludes it.

The narrow query's best P0004 passage identifies an orthotopic KPC PDAC model. The indexed full text also contains the concrete route: in the orthotopic PDAC model, oral antibiotic microbial depletion rendered tumors sensitive to anti-PD-1, with increased CD4+ and CD8+ T-cell activation in the TME (segments ordinal 93-94); the same review describes depletion-associated Th1/CD8+ infiltration, TAM MHC-II/CD86/TNF-alpha/IL-12/IL-6 increases, and reduced MDSCs (ordinals 55-56). It explicitly marks the positive Bifidobacterium/Akkermansia/Enterococcus anti-PD-1 examples as melanoma/sarcoma models (ordinals 87-91), and says PDAC ICI remains understudied and patient responses are not established.

## Cause and API boundary

Observed cause: the normal local triage search uses `search(index, gap.question, concepts=gap.concepts, ..., top_papers=6)` (`planning_material_triage.py:370-377`; `prepare_local_reading` default `top_papers=6`). The stored gap had no targeted concepts, so the long question was expanded into OR-style terms and P0004 ranked seventh by paper, just past the six-paper cutoff. No FTS corruption or missing card/snapshot was observed.

Observed API boundary: `run_gap_local_triage()` constructs `LocalGap.existing_handles` only from `gap["known_paper_handles"]` (`planning_supplement.py:2015-2039`). `_normalize_gaps()` preserves `known_papers` as a separate field (`progressive_review_plan.py:2928-2940`), but `needs_from_planner_gap_rows()` and the retrieval-loop `gap_row` pass only `existing_handles`/`known_paper_handles` (`planning_retrieval_loop.py:1074-1079`; `progressive_review_plan.py:7153-7159`). Therefore a `known_papers` entry containing P0004's `paper_id`/`source_handle` is not, by itself, an explicit local-triage pin. A caller that supplies `known_paper_handles: ["P0004"]` can mark the retrieved P0004 hit as existing once it is in the top-paper result, but the current caller does not derive that field from `known_papers`.

The last sentence is a code-path observation; it does not imply the review pipeline must be refactored for this acceptance. A suitable real test input is the existing metadata-backed normal question plus `known_paper_handles: ["P0004"]` (no expected answer field), or the narrower ordinary query above with the P0004 metadata. The local offline preview confirms retrieval; it does not make a scientific quality claim beyond the indexed passages.

## Verify

Read-only commands used:

```powershell
@' ... '@ | python -   # sqlite3 read-only schema/row/FTS inspection
@' ... '@ | python -   # PlanningMaterialIndex(readonly=True), search/prepare_local_reading
```

No model, network, credential, output pipeline, production/test/Git change was performed for this lookup. Only this audit report was written.
