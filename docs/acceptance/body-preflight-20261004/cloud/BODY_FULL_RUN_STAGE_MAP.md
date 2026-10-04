# OptoMind BODY 完整实验阶段图

锁定代码：`d3a2419b213ddbbfbf8a550af03b0b60edab3025`，分支 `body07-short-chain-cloud-20261004`。日期：2026-10-04。

**结论：主链与材料分工已能明确追踪，但正式 CLI 的缓存、早期工具反馈和历史材料兼容性仍有确定接缝。应先完成小范围修复或明确隔离，再授权完整真实实验。** 本轮只读代码、受控离线探针和报告；没有修改生产代码、付费调用或完整正文生成。具体问题见同附的 `BODY_PREFLIGHT_FINDINGS.md`。

## 如何读验证程度

- **S 代码追踪**：确认函数、字段及分支存在，不代表模型完成了认识任务
- **C 受控离线**：真实函数与持久化路径，受控模型/外部边界；局部投影探针另标注替代位置
- **H 历史真实响应重放**：已有模型输出经当前消费者重放，不是新生成实验
- **L 本地报告**：用户转述了本地07验收及 `setup→make_chain_fixture` 测试兼容改名；尚未给出该轮归档 SHA，未冒称云端亲自复核了其全部原始日志

本轮重新跑了6项代表性短链、8项缓存/恢复控制，并运行少量定向反例；未重跑数百项测试。此前06/07的历史通过数量不计作本轮新验证，也不代表科学质量通过。

## 1 正式入口与实际顺序

