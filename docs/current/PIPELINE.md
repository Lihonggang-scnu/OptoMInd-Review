# 当前完整链路与正式接线

**2026-10-13 材料解析接线：** 原生 JATS/TEI 继续优先；PDF 可显式启用本地 Docling 全页布局、公式/表格自动区域识别和原图裁切。区域调用使用 qwen3.5-plus、项目与账户双账本，结果经不可变 snapshot 进入普通阅读、A/B、定向精读和独立 M4。Docling 本地进程与整个批次的区域 HTTP 请求分别限流，不将每篇并发相乘。运行、恢复及硬件实测见 [材料入口](../local_materials.md) 和 [2026-10-13 验收](../verification/pdf-parser-parallel-20261013/README.md)。图像只保留原区域和图注，本轮没有加入科研图像解释。

**2026-10-12交付采用决定：** 用户已亲读新首尾及校验后的稿件，判定合格，允许交付。受测源码`b98771f2cc1548c301f6621d0b87414d858d9f43`；[本地真实采用记录](../acceptance/parts-checker-delivery-20261012/README.md)保存输入、请求、返回、选用稿、费用和恢复。post_body下游现为03首尾→校验selected_manuscript→04最终编号→05出版。此前[首尾实跑](../acceptance/serial-parts-live-20261011/CLOUD_READ_FIRST.md)及[研究/校验准备](../acceptance/manuscript-checker-preparation-20261012/README.md)继续保留为历史，不能再据其“未实现”状态判断当前代码。

**2026-10-10 决定：正式 BODY 采用 baseline。** 作者、任务核查/必要的一次有界补写、装配、一次全文局部编辑、实际选用稿编号已接入正常入口。作者仅 `qwen3.5-plus`。`chapter_coherence` 默认关闭；“细纲→指南→正文”及全文候选保留为历史实验。采用依据与原始结果见 [固定对照归档](../acceptance/outline-to-body-coherence-20261010/README.md)。基线源码 `638eb93cdb37fc95cb917130f69a5b16df3549b7`，验收归档 `2618319e3fe2a085acf11a66e6f6c66c73a9c135`；本次只核对接线、统一文档并修剩余状态接缝，未重写有效提示词。

接缝修复受测源码：`4e1e97c517440cdc11623fda56977f7528df396f`。完整BODY附终结诊断的状态、所选目录承接与首尾编号投影已免费验证；[收尾记录](RUN_GUIDE_EVIDENCE.md)保留确切测试范围、原稿哈希和费用。

**2026-10-11 接线更新：完整规划 CLI 默认后置细纲加强。** 复用正式 selector → ACCESS → OWNER → 完整章包合回与恢复入口；`--no-outline-strengthening` 显式关闭，预检及两级阶段停点不运行。原规划顺序、科研提示词、Max 加强模型/容量和 baseline BODY 算法保持原实现。本轮仅免费模拟验证；上游科学 `partial` 和加强执行状态分别保存，不用接线结果替代科学质量验收。

本文件是全链路统一阅读入口；可照用的命令、默认值、恢复及状态见 [RUN_GUIDE.md](RUN_GUIDE.md)。图中的箭头表示明确的产物交接，**不是一个缺省命令会自动运行所有模块**。当前专门的正式 BODY 入口从已批准细纲/编排及材料开始；其他 harness 路径保留原研究编排器，不能拿它们代替本表的新分阶段入口。

```text
用户问题/范围 → M1 检索规划 → M2 实际检索/身份归并 → M3 关系与阅读导航
  → 材料获取/解析 → A/B卡片及明确绑定的规划池
  → 逐级规划（各阶段可按需补读）
  → 全局协调/负责人修订 → 正式案例附加 → 完整章包
  → 默认：同章任务簇按需加强并合回完整章包（可显式关闭）
  → 编排/单元写作包 → 正式 baseline BODY 作者
  → 任务核查/必要的一次补写 → 装配 → 一次全文局部编辑
  → selected_body（句柄稿＋编号读者稿＋引用目录）
  → [显式：独立首尾/题名 → 全文精确局部校验/selected_manuscript → 最终稿图表与统一编号 → 出版]
```

## 输入输出及代码入口

