# Final before/after outline comparison

This is an offline comparison of the original complete candidate and the final merged candidate. Exact source payloads remain in the JSON files; this document lists every stable unit ID and field-level differences.

- Original: `F:\OptoMind-Review-2\outputs\outline_selection_20261006\FULL_CHAPTER_PAYLOADS.json` (SHA256 `8c609a41af3079d26cb70de2e26ee70db6ff1716b1efe7efd92e1651b09a0bd5`)
- Final: `F:\OptoMind-Review-2\outputs\outline_selection_20261006\on_demand_group3_ch1\CURRENT_CHAPTER_PAYLOADS.json` (SHA256 `52b4da0cd32f4a4e81025f3a7d175489033928b6866754cccd8f32248432a3d3`)
- Scope: 7 chapters, 29 units; selector selected 13, leaving 16 unselected.
- Unselected units exact-preserved: `True`; chapter metadata and plan metadata preserved: `True`; original material values losslessly retained: `True`.

## Ch1

Units: 4 → 4; order preserved: `True`.

| Unit | Selection | Result | Semantic paths | Format-only paths |
|---|---|---|---:|---:|
| `U1_跨癌种关联图谱` | no | exact unchanged | 0 | 0 |
| `U2_外部干扰与宿主背景` | yes | changed | 41 | 0 |
| `U3_动态监测与纵向演变` | no | exact unchanged | 0 | 0 |
| `U4_证据阶梯与转化边界` | yes | changed | 39 | 0 |

Changed field paths:
- `U2_外部干扰与宿主背景`: semantic `U2_外部干扰与宿主背景.argument_relations, U2_外部干扰与宿主背景.ordered_development, U2_外部干扰与宿主背景.paragraph_briefs[0].development, U2_外部干扰与宿主背景.paragraph_briefs[0].finding_conditions, U2_外部干扰与宿主背景.paragraph_briefs[0].point, U2_外部干扰与宿主背景.paragraph_briefs[0].source_handles[1], U2_外部干扰与宿主背景.paragraph_briefs[1].development, U2_外部干扰与宿主背景.paragraph_briefs[1].finding_conditions, U2_外部干扰与宿主背景.paragraph_briefs[1].point, U2_外部干扰与宿主背景.paragraph_briefs[2].development, U2_外部干扰与宿主背景.paragraph_briefs[2].finding_conditions, U2_外部干扰与宿主背景.paragraph_briefs[2].point, U2_外部干扰与宿主背景.substantive_point, U2_外部干扰与宿主背景.supporting_studies[0].contribution, U2_外部干扰与宿主背景.supporting_studies[0].paper_id, U2_外部干扰与宿主背景.supporting_studies[0].source_handle, U2_外部干扰与宿主背景.supporting_studies[1].contribution, U2_外部干扰与宿主背景.supporting_studies[1].paper_id, U2_外部干扰与宿主背景.supporting_studies[1].source_handle, U2_外部干扰与宿主背景.supporting_studies[2].contribution, U2_外部干扰与宿主背景.supporting_studies[2].paper_id, U2_外部干扰与宿主背景.supporting_studies[2].source_handle, U2_外部干扰与宿主背景.supporting_studies[3].contribution, U2_外部干扰与宿主背景.supporting_studies[3].paper_id, U2_外部干扰与宿主背景.supporting_studies[3].source_handle, U2_外部干扰与宿主背景.supporting_studies[4].contribution, U2_外部干扰与宿主背景.supporting_studies[4].paper_id, U2_外部干扰与宿主背景.supporting_studies[4].source_handle, U2_外部干扰与宿主背景.supporting_studies[5].contribution, U2_外部干扰与宿主背景.supporting_studies[5].paper_id, U2_外部干扰与宿主背景.supporting_studies[5].source_handle, U2_外部干扰与宿主背景.supporting_studies[6].contribution, U2_外部干扰与宿主背景.supporting_studies[6].paper_id, U2_外部干扰与宿主背景.supporting_studies[6].source_handle, U2_外部干扰与宿主背景.supporting_studies[7].contribution, U2_外部干扰与宿主背景.supporting_studies[7].paper_id, U2_外部干扰与宿主背景.supporting_studies[7].source_handle, U2_外部干扰与宿主背景.supporting_studies[8].contribution, U2_外部干扰与宿主背景.supporting_studies[8].paper_id, U2_外部干扰与宿主背景.supporting_studies[8].source_handle, U2_外部干扰与宿主背景.transition`; format-only `none`.
- `U4_证据阶梯与转化边界`: semantic `U4_证据阶梯与转化边界.argument_relations, U4_证据阶梯与转化边界.ordered_development, U4_证据阶梯与转化边界.paragraph_briefs[0].development, U4_证据阶梯与转化边界.paragraph_briefs[0].finding_conditions, U4_证据阶梯与转化边界.paragraph_briefs[0].point, U4_证据阶梯与转化边界.paragraph_briefs[0].source_handles[0], U4_证据阶梯与转化边界.paragraph_briefs[0].source_handles[1], U4_证据阶梯与转化边界.paragraph_briefs[1].development, U4_证据阶梯与转化边界.paragraph_briefs[1].finding_conditions, U4_证据阶梯与转化边界.paragraph_briefs[1].point, U4_证据阶梯与转化边界.paragraph_briefs[1].source_handles[0], U4_证据阶梯与转化边界.paragraph_briefs[2].development, U4_证据阶梯与转化边界.paragraph_briefs[2].finding_conditions, U4_证据阶梯与转化边界.paragraph_briefs[2].point, U4_证据阶梯与转化边界.paragraph_briefs[2].source_handles[0], U4_证据阶梯与转化边界.paragraph_briefs[2].source_handles[2], U4_证据阶梯与转化边界.substantive_point, U4_证据阶梯与转化边界.supporting_studies[0].contribution, U4_证据阶梯与转化边界.supporting_studies[0].paper_id, U4_证据阶梯与转化边界.supporting_studies[0].source_handle, U4_证据阶梯与转化边界.supporting_studies[1].contribution, U4_证据阶梯与转化边界.supporting_studies[1].paper_id, U4_证据阶梯与转化边界.supporting_studies[1].source_handle, U4_证据阶梯与转化边界.supporting_studies[2].contribution, U4_证据阶梯与转化边界.supporting_studies[2].paper_id, U4_证据阶梯与转化边界.supporting_studies[2].source_handle, U4_证据阶梯与转化边界.supporting_studies[3].contribution, U4_证据阶梯与转化边界.supporting_studies[3].paper_id, U4_证据阶梯与转化边界.supporting_studies[3].source_handle, U4_证据阶梯与转化边界.supporting_studies[4].contribution, U4_证据阶梯与转化边界.supporting_studies[4].paper_id, U4_证据阶梯与转化边界.supporting_studies[4].source_handle, U4_证据阶梯与转化边界.supporting_studies[5].contribution, U4_证据阶梯与转化边界.supporting_studies[5].paper_id, U4_证据阶梯与转化边界.supporting_studies[5].source_handle, U4_证据阶梯与转化边界.supporting_studies[6].contribution, U4_证据阶梯与转化边界.supporting_studies[6].paper_id, U4_证据阶梯与转化边界.supporting_studies[6].source_handle, U4_证据阶梯与转化边界.transition`; format-only `none`.

