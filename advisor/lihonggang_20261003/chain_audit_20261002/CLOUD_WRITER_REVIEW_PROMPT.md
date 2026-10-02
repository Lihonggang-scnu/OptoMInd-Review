你接手的是 OptoMind 本轮 BODY 写作结果的任务完成度复核。请先阅读：

- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\chain_audit_20261002\WRITER_TASK_COMPLETION_ADVISORY.md`
- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\body_assembly_case_restore_20261002\assembled\BODY_COMPLETION_AUDIT.json`
- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\body_assembly_case_restore_20261002\assembled\ASSEMBLY_SUMMARY.json`

只读核查原始任务和正文后，先提出通用的任务完成度方案，再决定最终动作。结构核对（任务/表格数量、表格块、未知句柄、wrapper）可由程序完成；任务是否完成其实质内容必须人工或 LLM 语义阅读，不能宣称纯离线规则覆盖全部情况。已确证：20/20 writer 调用和装配完成，不等于任务全部消费；CH03_U01 的 CH03_U01_T01 比较表输入和 UNIT_MESSAGES 都存在，但 UNIT_BODY.md 没有表格；CH05_U02 的第二个任务明确要求 PDO/培养组学/免疫–类器官共培养，UNIT_MESSAGES 含 P0172/P0367/P0591，正文只有第一任务肌苷–UBA6 段（529 字符）。

保持已写好的正文和上游规划。词面缺失只作为候选证据，根本判据是任务实质内容是否缺失。CH03_U01 缺表、CH05_U02 缺 P02 是机制验收样例；云端先提出并验证通用方案，再决定是否局部补写，不能把处理这两个样例直接称为链路已修好。若方案确认需要处理，才分别考虑复用现有正文补表或补 P02。允许合理合并、取舍和不逐篇引用，不把 142 handles、141 catalog identities、120 used papers 当硬性引用配额；P0402/P0576 的同 DOI/题名合并是已知身份口径。不要重跑上游、不要全量重写、不要仅依据 status=complete 放行，也不要把所有未引用来源都判为问题。

若通用方案最终决定执行局部修补，必须使用原始 supplied material/handles，保留现有正确段落和条件边界；修补后再离线核对 20 units、9 planned tables 对应 9 actual tables、CH05_U02 P02 有正文落点、无未知 citation 或 wrapper 泄漏。若不修补，则明确记录 pending，不把样例当作链路已修好。最终报告应区分：程序调用完成、任务完成、正文质量判断。


