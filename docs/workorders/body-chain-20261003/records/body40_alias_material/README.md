# BODY40 explicit alias material consumption — second stop

Status: **READY_FOR_LOCAL_BODY40_ALIAS_MATERIAL_02** after bounded offline verification. Stop for local acceptance; no scientific-quality certification.

Independent branch: `body40-alias-material-cloud-20261005`.
Base: accepted first-stop tip `e75c66c1ffd018a93636960caa7c51e74911c2af` (source repair `222c01ea4e9d6f406d507d994f7952135183fdb6`). Final source commit will be recorded in `PUBLICATION.json`. No original branch is modified or merged.

## What the evidence supports

The public archived Ch6 catalog explicitly declares P0605 as an alias of P0049. The same canonical entry already contains A/B and supplemental material. The writer previously performed exact-key lookup, adding a false `missing_material` row for P0605. The repaired consumer resolves the explicit relationship, emits one canonical material row, preserves original task/source labels, and recognizes either label in citation diagnostics and completion.

This is not an owner-material loss finding. The actual saved owner `cache_inputs` contains both identities through a shared multi-source tool narrative; that narrative is retained in the final packet. Read `OWNER_ALIAS_TRACE.md` for exact paths and hashes, and for the distinction between cached payload and provider message. No owner, planner, case or arrangement algorithm needs changing for this defect.

## Minimal production scope

Only `optomind_research/runtime/upgrade3/review_unit_writer.py` changes:

- Validate and consume the catalog's explicit alias declarations. No same-title/DOI inference is added to the writer.
- Resolve alias-only or canonical-plus-alias requests to one canonical material row, including old cached arrangements with no available packet file.
- Retain original paragraph IDs, source briefs, task source uses and requested labels. Different historical record IDs with an equal normalized DOI can be valid declared duplicates; explicit incompatible identities cannot silently merge.
- Reject duplicate alias owners, a separate catalog key colliding with an alias, self aliases and malformed declarations conservatively. Even apparently matching direct duplicate keys require upstream normalization rather than arbitrary consumer selection.
- Apply the same identity group to normal/completion citations, unused-source reporting and source-linked completion tools in both directions. Requested tasks remain scoped; unrelated tools are not pulled in.
- Correct `source_count` to count material identities once rather than counting the same source's requested aliases. This is accounting, not evidence of changed scientific coverage.
- A narrow false-empty warning correction recognizes nonempty usable supplemental/local/tool narrative. Metadata alone is not evidence; review-reported original studies remain eligible without their own A/B or full text.

Prompt wording, BODY order, source routing, owner revision/recovery, formal case append and arrangement are unchanged. Message **data** changes where alias material replaces false missing and completion identity metadata becomes correct; full message byte equality is neither expected nor claimed. First-stop numeric uncertainty and historic-handle isolation remain intact.

## Actual replay and controlled validation

`ACTUAL_CONSUMER_REPLAY.json` and `replay_archived_consumer.py` compare the accepted baseline with the repair for both archived Ch6_U1 and Ch6_U2. They record material/message metadata and hashes, never duplicate paper passages or scientific manuscript text. Canonical A/B/supplement fields remain unchanged; the false missing alias disappears and the material is not duplicated. Original task IDs and source-use relationships are checked.

The archived Windows card/packet paths are unavailable in this Linux executor. A narrowly scoped replay adapter reports exactly those foreign locator paths as unavailable for **both** baseline and current runs, avoiding an existing Linux ENAMETOOLONG interpretation of long Windows path strings. It neither supplies substitute scientific material nor changes production path handling. Real temporary JSON-card I/O is tested separately, including reading canonical material once. This is not a native Windows run, reconstructed original-provider request, or new generation.

Synthetic producer→catalog→writer tests cover alias-only, both orders, explicit conflict, missing alias, restored owner brief, completion, actual output persistence, usable review-reported original material and metadata-only negatives. Fixed offline model returns verify text preservation. A reviewer found and closed a one-way completion-tool selection gap before publication; `INDEPENDENT_REVIEW.md` records it.

