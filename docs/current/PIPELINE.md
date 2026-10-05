# 当前链路与状态

## 输入输出及代码入口

| 环节 | 输入 → 输出 | 实现与入口 |
|---|---|---|
| M1 问题/检索规划 | 用户问题、范围 → Facet、查询、PLAN | `runtime/upgrade3/query_plan.py`；`optomind_research/query_planner.py` 的 v2 适配路径 |
| M2 候选语料 | PLAN → 多通道候选、身份归并、检索记录 | `runtime/upgrade3/candidate_corpus.py`；`scripts/upgrade3/run_candidate_corpus.py` |
| M3 关系骨架 | 候选、引用关系、Facet → 学术导航及待调查事项 | `runtime/upgrade3/scholarly_skeleton.py`、`skeleton_edges.py`、`skeleton_enrich.py`；`scripts/upgrade3/run_scholarly_skeleton.py` |
| 本地材料层 | 论文身份、可用入口 → 多源材料、结构快照和缺口 | `runtime/upgrade3/local_materials.py`、`material_acquisition.py`、`document_snapshot.py`、`public_fulltext_rescue.py`；`tools/academic_backends/` |
| M4 阅读 | 实际材料、用户问题/Facet → 单篇理解、A/B 卡片 | `runtime/upgrade3/paper_reading_card.py`、`module4/`；`scripts/upgrade3/module4.py`、`paper_reading_card.py`、`paper_reading_batch.py` |
| 定向补充与精读 | 当前知识需求、已有材料 → 可用回答/partial/缺口 | `planning_supplement.py`、`directed_reading.py` 及对应 scripts/upgrade3 入口 |
| BODY 逐级规划 | 问题、PLAN、A/B、精读/补充 → 章级任务与细纲 | `runtime/upgrade3/progressive_review_plan.py`；`scripts/upgrade3/progressive_review_plan.py` |
| 编排 | 已形成的知识任务与来源 → 顺序、拆合及 writer 输入 | `runtime/upgrade3/chapter_arrangement.py`；同名 CLI |
| 写作/补写 | 实际任务与材料 → 单元正文、引用与未决报告 | `runtime/upgrade3/review_unit_writer.py`；同名 CLI |
| 全文装配 | 选定批次和单元结果 → BODY 与交付状态 | `scripts/upgrade3/full_review_draft.py`、`runtime/upgrade3/review_delivery.py` |
| 独立首尾 | 已完成 BODY、上下文/材料 → 后置职责构思、首尾与题名 | `serial_manuscript_parts.py`、`serial_parts_application.py`；`scripts/upgrade3/manuscript_parts.py` |
| 局部修订实验 | 固定正文、细纲、材料 → A/B/C 独立候选与评价 | `post_body_revision*.py`；`scripts/upgrade3/post_body_revision.py` 与 `evaluate_post_body_revision.py` |

表中的 `runtime/upgrade3/` 均位于 `optomind_research/` 下；未写完整路径的末三行 runtime 文件也位于该目录。M1 是运行库加适配器，不凭空提供一个不存在的 query_plan 独立 CLI。

## 执行与使用边界

- BODY 保持实际已恢复的顺序：协调及负责人修订 → 案例正式附加 → 编排 → 写作。不能按 `_post_case_review` 等函数名称猜阶段关系或再次加入案例后的整章改写。
- `--planning-revision` 是历史升级规划入口的显式开关；核对当前 CLI 及运行记录，旧兼容模式不是另一套质量候选。
- 深度背景、理论、机制、方法比较和实质 Outlook 属于 BODY；独立首尾负责文章入口、范围宣告及收束。没有重新导入早期并行 PartsPlan 流水线。
- 独立首尾仅显式 `post_body` 方式启用；CLI 采用 fixture/recordings，真实 provider 需本地另行注入与预算管理。普通交付的缺省路径不自动改成该模式。
- 单元/装配 pending 继续阻止不当向后交付；已保存正文仍保留。首尾接回不能绕过这一门槛。
- A/B/C 是后置正文局部修订实验，不修改细纲；没有自动质量认证。真实三问题结果只显示有限小修，下一轮细纲实验尚未实施。
- 综述转述原始研究可以凭可用内容与明确引用身份正常参与，不强制自身 A/B 或全文。多个任务可以共用实质充分的一张表。

## 继续阅读

- 首尾输入输出及 CLI：`docs/POST_BODY_MANUSCRIPT_PARTS.md`
- 局部修订与实验：`docs/post_body_revision/README.md`、`EXPERIMENT_PLAN.md`
- 最新真实修订实验：`docs/acceptance/post-body-revision-local-20261005/METHOD_REVIEW_FIRST.md`
- BODY40 实际顺序：`docs/acceptance/body40-20261005/RECOVERY_AND_STAGE_ORDER.md`

历史文件中的“下一步”“已通过”只针对其当时范围。当前目录的说明也不替代源码核查和真实科学质量验收。
