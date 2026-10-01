# Manuscript-parts evolution in restored BODY run

Source root: `body_restore_20261001`. This record uses only this restored run and does not mix `new_plan` or prior acceptance artifacts.

## v0 — provisional scope responsibility cards

- Status: `provisional`; revision: `None`; frozen: `—`
- Sources: `plan/PROGRESSIVE_REVIEW_PLAN.partial.json#provisional_scope.manuscript_parts_plan`, `plan/PROGRESSIVE_REVIEW_PLAN.partial.json#provisional_scope.provisional_outline`
- BODY outline roles: Ch1_Intro, Ch2_Clinical, Ch3_Mechanisms, Ch4_Context, Ch5_Interventions, Ch6_Challenges, Ch7_Conclusion
- Interpretation: Initial standalone abstract/introduction/conclusion cards existed while the provisional outline still contained Ch1_Intro and Ch7_Conclusion.

| Card | Placement | Focus | Boundary count |
|---|---|---|---:|
| abstract | manuscript_start / standalone | 微生物组 -ICI 临床关联; 代谢物介导机制; 干预性试验结果 | 3 |
| conclusion | manuscript_end / standalone | 证据总结; 临床转化障碍; 研究优先级建议 | 3 |
| introduction | after_abstract / standalone | ICI 治疗局限性; 微生物组调节 rationale; 综述结构概览 | 3 |

## v1 — level-one planning responsibility cards

- Status: `partial`; revision: `v1`; frozen: `—`
- Sources: `plan/PROGRESSIVE_REVIEW_PLAN.partial.json#manuscript_parts_plan`, `plan/stages/level1_outline.json#response.manuscript_parts_plan`, `plan/stages/level1_outline.json#response.shared_outline`
- BODY outline roles: Ch2_Clinical, Ch3_Mechanisms, Ch4_Context, Ch5_Interventions, Ch6_Challenges
- Inherited provisional roles visible at this stage: Ch1_Intro, Ch2_Clinical, Ch3_Mechanisms, Ch4_Context, Ch5_Interventions, Ch6_Challenges, Ch7_Conclusion
- Interpretation: The cards gained stronger evidence boundaries and finalize_from inputs. The level-one response exposed five shared BODY chapters, while the inherited provisional seven-chapter outline still carried the lightweight Ch1/Ch7 responsibilities that later required boundary repair.

| Card | Placement | Focus | Boundary count |
|---|---|---|---:|
| abstract | manuscript_start / standalone | 微生物组 -ICI 临床关联的观察性证据强度; 肌苷 -UBA6 等代谢物介导机制的人类验证数据; FMT 等干预策略的 Phase 2 试验结果及安全性; III 期 RCT 证据缺口与标准化挑战 | 4 |
| conclusion | manuscript_end / standalone | 观察性关联证据的一致性与干预性证据的早期性; 机制验证的人类数据局限（UBA6 替代标志物 vs 代谢物直接定量）; III 期 RCT 证据缺口及正在进行试验（CanBiome2）; 标准化建议：供体筛选、代谢物测量、多层面闭合链条 | 4 |
| introduction | after_abstract / standalone | ICI 治疗在实体瘤中的疗效异质性临床问题; 微生物组作为可调节因子的生物学 rationale; 观察性关联与干预性证据的证据等级区分; 综述核心论点：早期证据支持但需 III 期确证 | 4 |

## v2 — frozen final responsibility cards

- Status: `complete_frozen`; revision: `v2`; frozen: `True`
- Sources: `plan/DETAILED_REVIEW_PLAN.json#manuscript_parts_plan`, `plan/DETAILED_REVIEW_PLAN.json#shared_outline`, `plan/RUN_STATE.json#manuscript_parts_plan`, `body_plus/batch/MANIFEST.json#manuscript_parts_plan`
- BODY outline roles: Ch2_Clinical, Ch3_Mechanisms, Ch4_Context, Ch5_Interventions, Ch6_Challenges
- Interpretation: Final v2 separates standalone abstract/introduction/conclusion duties from the five substantive BODY chapters while retaining deep mechanism, clinical, intervention, and challenge content in BODY.

| Card | Placement | Focus | Boundary count |
|---|---|---|---:|
| abstract | manuscript_start / standalone | 微生物组 -ICI 临床关联的观察性证据强度; 肌苷 -UBA6 等代谢物介导机制的人类验证数据; FMT 等干预策略的 Phase 2 试验结果及安全性; III 期 RCT 证据缺口与标准化挑战 | 4 |
| conclusion | manuscript_end / standalone | 观察性关联证据的一致性与干预性证据的早期性; 机制验证的人类数据局限（UBA6 替代标志物 vs 代谢物直接定量）; III 期 RCT 证据缺口及正在进行试验（CanBiome2）; 标准化建议：供体筛选、代谢物测量、多层面闭合链条 | 4 |
| introduction | after_abstract / standalone | ICI 治疗在实体瘤中的疗效异质性临床问题; 微生物组作为可调节因子的生物学 rationale; 观察性关联与干预性证据的证据等级区分; 综述核心论点：早期证据支持但需 III 期确证 | 4 |

## Final Plus production evidence

- Writer results: `20` complete `qwen3.5-plus` units under `body_plus/writer`.
- Source breadth: `127` used handles and `125` canonical references after assembly deduplication.
- Assembly: `5` tables, `5` issue-bearing units retained, `0` missing units, `0` unknown citations.
- Output: `body_plus/assembly/REVIEW_DRAFT_HANDLES.md`.
- Verified repair commits: `23fde51` (unique handle-suffix citation normalization) and `0b7db9deae520fec41757742fca2b23dd53613ab` (Plus writer profile / Markdown envelope decoding).
- Front/back generation is intentionally not marked complete in this record.