Final bounded aggregate: **557 passed** (`FINAL_CONTROLS.txt`); diff-check and compilation passed. The unchanged first-stop baseline has 539 passing scoped tests (`BASELINE_CONTROLS.txt`). These are bounded overlapping controls, not the complete repository suite or a quality score.

From repository root with installed dependencies:

```bash
PYTHONHASHSEED=0 python docs/workorders/body-chain-20261003/records/body40_alias_material/run_controls.py
python docs/workorders/body-chain-20261003/records/body40_alias_material/replay_archived_consumer.py
```

This cloud executor used lightweight dependencies in `/tmp/optomind-body40-deps` and the documented first-stop offline bootstrap:

```bash
PYTHONHASHSEED=0 PYTHONPATH=docs/workorders/body-chain-20261003/records/body40_identity_citations/offline_bootstrap:/tmp/optomind-body40-deps:. python docs/workorders/body-chain-20261003/records/body40_alias_material/run_controls.py
```

The bootstrap denies network and bypasses only the unrelated eager AgentScope runtime facade. Actual upgrade3 producers, consumers, files and message builders run. This is not full installed-application integration. Prompt/order checks, compile and diff-check results are recorded separately.

## Restore/start/selection contract

- Use this source tip, not a mixed baseline; `PUBLICATION.json` identifies the complete source commit.
- This repair executes at material/message consumption. Existing compatible arrangement exports can be re-read without calling a model. Rebuild UNIT_INPUT/UNIT_MESSAGES from the selected final arrangement; reusing an old serialized writer request does not apply the new consumer logic.
- For this archive, explicitly select `body/body_assembly_final/arrangements/Ch6/CHAPTER_ARRANGEMENT.json` and `body/arrangement_repaired/Ch6/ARRANGEMENT_INPUT.json`. The full-run batch historically selects repaired Ch2/Ch3 results and live results for other chapters, as documented in the inherited first-stop record and BODY40 `BATCH_JOBS.json`. Do not guess snapshot choice from the latest file timestamp.
- The historical execution order remains provisional scope → level1 tools/outline → routing/proposals → harmonization → level2 tools → scope finalization → chapter needs/tools → details → global coordination/owner revision → formal case append → arrangement → writer → assembly. The original global/owner coordination remains before formal case append. No new stage, cache migration or automatic paid retry is introduced. Historical scientific prose containing wrong identities remains historical; no global P substitution.
- Current tests operate on selected units only. No full BODY was generated or reassembled in this stop. No budget/provider configuration is required.

## Local acceptance priorities and remaining limits

1. With original local assets, rebuild normal and completion messages for the two affected units from the exact final arrangement. Confirm one canonical material row, actual usable content, original tasks and both accepted citation identities. Compare actual input content, not only counts or flags.
2. Exercise a contradictory alias identity and an undeclared handle: neither may borrow another paper's material. Check a review-reported original with only usable tool narrative remains available without own A/B/fulltext.
3. Keep the first-stop real-response replay protection for [11] and Q01. No claim that old erroneous cache prose has been corrected.
4. The existing producer keeps the canonical A/B snapshot when merging duplicate records; distinct alias A/B snapshots remain in the original packet but are not separately emitted. This pre-existing policy is documented, not silently broadened into alternative-interpretation merging.
5. The existing locator-card check conservatively rejects a different nonempty paper ID even when DOI matches. Canonical catalog material survives; alias-card extras can remain ignored with an explicit conflict note. This inherited policy was not changed here and may merit a separate decision if local evidence shows important extras withheld.
6. The native first-stop archive f60dc3f3f9cf86eb6eb638b17ab7549877f9f838 arrived before publication and was directly crosschecked; see `NATIVE_ACCEPTANCE_CROSSCHECK.md` and the dated update in `LOCAL_ACCEPTANCE_ALIGNMENT.md`. This does not constitute native execution of the second-stop repair or scientific quality certification.

No paid model call, research retrieval/download, original BODY editing, full planning/writing, scientific prompt revision, protected-branch modification or merge. Metadata-only evidence avoids republishing real scientific text. Stop here for local GPT 6.1 sol acceptance.