| 环节 | 输入 → 输出 | 实现与入口 |
|---|---|---|
| M1 问题/检索规划 | 问题、范围 → 生成/校验/有界修订 → 含 `plan` 的 outcome、Facet与查询；调用方保存选定 PLAN/schema，M2读取它 | `optomind_research/query_planner.py::QueryPlannerAgent.query_plan_v2` → `runtime/upgrade3/query_plan.py`；字段配置 `config/query_plan_fields.json`。API不自动写固定 `PLAN.json`，无独立 query_plan CLI |
| M2 候选语料 | 选定 schema/PLAN → keyword/question 通道检索、片段和身份归并 → `CANDIDATE_CORPUS.json`、命中记录、`SNIPPET_TEXT.sqlite` → M3及材料名单 | `candidate_corpus.py`；`scripts/upgrade3/run_candidate_corpus.py --schema … --out … --snippets …`。执行会联网，不是免费预览 |
| M3 关系骨架 | corpus、`GRAPH_EDGES.jsonl`、Facet与可用元数据/向量/片段 → 社区/角色/相关性与阅读分流 → `SCHOLARLY_SKELETON.json`、`COMMUNITIES.json`、`INVESTIGATION_PORTFOLIO.json`、`INVESTIGATION_CARDS.json`、选择trace → 阅读名单/材料/独立M4导航 | `scholarly_skeleton.py`；`run_scholarly_skeleton.py` 默认v2。`skeleton_edges.py` 边采集和 `skeleton_enrich.py` enrich是独立步骤，不能假定骨架CLI代做 |
| 材料获取/解析 | 稳定身份、DOI/题名和已知URL/本地XML/PDF → 获取、公开全文救援、解析 → 不可变snapshot及获取缺口 → 阅读器读取实际文本与范围 | `MaterialAcquirer.acquire`、`local_materials.py`、`document_snapshot.py`；`build_local_materials.py --input/--prepared/--record-json/--batch-manifest`。snapshot有 `DOCUMENT_MANIFEST.json`、`DOCUMENT_BLOCKS.jsonl`、`DOCUMENT_ASSETS.json`、`REFERENCES.json`、`READING_VIEW.md` |
| 普通整篇阅读API | snapshot＋问题/Facet → reading packet/messages → 注入返回规范化为带来源的observations → 调用方保存/消费 | `PreparedSnapshot.build_reading_messages/normalize_reading_result`、`material_reading.py`。不是获取CLI自动执行的模型阅读，无自身固定模型 |
| A/B卡片主路 | snapshot＋原PLAN → 一次带题目语境的联合提炼 → `PAPER_READING_CARD.json`、输入/消息/RAW；批量 `BATCH_STATE.json`、`BATCH_CARDS_INDEX.jsonl`、`BATCH_PLANNING_VIEWS.jsonl` → 明确采用的规划池 | `paper_reading_card.py`；`paper_reading_batch.py --manifest … --output-root …`。A为论文一般理解、B为本题规划用途，但 `general_understanding_topic_independent=false`；A不是已验证的无条件跨题缓存 |
| 独立M4档案（可选） | `READING_INPUT`绑定题目/PLAN、候选身份、M3导航卡与snapshot → 理解、anchors、coverage与verification → `PAPER_READING_DOSSIER`、`COVERAGE_REPORT`、`CALL_LEDGER`；单独handoff生成 `WRITING_HANDOFF` | `scripts/upgrade3/module4.py prepare-input/read/handoff`、`module4/reader.py`。read必须显式 `--dry-run` 或 `--real`；不是A/B规划池的自动前置步骤 |
| 按需补充/精读 | 当前questions/required_outputs＋身份＋池/索引/snapshot → 本地先查、必要时外搜获取、任务化提炼 → `DIRECTED_READING.json`、回答/partial/remaining_points、`SOURCE_UNIT`、`SUPPLEMENT_INDEX`/`SUPPLEMENT_MATERIALS`及增量池 → 当前规划或负责人实际输入 | `planning_supplement.py`、`directed_reading.py`、`planning_material_search.py` 与同名CLI；规划runner在各材料窗口调用，不是计划结束后才统一补读 |
| BODY逐级规划 | `--plan`＋有准确card_path的 `--pool`＋可选索引/prior-reading → 构思、一级纲要、路由/章提案/协调、二级备料/定案、章节工具/细化、全文协调/负责人修订、案例附加 → `RUN_STATE.json`、`DETAILED_REVIEW_PLAN.json`、完整 `writer_packets/` → 默认后置加强 | `progressive_review_plan.py::run/_write_final_outputs`；同名CLI默认预检，`--run`执行，恢复显式 `--resume`。`--stop-after level1/level2` 不进入加强。最终章包不能用早期level1/章节cache替代 |
| 细纲加强（完整规划 CLI 默认） | 完整规划及packet清单 → 第一层选同章任务簇 → ACCESS完整回读/OWNER局部加强 → 合回完整章节 → revisions章包、`HANDOFF_MANIFEST`、规划根 `CURRENT_PLAN.json`、`PLANNING_RUN_REPORT.json.packet_root` → 编排 | 规划CLI `post_planning_handoff` 复用 `scripts/upgrade3/outline_strengthening_pipeline.py::run_pipeline`；独立入口仍可直接准备/运行。`--no-outline-strengthening` 免费导出完整原计划 revision。单组 `ARRANGEMENT_PACKET`不能冒充完整章包 |
| 编排/单元写作包 | `--packet-root CURRENT_PLAN.json` 或最终完整章包目录、主线、原任务、材料/条件/案例 → 顺序、拆合、来源用途 → `ARRANGEMENT_INPUT.json`、`CHAPTER_ARRANGEMENT.json`、source_catalog/usage、`ARRANGEMENT_RUN.body_ready/body_manifest` → BODY manifest loader | `chapter_arrangement.py::build_chapter_view/validate_arrangement`；同名CLI全章成功后导出绑定实际文件/哈希的manifest，缺章或失败不宣布可用。须显式 `--planning-revision`；不会从planner默认值自动继承 |
| 正式BODY输入 | 完整 `FULL_BODY_INPUT.json`，或 `optomind.fullbody_manifest.v1` 绑定最终plan/packet_root、各章有效arrangement/view → 同一manifest loader导出任务与完整材料 → baseline运行库 | `run_review_harness.py --delivery-start body --delivery-input …` 或 `--delivery-manifest …`；共用 `scripts/upgrade3/fullbody_writer.py::load_body_manifest`。`fullbody_writer.py --prepare-manifest`只离线固定输入，不启用历史全文作者 |
| 正式作者/核查/补写 | 每单元完整任务、用途、章节框架和实际材料 → Plus作者；核查实际正文，必要时一次有界补写/后核查 → `UNIT_MESSAGES`、RAW、`UNIT_RESULT`、原始及采用正文、诊断 → 装配 | `legacy_unit_route.py`、`review_unit_writer.py`、`unit_realization.py`；`scripts/upgrade3/legacy_unit_writer.py`与正常入口共用。主作者不带实际前文；不经过指南转换 |
| 装配/一次全文编辑/选择与编号 | 明确采用单元结果 → 装配句柄稿、references/summary；一次全文局部编辑 → 实际编辑稿或装配稿 → `DELIVERY_REPORT.json.selected_body` 的句柄稿/编号稿/引用目录 | 原 `full_review_draft.py` 汇编器、`article_text_editor.py`、`review_delivery.py::_body_delivery_artifacts`、`delivery_citations.py`。下游读选择，不按文件时间猜稿；BODY编辑已处理，delivery不再执行第二次02编辑 |
| 独立首尾/题名（显式） | **所选句柄BODY**＋原问题＋最终主张/范围/细纲＋可选材料与身份 → 后置构思职责卡 → 结语→引言→摘要/关键词/题名 → `MANUSCRIPT_FINAL.md`、实际消息/响应与报告 → 04编号 | `serial_manuscript_parts.py`、`serial_parts_application.py`、`scripts/upgrade3/manuscript_parts.py`；delivery配置 `front_back.mode=post_body` 维持fixture/严格录制。独立CLI新增 `--live` 预览、`--run` 显式双账本Plus生成及缓存恢复，只有standalone自动装配 |
| 全文精确局部校验（post_body默认） | 03实际完整句柄稿、同一身份目录和显式材料 → 最多两轮快照/块约束JSON提案 → 严格应用、逐项收据/待办 → `CHECKER_REPORT.selected_manuscript` 的路径/SHA/来源 → 04消费该选择 | `manuscript_checker.py`复用`post_body_revision_contracts.py`；独立`manuscript_delivery.py`与正常`run_downstream_delivery`共用消费者。`--no-manuscript-checker`可关闭；普通入口的live provider须显式注入，不能靠开关假定已建真实客户端 |
| 最终图表/引用与出版（显式） | 校验后selected_manuscript（关闭时为03）＋同一身份目录/图资产 → 先挂图、再按阅读顺序统一编号 → 04 `MANUSCRIPT_READER.md`、`REFERENCES.json`、图映射、assets → 05只消费这些真实路径 | `delivery_citations.py`、`review_delivery.py::run_publication_delivery`、`latex_publication_renderer.py`。出版不再编号/挂图；Markdown默认，`compile_pdf=true`才TeX/PDF，Crossref/S2 enrich关闭 |