规划入口是 `scripts/upgrade3/progressive_review_plan.py::main`，不是在 `run_review_harness.py` 里选择 `--delivery-start plan`。后者是**已有 writer packet 的离线重放交付**，不会重新做全池构思。[规划入口](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/scripts/upgrade3/progressive_review_plan.py#L102-L170)

```text
原始 PLAN + 当前材料池
→ provisional_scope → level1_tools → level1_outline
→ source_routing → chapter_proposals → harmonized_scope
→ level2_tools → finalize_chapter_scope
→ chapter_need_analysis → chapters_tools（有需求才执行）
→ chapter_details（必要时按材料分批，再合并细纲）
→ 显式兼容恢复（本次新实验不使用）
→ 晚到来源增量路由与材料更新
→ whole_plan_improvement → affected_chapter_revision
→ 再补入负责人新采用来源的路由
→ case_groups → _attach_case_groups → DETAILED_REVIEW_PLAN / writer_packets
→ 独立 chapter_arrangement → 独立 review_unit_writer
→ 当前批次 manifest/jobs → full_review_draft 装配
```

- 代码没有单独名为 `level2_outline` 的模型阶段；二级范围细化由 harmonize、level2工具、finalize及后续章级细化共同完成
- `_post_case_review` 是旧函数名。当前 opt-in 分支在案例前调用它；案例后只正式附加并输出，没有再整章采纳/改写
- `--planning-revision` 在规划、编排、writer三个 CLI 均默认关闭。它决定输入投影和负责人合同，不是一个可忽略的显示选项。新实验应明确固定同一模式
- 规划器 `run()` 到 writer packets 即结束；不会自动调用编排和正文写作。[主链](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/progressive_review_plan.py#L4376-L5221)

## 2 阶段 材料 产物与实际消费者

表中 `P` 指 `optomind_research/runtime/upgrade3/progressive_review_plan.py`。阶段 JSON 通常包在 `response` 中；精确输入还要读 `RUN_STATE.json.stage_inputs` 和实际 messages，不能只看渲染的 Markdown。

| 阶段与函数 | 实际输入及投影 | 主要产物 | 下一消费者与观察位置 | 验证 |
|---|---|---|---|---|
| 装载 `load_original_plan/load_planning_pool` | 原始问题、facets、全池身份；`_compact_b_record`选择B字段，不是整篇全文 | 当前句柄映射、内存B池；RUN_STATE | 核查 pool/card/index 身份与内容版本；585不是引用目标 | S；本地全池待查 |
| 构思 `_model_stage(provisional_scope)` | 完整B摘要列表、原始PLAN；不带全池A/论文全文 | `stages/provisional_scope.json` | 一级工具请求、主题清单、provisional outline | S；C只证接线 |
| 一级工具 `_tool_cycle(level1)` | 构思提出的具体问题；本地优先、按需补检索/定向阅读 | `stages/level1_tools.json`、检索日志、需求缓存、精读/补充目录 | 合并入池、身份重绑、theme更新；纲要消费compact反馈 | C；本地答复的早期投影有遗漏 |
| 一级纲要 `level1_outline` | provisional、compact原始意图、主题清单、compact工具结果；不是重喂全池A/B | `stages/level1_outline.json`；可stop-after level1 | `shared_outline/review_argument/scope`进入路由和提案 | S |
| 路由 `_route_sources` | 更新后全B池，每批60；精简章纲、B/补充内容；不带完整A | `stages/source_routing*`及汇总 | 每章提案；检查有意义的材料为何路由/未路由 | S；C含正常、增量路径 |
| 提案 `_propose_chapters` 与 harmonize | 每章路由及章级工具投影；协调看各章提案和完整路由账本 | `stages/chapter_proposals*`、`harmonized_scope.json` | 恢复路由来源、生成二级工具需求与分工 | S |
| 二级工具与 finalize | level2具体问题；当前素材；finalize看更新纲要、主题、工具摘要及有补充的B | `level2_tools.json`、`finalize_chapter_scope.json` | 最终章节范围；可stop-after level2 | S/C工具边界 |
| 章级需求与工具 | 当前章范围、路由用途/限制、一级二级反馈 | `chapter_need_analysis.json`、有需求时`chapters_tools.json` | 章细化、负责人、案例的可用实质材料 | S/C；失败外层缓存见问题单 |
| 细化 `_chapter_details` | opt-in：选中来源A/B、精读、补充、工具叙述；候选导航与少量本地片段。默认旧模式不含同等A/B投影 | `stages/chapter_details*`、章计划与材料记录；超限批次/merge缓存 | 读 `paragraph_briefs` 的具体主张、展开、条件，不只数units | S；超限分支需本地代表章验证 |
| 全文协调 `_post_case_review` 内现有whole调用 | 各章细纲、引用来源A/B投影、跨章用途、材料增量、工具认识、主线；并非完整深读池 | `whole_plan_improvement.json` | 反馈交受影响负责人；不能把建议当已落实结果 | S；局部真实历史、C输入交接 |
| 负责人修订与恢复 | 该章完整计划、选中真实材料、反馈、必要候选与deep；成功/无改动/失败分别处理 | `affected_chapter_revision/<章>.json`及`successful/`、更新计划 | 对照修前计划与正式采用计划；局部失败应保留最后有效结果 | C恢复；真实新章效果待查 |
| 案例 `case_groups/_attach_case_groups` | 每章路由，每批≤96；当前units/briefs与候选实际材料；候选材料行的每个自由文本串裁到1200字符 | `case_groups/`批次、汇总、`supporting_studies`、材料包 | 查看用途是否进入编排任务，不能只看挂了source handle | S；C到case输入，不等于真实案例完整采纳 |
| 最终 `_assemble_final/_write_final_outputs` | 最终负责人计划+正式案例+工具材料+身份 | `DETAILED_REVIEW_PLAN.json`、`writer_packets/<章>.json/.md` | 重建shared_outline，传scope/argument；编排读取packet | S/C部分路径 |
| 编排 `build_chapter_view/run_arrangement/validate_arrangement` | 原brief原文；案例用途；精简来源导航，非全文材料。原A/B留本地catalog | `ARRANGEMENT_INPUT.json`（完整view，非实际compact消息）、`CHAPTER_ARRANGEMENT.json`、`SOURCE_USAGE.json`、运行报告/缓存 | writer消费`source_briefs/source_brief_details/portion`和source_catalog | C正常合并；CLI缓存往返已发现反例 |
| 写作 `build_unit_view/unit_messages/run_unit_writing` | 具体段落/表任务、原brief、单元上下文、全篇argument、实际材料；每源默认20000字符裁剪目标，结构化A/B仍可能超出 | `UNIT_INPUT.json`、`UNIT_MESSAGES.json`、RAW、`UNIT_RESULT.json`、`UNIT_BODY.md` | 比较请求、原返回、解析正文三层；补写另存且保留原文 | C；H已有正文消费；不保证任务充分展开 |
| 装配 `full_review_draft.run/assemble_documents` | 当前manifest/jobs、当前UNIT_RESULT、身份目录；可接跨目录reused_result | `ASSEMBLY_SUMMARY.json`、`REVIEW_DRAFT_HANDLES.md`、参考文献与报告 | BODY实验到此；装配complete≠问题解决≠科学通过 | C；L本地真实响应重放 |

[编排实现](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/chapter_arrangement.py#L257-L356) · [写作实现](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/review_unit_writer.py#L693-L795) · [装配实现](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/scripts/upgrade3/full_review_draft.py)

## 3 裁剪 恢复与超限不能忽略

1. B全池阅读是结构化投影；主题增量预览只保留序列化内容前1800字符。候选导航默认12篇，每篇本地片段默认2×1200字符；它不等于所有未选论文都被深读。
2. 全文协调的引用材料投影默认不包含全部deep；负责人修订才补紧相关deep。必须比较真实 messages，不能用磁盘上“有精读文件”证明协调已经看见。
3. 正常编排CLI保存的`ARRANGEMENT_INPUT.json`是完整view，不是实际compact请求。实验驱动应在模型边界另存实际messages；没有原请求时，只能标明按锁定配置重建。writer已有`UNIT_MESSAGES.json`。正常编排CLI的`--max-source-chars`默认每字符串240字符；底层helper默认900，不能混为一谈。原brief保留；writer才接完整catalog，并按每源20000字符目标裁剪额外材料；结构化A/B保留，仍可标`still_over_limit`。末端关键条件有无被裁掉，需看具体消息。
4. 只有chapter_details有材料级自适应分批及细纲merge。合并模型读分批细纲而不是重新读完整原材料；它仍可能压薄。全池构思、全局协调和案例不是自动由该分批机制兜底。
5. 一般stage缓存含消息/模型合同，但工具外层缓存、案例批次缓存与编排CLI缓存各有不同规则，不能一概称“兼容resume”。失败工具缓存和编排字段丢失见问题单。
6. 运行模型参数按stage另行收紧：路由/细化/案例thinking为2048；负责人与协调thinking至多1024；协调输出至多5000、细化/负责人至多16000。记录生效参数，不能只抄启动命令的全局8192/18000。

7. writer大输入检查目前主要告警，不等同于规划器的硬容量拒绝。默认输出4000、thinking 0；须明确选择实际参数。若关闭每源裁剪上限，应先预览实际token，不能假设输入一定放得下。编排CLI可在某章失败后继续并返回0，须消费逐章报告。

## 4 完整真实实验的阶段观察表

不设统一引用数、段落数或JSON状态及格线。每次检查选择原来就重要的机制、背景解释、比较、负面结果与条件，并沿相邻产物追踪。

| 何时 | 阅读哪两份实际产物 | 要判断什么；出问题先归因到哪里 |
|---|---|---|
| 构思完成 | 原始PLAN/B规划视图 ↔ provisional输入与原回答 | 原始研究问题和有价值方向是否进主线；输入没有先查投影，输入有但回答没有再查模型选择 |
| 一级/二级工具后 | 需求及真实答案/partial ↔ 下一纲要messages | 新认识、未知状态是否真的送达；不要把provider失败写成领域不存在 |
| 路由与章范围稳定 | 路由具体用途 ↔ 提案/finalize | 背景、机制、比较是否各有承载处；未路由论文不自动判错，需看遗漏的是不是认识任务 |
| 细化与分批merge后 | 每批生成细纲 ↔ merge/最终章细纲 | 已形成的解释、条件、不同研究关系是否保住；看任务实质而非只比任务总数 |
| 协调/负责人后 | 协调原反馈、修前章计划 ↔ 修后正式章计划 | 建议是否真正执行；是否把具体任务换成空泛总论；失败时哪个成功版本被采用 |
| 案例附加后 | case响应与真实材料 ↔ supporting_studies与SOURCE_USAGE | 案例为何值得讲、在哪个任务展开；`case_only`或笼统合并应亲读，不用引用数量掩盖 |
| 编排后/首次resume | 最新owner briefs ↔ fresh及cached arrangement | 合并/拆分关系、portion、原细节是否相同；失败validation应停止写作，不能只看CLI退出码 |
| 写作后 | 最终messages ↔ 原始模型返回 ↔ UNIT_BODY | 未送到是交接问题；原返回省略是生成问题；原返回有而正文丢是消费者问题。保留设置、指标基准、比较条件与限制 |
| 装配后 | 本次各UNIT_RESULT ↔ jobs/manifest/装配稿 | 是否混入旧正文、漏单元/表格、引用身份错配；`complete`只说明加载齐全，另读pending与科学正文 |

少量科学措辞偏强可记录并继续人工评价；明确错源、跳过未完成节点、丢原任务/表、混入旧稿应先停受影响分支。无需为追求零误差重跑所有上游。

## 5 下轮启动条件

- 用**新的规划输出根**，首轮不带`--resume`、不带`--recover-chapters-from`，不复制旧`RUN_STATE/stages/细纲/packets`进新根。中断后只resume同次、同配置运行，并先核查已知缓存接缝
- 原始PLAN、论文卡和索引可以复用；prior-reading必须经过身份、来源版本与当前任务核对。旧精读与旧规划的复用权限不同
- 补充素材可作为当前pool中真实source material复用；当前CLI没有一个等价于“安全导入全部历史supplement完成缓存”的统一参数。不要为了省调用复制整个旧run目录
- 明确固定规划、编排、writer的`--planning-revision`，显式填写新plan/packet、输出根、材料上限及实际模型参数。预算由本地检查当次token档位与当前账本
- 编排和writer是独立入口；需要本地提供实际批处理启动脚本/命令及生成的manifest/jobs。仓库没有本次完整批量编排写作的统一已验收启动器，历史路径默认值不能当本轮默认
- 装配显式传本次manifest/batch/output，检查每条`reused_result`；空掉本轮不应存在的旧front/back输入。首尾独立模块、全文编辑和出版均不纳入这次BODY实验

**启动前应先给本地核对上述路径与生效参数清单，再做代表章的输入预览及fresh/cache等价检查。两边审阅通过后由用户另行授权完整运行。**
