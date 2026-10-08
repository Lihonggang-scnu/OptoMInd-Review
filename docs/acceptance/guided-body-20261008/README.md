# Guided BODY writer 公开归档（2026-10-08）

这是本轮人工 guide-first A/B 写作者实验的最终本地公开快照，绑定源码 `e63b12282d2269ca146a53266a47d06f2309a47e` 与入口 `scripts/upgrade3/guided_body_writer.py`。实验是 human guide 新作者实验，不是 Qwen 自主中间层验证；A/B 均已停止，没有追加模型调用、付费重试、补写或算法修改。

## 最终状态

- **A**：7 次 author 调用、7 章完成，实际费用 **6.750730 元**。完整正文：[routes/A/FULL_BODY.md](routes/A/FULL_BODY.md)，结果与逐次材料见 [routes/A/FULL_BODY_RESULT.json](routes/A/FULL_BODY_RESULT.json) 和 `routes/A/stages/`。ROOT 已通读 A 全部 7 章，但没有把它批准为默认路线。
- **B**：6 次 author 调用；Ch1–Ch5 完成，Ch6 返回可读正文但 `finish_reason=stop` 且缺 guided metadata/complete 标记，Ch7 未调用，实际费用 **4.971964 元**。保留的未完成稿：[routes/B/FULL_BODY.md](routes/B/FULL_BODY.md)，实际请求和失败停点见 `routes/B/stages/`。
- **最终账本**：60 元上限；此前已结算 6.6382292 元，本轮增量 11.722694 元，累计已结算 **18.3609232 元**，held **0**，剩余 **41.6390768 元**。详见 [FINANCE_SUMMARY.json](FINANCE_SUMMARY.json)。

## ROOT 最终审计

优先阅读 [ROOT_GUIDED_BODY_REVIEW.md](review/ROOT_GUIDED_BODY_REVIEW.md)、[ROOT_QUALITY_LOG.json](review/ROOT_QUALITY_LOG.json)、[ROOT_ACTUAL_REQUEST_AUDIT.json](review/ROOT_ACTUAL_REQUEST_AUDIT.json)、[SOURCE_CONFLICT_SPOTCHECK.json](review/SOURCE_CONFLICT_SPOTCHECK.json) 和 [FINAL_EXECUTION_SUMMARY.json](review/FINAL_EXECUTION_SUMMARY.json)。请求线缆检查见 [A_B_MESSAGE_PREFIX_WIRE_CHECK.json](review/A_B_MESSAGE_PREFIX_WIRE_CHECK.json)。这些是运行后审阅记录，没有被写回模型请求。

实际 author 请求的四个顶层字段为 `manuscript_guide`、`chapter_assignment`、`materials`、`accepted_body_markdown`；精确的旧 `task`/unit/completion-key 结构计数为 0。但材料投影中仍发现 `outline_action` 写作安排，本包按实际情况保留并标记该接缝，不能概括为所有历史编排字段都不存在。

共享 `FULL_BODY_INPUT` 只保留一份于 `shared/canonical_input/`；A/B 各自的请求、messages、返回、有效参数、用量、guide 绑定、正文与恢复/失败记录分开保存。SQLite、密钥、认证头、签名下载链接、传输 SSE/raw 流、第三方论文全文和权利不明全文字段不公开；本地路径只是审计定位，不是可访问链接。所有可识别的 rights-bound full-source 字段均以明确占位符替换。

大文件按严格 UTF-8 字符边界切分，每片不超过 600,000 bytes。发布前请运行 `VERIFY_CANONICAL_PAYLOADS.py <this-directory> --index`，并检查 [PUBLIC_FILE_INDEX.json](PUBLIC_FILE_INDEX.json)。
