# 离线验证汇总

基线 `6aee581023ebd23921db45e1270f1f35213030a5`，没有供应商真实调用。以下统计有交集，不相加为总测试量；没有运行整个 tests/upgrade3 或完整正文链。

## 负责人最终合并检查

```sh
PYTHONUTF8=1 PYTHONPATH=/tmp/optomind-wo01-deps:. \
python docs/workorders/body-chain-20261003/records/preflight_consumers/run_offline_controls.py
```

**164 passed in 18.87s**，实际日志 FINAL_CHECKS.txt。运行器拒绝 socket 网络连接。新增 36 个编排缓存/独立入口检查、11 个工具消费者/恢复检查、3 个上下文检查；另外覆盖既有全局论证、工具缓存、本地查询、短链、反馈门槛和两处本地小修。生产修复前，原始八文件选集在独立未修改快照上 **102 passed in 2.20s**，见 BASELINE_CHECKS.txt。最终选集另加 12 个既有 global-argument 检查和上述 50 个新检查。

原 global-argument 测试把 collector 调用一次当作外层缓存假设；现在改为每次 resume 进入 collector。其“模型调用零增加”“主题 inventory 不变”断言完整保留；真正内层已满足任务零新增调用另以真实 collector/SQLite/cache 测试证明。

## 失败先行与其他相关控制

- F1 最终同一 36 检查文件放入未修改锁定快照：33 fail / 3 pass；修后 36 pass。不是拿测试数量代替验证：CACHE_MESSAGE_COMPARISONS、INVALID_WRITER_DISK_COMPARISONS、OLD_LOSSY_CACHE_RECOVERY 记录实际任务、消息和正文哈希
- F2/F3 首次失败与修后产物：见 f23/。内层 collector/journal、索引更新、显式重试、已有有用 partial、新增 required_outputs 和下一纲要实际 messages 均有对照
- 独立 reviewer 的 3 个上下文检查：原版运行时重放 3 fail；修后 3 pass，见 review/。分别验证本地恢复清除旧缺口、外部回答后不复活镜像缺口、增加需求后旧新答案均保留来源与实质限制
- 独立复核：41 个 F1/控制、20 个 F2/F3/上下文/控制通过；与上述选集重叠
- 扩展相关消费者选集曾在 baseline 和当时修后树均为 252 pass / 12 fail。12 个失败 node ID 完全相同，原因是缺本地历史稿/manifest及硬编码 Windows 工作目录。f1/REGRESSION_RESULTS.txt 保存精确集合，不把它们泛称为任何其他历史的“12项”
- 并行修改期间 portable 检查出现 1 个旧 collector 次数断言失败，该断言已按上文修正，负责人最终选集验证通过；不能把这个中间失败列为未修的环境问题

## 静态边界

```sh
git diff --check
python -m compileall -q \
 optomind_research/runtime/upgrade3/planning_retrieval_loop.py \
 optomind_research/runtime/upgrade3/progressive_review_plan.py \
 optomind_research/runtime/upgrade3/review_unit_writer.py \
 scripts/upgrade3/chapter_arrangement.py \
 tests/upgrade3/test_body_preflight_f1_arrangement_cache_consumer.py \
 tests/upgrade3/test_progressive_review_plan_tool_consumer_recovery.py \
 tests/upgrade3/test_preflight_material_recovery_context.py \
 tests/upgrade3/test_body05_global_argument_handoff.py
```

通过。CODE_BOUNDARY_CHECK.json 记录 AST 对照：`_messages_for` 与 `ProgressiveReviewPlanner.run` 和基线相同。没有修改提示词文件，新增生产变化仅涉及四个文件的消费者/恢复/材料投影函数。

## 不代表什么

- 局部受控答案不是新的科学质量实验；历史真实正文并未重新生成
- 已保存的旧有损缓存不会自动变完整；需要明确恢复来源或另行授权
- F2 是正式 CLI 使用的 adaptive 路径（CLI 构造并注入 make_retrieval_loop_runner）。自定义 legacy 非 adaptive 路径仍有外层缓存恢复限制，本轮未改
- F4 身份/历史精读兼容与首次案例 deep 交接尚未修；完整实验仍未授权
