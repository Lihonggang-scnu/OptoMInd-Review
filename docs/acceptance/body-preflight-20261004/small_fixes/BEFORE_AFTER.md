# Offline fixes: concise before/after

## Citation consumer

Before: the real writer body used explicit [参考P...] markers, while the strict writer/assembly consumer recognized zero sources. After the two narrow consumer changes, the same exported body identifies all seven handles (P0576, P0602, P0564, P0561, P0388, P0190, P0582) and renders standard numeric markers. The body hash is unchanged, standard citations and links remain protected, unknown handles remain reported, and the raw model response is untouched. Validation: 74 focused citation tests passed; paid/model calls: 0.

## Feedback material export

Before: the actual FeedbackLoop.arrangement_runner export omitted chapter_tool_materials, so downstream build_unit_view produced no tool material for either unit. After: the one-line export uses the existing compact chapter projection. U01 keeps one multi-source answer from P0001 plus P0002; U02 keeps only its P0002 answer; cross-unit isolation is true. Validation: 15 focused feedback tests passed; paid/model calls: 0. The repair preserves source identity and the existing material policy.
