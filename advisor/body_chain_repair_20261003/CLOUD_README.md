# BODY 修复：云端审核入口

分支：review-v2-body-chain-repair-20261003。测试代码已提交为 f067eeaf7ea6c157765bebaab88ef1839f50614f；此目录保存随后上传的局部验收材料。main、review-v2、lihonggang 和首尾模块分支没有合并或覆盖。

本轮实现的是链路修复，尚未进行完整模型测试。目标是保住已完成的正文规划与材料交接，并为明确漏写任务提供局部补写；不是人工改善当前稿件。

## 请按顺序阅读

1. 本文件与 PUBLICATION.json：当前代码身份、材料范围、公开副本省略项。
2. ROOT_LOCAL_REVIEW.md：根代理亲审和尚未验证事项。里面“未提交/未推送”描述的是发布前的验收状态，之后已提交并上传；不能以基线 e100e206 代替现在源码。
3. LOCAL_REPAIR_DELTA.patch：本轮开始前四份可用源码快照与当前代码的差异。Git 对比 e100e206 → f067eea 还包含先前运行期间的本地修复，两者不能混为单轮 diff。
4. PHASE1_OWNER_MATERIAL_CLOSURE.md/.json：P0574 本地卡片实际进入负责人输入，未灌整池。
5. chapter_recovery/RECOVERY_REPORT.json 及 stages/recovered_chapters/CH03.json、CH04.json：恢复出 13、12 条段落任务，CH04 只补 P0045/P0340。
6. writer_completion/WRITER_COMPLETION_REPORT.md；CH03_preview 与 CH05_preview 下实际 COMPLETION_INPUT/MESSAGES；对应 fake 目录只验证补写接线，不是科学内容。

## 核心源码

- optomind_research/runtime/upgrade3/progressive_review_plan.py：负责人材料闭包、按任务复用精读、recover_compatible_chapter_details。
- scripts/upgrade3/progressive_review_plan.py：显式 --recover-chapters-from / --recover-chapter。
- optomind_research/runtime/upgrade3/review_unit_writer.py：build_completion_payload、run_unit_completion、write_unit_completion。
- scripts/upgrade3/review_unit_writer.py：--existing-body / --complete-task；默认离线预览，真实调用需 --run。
- prompts/review_unit_writer.md：通用成表要求，未加入本题答案。
- tests/upgrade3/test_progressive_review_plan_{case_chain,material_reuse,chapter_recovery,transport_retry}.py 和 test_review_unit_writer_completion.py：本轮局部验收。

保持成功顺序：协调及负责人修订 → 正式案例附加 → 编排 → 写作。没有再增加“案例后整章重写”。恢复入口在全局协调之前，后续负责人失败也保留恢复的 baseline；现有成功 owner 结果优先。原始 stages/chapters 不被恢复文件覆盖。

## 上传边界

仓库公开。保留生成的 A/B、任务、条件、工具提纯、历史生成正文和模拟输出；local_passages/passages 原论文片段及 paper_contexts 中的 opening 原文开头已从公开副本移除，messages 中嵌入的 JSON 也处理了。没有上传密钥、PDF、全文缓存或数据库。PUBLICATION.json 列出逐文件省略字段。公开 messages 是处理后的可读副本，不宣称字节级原始录音。

本地绝对路径仅说明原运行来源。云端不存在这些文件，不要假定它们可读；局部测试主要使用临时夹具，可直接运行。更早完整运行来源见已有诊断文档与历史归档，不能拿恢复成果充当其他题目的生产缓存。

只有局部离线 30+8 项通过、语法检查和真实输入预览/模拟。Qwen 新调用 0，新增费用 0。完整科学质量、补写效果及泛化性尚未验证，不能据此声称恢复旧176稿的水平。

## 本轮请审核什么

只读评估与局部离线复现，不发起完整规划、正文写作、付费调用或自动合并。检查具体材料和生产调用点，不以 PASS 数量代替质量判断。提出仍会损失任务或材料的明确接缝、定位函数及最小修法；区分已复现的问题与推测。判断是否值得放行下一次完整本地测试，优先质量与低改动成本，避免继续叠加复杂审核平台。
