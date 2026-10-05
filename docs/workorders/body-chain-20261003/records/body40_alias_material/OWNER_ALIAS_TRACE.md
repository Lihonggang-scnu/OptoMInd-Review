# Owner / explicit alias material trace

## Finding and limits

- Read-only inspection of archived BODY40 inputs, based on runtime baseline `e75c66c`; no model calls, network calls, production data edits, or full BODY execution. The latest user-local first-stop acceptance was not uploaded and is not firsthand evidence here.
- The archived Ch6 writer has a genuine exact-key false-missing: `source_catalog.P0049.aliases == ["P0605"]`, while `UNIT_INPUT.materials[6] == {"source_handle":"P0605","missing_material":true}`. The canonical material already reached the formal catalog and writer under P0049.
- This does not establish an owner-material loss. The actual saved owner request has a different channel shape: 17 standalone source rows contain neither P0049 nor P0605, but its multi-source tool narrative contains both identities. That usable narrative survives unchanged into the final packet and formal arrangement. Later packet assembly includes the two standalone rows.
- The owner request is witnessed through `cache_inputs` in the saved stage result, not an independently archived serialized provider message. Baseline `progressive_review_plan.py` passes `revision_payload` to `_call_record` and saves the same payload as `cache_inputs` (around lines 5634–5655). No claim is made about undocumented transformations in the provider transport.

## Identity and boundary path

1. `planning/stages/affected_chapter_revision/Ch6.json.gz`: `$.cache_inputs.tool_materials[3].sources[11]` and `[20]` name P0049, paper ID `1607ffdb79bd5dbb1f94932e27410c55f0aa8290`, DOI `10.1016/j.ejca.2025.115221`. `sources[28]` names P0605 with canonical paper ID `CorpusId:275340168`. The complete 31-source narrative stays multi-source; it is not copied into any original study A/B.
2. `planning/writer_packets/Ch6.json.gz`: `$.source_materials[18]` is P0049; `[19]` is P0605, paper ID `CorpusId:275340168`, same normalized DOI. Both have A/B; P0605 also has the supplemental channel. Its title/hash identity is not used here as a new fuzzy alias inference.
3. Baseline `chapter_arrangement.merge_doi_duplicates` creates an explicit P0605 alias on P0049 using same normalized DOI plus close title. `build_source_catalog` serializes one canonical entry with the alias list. `_merge_duplicate_source_material` retains distinct supplement/deep-read/local/tool channels; A/B remain the keeper snapshot.
4. Formal `$.source_catalog.P0049` retains P0049 A/B byte-equivalent as JSON values and P0605 supplemental material byte-equivalent as JSON values. P0605 has no independent catalog key. `$.materials[5]` in the archived writer retains those three values exactly; `$.materials[6]` is the erroneous missing entry.
- Important limitation: P0605 A/B differ from P0049 A/B and are not separately exported by the existing duplicate merge. This report does not label those differences scientific loss or claim complete retention of every duplicate snapshot. Fixing this independent archival/variant policy is not needed to consume the already-established canonical material.

## Owner brief restoration

- Archived Ch6 `$.units[0].paragraph_tasks[2]` and `$.units[1].paragraph_tasks[2]` include both P0049 and P0605 in `source_uses`. They reference restored briefs Ch6_U1_P03 and Ch6_U2_P03 respectively.
- Their archived `source_brief_details[0].source_handles` are `[P0308,P0312]` and `[P0098]`; no archived restored brief in this Ch6 arrangement names P0605. Thus the alias source use exists, but it must not be described as an observed restored-owner-alias example.
- Baseline `_restore_original_briefs` copies full source brief details and can append original aliases to `source_uses`, because `known_handles` includes declared aliases. Writer alias consumption must support that generic path as well. A synthetic regression is the appropriate evidence for the restored alias edge.

## Minimum change and conflict policy

- Change the writer consumer at catalog selection/material lookup and associated identity provenance, so an explicitly declared alias selects the canonical catalog material while preserving the requested handle and canonical relationship. Missing unknown handles must remain missing. No additional owner runtime fix is justified by this trace.
- Never infer aliases from similar titles, same-looking paper IDs, or a writer missing warning. Multiple canonical claimants, a direct independent catalog key colliding with an alias, or explicit incompatible DOI/identity evidence must fail closed rather than silently borrow material.
- The archive includes different historical identifier namespaces for this declared same-DOI alias (hash ID versus CorpusId). A blanket unequal-paper-ID rejection would reject this valid archived explicit relationship. Consume its canonical catalog authority; do not re-import unrelated stale alias rows from packet/tool sources. A different DOI is contradictory even when an alias name matches.
- Review-reported original studies do not require their own A/B. The owner resolver explicitly admits attributable tool narrative with supplied identity, preserving multi-source provenance. This actual owner request demonstrates that channel. Material availability and identity consistency remain mandatory.

