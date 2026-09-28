"""Cross-domain reading guidance, independent of any acceptance paper."""

READER_DETAIL_GUIDANCE = """
DEPTH AND COVERAGE WORKSHEET
Do not begin with a short list of headline findings. Begin the JSON with paper_map as
a post-reading worksheet, then content_units, then the remaining arrays. In paper_map,
include one row for each applicable dimension below, with category, material_status
(reported|not_in_available_material|uncertain), text, and content_unit_ids. The text
must enumerate the actual details found, not merely state that this category exists.
Each reported detail must also be retained in source-bound content_units; the
worksheet is navigation, never a substitute for evidence units.

- design_scope: population/material/system, study design and outcome definitions;
- protocol: intervention/comparator, dose or algorithm settings, timing and controls;
- acquisition: sampling/data acquisition, preservation and inclusion/exclusion;
- processing: assay/platform, preprocessing, filters, thresholds and normalization;
- analysis_sets: each distinct cohort/dataset, subgroup denominators, repeated
  samples versus independent participants, timepoints and analysis populations;
- result_conditions: analysis/test, effect/direction, uncertainty and significance
  separately for each measurement method and cohort, with negative results;
- interpretation_limits: confounding, exceptions, applicability, evidence origin.

Adapt these dimensions to the field. Do not fabricate biomedical protocols for an
algorithm paper. For abstract-only material, unreported details are unknown from
the abstract, not absent from the study. A dimension may require multiple units;
there is no maximum or target unit count. Retain reproducibility details and
conditions even when they do not answer a Facet. Before finishing, cross-check
results prose against captions, tables and methods. Two methods with the same
direction need not have the same statistical significance. Preserve exact p-values
and the authors' uncertainty where available; never merge them into 'both confirm'.

FACET COVERAGE IS A CONJUNCTION
For every Facet, explicitly consider the requested object, comparison, setting,
outcome and evidence strength. Record missing dimensions in conditions_and_boundaries.
Use addressed only when the material meets the question's essential dimensions.
Otherwise retain the useful bounded evidence as partly_addressed, or explain what
the available material cannot answer. An experiment on a related object, or an
empirical proxy for a requested formal guarantee, is not a complete answer.
Distinguish 'this cohort shows an association' from predictive validation and
'the authors claim a guarantee' from the material establishing that guarantee.

Keep model_interpretations empty unless an interpretation adds useful, explicitly
bounded reasoning. Do not speculate about author intent or turn absence of a
correlation into a demonstrated causal/local mechanism. Do not strengthen a
tentative source conclusion in either a content unit or a Facet summary.
"""

VERIFIER_SOURCE_GUIDANCE = """
The full_available_source_blocks bank contains ALL supplied research text, once.
Target source_context_blocks and source_anchors are navigation references into
that bank, not the limit of your evidence access. Locate their original blocks,
then cross-check relevant methods, result paragraphs, figure/table captions and
qualifying discussion elsewhere in the bank. A reader-selected paragraph may omit
a crucial condition or a nonsignificant validation result. Do not accept a claim
just because one selected paragraph sounds supportive.

Review the exact target.statement, context and quantities. Only targets explicitly
marked facet_coverage_judgment ask whether the coverage LABEL suits the question.
Other targets ask only whether the actual statement is supported; do not silently
append a broader question to a bounded statement. 'Partly addressed' is a coverage
label, never a reason to downgrade a truthful statement's evidence support.
For coverage judgments, supported_as_report means the assigned label and stated
boundaries are appropriate; it does NOT mean the paper answers the entire question.
Review assumptions and uncertainty in interpretations; merely labeling speculation
an inference does not make it supported.

The chosen source anchors must support the claim. If another source block in the
bank supplies missing support or a qualification, identify that block in your reason
and flag the existing source selection as partly_supported until corrected. Do not
silently bless an irrelevant citation because the fact occurs elsewhere in the paper.
For each target, return target_id, then a reason of at most TWO concise sentences,
then the final status. The final status must agree with the reason. Do not write
deliberation, self-correction or debate in any field. Include every target exactly
once in {"reviews": [...]}; do not omit later targets to explain earlier ones.
"""
