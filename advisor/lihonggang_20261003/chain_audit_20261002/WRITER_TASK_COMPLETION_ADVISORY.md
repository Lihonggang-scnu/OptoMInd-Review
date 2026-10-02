# BODY 写作任务完成度事实与建议

日期：2026-10-03
范围：本轮 `body_arrangement_case_restore_20261002`、`body_writing_case_restore_20261002`、`body_assembly_case_restore_20261002`。本文件只做离线产物核对，不修改代码、规划或正文。

## 先给结论

20/20 个单元都有写作结果，表示程序完成了 20 次 writer 调用和装配；它不等于每个单元已消费完所有 paragraph task/table task。当前可确认两处任务完成度问题：

- CH03_U01 的一张已规划比较表没有进入正文。
- CH05_U02 的第二个 paragraph task（类器官、培养组学、免疫/类器官共培养）没有形成正文；正文只有第一任务的肌苷–UBA6 段，529 字符。

这两处属于写作任务消费遗漏，不应推断为全部来源数少或整篇正文必然低质。其它任务允许合理合并、取舍和不逐篇引用；本建议只标出有明确输入任务、输出缺失的情况。

## 数量口径

证据来源：

- `body_assembly_case_restore_20261002/assembled/BODY_COMPLETION_AUDIT.json`
- `body_assembly_case_restore_20261002/assembled/ASSEMBLY_SUMMARY.json`

实际记录：

- 20 个单元均加载并写入，`missing_units=[]`，20 次 writer calls，writer 后追加模型调用为 0。
- 规划任务为 44 个 paragraph tasks、9 个 table tasks；正文实际为 8 张表，缺 1 张。
- 编排层安排 142 个 distinct source handles；装配目录合并为 141 个论文身份，原因是 P0402/P0576 共享规范化 DOI/题名；不是材料静默丢失。
- 正文实际使用 120 个论文身份，21 个未使用。这个差值本身不能判定质量问题，因为来源可合理取舍，且表格和段落的使用口径不同。
- 装配状态为 `complete`，但 `planning_status=partial`，且审计明确保留 root 内容复核；因此“装配完成”与“所有科学任务完成”必须分开读取。

## 确证一：CH03_U01 表格任务未消费

输入任务：

- 编排原件：`body_arrangement_case_restore_20261002/CH03/CHAPTER_ARRANGEMENT.json`，对象 `units[unit_id=CH03_U01].table_tasks`，唯一任务 `CH03_U01_T01`。
- writer 实际消息：`body_writing_case_restore_20261002/CH03_U01/CH03_CH03_U01/UNIT_MESSAGES.json`，user payload 仍含 `table_tasks`，表格目的为比较 FMT、难治队列、益生菌/菌群组合及饮食/生活方式的证据等级、疗效指标和局限。
- 该任务带有明确的行级来源用途，包括 P0583、P0577、P0578、P0143、P0387、P0156、P0452、P0576 等。

输出：

- `body_writing_case_restore_20261002/CH03_U01/CH03_CH03_U01/UNIT_BODY.md` 只有两段正文，没有 Markdown 表格。
- `body_writing_case_restore_20261002/CH03_U01/CH03_CH03_U01/UNIT_RESULT.json` 的 `completion_status=complete`、`issues=[]`，说明 wrapper/调用成功，但没有证明 table task 已完成。
- `BODY_COMPLETION_AUDIT.json` 也记录：CH03 规划 1 张表、实际 0 张表。

短输出证据：正文从“当前微生物组干预策略……”开始，随后是“非 FMT 干预策略……”，两段均无表格块。现有段落本身包含部分表格材料，问题是比较表任务没有落地，不是 CH03_U01 全部正文不存在。

## 确证二：CH05_U02 第二个 paragraph task 未消费

输入任务：

