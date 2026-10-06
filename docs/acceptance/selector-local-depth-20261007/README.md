# 第一层自主选择真实测试公开归档

本包记录 2026-10-07 在提交 `e7a1d835ccee9430558596d76f77343f663a3942` 上进行的一次第一层自主单元选择调用。建议阅读顺序：

1. [USER_REQUESTS.md](USER_REQUESTS.md)：用户意图和本地执行归属。
2. [ARCHIVE_SCOPE.json](ARCHIVE_SCOPE.json)、[SECURITY_SCAN.md](SECURITY_SCAN.md)：测试边界、脱敏和省略项。
3. [first_layer/SELECTION_REQUEST.json](first_layer/SELECTION_REQUEST.json)、[first_layer/FIRST_CALL_CHECKPOINT.md](first_layer/FIRST_CALL_CHECKPOINT.md)：离线准备、完整7章29单元输入和实际请求。
4. [live/SELECTION_RESULT.json](live/SELECTION_RESULT.json)、[live/EFFECTIVE_REQUEST.json](live/EFFECTIVE_REQUEST.json)、[live/RAW_RESPONSE.json](live/RAW_RESPONSE.json)、[live/RUN_REPORT.json](live/RUN_REPORT.json)：实际响应、参数、用量和结果合同。
5. [reviews/ROOT_QUALITY_REVIEW.md](reviews/ROOT_QUALITY_REVIEW.md)：返回后的独立质量复核；它没有进入模型请求。
6. [costs/COST_SUMMARY.json](costs/COST_SUMMARY.json)、[SHA256_MANIFEST.json](SHA256_MANIFEST.json)：费用与逐文件完整性。

## 测试范围

本次只调用第一层选择器：输入完整7章29单元细纲，模型返回4个同章逻辑组、5个单元；实际费用 `1.420884 CNY`，本轮新批准40元后剩余 `38.579116 CNY`。没有调用按需 access、Max owner、编排模型或正文 writer。第二层源码和配置与基线 git tree bytes 相同。

模型可见选择消息含完整 chapter_plan、研究问题、全篇职责和同章边界；默认材料通道是稳定身份/回读导航，不把轻量目录当已读证据。选择结果投影保留完整来源材料通道并交给下层，但本次没有付费执行下层。

## 版本、费用和公开副本

源工作树在测试时干净。共享账本由85提高到125以容纳用户新批准的40元轮；历史 settled/reserved/uncertain 只以汇总保留，SQLite 未上传。实际 reservation 为 `3.319596`，结算 `1.420884`，未决为0。费用是本地账本估值，不宣称供应商最终扣费。

`inputs/FULL_CHAPTER_PAYLOADS_PUBLIC.json` 是可公开重建输入；明确的全文/`local_passages` 字段只保留原 SHA256 和长度标记。33MB 的 `SELECTED_ON_DEMAND_PAYLOADS.json` 是未实际发送的离线投影，未重复上传，见 [indexes/SELECTED_ON_DEMAND_PAYLOADS_OMITTED.json](indexes/SELECTED_ON_DEMAND_PAYLOADS_OMITTED.json)。

归档整理阶段未新增模型调用；受测版本与归档提交分别记录，上传不代表重新测试。
