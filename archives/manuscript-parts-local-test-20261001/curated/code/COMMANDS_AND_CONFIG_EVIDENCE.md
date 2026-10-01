# 命令与配置的记录边界

本文件是归档时的说明，不是待执行任务。归档没有执行任何下列模型命令。

## 有原文件依据的入口

- 初次规划：`RUN_PLAN.ps1`，参数 Stage=level1|level2|full，Resume开关；实际参数含 `scripts/upgrade3/progressive_review_plan.py --run --planning-revision`，模型/池/PLAN/材料索引/输出目录等详见脚本原件。运行过程中有多次补修与恢复；脚本最终版不能替代各次启动时的历史版本。
- BODY恢复规划：`body_restore_20261001/RUN_RESTORE_PLAN.ps1`。对应 LEVEL1_RUN.log、LEVEL1_RETRY_BOUNDARY.log、FULL_RESUME_TOOL_REUSE.log 和 RESTORE_STATE.json。
- BODY编排/写作：`RUN_BODY.py` 与 `body_restore_20261001/RUN_RESTORED_BODY.ps1`，Plus批次补建脚本 `BUILD_PLUS_BATCH.py`。实际 Plus设置由各 UNIT_INPUT.json / UNIT_MESSAGES.json / raw 和批次/运行报告确认，不以脚本默认值替代。
- 首尾真实串行：`RUN_PARTS.py`；两轮分别由 parts 和 parts_tuned 保存实际messages、raw、usage、预检、结果。后轮不能冒充首轮同提示。

## 程序测试

历史报告记录目标回归及少量实际材料重放；最终亲审报告记录 parser/citation 10项、首尾/placement/材料交接53项、串行依赖5项等。并非所有测试都保留完整 shell命令和stdout，不能从数量逆推出命令或声称全量回归全部通过。相应测试源代码保留在测试提交中。

若日志或报告有明确命令，按原文归档；没有记录的命令视为缺失，不重新执行来补造历史。模型名称、计费输入/输出token、返回finish_reason，以实际raw/usage及账本为准。

## 原报告状态有历史性

body_plus/RUN_BODY_REPORT.json保留了早期write解析失败的状态。后续离线重解析、保留独立表、重装配后，RESTORE_STATE.json、各UNIT_RESULT/ASSEMBLY_SUMMARY及亲审报告确认最终正文完整。归档保留失败报告，并没有把它改成成功。不得只读一个较早driver报告断言整轮未完成，也不能删去它来隐藏失败。