Material retention (original values are checked as recursive subsets, so accepted-material enrichment cannot erase original A/B/reading/tool/local values):
- `source_materials`: exact=`False`; original=56, final=57; original identities retained=`True`; original values lossless=`True`.
- `candidate_materials`: exact=`True`; original=1, final=1; original identities retained=`True`; original values lossless=`True`.
- `tool_materials`: exact=`True`; original=2, final=2; original identities retained=`True`; original values lossless=`True`.
- `candidate_navigation`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
- `source_identity_map`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.

## Ch2

Units: 4 → 4; order preserved: `True`.

| Unit | Selection | Result | Semantic paths | Format-only paths |
|---|---|---|---:|---:|
| `Ch2_U01` | no | exact unchanged | 0 | 0 |
| `Ch2_U02` | no | exact unchanged | 0 | 0 |
| `Ch2_U03` | no | exact unchanged | 0 | 0 |
| `Ch2_U04` | no | exact unchanged | 0 | 0 |

Changed field paths:
- none

Material retention (original values are checked as recursive subsets, so accepted-material enrichment cannot erase original A/B/reading/tool/local values):
- `source_materials`: exact=`True`; original=367, final=367; original identities retained=`True`; original values lossless=`True`.
- `candidate_materials`: exact=`True`; original=0, final=0; original identities retained=`True`; original values lossless=`True`.
- `tool_materials`: exact=`True`; original=0, final=0; original identities retained=`True`; original values lossless=`True`.
- `candidate_navigation`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
- `source_identity_map`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.

## Ch3

Units: 5 → 5; order preserved: `True`.

| Unit | Selection | Result | Semantic paths | Format-only paths |
|---|---|---|---:|---:|
| `U3_1` | no | exact unchanged | 0 | 0 |
| `U3_2` | yes | changed | 16 | 0 |
| `U3_3` | no | exact unchanged | 0 | 0 |
| `U3_4` | no | exact unchanged | 0 | 0 |
| `U3_5` | yes | changed | 12 | 0 |

