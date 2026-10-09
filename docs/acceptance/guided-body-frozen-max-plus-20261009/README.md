# 冻结 Max 指南→Plus 七章正式测试公开归档（2026-10-09）

本包记录冻结自动 Max 指南切换到 Plus-only 的正式七章测试。源码绑定 `6a5ed067f7e302db569c6f8edcf7379c3ed252b5`；七章正文已完成并由 root 亲读，但整体科学质量未胜旧 B，不能把完成状态写成质量通过。旧 B 已发布于 commit `6d48a3b0350a745d0a803d41f7ba5a340c5c4e33`，本包只保留引用，不重复复制。

## 建议阅读顺序

1. [ROOT_FINAL_REVIEW.md](review/ROOT_FINAL_REVIEW.md)、[ROOT_CHAPTER_NOTES.json](review/ROOT_CHAPTER_NOTES.json)、[ROOT_SOURCE_TRACE.json](review/ROOT_SOURCE_TRACE.json)
2. [七章原始正文](routes/frozen_max_plus/FULL_BODY.md) 与 [DELIVERY_BODY.md](routes/frozen_max_plus/DELIVERY_BODY.md)
3. [编号读者稿](review/records/NUMBERED_READER_BODY.md)、[编号映射](review/records/NUMBERED_READER_MAPPING.json)、[REFERENCES.json](review/records/REFERENCES.json)
4. [FINAL_EXECUTION_SUMMARY.json](review/records/FINAL_EXECUTION_SUMMARY.json)、[LEDGER_FINAL_SNAPSHOT.json](review/records/LEDGER_FINAL_SNAPSHOT.json)、[FINANCE_SUMMARY.json](FINANCE_SUMMARY.json)

本轮包含 **7 个 author 调用 + 1 个 Ch6 缺表补表调用**，共 8 个实际 provider calls、5 条 run 轨迹，实际已结算使用费用 **7.953508 元**；账本累计结算 32.4145152 元，未决 0。Ch3–Ch5 的 metadata 声明、Ch6 的真实缺表补写及前文链均以原记录保留；108 个唯一稳定身份、323 次引用出现和前文全部来自本组。

每个物理 stage 的 ACTUAL_REQUEST、REQUEST、MESSAGES、RAW_RESPONSE、RESULT、BODY、USAGE 和阶段报告均保留，另含 CLI/启动包装、有效配置、冻结指南、输入快照、恢复决定、root trace、费用和离线审计。模型正文与项目生成科学材料尽量完整。

认证值、密钥、签名下载链接、cookies、密码、secret/key-file 路径、SQLite、SSE 原始传输和未授权论文全文不公开。共享 FULL_BODY_INPUT 只保存一份；所有公开数据先脱敏，再按不超过 600,000 bytes 的 UTF-8 字符边界分片。ARCHIVE_BUILD_MANIFEST.json 记录脱敏前来源 SHA/bytes 与公开副本 SHA/bytes。

本地路径只用于审计定位，不是可访问链接。请运行 `VERIFY_PUBLIC_ARCHIVE.py <this-directory>`，并配合 `VERIFY_CANONICAL_PAYLOADS.py <this-directory> --index` 复验索引、JSON、分片和敏感扫描。
