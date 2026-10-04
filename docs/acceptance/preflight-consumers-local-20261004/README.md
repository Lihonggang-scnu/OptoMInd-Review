# Preflight consumer acceptance: local real handoff

This is a small public evidence package for the first-stop local F1–F3 acceptance on 2026-10-04 at commit `b423646db1dbcf92831373b64563318737696fee`. It records engineering behavior and the actual bounded model handoff. It does not claim full BODY quality or scientific correctness.

Read in this order:

1. `ASTRA_ACCEPTANCE.md` — root verdict and scientific-content limits.
2. `LOCAL_OFFLINE_ACCEPTANCE.md` — focused commands, 164 offline controls, and selected recovery disk/message evidence.
3. `LOCAL_REAL_PROBE.md` — actual F1/F3 result, model settings, cost, and blockers.
4. `real_f1/RESULT.json`, `TASK_COMPARISON.json`, and `MESSAGE_COMPARISON.json` — old actual CH02:U3 fresh/resumed task and writer-message comparison.
5. `real_f3/CONFIG.json`, `INPUTS.json`, `COST.json`, and `TOOL_RESULT.md` — sanitized real input and answer handoff.
6. `real_f3/COMPACT_TOOL_FEEDBACK.json` — the actual compact tool-feedback fields with the AI answer and source identities; passage bodies and paths are absent.
7. `real_f3/LEVEL1_PAYLOAD_PROJECTION.json` — the actual `research_question`, `provisional_scope`, and compact tool-feedback projection; the remaining B summaries are explicitly omitted.
8. `real_f3/OUTLINE_RESPONSE.json` — raw provider-generated level1 outline response without transport or source fulltext.
9. `real_f3/BOUNDARIES.md`, `STAGE_MESSAGES.json`, and `SYSTEM_MESSAGE.md` — honest content limits and safe stage-message evidence.
10. `offline/F1_BLOCKED_CONTRACT_FAILED.json` and `offline/F3_LOCAL_RETRY_PARTIAL.json` — two selected recovery/partial-condition records, not the replay tree.
11. `PROVENANCE.md`, `SECURITY_SCAN.md`, and `MANIFEST.md` — omissions, public-safety checks, and file index.

The F1 real unit is one-to-one, so its `portion` comparison is explicitly not applicable. Split/merge behavior is represented only by the selected offline evidence. F3 used a matching 12-row B subset, a real local adaptive judge, and the normal `qwen3.5-plus` level1 stage with `allow_external=false`. The adaptive judge settled two provider calls internally, and the outline settled one, for three provider calls total and `+0.0372138 CNY`.

The original provisional context in the level1 payload was prepared by the probe driver from the original PLAN and selected B summaries; it was not a new model-generated research plan. The local answer is intentionally shown as partial. It retained a PDAC microbiota-depletion/anti-PD-1 mouse-model route and patient-to-mouse transfer conditions, while preserving the absence of PDAC patient-level prospective evidence. The outline adopted that boundary but also broadened some unmet local requirements into field-level gaps and stated that FMT-LUMINate results were not fully published. Review those limitations before using any generated content.

The suggested next stop is F4A/F4B identity and history compatibility together with the first-case deep-reading handoff. Reproduce the case-cache behavior before deciding whether a code change is needed.

No production code, prompts, source outputs, databases, fulltext, PDFs, credentials, or Git refs were modified by this packaging task.
