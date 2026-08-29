# Research Harness 可观测性

`run_review_harness.py` 会从用户提交问题开始，自动生成以下运行级产物：

- `HARNESS_EVENTS.jsonl`：追加式总时间轴，记录运行、阶段开始/结束和异常。
- `HARNESS_METRICS.json`：总耗时、各阶段耗时、输入/输出 token、人民币成本和运行统计。
- `HARNESS_LOG_INDEX.json`：当前任务及历史重试任务的 `EVENTS.jsonl`、`RESULT.json`、`COST.json` 和状态文件索引。
- `HARNESS_RUN_REPORT.md`：面向人的简明运行报告。

所有阶段的详细模型调用、工具调用、验证、错误和恢复信息仍保存在各任务目录的
`EVENTS.jsonl` 中；总索引只保存路径、计数和脱敏摘要，不复制模型全文，也不记录
API 密钥。

`REVIEW_CONTENT_PACKAGE.json` 会登记以上四个产物，并同时写入系统自产的总耗时、
token、人民币成本和运行统计。预检预算只是费用上限，不计入实际支出。

若旧运行因进程中断或旧版预检逻辑造成总账缺失，可在不调用模型、不恢复科研任务、
不修改科学内容的情况下重建监控报告：

```powershell
py -3.11 scripts/rebuild_harness_observability.py `
  --run-dir "outputs/research_harness_e2e/<run_id>"
```

该重建工具会优先采用 Query Planner 自己的实际成本记录，并重新纳入历史重试目录，
避免把预算预留误当成消费，也避免隐藏早期失败。
