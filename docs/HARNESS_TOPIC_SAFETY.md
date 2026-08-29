# Research Harness Topic-Safety and Fail-Closed Contract

This document records reusable runtime safeguards. They are pipeline
invariants, not repairs for one test topic.

## Failure that motivated the contract

A natural-language run once continued after both Query Planner model calls
failed. Its deterministic diagnostic fallback was automatically confirmed.
The Review Lead then saw generic optical terms plus a historical radiative
cooling knowledge base and designed a radiative-cooling article for an
achromatic-metalens question. Structural validators passed because they did
not check semantic identity. Downstream workers therefore spent most of the
run budget on a coherent but wrong article.

## Mandatory safeguards

1. DashScope HTTP 400 responses are parsed for provider error codes. Account,
   quota, arrearage, and free-tier failures rotate API keys before changing
   models.
2. Only `primary_valid`, `repaired_by_format_model`, and an explicitly
   provided human-confirmed plan are execution-ready. A deterministic Query
   Planner fallback is diagnostic and can never enter retrieval or writing,
   even when automatic confirmation was requested.
3. A valid English plan creates `TOPIC_IDENTITY.json`. This sidecar preserves
   the scientific object without changing the strict Query Planner schema.
4. Review Lead corpus concepts are filtered by the topic contract. An
   unrelated historical corpus may report counts but cannot inject its themes.
5. Deterministic topic gates run after blueprinting, section coverage,
   section writing, final review assembly, visual planning, and research-plan
   generation. Expensive downstream work is blocked when the scientific
   object is missing.
6. Three identical validation failures trigger a circuit breaker. The worker
   reports `validation_failed` instead of repeatedly paying for the same
   unsuccessful action.
7. All gates write auditable JSON artifacts and observability events. They
   never silently rewrite scientific content or pretend that unrelated
   evidence is relevant.

## Operator interpretation

- `needs_model_recovery`: restore a working key/model and rerun Query Planner.
- `needs_query_plan_revision`: the confirmed plan lacks a stable scientific
  object.
- `semantic_drift_blocked`: blueprinting changed the scientific object.
- `needs_more_literature` at the coverage topic gate: retrieved material is
  insufficiently aligned; do not draft from it.
- `validation_failed` with
  `repeated_identical_validation_failure_circuit_breaker`: inspect the first
  validator error and repair its root cause before resuming.

These states are successful safety outcomes. They prevent a polished wrong
answer and protect API budget.