表中的运行库位于 `optomind_research/runtime/upgrade3/`，脚本位于 `scripts/upgrade3/`，除已写完整路径者。每次选定输入保存路径、SHA及所属运行，不把不同题目的PLAN、P号或历史删节包混成生产输入。

## 配置、启用与验证范围

| 环节 | 模型/配置位置与启用条件 | 当前证据范围 |
|---|---|---|
| M1 | `config/model_policy.yaml` 的 QueryPlannerAgent→c_model，当前Plus；显式model_tier可覆盖。query_plan字段及运行客户端决定实际请求 | 冻结M1/M2/M3历史跨领域运行；本轮只读源码核对，没有新增检索/LLM |
| M2/M3/获取解析 | gateway/CLI参数、`config/scholarly_skeleton.json`、`AcquisitionConfig`、工具provider；获取解析不等于模型阅读 | 历史运行与快照、各模块局部控制；本轮未重跑公网获取和全部旁件 |
| A/B卡 | 固定qwen3.7-flash；economy、4096思考＋12288回答，card runtime默认值及CLI可见参数 | 历史574/585卡片与准确选用表；本轮核对产物合同，不新生成卡片 |
| 独立M4 | Flash/Plus须显式选择，whole_text；thinking缺省关闭，开启额度8192、回答32768，module4 CLI/runtime | 历史档案/有界测试，未作为本轮A/B生产输入自动运行 |
| 补充/精读 | directed默认Flash 8192/20000；helper Flash/Plus及容量在 `planning_helper_runtime_config`。外搜开关不关闭已知论文本地精读 | 既有任务复用/partial/材料交接测试、历史真实补读；本轮保留合同 |
| 规划/负责人 | Plus；chapter_details/case_groups为Flash，`_planner_call_settings`。主规划16384/32768，读者8192/20000；CLI与runtime实际设置 | BODY40历史585池真实规划及其partial状态；条件/论证交接修复局部控制，不冒称全新上游端到端测试 |
| 默认细纲加强 | `config/outline_revision/quality.json` 的 selection/strong/owner Max及ACCESS Plus；正式SSE与容量在按需运行库/入口；共享规划原账本及封顶 | 两单元历史真实加强＋正式入口离线回放/短链；新增免费CLI默认/关闭/停止/none/失败/恢复验收，不新增真实模型测试 |
| 编排 | 独立CLI默认Plus、16384/32768，max_source_chars=0；必须显式模式、材料和账本 | 历史完整编排及消息交接；正常BODY manifest导出免费验证 |
| 正式BODY作者/补写 | 固定Plus 8192思考＋32768回答；正常入口 `BODY_DELIVERY_DEFAULTS` 是 baseline＋核查True＋全文编辑True | 已认可7章29单元；既有完整稿187身份的正式选择及免费恢复；固定五单元真实比较后选择baseline |
| 核查/全文编辑 | 固定Plus 16384/24576，`unit_realization.py`、`legacy_unit_route.py` 的角色profile；不由旧quality.json的Flash writer字段替代 | 真实任务检查/一次补写/严格编辑与一次全文编辑；本轮不追修已接受局部科学问题 |
| 首尾/最终编号/出版 | delivery显式config；独立live CLI为Plus 16384思考/32768回答、双账本及固定增量额度。出版无LLM，PDF需本机工具链 | 历史：2026-10-11首尾5调用0.356588元及49页PDF经视觉检查。当前采用以2026-10-12补充为准：新首尾及两次校验共9调用1.4544032元，187身份，Markdown接通，用户亲读接受；本轮未重跑BODY或PDF |

