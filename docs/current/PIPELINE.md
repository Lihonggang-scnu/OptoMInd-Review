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
| 当前分单元正文入口及质量步骤 | 完整细纲和材料 → baseline 单元作者 → 实际正文核查/一次补写 → 装配 → 一次全文局部编辑 → 所选 BODY 编号稿和引用目录 | 正常 `run_review_harness.py --delivery-start body`，以 `--delivery-manifest` 复用生产 manifest/packet/arrangement 导出，或 `--delivery-input` 接完整 `FULL_BODY_INPUT.json`；兼容 CLI `scripts/upgrade3/legacy_unit_writer.py`，共用 `legacy_unit_route.py`、`unit_realization.py`、`article_text_editor.py` 和同一预算工厂；见 [运行和恢复](../writing_candidates/LEGACY_UNIT_QUALITY.md) |
| 可选全文写作候选 | 完整批准细纲、全部章节编排与材料 → 一次成文/连续作者/长文工作台/读者修订的完整 BODY | `runtime/upgrade3/fullbody_writer.py`、`fullbody_contracts.py`；`scripts/upgrade3/fullbody_writer.py`，见 [说明](../fullbody_writer/README.md) |
| 全文装配 | 选定批次和单元结果 → BODY 与交付状态 | `scripts/upgrade3/full_review_draft.py`、`runtime/upgrade3/review_delivery.py` |
| 独立首尾 | 已完成 BODY、上下文/材料 → 后置职责构思、首尾与题名 | `serial_manuscript_parts.py`、`serial_parts_application.py`；`scripts/upgrade3/manuscript_parts.py` |
| 局部修订实验 | 固定正文、细纲、材料 → A/B/C 独立候选与评价 | `post_body_revision*.py`；`scripts/upgrade3/post_body_revision.py` 与 `evaluate_post_body_revision.py` |

表中的 `runtime/upgrade3/` 均位于 `optomind_research/` 下；未写完整路径的末三行 runtime 文件也位于该目录。M1 是运行库加适配器，不凭空提供一个不存在的 query_plan 独立 CLI。

## 执行与使用边界

- BODY 保持实际已恢复的顺序：协调及负责人修订 → 案例正式附加 → 编排 → 写作。不能按 `_post_case_review` 等函数名称猜阶段关系或再次加入案例后的整章改写。
- 正式规划默认开启 `planning_revision`；`--planning-revision` 仍兼容，`--no-planning-revision` 显式选择历史编排模式。两种模式的章节细化都保留完整 A/B，路由说明不能替代科研材料。运行前先读 [运行须知](RUN_GUIDE.md)。
- 深度背景、理论、机制、方法比较和实质 Outlook 属于 BODY；独立首尾负责文章入口、范围宣告及收束。没有重新导入早期并行 PartsPlan 流水线。
- 独立首尾仅显式 `post_body` 方式启用；CLI 采用 fixture/recordings，真实 provider 需本地另行注入与预算管理。普通交付的缺省路径不自动改成该模式。
- 单元/装配 pending 保留原有状态和记录；正常 BODY 入口仍交付已有可读编号稿，不因已接受初稿的科学残余另行阻断。缺失正文、未完成生成或质量记录继续受原有门槛约束；首尾接回不能绕过这些门槛。
- A/B/C 是后置正文局部修订实验，不修改细纲；没有自动质量认证。真实三问题结果只显示有限小修，下一轮细纲实验尚未实施。
- 综述转述原始研究可以凭可用内容与明确引用身份正常参与，不强制自身 A/B 或全文。多个任务可以共用实质充分的一张表。
- BODY 正常入口正式选择 `baseline`，默认 `quality_control=True`、`article_edit=True`；默认作者 Plus 8192 思考＋32768 输出，核查/全文局部编辑 Plus 16384 思考＋24576 输出。`chapter_coherence` 仅通过 `--body-version chapter_coherence` 显式选择作实验；原任务和材料保留。`--no-quality-control`、`--no-article-edit` 可显式关闭对应步骤。旧独立 CLI 仍默认 baseline 且两步骤关闭。真实请求、恢复、项目/账户账本共用；正式默认集中于 `review_delivery.BODY_DELIVERY_DEFAULTS`。
- `DELIVERY_REPORT.json.selected_body` 指向实际选中正文；成功全文编辑后使用其编辑稿和已有编号结果。BODY 只交付当前正文、可读编号稿和引用目录；首尾/出版由 `--delivery-config` 显式请求，消费者不会再运行第二次 02 编辑。

## 继续阅读

- 首尾输入输出及 CLI：`docs/POST_BODY_MANUSCRIPT_PARTS.md`
- 局部修订与实验：`docs/post_body_revision/README.md`、`EXPERIMENT_PLAN.md`
- 最新真实修订实验：`docs/acceptance/post-body-revision-local-20261005/METHOD_REVIEW_FIRST.md`
- BODY40 实际顺序：`docs/acceptance/body40-20261005/RECOVERY_AND_STAGE_ORDER.md`

历史文件中的“下一步”“已通过”只针对其当时范围。当前目录的说明也不替代源码核查和真实科学质量验收。

当前全文候选以全部批准 BODY 章节为交付范围；历史单章 `writer_candidates.py` 保留作局部工具与对照。连续路线每次消费真实已写正文，所需调用与恢复均显式记录；全文独立首尾仍在 BODY 完成之后。

2026-10-07补充：同一全文入口还提供 `plain_whole`、`chapter_concat`、`hierarchical_full` 三条朴素基线。分层方案可复用独立章节的完整拼接稿，只新增一次真正全文编辑。四条高级路线与三条朴素路线合计测试预算已下调为40元；执行与旧账本降额见 `docs/fullbody_writer/PLAIN_BASELINES_40_CNY.md`。