Changed field paths:
- `U3_2`: semantic `U3_2.ordered_development, U3_2.paragraph_briefs[0].development, U3_2.paragraph_briefs[1].development, U3_2.paragraph_briefs[1].point, U3_2.paragraph_briefs[2].development, U3_2.paragraph_briefs[2].point, U3_2.paragraph_briefs[3].development, U3_2.paragraph_briefs[3].point, U3_2.substantive_point, U3_2.supporting_studies[0].contribution, U3_2.supporting_studies[1].contribution, U3_2.supporting_studies[3].contribution, U3_2.supporting_studies[4].contribution, U3_2.supporting_studies[5].contribution, U3_2.synthesis, U3_2.transition`; format-only `none`.
- `U3_5`: semantic `U3_5.ordered_development, U3_5.paragraph_briefs[0].development, U3_5.paragraph_briefs[0].point, U3_5.paragraph_briefs[1].development, U3_5.paragraph_briefs[1].point, U3_5.paragraph_briefs[2].development, U3_5.substantive_point, U3_5.supporting_studies[0].contribution, U3_5.supporting_studies[1].contribution, U3_5.supporting_studies[2].contribution, U3_5.supporting_studies[3].contribution, U3_5.synthesis`; format-only `none`.

Material retention (original values are checked as recursive subsets, so accepted-material enrichment cannot erase original A/B/reading/tool/local values):
- `source_materials`: exact=`False`; original=23, final=23; original identities retained=`True`; original values lossless=`True`.
- `candidate_materials`: exact=`True`; original=1, final=1; original identities retained=`True`; original values lossless=`True`.
- `tool_materials`: exact=`True`; original=2, final=2; original identities retained=`True`; original values lossless=`True`.
- `candidate_navigation`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
- `source_identity_map`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.

## Ch4

Units: 3 → 3; order preserved: `True`.

| Unit | Selection | Result | Semantic paths | Format-only paths |
|---|---|---|---:|---:|
| `Ch4_U01` | yes | changed | 17 | 0 |
| `Ch4_U02` | yes | changed | 18 | 1 |
| `Ch4_U03` | no | exact unchanged | 0 | 0 |

Changed field paths:
- `Ch4_U01`: semantic `Ch4_U01.ordered_development, Ch4_U01.paragraph_briefs[0].development, Ch4_U01.paragraph_briefs[0].point, Ch4_U01.paragraph_briefs[1].development, Ch4_U01.paragraph_briefs[1].point, Ch4_U01.paragraph_briefs[1].source_handles[1], Ch4_U01.paragraph_briefs[2].development, Ch4_U01.paragraph_briefs[2].point, Ch4_U01.substantive_point, Ch4_U01.supporting_studies[0].contribution, Ch4_U01.supporting_studies[3].contribution, Ch4_U01.supporting_studies[3].paper_id, Ch4_U01.supporting_studies[3].source_handle, Ch4_U01.supporting_studies[4].contribution, Ch4_U01.supporting_studies[4].paper_id, Ch4_U01.supporting_studies[4].source_handle, Ch4_U01.synthesis`; format-only `none`.
- `Ch4_U02`: semantic `Ch4_U02.cases[2].paper_id, Ch4_U02.cases[2].source_handle, Ch4_U02.ordered_development, Ch4_U02.paragraph_briefs[0].development, Ch4_U02.paragraph_briefs[1].development, Ch4_U02.paragraph_briefs[2].development, Ch4_U02.paragraph_briefs[2].point, Ch4_U02.paragraph_briefs[2].source_handles[0], Ch4_U02.substantive_point, Ch4_U02.supporting_studies[1].contribution, Ch4_U02.supporting_studies[2].contribution, Ch4_U02.supporting_studies[2].paper_id, Ch4_U02.supporting_studies[2].source_handle, Ch4_U02.supporting_studies[3].contribution, Ch4_U02.supporting_studies[4].contribution, Ch4_U02.supporting_studies[4].paper_id, Ch4_U02.supporting_studies[4].source_handle, Ch4_U02.synthesis`; format-only `Ch4_U02.paragraph_briefs[1].point`.

Material retention (original values are checked as recursive subsets, so accepted-material enrichment cannot erase original A/B/reading/tool/local values):
- `source_materials`: exact=`False`; original=121, final=121; original identities retained=`True`; original values lossless=`True`.
- `candidate_materials`: exact=`True`; original=5, final=5; original identities retained=`True`; original values lossless=`True`.
- `tool_materials`: exact=`True`; original=2, final=2; original identities retained=`True`; original values lossless=`True`.
- `candidate_navigation`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
- `source_identity_map`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.

