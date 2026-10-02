# BODY 链路局部修复交接

本轮只修链路并做局部离线测试，完整测试等待云端审核。没有重新生成整篇正文，没有调用 Qwen。

先阅读 ROOT_LOCAL_REVIEW.md：包含根代理亲审、真实材料观察、修复范围及未验证事项。

## 文件导航

- PHASE1_OWNER_MATERIAL_CLOSURE.md / .json：负责人接收本地卡片的真实局部验证。
- chapter_recovery/RECOVERY_REPORT.json：CH03/CH04 成功成果的恢复结果；详细任务在其 stages/recovered_chapters 下。
- writer_completion/WRITER_COMPLETION_REPORT.md：补写工具接线与局部测试。
- writer_completion/CH03_preview、CH05_preview：真实历史输入的定点任务与实际 messages；零模型调用。
- writer_completion/CH03_fake、CH05_fake：模拟补写，仅证明程序路径，不是科学内容。
- before/：本轮开始前的部分源码备份，不是可直接生产的平行版本。

## 修改位置

代码工作区：F:/OptoMind-Review-2/outputs/lihonggang_full_acceptance_20261001_50cny/worktree

- optomind_research/runtime/upgrade3/progressive_review_plan.py：负责人材料闭包、精读复用、兼容章节恢复。
- scripts/upgrade3/progressive_review_plan.py：显式 --recover-chapters-from / --recover-chapter。
- optomind_research/runtime/upgrade3/review_unit_writer.py：定点任务补写及材料选择。
- scripts/upgrade3/review_unit_writer.py：--existing-body / --complete-task，默认预览。
- prompts/review_unit_writer.md：通用成表要求。

已有案例顺序保持：协调与负责人修订 → 正式案例附加 → 编排 → 写作。没有增加案例后再重写整章的环节。

## 局部验证

在代码工作区执行以下两组（本轮已由 Luna 执行，根代理审阅代码与实际产物）：

```powershell
C:\Anaconda\python.exe -X utf8 -m pytest -q tests/upgrade3/test_progressive_review_plan_chapter_recovery.py tests/upgrade3/test_progressive_review_plan_case_chain.py tests/upgrade3/test_progressive_review_plan_material_reuse.py tests/upgrade3/test_progressive_review_plan_transport_retry.py
C:\Anaconda\python.exe -X utf8 -m pytest -q tests/upgrade3/test_review_unit_writer_completion.py
```

结果：30 + 8 passed。未运行全套测试。完整科学内容效果尚未验收；不能以这些数字宣称恢复旧176稿的水平。

未提交、未推送。源码基于 e100e206 加原有未提交修复及本轮局部修复，不能用基线提交代替当前测试源码。
