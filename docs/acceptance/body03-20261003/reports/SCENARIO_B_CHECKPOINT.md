# Scenario B paid checkpoint

Status: awaiting root approval. No Scenario B paid call has been made.

The bounded question is:

> Which PDAC microbiota to immune-checkpoint routes in the existing local
> materials are source-bound enough to support a bounded mechanism paragraph,
> including model or cohort conditions and the patient-level limit?

The first request uses `gap_id=WO03-G-P0004`, `facets=[]`, and an empty
`targeted_queries=[]` list. Its required output is:

- `WO03-O-MECHANISM`: route, immune change, checkpoint outcome, and evidence
  condition.

The changed-output request retains `WO03-O-MECHANISM` and adds:

- `WO03-O-BOUNDARY`: PDAC-specific boundary and explicit patient-level limit.

The production wiring is local-first. The supplement adapter keeps
`allow_external=False`, so there are zero external search, acquisition, card,
or extraction calls. The retrieval loop itself has `allow_external=True` only
so its real Qwen3.7-Flash empty-query refiner can run if local triage leaves
the need unresolved. If local triage returns `direct_use`, the initializer is
correctly bypassed; the live report records that outcome and does not claim an
initializer call.

The expected call shape is zero to four Qwen3.7-Flash judgments/refinements:
one initial local triage, zero or one empty-query refinement in the usual local
answer path, zero for the same-answered-need rebind when reuse is valid, and
one local triage with zero or one refinement for the changed-output phase. The
existing three-phase loop permits up to nine model attempts in the unresolved
case; actual counts come from shared-ledger reservation deltas. Retrieval-loop
bounds remain three rounds, two empty rounds, and three queries per round.
External provider calls remain zero.
The shared ledger is unchanged by this checkpoint at
`0.5509316 CNY` actual and `0 CNY` reserved; the provisional B ceiling remains
`5 CNY` within the existing `30 CNY` ledger.
