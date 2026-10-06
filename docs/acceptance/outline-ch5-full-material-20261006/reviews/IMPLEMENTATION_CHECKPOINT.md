# Ch5 full strengthening offline checkpoint

Status: prepared offline; no paid call started.

## Scope and request

- Source checkout at preparation: `a0b645e096a148382196b5aadd3dcf36c43c69d6` (working tree changes are recorded in the source checkout; this output does not alter them).
- Chapter: Ch5, five editable units (`Ch5_U01` through `Ch5_U05`) with seven read-only adjacent roles from Ch4 and Ch6.
- Input: 34 source records, 8 candidate records, 6 tool records, 75 source identities, and the assembled Ch5 body. No root review, prior candidate answer, or manual scientific issue list is in the input.
- Stable derived unit mapping and source provenance: `ch5_full_material/UNIT_ID_MAP.json` and `ch5_full_material/PROVENANCE.json`.
- Full Max messages: `ch5_full_material/FULL_MAX_MESSAGES.json`.

The full-material Max request estimates 331,374 prompt tokens, 65,536 output tokens plus 32,768 thinking budget (98,304 maximum completion), 477,635 total context tokens, and 8.090916 CNY under the configured conservative pricing. The one-million-token qwen3.8-max context assumption accommodates the whole chapter, so the selected preparation is one full-chapter Max request rather than a split run. The demand upper bound is 11.416564 CNY including its Plus access pass and one possible Max continuation; its serialized owner request is larger than the full request.

## Engineering checks

- Access messages state the editable scope and preserve read-only-only evidence with an owner-visible diagnostic; no automatic reselection occurs.
- Model-visible material records are deduplicated by complete record and nested selected records point to the selected parent JSON record. No material content is summarized or truncated.
- Formal on-demand CLI passes the effective profiles and `output/stages` checkpoint directory to the production runner and resumes only when request, payload, catalog, model, budgets, and profile signatures match. Incompatible stages are preserved under `stages/history`.
- Test result: `31 passed` for `tests/upgrade3/test_outline_strengthening.py` and `tests/upgrade3/test_material_visibility_catalog.py`; Python compilation also passed.

The next action is a separate root-reviewed paid boundary. This checkpoint itself contains no model response or charge.
