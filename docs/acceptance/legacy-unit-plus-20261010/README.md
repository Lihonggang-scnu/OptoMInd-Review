# 旧分单元作者 Plus：2026-10-10 本地真实验收归档

**本轮部分完成。前五章21/29单元已生成，第六章首个请求读取超时，后续八个单元缺失；没有完整七章稿。**

本包仅归档，生产代码和提示词未改。受测源码为 **054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512**，本次归档提交另算。新独立30元账本：已结算 **1.936256 CNY**，未确定占额 **1.639640 CNY**。没有重试、释放占额、调用Max、重跑规划或生成指南。

## 云端优先阅读

1. [用户原始指令](ORIGINAL_USER_REQUEST.md)：区分用户方案与本地执行。
2. [根智能体完整亲审](ROOT_REVIEW.md)：已写五章的改善、遗漏、重复、输入冲突及旧稿对照。
3. [正式装配的原始句柄部分稿](assembled/REVIEW_DRAFT_HANDLES.md)；[读者编号稿](assembled/REVIEW_DRAFT.md)。均明确标21/29受限稿。
4. [执行终态](FINAL_RUN_STATUS.md)、[正式运行报告](RUN_REPORT.json)、[费用导出](LEDGER_SUMMARY.json)、[禁止新增调用的只读恢复核对](RECOVERY_GUARD_AUDIT.json)。费用未知保持占额，不能当作0元。
5. [实际发送与配置审计](LIVE_AUDIT.json)、[参数审计](EFFECTIVE_REQUEST_AUDIT.json)、[29单元预览验收](READINESS_CHECKPOINT.md)。22/22实际请求与预览语义一致；未启动的七个单元只有预览，不冒充实际发送。
6. [漏任务实例](ROOT_TASK_CONSUMPTION_EXAMPLE.json)、[输入身份冲突实例](ROOT_INPUT_IDENTITY_CONFLICT.json)。完成标记和合法引用token不能证明知识展开及科学引用正确。
7. [同范围引用对照](HISTORICAL_REFERENCE_STATS.md)、[旧稿固定版本与哈希](ROOT_COMPARISON_PROVENANCE.json)。前五章新稿132、173篇Flash稿124、117篇Plus指南稿83；旧两稿整篇重新核对分别173/117。篇数用于观察覆盖，不能代替内容判断。

## 本轮实际机制

当前完整FULL_BODY_INPUT → 正式legacy_unit_writer → 逐单元qwen3.5-plus → 生产装配。全部保留7章/29单元/92段落任务/3表格任务。作者采用历史生效system提示词，读完整自身任务及材料、章节框架和其他单元职责，**不读实际前文，不读旧稿，不读GUIDE**。

配置：thinking=true/8192、回答32768、实际max_completion_tokens40960、stream=true、读取超时1800秒/整体3600秒、max_retries=0。成功单元完整响应先落盘再解析；第22次保存推理partial，没有正文/finish/usage，报qwen_stream_read_timeout后自动停止新增调用。

前五章已亲读，有具体机制、比较、负面结果和条件，较少任务书口吻。但Ch3 U3_5三任务仅展开一项，Ch1/Ch4及FMT案例仍重复；TACITO的P0593输入身份冲突被作者接受。保留原样交云端独立复读，没有把根科学评价喂回作者。

## 原始请求与恢复

`units/<章>/<单元>/<缓存哈希>/attempt_001/`保存实际消息、请求参数、原始响应、结果和正文。第六章失败请求另有RUN_ERROR与partial流。大于600KB的消息见同目录`*.parts.json`，按顺序拼接，SHA-256在清单中；原材料及消息没有无故裁剪。

原输入已有固定归档，不重复上传：[75分片索引](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/f5103e99c624951ab549a90b443ab090876ca301/docs/acceptance/guide-content-units-20261010/input/FULL_BODY_INPUT.json.parts.json)。拼接原输入SHA-256：`6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7`。启动命令见STARTUP_RECORD；源代码锁定054f94a3，local run目录保留专用账本和所有原件。

[PUBLIC_FILE_INDEX](PUBLIC_FILE_INDEX.json)给出191个本地原件的SHA、公开路径及分片；[上传/恢复边界](OMITTED_AND_RESTORE.md)说明本地未上传项。排除密钥、SQLite数据库、tokenizer原文件及未经授权论文全文。原始消息内是已有结构化理解材料，未另搬论文文件；全部公开候选经过本地凭据检查。

## 建议云端复读的问题

先判断本地测试是否忠实恢复旧机制及是否正确选择基线，再读正文。请独立判断：旧单元路线是否更能保住任务展开；漏任务的通用消费/结束判定如何处理；身份冲突应在哪个接缝处理；重复是否需要后置协调。保留有用机制和反例，不把本题科学答案加到生产提示词。当前服务超时和未决费用尚待本地核对，云端不要发起真实调用或把部分稿写成七章成功。