验证记录分别见 [RUN_GUIDE_EVIDENCE](RUN_GUIDE_EVIDENCE.md)、[容量/条件交接](QUALITY_CAPACITY_VERIFICATION.md)、[按需正式接入](../verification/on-demand-promotion-20261007/README.md) 及本轮BODY归档。不相加重叠测试数，也不把合成调用当真实科学质量证据。

## 执行与使用边界

- BODY 保持实际顺序：`progressive_review_plan.run` 先调用 `_post_case_review` 做全文协调/负责人修订（case_record为空），再生成case_groups并正式附加案例，最后导出章包；然后独立编排与写作。不能按函数名称加入案例后整章重写。
- 正式规划默认开启 `planning_revision`；`--planning-revision` 仍兼容，`--no-planning-revision` 显式选择历史编排模式。两种模式的章节细化都保留完整 A/B，路由说明不能替代科研材料。运行前先读 [运行须知](RUN_GUIDE.md)。
- 完整规划 CLI 的细纲加强默认开启，与 `planning_revision` 是两个独立开关。选择 none 可自主正常完成，无需人工科学意见或填写阶段状态。selection 前已保存完整有效快照；失败/预算不足保留原计划、成功前缀与先前采用版本，返回未完成状态。同目录 `--resume` 沿用原规划/选择/ACCESS/OWNER兼容缓存；无效结构回答仅显式 `--retry-unresolved` 重试一次。
- `ProgressiveReviewPlanner.run` API 继续负责原规划，不隐式买 Max；正常 CLI 通过共享 `run_pipeline` 组合后置加强。程序调用方可以复用同一入口。规划命令不会隐式启动编排或 BODY，后续两个正式 CLI 按 `CURRENT_PLAN` → `ARRANGEMENT_RUN.body_manifest` → `--delivery-manifest` 接续。
- 深度背景、理论、机制、方法比较和实质 Outlook 属于 BODY；独立首尾负责文章入口、范围宣告及收束。没有重新导入早期并行 PartsPlan 流水线。
- 独立首尾仅显式启用：delivery config采用 `post_body` fixture/recordings；独立CLI另提供 `--live` 预算受控Plus适配器。普通交付的缺省路径不自动改成该模式。实时输入、缓存恢复和预算绑定见 [首尾说明](../POST_BODY_MANUSCRIPT_PARTS.md)。
- 单元/装配 pending 保留原有状态和记录；正常 BODY 入口仍交付已有可读编号稿，不因已接受初稿的科学残余另行阻断。缺失正文、未完成生成或质量记录继续受原有门槛约束；首尾接回不能绕过这些门槛。
- A/B/C 是早期后置正文局部修订实验，不修改细纲。其有限小修结论只针对当时实验；细纲加强当前按上表默认接在完整规划 CLI 后，二者不能混同。
- 综述转述原始研究可以凭可用内容与明确引用身份正常参与，不强制自身 A/B 或全文。多个任务可以共用实质充分的一张表。
- BODY 正常入口正式选择 `baseline`，默认 `quality_control=True`、`article_edit=True`；默认作者 Plus 8192 思考＋32768 输出，核查/全文局部编辑 Plus 16384 思考＋24576 输出。`chapter_coherence` 仅通过 `--body-version chapter_coherence` 显式选择作实验；原任务和材料保留。`--no-quality-control`、`--no-article-edit` 可显式关闭对应步骤。旧独立 CLI 仍默认 baseline 且两步骤关闭。真实请求、恢复、项目/账户账本共用；正式默认集中于 `review_delivery.BODY_DELIVERY_DEFAULTS`。
- `DELIVERY_REPORT.json.selected_body` 指向实际选中正文；成功全文编辑后使用其编辑稿和已有编号结果。BODY 只交付当前正文、可读编号稿和引用目录；首尾/出版由 `--delivery-config` 显式请求，消费者不会再运行第二次 02 编辑。