## Reproducible metadata evidence

Archive-relative paths below are under `docs/acceptance/body40-20261005`. Value SHA256 uses UTF-8 `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"))`. Sizes are compact JSON character counts; scientific text is not duplicated.

| File | Field path | JSON chars | Value SHA256 |
|---|---|---:|---|
| `planning/stages/affected_chapter_revision/Ch6.json.gz` | `$.cache_inputs.tool_materials[3].usable_content` | 12978 | `22fb9c063e4e9e2fb9946a0630627ff9f5801f4f479a96fdeca1bfebabcce838` |
| `planning/stages/affected_chapter_revision/Ch6.json.gz` | `$.cache_inputs.source_materials` | 102503 | `6c75760d5fff01e911e6e9dc5a1c4b6a49f1e0f2b8cffa36d6aecad9f71b543e` |
| `planning/writer_packets/Ch6.json.gz` | `$.source_materials[18].study_summary_A` | 1644 | `48d325f2c20de719f0654fbc9a91400db960c11f358fb845bdb912dc9cca2b28` |
| `planning/writer_packets/Ch6.json.gz` | `$.source_materials[18].review_planning_B` | 1633 | `609e43d69fe9482a81e5b0335306917df729cf55a5f98668652fdd14338602ff` |
| `planning/writer_packets/Ch6.json.gz` | `$.source_materials[19].study_summary_A` | 1766 | `cc1f4ef36625d7efdc940ae3525a1537cce5876c596b7dfcb708b38ded8d8176` |
| `planning/writer_packets/Ch6.json.gz` | `$.source_materials[19].review_planning_B` | 1632 | `2c1c79897c9bb42e5d3ba3c0d75f18c218d4140cd2e1ed5a2ee7b93abd807cb9` |
| `planning/writer_packets/Ch6.json.gz` | `$.source_materials[19].supplement_gap_material` | 3364 | `db0ecb42664de4b7e09b0b7d0e9f157b0a837266112c35ed940937beb2f85e54` |
| `planning/writer_packets/Ch6.json.gz` | `$.tool_materials[1].usable_content` | 12978 | `22fb9c063e4e9e2fb9946a0630627ff9f5801f4f479a96fdeca1bfebabcce838` |
| `body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json` | `$.source_catalog.P0049.study_summary_A` | 1644 | `48d325f2c20de719f0654fbc9a91400db960c11f358fb845bdb912dc9cca2b28` |
| `writer/live/Ch6/Ch6_Ch6_U1/UNIT_INPUT.json` | `$.materials[5].study_summary_A` | 1644 | `48d325f2c20de719f0654fbc9a91400db960c11f358fb845bdb912dc9cca2b28` |
| `body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json` | `$.source_catalog.P0049.review_planning_B` | 1633 | `609e43d69fe9482a81e5b0335306917df729cf55a5f98668652fdd14338602ff` |
| `writer/live/Ch6/Ch6_Ch6_U1/UNIT_INPUT.json` | `$.materials[5].review_planning_B` | 1633 | `609e43d69fe9482a81e5b0335306917df729cf55a5f98668652fdd14338602ff` |
| `body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json` | `$.source_catalog.P0049.supplement_material` | 3364 | `db0ecb42664de4b7e09b0b7d0e9f157b0a837266112c35ed940937beb2f85e54` |
| `writer/live/Ch6/Ch6_Ch6_U1/UNIT_INPUT.json` | `$.materials[5].supplement_material` | 3364 | `db0ecb42664de4b7e09b0b7d0e9f157b0a837266112c35ed940937beb2f85e54` |
| `body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json` | `$.chapter_tool_materials[1].usable_content` | 12978 | `22fb9c063e4e9e2fb9946a0630627ff9f5801f4f479a96fdeca1bfebabcce838` |
| `body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json` | `$.chapter_tool_materials[3].usable_content` | 12978 | `22fb9c063e4e9e2fb9946a0630627ff9f5801f4f479a96fdeca1bfebabcce838` |
| `body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json` | `$.chapter_tool_materials[5].usable_content` | 12978 | `22fb9c063e4e9e2fb9946a0630627ff9f5801f4f479a96fdeca1bfebabcce838` |

### Archive file hashes

