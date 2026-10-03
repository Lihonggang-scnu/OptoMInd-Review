# BODY 链路云端工单包 — 2026-10-03

本包把已审阅的 BODY 链路修复方案转换为云端实施目标的有界工单。这里是文档和执行控制包；本包不直接修改生产代码、不调用付费模型、不运行完整综述。

## 依据文件

按以下顺序阅读：

1. `references/BODY_CHAIN_CONSOLIDATED_REPAIR_PLAN.md` — 已审阅主方案及 F01–F18 索引。
2. `references/BODY_CHAIN_REPAIR_REVIEW.md` 与 `references/BODY_DESIGN_CONTINUITY_AUDIT (2).md` — 合并审阅报告。
3. `advisor/body_design_continuity_20261003/` — 云端可访问的连续性核验材料。
4. `advisor/body_chain_repair_20261003/` — 现有 BODY 修复基线和记录。

本包已附带 `references/` 中的报告和主方案。标为 `LOCAL_ONLY` 的来源是本地诊断或历史资产，只能引用文件名和 finding 编号；云端检出不应依赖它，也不能把它重建为生产输入。

## 云端限定指令

旧版 `README.md`、根目录 `AGENTS.md` 和冻结说明描述的是本地审阅工作区及其不发布状态。用户已授权本次限定范围的云端实施，云端 worktree 是实施目标，以下规则控制工单顺序。授权不包含凭据、付费调用、完整综述或超出指定 worktree/工单的改动。修复分支可以提交并推送阶段成果供验收；交接分支、`main`、`review-v2` 及 `lihonggang` 分支保持不变。

## 所有工单必须保持的决定

- Keep the BODY order: global coordination and owner revision → formal case attachment → arrangement → unit writing.
- Keep lightweight front/back work in its existing independent post-body modules.
- Treat usable content and independent per-paper A/B cards as separate questions.
- A review-derived identity-only row is valid when usable review/tool content supplies the study substance; identity alone never licenses invented content.
- One table may complete multiple comparison tasks; completion is judged from content and conditions, never from table or paper counts.
- Preserve the case after owner decision and do not reintroduce whole-chapter review after case attachment.
- Preserve useful partial material while exposing unresolved gaps.
- Reuse existing stores, retrieval loops, coordinator, arrangement, and writer; do not create a second planner or an unbounded retrieval loop.

## 闸门顺序

入口授权只覆盖 `00.md` 和 `01.md`。01 完成后必须停止，并随工单记录写出精确标记 `READY_FOR_ASTRA_01`。02–07 继续等待明确授权；局部检查通过或随后讨论更大修复范围，都不构成隐式授权。后续授权必须由用户明确作出，并由 root 转发到实施线程；授权应点名允许的工单。

| Work orders | Gate | Allowed activity |
| --- | --- | --- |
| 00 | Entry | Baseline/evidence alignment; no algorithm change. |
| 01 | Entry; stop after completion | Owner recovery, material freshness, cache projections, and bounded chapter-batch recovery. |
| 02 | Explicit approval after 01 | Directed-reading repairs. |
| 03 | Explicit approval after 02, or an approval that explicitly names 02–03 | Supplement/retrieval repairs. |
| 04–05 | Explicit approval after 02–03 | Identity/material admission, late routing, and organization continuity. |
| 06 | Explicit approval after 04–05 | Writer output consumption and completion boundaries. |
| 07 | Explicit approval after 06 | Local short-chain validation and cloud handoff; never a full run. |

Every implementation record must use `RECORD_TEMPLATE.md`, identify actual files/functions and observed outputs, and state unresolved items. “All tests passed” without the concrete path and output is not an acceptable record.

## Common execution limits

- Use only repo-relative source paths in cloud records.
- Do not read, print, copy, or upload credentials.
- Do not run a paid model call, full planning run, full-body run, or full test suite for this package. Targeted local regression and fixture checks are allowed within an opened work order and must not be described as full scientific acceptance.
- Network/model boundaries may be replaced by controlled fixtures in approved local tests; the repair functions under test must remain real.
- Preserve unrelated worktree changes and existing evidence. Do not reset, clean, or overwrite a run directory.
- Record implementation and tests on the cloud repair branch; this package itself contains no production implementation.