## 已知需显式交接的上游边界

- A/B批量导出的 `output_dir` 和B视图不是完整规划池合同。当前 `PLANNING_POOL.jsonl` 必须明确绑定每篇最终采用卡片的 `card_path`、snapshot与PLAN身份；已认可585池具备这些绑定。通用批量导出到规划池仍需适配，不能直接拿 `BATCH_PLANNING_VIEWS.jsonl` 替代。
- M3片段库、图边、元数据与向量是独立旁件。当前默认从M3输出目录找片段库；调用方需保留匹配的命中ID和旁件，不只搬一个corpus JSON。不能写成自动全覆盖。
- 旧输入输出地图记录的附录/书目后科学内容选择、部分精读索引字段及任务目录仍有边界；本轮没有修改冻结上游。需要这些内容时明确供给完整材料或所选精读记录，不能因索引未中就称材料不存在。

旧本地对齐文件 `OPTOMIND_LIHONGGANG_INPUT_OUTPUT_MAP_20261007 (2).md` 未在Git历史中追踪；其历史观察保留。现有 `PIPELINE`、`RUN_GUIDE`、`RUN_GUIDE_EVIDENCE` 是统一入口，本轮不另建一套“最新版”地图。来源与替代关系见 [记录对照](RUN_GUIDE_EVIDENCE.md)。