## Ch5

Units: 5 → 5; order preserved: `True`.

| Unit | Selection | Result | Semantic paths | Format-only paths |
|---|---|---|---:|---:|
| `Ch5_U01` | yes | changed | 3 | 2 |
| `Ch5_U02` | yes | changed | 0 | 1 |
| `Ch5_U03` | no | exact unchanged | 0 | 0 |
| `Ch5_U04` | yes | exact unchanged | 0 | 0 |
| `Ch5_U05` | yes | exact unchanged | 0 | 0 |

Changed field paths:
- `Ch5_U01`: semantic `Ch5_U01.paragraph_briefs[2].development, Ch5_U01.paragraph_briefs[2].point, Ch5_U01.synthesis`; format-only `Ch5_U01.supporting_studies[0].contribution, Ch5_U01.supporting_studies[2].contribution`.
- `Ch5_U02`: semantic `none`; format-only `Ch5_U02.supporting_studies[0].contribution`.

Material retention (original values are checked as recursive subsets, so accepted-material enrichment cannot erase original A/B/reading/tool/local values):
- `source_materials`: exact=`False`; original=34, final=34; original identities retained=`True`; original values lossless=`True`.
- `candidate_materials`: exact=`True`; original=8, final=8; original identities retained=`True`; original values lossless=`True`.
- `tool_materials`: exact=`True`; original=6, final=6; original identities retained=`True`; original values lossless=`True`.
- `candidate_navigation`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
- `source_identity_map`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.

## Ch6

Units: 4 → 4; order preserved: `True`.

| Unit | Selection | Result | Semantic paths | Format-only paths |
|---|---|---|---:|---:|
| `Ch6_U1` | yes | changed | 4 | 0 |
| `Ch6_U2` | no | exact unchanged | 0 | 0 |
| `Ch6_U3` | yes | changed | 9 | 0 |
| `Ch6_U4` | yes | changed | 5 | 1 |

Changed field paths:
- `Ch6_U1`: semantic `Ch6_U1.ordered_development, Ch6_U1.paragraph_briefs[2].development, Ch6_U1.paragraph_briefs[2].point, Ch6_U1.synthesis`; format-only `none`.
- `Ch6_U3`: semantic `Ch6_U3.ordered_development, Ch6_U3.paragraph_briefs[0].development, Ch6_U3.paragraph_briefs[0].point, Ch6_U3.paragraph_briefs[1].development, Ch6_U3.paragraph_briefs[1].point, Ch6_U3.paragraph_briefs[2].development, Ch6_U3.substantive_point, Ch6_U3.synthesis, Ch6_U3.transition`; format-only `none`.
- `Ch6_U4`: semantic `Ch6_U4.ordered_development, Ch6_U4.paragraph_briefs[2].development, Ch6_U4.paragraph_briefs[2].point, Ch6_U4.substantive_point, Ch6_U4.synthesis`; format-only `Ch6_U4.supporting_studies[2].contribution`.

Material retention (original values are checked as recursive subsets, so accepted-material enrichment cannot erase original A/B/reading/tool/local values):
- `source_materials`: exact=`False`; original=26, final=26; original identities retained=`True`; original values lossless=`True`.
- `candidate_materials`: exact=`True`; original=9, final=9; original identities retained=`True`; original values lossless=`True`.
- `tool_materials`: exact=`True`; original=3, final=3; original identities retained=`True`; original values lossless=`True`.
- `candidate_navigation`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
- `source_identity_map`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.

## Ch7

Units: 4 → 4; order preserved: `True`.

| Unit | Selection | Result | Semantic paths | Format-only paths |
|---|---|---|---:|---:|
| `Ch7_U01` | no | exact unchanged | 0 | 0 |
| `Ch7_U02` | no | exact unchanged | 0 | 0 |
| `Ch7_U03` | no | exact unchanged | 0 | 0 |
| `Ch7_U04` | no | exact unchanged | 0 | 0 |

Changed field paths:
- none

Material retention (original values are checked as recursive subsets, so accepted-material enrichment cannot erase original A/B/reading/tool/local values):
- `source_materials`: exact=`True`; original=89, final=89; original identities retained=`True`; original values lossless=`True`.
- `candidate_materials`: exact=`True`; original=9, final=9; original identities retained=`True`; original values lossless=`True`.
- `tool_materials`: exact=`True`; original=1, final=1; original identities retained=`True`; original values lossless=`True`.
- `candidate_navigation`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
- `source_identity_map`: exact=`True`; original=n/a, final=n/a; original identities retained=`n/a`; original values lossless=`True`.
