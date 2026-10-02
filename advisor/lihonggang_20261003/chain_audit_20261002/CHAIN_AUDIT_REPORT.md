# Chain audit 2026-10-02

只读离线复现，代码来自当前隔离 worktree；未调用模型或网络，未修改生产源码。

## 1. 自适应 chapter_details merge 的确证风险

复用 `tests/upgrade3/test_progressive_review_plan_chapter_capacity.py` 的批次计数夹具，将 6 个 source records 强制分成 2 批。fake planner 对两批返回正常 `chapter_plan`，对 merge 返回合法 JSON：`{"chapter_plan":{"thesis":"merged","reader_objective":"merged","units":[],"source_handles":[]}}`。

结果：

- `progressive_review_plan._chapter_details_adaptive_record` 返回 `mode=adaptive_source_batches`、`batch_count=2`，没有报错；
- merge 记录写入 `adaptive_batches/merge_*.json`，缓存状态为 `complete`；
- 将该返回继续交给 `_assemble_final` 和 `_write_final_outputs` 后，`writer_packets/CH01.json` 与 `DETAILED_REVIEW_PLAN.json` 均实际保留 `chapter_plan.units=[]`；
- 因此这是已实证的结构性缺口：merge 只做 JSON/top-level 接受，没有核对批次中的 unit/source handles 是否保留。

代码入口：

- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\optomind_research\runtime\upgrade3\progressive_review_plan.py:1271-1375` 生成批次、merge 并缓存；
- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\optomind_research\runtime\upgrade3\progressive_review_plan.py:4420-4453` 直接消费 merge 返回并写 packet；
- 现有容量测试只验证正常 fake merge 保留全部 findings，未覆盖合法空 merge。

复现实验脚本与 JSON：

- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\chain_audit_20261002\audit.py`
- `F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\chain_audit_20261002\CASE_CHAIN_AUDIT.json`

## 2. retrieval adapter 的 `complete` 含义与 unmet 可见性

用不存在的本地索引、`allow_external=False`、一个 supplement need 离线运行 adapter。结果：

- adapter 顶层 `status="complete"`；
- `retrieval_loop.needs[0]` 保留 `status="stopped_no_query"`、`still_missing="specific evidence"`；
- `supplement_results[0]` 保留 `status="unmet"`；
- 这证明 `complete` 表示 retrieval loop 已运行结束，不表示 need 满足，不能直接把它解释为材料丢失；
- 但把该 result 传入 `_chapter_details` 后，章节模型 payload 的 `relevant_tool_feedback=[]`。原因是 `_chapter_details` 的 `nested_supplement_groups` 只接受带 `gap_id` 或 `fulfillment_judgment` 的 group，而 adapter 产生的 unmet row 没有这两个字段。

因此第 3 点应精确表述为：need 状态和 unmet 结果仍被保存，流程完成语义本身合法；但在章节 owner 的实际 prompt 中，纯 unmet 行会被过滤，模型看不到“该需求未满足”的显式反馈，只能收到没有新材料这一事实。这是可复现的反馈可见性风险，不是顶层 `complete` 本身的丢料 bug。

代码入口：

- adapter 固定返回 `status="complete"`：`F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\optomind_research\runtime\upgrade3\progressive_review_plan.py:6057-6120`；
- unmet need 原样来自 retrieval loop：`F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\optomind_research\runtime\upgrade3\planning_retrieval_loop.py:687-706,845-878`；
- 章节过滤反馈：`F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree\optomind_research\runtime\upgrade3\progressive_review_plan.py:4228-4310`。

