# Planning literature supplements

`planning_supplement.py` adds one bounded, gap-focused source set to a derived planning pool. It reads the existing M1 plan and pool without editing either. A supplement can use planner-supplied targeted queries, explicitly selected existing plan facets, known papers for a material upgrade, or reviewed references for a direct-source lookup.

## Request

Save one UTF-8 JSON request. Paths are resolved relative to the request file unless absolute. Query `facet_id` values must exist in the referenced M1 plan; an empty `targeted_queries` array does not create a generic query. `reuse_plan_facet_ids` opts into the plan's existing queries for only those facets.

```json
{
  "schema_version": "optomind.planning_supplement.request.v1",
  "request_id": "external-validation-2026-09",
  "topic_id": "predictor-review",
  "gap_id": "G-EXTERNAL-VALIDATION",
  "gap_question": "Does the predictor generalize to an external cohort?",
  "success_criteria": ["Direct study-level evidence from an external cohort"],
  "base_pool_path": "path/to/PLANNING_POOL.jsonl",
  "plan_path": "path/to/PLAN.json",
  "targeted_queries": [
    {"query_text": "predictor external validation cohort", "query_type": "keyword", "facet_id": "F1"},
    {"query_text": "How is predictor performance externally validated?", "query_type": "question", "facet_id": "F1"}
  ],
  "reuse_plan_facet_ids": [],
  "known_papers": [],
  "reviewed_references": [],
  "limits": {"max_candidates": 20, "max_acquisitions": 2, "per_query_limit": 20}
}
```

Both a plain validated query plan and the formal planning-run wrapper (`{"plan": ..., "status": ..., ...}`) are accepted. For a wrapper, the status must be `ok`, and any repair or refused-query audit blocks the supplement. The wrapper status, plan/prompt hashes, validator fingerprint, transport/degradation state, and refused-query audit are preserved in preflight output and `PLAN_SOURCE_AUDIT.json`.

`known_papers` and `reviewed_references` are arrays of identity records. Each needs at least one canonical/provider ID, DOI, CorpusId, OpenAlex ID, or arXiv ID. A reviewed reference can be supplied with ordinary bibliography fields (`citation_text`, title, DOI, year), a `review_claim`, and a review snapshot/card path. `reference_id` and `reviewed_source_unit_id` are optional; short local IDs are filled in when they are absent. This path uses the review-reported account with its attribution and bibliography and does not acquire the cited paper's material or require a quote match. Missing bibliography details can be completed later. Limits are bounded: at most 3 queries, 50 candidates per run, 8 material acquisitions, and 50 results per query. Defaults are 20, 2, and 20.

## Run

Preflight performs local schema/path validation only:

```powershell
python scripts/upgrade3/planning_supplement.py --request outputs/upgrade3/planning_supplements/REQUEST.json --preflight
```

An explicit `--run` enables retrieval, material acquisition, A/B card generation, and one task-specific Qwen usefulness edit per usable source. The editor receives the captured reading material and card B as orientation and returns direct contribution, useful supporting material, and the remaining gap. Useful material is retained even when the main gap stays open. Supply the existing shared ledger and its finite total limit; the A/B runner and editor use the same limit and ledger. The CLI has no unlimited-budget default.

```powershell
python scripts/upgrade3/planning_supplement.py `
  --request outputs/upgrade3/planning_supplements/REQUEST.json `
  --run `
  --key-file api_keys/qwen-api-key.txt `
  --budget-ledger outputs/upgrade3/shared_budget.sqlite `
  --budget-limit-cny 35
```

When a request contains only `reviewed_references`, with no targeted/reused queries or known papers, `--run` is a local citation-material operation. It requires no Qwen key or budget ledger and reports `network_call: false` and `model_call: false`.

Retrieval uses the existing Semantic Scholar gateway, with the existing OpenAlex backend as a bounded paper-search fallback; snippet queries stay on Semantic Scholar. Existing `MaterialAcquirer` and A/B card code handle material and card generation. The editor reads the acquired snapshot and uses card B for orientation. It returns practical writing material and a separate gap status; there is no independent evidence-index or quotation gate in the normal workflow. A useful contribution can remain available while the main gap stays partial or unmet. The prompt asks it to keep findings attached to the studies and conditions described in the supplied material and to leave unspecified details open rather than inventing them.

## Outputs and interpretation

The new output directory contains the unchanged `BASE_PLANNING_POOL.jsonl`, derived `PLANNING_POOL.jsonl`, `SUPPLEMENT_INDEX.json`, the normalized request, `SUPPLEMENT_MATERIALS.md`, search hits, and one directory per source unit. The Markdown report is the convenient writing handoff: it groups useful and supporting material with each source, and shows open points separately. Reviewed references produce `CITATION_ANCHORS.jsonl` and a review-attributed section in the Markdown report without material acquisition. `material_ready` indicates that usable content was returned; it does not mean the whole gap is fulfilled. Existing pool rows and cards remain available alongside the added material.

For review-reported citation material, the anchor, `SUPPLEMENT_INDEX.json`, aggregate judgment, and CLI output expose `material_ready: true` and `planning_use: equal_use` when a review account is supplied. It has the same planning/writing use as an original-source item for that attributed claim; the original full text need not be acquired. `substantive_gap_status: unassessed` keeps citation readiness separate from whether the scientific gap is fulfilled. For acquired sources, `material_ready` can separately expose useful material even when the main gap remains partial or unmet.

An identity match currently prevents duplicate pool rows and preserves lineage, but it does not yet test an existing card/snapshot for whether that specific gap is already covered before material acquisition. The M5 planning layer should perform that full-content sufficiency check and return `gap_already_covered` before issuing a supplement request.

The older offline quote-matching revalidation route remains available for historical outputs. It is not part of the normal practical-material workflow.

```powershell
python scripts/upgrade3/planning_supplement.py `
  --output-dir outputs/upgrade3/planning_supplements/REQUEST_GAP `
  --revalidate-judgment
```

`fulfilled`, `partial`, and `unmet` describe the editor's overall gap judgment. Read `material_ready` and the writing materials separately: a useful contribution can be carried forward even if the answer remains incomplete. `retryable_provider_outage` and `provider_access_error` preserve provider failure as an operational issue, not evidence of scientific absence. Abstract/snippet-only sources retain their explicit material-depth label.

Offline acceptance:

```powershell
python -m pytest tests/upgrade3/test_planning_supplement.py -q
```

The v2 supplement-judge re-judgment limitation and the combined v8 round-two directed-reading results are recorded in the [2026-09-23 quality checkpoint](directed_reading_quality_checkpoint_20260923.md).
