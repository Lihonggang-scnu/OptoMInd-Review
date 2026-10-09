# 修复后 guide-first B 七章公开归档（2026-10-09）

这是修复后人工 guide-first B 组的最终本地公开验收包，绑定源码 `145d68788037b4aa31295253d0b0d06f7de88685`。本轮已停止新增调用；归档只保存事实记录，不把七章完成等同于科学质量通过，也不把本包当作 Qwen 自主中间层验证。

## 建议阅读顺序

1. [ROOT_FINAL_REVIEW.md](review/ROOT_FINAL_REVIEW.md) 与 [ROOT_QUALITY_LOG.json](review/ROOT_QUALITY_LOG.json)
2. [原始七章正文](routes/B_repair/FULL_BODY.md)
3. [正式交付副本](routes/B_repair/DELIVERY_BODY.md)、[编号交付稿](routes/B_repair/NUMBERED_DELIVERY_BODY.md)、[REFERENCES.json](routes/B_repair/REFERENCES.json)
4. [最终执行状态](review/records/FINAL_EXECUTION_STATE.json)、[阶段审计](review/records/B_STAGE_AUDIT.json)、[最终费用](FINANCE_SUMMARY.json)

本轮 **7 个物理 author 调用、3 条 run 轨迹、Ch1–Ch7 均有正文**。三条轨迹分别为首次 Ch1–Ch3、恢复 Ch4–Ch5、恢复 Ch6–Ch7；Ch3 与 Ch5 的完成标记遗漏按原生精确哈希声明恢复，原始响应、解析结果和正文均保留。没有付费重试、补写或科学评价调用。旧 B 只有前六章可评、第七章未运行，本包不把旧 B 当完整基线。

实际新增费用为 **6.100084 元**；启动时账本 S0 为 18.3609232 元、原库占额为 0，本轮有效封顶 58.3609232 元；最终累计结算 24.4610072 元，未决/held 为 0，新的封顶余额为 33.899916 元。SQLite 不上传，完整费用审计在 `review/records/FINAL_FINANCE.json`。

免费验证记录也随包保留：78 项正式 guide/writer/CLI 专项测试，以及 5 项 wrapper 自测，见 `review/records/OFFLINE_TESTS.log`、`review/records/COMPILE.log`、`review/records/DIFF_CHECK.log`、`review/records/SELFTEST.log` 与 [SELFTEST.json](review/records/SELFTEST.json)。这些测试使用临时账本，不写入正式账本。

每个真实 stage 的 `ACTUAL_REQUEST.json`、`REQUEST.json`、`MESSAGES.json`、`RAW_RESPONSE.json`、`RESULT.json`、`BODY.md` 与 `USAGE.json` 均按路线保存；三条 `runs/` 轨迹和两次 metadata recovery 也保留。共享 `FULL_BODY_INPUT` 只保存一份于 `shared/canonical_input/`。

认证值、密钥、签名下载链接、cookies、密码、secret/key-file 路径、SQLite、SSE 原始传输和未授权论文全文不公开。原始文件先脱敏再以严格 UTF-8 字符边界切分；脱敏前来源 SHA/bytes 与公开副本 SHA/bytes 的对应关系保存在 `ARCHIVE_BUILD_MANIFEST.json`。本地路径仅作审计定位，不是公开链接。

请运行 `VERIFY_PUBLIC_ARCHIVE.py <this-directory>` 完成索引、JSON、分片和秘密扫描。