- 编排原件：`body_arrangement_case_restore_20261002/CH05/CHAPTER_ARRANGEMENT.json`，对象 `units[unit_id=CH05_U02].paragraph_tasks`。
- writer 实际消息：`body_writing_case_restore_20261002/CH05_U02/CH05_CH05_U02/UNIT_MESSAGES.json`，user payload 仍含两个完整任务。
- P01：肌苷–UBA6 轴、CheckMate 025 血浆关联、TME 绝对定量和同位素溯源，主要来源 P0582/P0462。
- P02：PDO 类器官缺乏免疫组分、培养组学补足宏基因组遗漏、多组学和免疫–类器官共培养，来源用途明确列出 P0172、P0367、P0591。

输出：

- `body_writing_case_restore_20261002/CH05_U02/CH05_CH05_U02/UNIT_BODY.md` 只有一个 529 字符段落，内容从“肌苷 -UBA6 轴……”开始，覆盖 P01 的肌苷、血浆 OS、TME 定量缺口和 P0462 的方法路径。
- 正文没有 PDO、培养组学、宏基因组遗漏物种、免疫–类器官共培养等 P02 的对应展开。
- `UNIT_RESULT.json` 仍为 `completion_status=complete`、`issues=[]`，所以这里再次证明程序完成状态不是任务语义完成状态。

## 建议给云端写作代理

保持已写好的正文，先做任务完成度回读，再决定是否需要局部写作修补。回读应分两层：程序可以可靠完成结构核对（例如任务/表格数量、表格块是否存在、未知句柄、wrapper 泄漏）；任务是否在正文中完成了其实质内容，需要人工或 LLM 语义阅读，不能宣称纯离线规则能完整判断。词面未出现只作为候选证据，根本判据是任务要求的实质判断、展开关系或比较是否缺失：

1. 从原始 `UNIT_MESSAGES.json` 或编排 JSON 读取每个单元的 paragraph task/table task；记录 task id、point、development、来源用途。
2. 对已有 `UNIT_BODY.md` 做两层回读：先用程序检查任务/表格结构、表格块、引用句柄和 wrapper；再由人工或 LLM 判断每个任务的核心判断、展开关系和证据用途是否实质落地。来源未被引用不自动判失败，词面未出现只列为候选，不作为自动判定；只有任务实质内容缺失才进入补写候选。
3. CH03_U01 的缺表可作为“结构核对 → 语义确认 → 局部处理”的机制验收样例；是否补表由云端通用方案在确认后决定，不把本轮样例直接当作已执行修稿。
4. CH05_U02 可作为“输入任务存在但输出实质内容缺失”的语义验收样例：由云端方案确认是否需要在已有肌苷–UBA6 段后局部补写 P02；保留正确正文，不把该样例写成当前已经修好的链路。
5. 修补后重新离线核对：20 个 unit 仍在、表格由 8 变 9、CH05_U02 的 P02 已有正文落点、引用仍来自 supplied handles，且不产生 JSON wrapper 泄漏。
6. 若不再调用模型，则把两处标为 pending task completion，交 root 做最终取舍；不要用“20/20 complete”掩盖任务级未完成，也不要因为 120/141 的来源使用差异机械补齐。

推荐的工程边界是“通用任务完成回读 + 必要时局部补写”：结构核对由程序完成，语义覆盖由人工或 LLM 完成；不重跑规划、不重写好正文、不建立复杂审核平台，也不依赖只报错字段。云端应先提出并验证通用方案，再决定是否处理上述两个样例；不能因发现或修补样例就宣称整条链路已修好。

## 证据路径索引

- 编排：`F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\body_arrangement_case_restore_20261002`
- writer：`F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\body_writing_case_restore_20261002`
- 装配：`F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\body_assembly_case_restore_20261002\assembled`
- 任务完成审计：`...\assembled\BODY_COMPLETION_AUDIT.json`
- 装配摘要与保留问题：`...\assembled\ASSEMBLY_SUMMARY.json`