- `planning/stages/affected_chapter_revision/Ch6.json.gz`: SHA256 `80d2deec2af5bd0b65d779205054aeb838191ddadeaf0caf5ab9cdf405b39cc1`
- `planning/writer_packets/Ch6.json.gz`: SHA256 `a1e1338cb7b87b32c542d6241a59b8c0045d2a876e0a1595de098888af32f60a`
- `body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json`: SHA256 `d2fcbc30c4c3aa03f1f12b9f40cf9245b19144e36e64c7232e806c3046b94283`
- `body/arrangement_repaired/Ch6/ARRANGEMENT_INPUT.json`: SHA256 `58da789a77aec0eb783bf0bd48ee5f4c5d1c3b06feb146e8959639ba531fcde0`
- `writer/live/Ch6/Ch6_Ch6_U1/UNIT_INPUT.json`: SHA256 `4e774380c6894611cb6dee3cf30b1a70330db429f6a59b9ab7676401c6e8b61a`

These are archive-byte hashes (compressed where .gz), not hashes of rewritten/decompressed copies. No source archive files were changed.

## Duplicate A/B snapshot policy (nonblocking, pre-existing)

The archived final packet still retains both complete original A/B snapshots at `$.source_materials[18]` and `[19]`. They remain available there for a future explicit variant-preservation decision. This second-stop consumer fix should not blindly merge alternative A/B interpretations. Formal catalog serialization keeps the canonical A/B snapshot; writer alias lookup is the current blocker.

Differences are quantified below by keys and hashes only, without claiming semantic or scientific loss.

### `study_summary_A`
- Keys compared: 7; changed keys: 6; canonical-only keys: []; alias-only keys: []

| Changed key | Canonical value SHA256 | Alias value SHA256 |
|---|---|---|
| `approach` | `a5c4e69e5ab727f9b07000c544b34cbebfc35ae7c3b7efbaf532326b006a858c` | `06d760371d0294f1d0f5fedf2a9a731fc73891aa40be9a9d08f2b7dc42ffe5b3` |
| `contribution_and_limits` | `3ca0ecad59d269545e1fc9d16db0e7f8c4953b771de78b2fb832d7b092b60713` | `c57bfd31765e3f3ff09ced6757e33aba96fb18459b763c0abadfeb59b60cc19c` |
| `key_findings` | `e8d140a4637a98135ce6259cba470f0e1e4e5ec7905ce9c3a44013efb2743488` | `1a56de9f9505bdd673c8f038985be4e12d222b4f9e3195e0da234681203f3b87` |
| `problem_or_question` | `53c41755d13445c0929445ca2c1576b0ee4233fd08e03a2c62ff67cb8a9fb94f` | `9b8dbe4e617c00d57b9176f3ea14587e2a46770e1d8f7b138cd5090896dc86a4` |
| `research_scope` | `93f8baa6b952e9c1b430e8f7bbab670a1b3f5fdb720e399a35c8cbdc4b4cbce0` | `972f80ab11e7ac158bf181985843015d8474e1c8d230f446b08a5e0beca4ea35` |
| `work_summary` | `e44f37f515c421d64b145d38376920750491402926041ae56c072772932bf3a0` | `e36ddf4722bb24ed415596067fe05d4a8f61ae16f7a8ea43a40931cce3ff21fb` |

### `review_planning_B`
- Keys compared: 5; changed keys: 5; canonical-only keys: []; alias-only keys: []

| Changed key | Canonical value SHA256 | Alias value SHA256 |
|---|---|---|
| `broader_review_uses` | `07ae7a68d60c5452ea43c7c269819a41c68c049a3bed5782b5a84355353b53d0` | `f2d9ac136ba79e908c63e5228f1f2147d62673c8120ea2aef537ddaa0bcd33a3` |
| `facet_contributions` | `04b1ead3178be889b42e8377a879936aa9e32abeb9979f6b1bcdd9181a117a9b` | `6598cfccf22ac426e681b18d0e17c338138119e084562716e240c2a1b24760fe` |
| `planning_summary` | `b46f523188dc9096700dc3b408be10fac1832308d1a14b9b1b8d2dd87d9b7b7c` | `bb1ae480b98ea6a49872b08f189092aace0633d4f09fdee8174cf3d83891e0bd` |
| `scope_interpretation_cautions` | `e2e4cc61638d139da4c7cb9d11d2bc5f4619548bffda72d93a71e9d18f8cea9a` | `b71a225f5bce56a757d2f34f3cb3489e2ba8cd2f4939bb220df51f6afc1fa91b` |
| `topic_handles` | `d05f36c83307bb1266f6b145f5fee8d7255be5673b349c1e6bab2003a73511a3` | `1a30952e426bc52cb208cd9513eb746f712dfae799224ba4141434a92c1b9101` |