## 历史实验说明（保留，已由2026-10-10决策替代默认地位）

以下继续阅读和2026-10-07候选/预算说明是当时记录，不是下一轮开工要求，也不是本轮项目预算。可选实验代码保留；正式默认以本页开头及RUN_GUIDE为准。

- 首尾输入输出及 CLI：`docs/POST_BODY_MANUSCRIPT_PARTS.md`
- 局部修订与实验：`docs/post_body_revision/README.md`、`EXPERIMENT_PLAN.md`
- 最新真实修订实验：`docs/acceptance/post-body-revision-local-20261005/METHOD_REVIEW_FIRST.md`
- BODY40 实际顺序：`docs/acceptance/body40-20261005/RECOVERY_AND_STAGE_ORDER.md`

历史文件中的“下一步”“已通过”只针对其当时范围。当前目录的说明也不替代源码核查和真实科学质量验收。

当前全文候选以全部批准 BODY 章节为交付范围；历史单章 `writer_candidates.py` 保留作局部工具与对照。连续路线每次消费真实已写正文，所需调用与恢复均显式记录；全文独立首尾仍在 BODY 完成之后。

2026-10-07补充：同一全文入口还提供 `plain_whole`、`chapter_concat`、`hierarchical_full` 三条朴素基线。分层方案可复用独立章节的完整拼接稿，只新增一次真正全文编辑。四条高级路线与三条朴素路线合计测试预算已下调为40元；执行与旧账本降额见 `docs/fullbody_writer/PLAIN_BASELINES_40_CNY.md`。

## 2026-10-12正式交付补充

独立首尾CLI的LIVE_REPORT.runtime_report包裹原FRONT_BACK_REPORT；不要将外层费用累计delta当每阶段单独费用。校验严格执行后以selected_manuscript为准，complete_with_diagnostics可以交付并附带pending/unresolved；缺原稿、哈希不符或首轮没有有效结果仍停止。04书目投影、05 reader及MANUSCRIPT_SELECTION sidecar的实际路径和SHA逐段保存。恢复细则与正式命令以[MANUSCRIPT_CHECKER_RUN_GUIDE](MANUSCRIPT_CHECKER_RUN_GUIDE.md)为准。

本轮真实首尾/校验共9次Plus调用1.4544032元，无占额；最终187身份，Markdown消费接通。448项相关回归通过；没有重跑BODY或本轮PDF。已知写作瑕疵保留在亲审记录，用户验收决定已采纳。

## 2026-10-13 材料解析消费者接线

首次获取与规划补读共用显式配置的 MaterialAcquirer 工厂，原生 XML 优先；PDF 正文提取配合 Docling 布局和区域 Plus 识别。快照中的公式、表格、脚注及缺口进入阅读输入。实际部署为本地2进程、区域全局8并发、外层4 worker。输入输出及恢复契约见 [消费者衔接](../verification/pdf-parser-parallel-20261013/CONSUMER_COUPLING.md)。本轮光学测试已完成270条材料获取，正恢复A/B阅读，尚非端到端论文完成记录。
