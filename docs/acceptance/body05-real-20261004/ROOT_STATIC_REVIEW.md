# WO05 根节点代码复核

受测代码：`6efc7a44615f0414109678c8c2ac7a040ee0ffc7`；本地隔离分支 `review-v2-body05-local-acceptance-20261004`。

已亲读与 WO04 的三个主要生产模块差异和云端实施记录。当前没有修改生产代码。

## 对真实旧产物的观察

`planning_level1/stages/level1_outline.json` 的模型 response 已有独立 `review_argument`；同一运行的 `DETAILED_REVIEW_PLAN.json` 顶层没有该字段，只有 `shared_scope`。这是实际交接损失，不只是合成测试。

## 修复路线判断

- `_review_guidance` 分别取得范围与论证，优先使用协调返回的 `finalized_review_argument`，没有最终论证时保留早期内容并说明未校准。没有用范围填补论证。
- 既有 whole-plan 协调提示只增加根据实际材料校准论证的任务；未另设第二 planner。最终 `_assemble_final` 把指导信息写入各 packet。
- 编排、反馈 CLI 去掉用 scope/purpose 代替论证的入口。编排与 writer 同时接收论证、范围及来源状态。
- 显式单元与段落身份优先；无显式身份的旧包保留兼容路径。拆合任务保留原 briefs 的内容与引用关系，冲突身份不按位置猜测。
- 晚到材料在协调前进入相关章节，保留协调→负责人→正式案例的执行关系。

静态路线合理，未发现值得立即修改生产代码的阻塞。`calibrated` 只是表示该字段来自协调调用，不能代替科学质量判断。

## 已完成的本地检查

云端提供的有界离线控制：392 passed / 2 deselected，25.99 秒。首次直接执行脚本时未设置 PYTHONPATH，出现目录导入问题；同一代码设置工作区 PYTHONPATH 后通过，未为此修改生产代码。

## 真实验收边界

准备复用旧真实五章任务及已使用的材料，重新调用协调、编排和单元写作。不会重跑五章 BODY，也不会以本次局部结果宣称旧 176 引用稿已全面恢复。真实协调返回的论证是否有价值、writer 是否保留原有细节，待亲读结果判断。
