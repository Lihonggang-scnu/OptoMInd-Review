# 脱敏、范围与省略边界

公开包保留本轮实际请求、messages、返回、有效参数、正文、usage、stage 报告、恢复/complete 记录、引用映射、生产回归、费用 JSON 与 root 审查。原始 source SHA 与公开副本 SHA 分开记录。

API keys、bearer/auth headers、signed URLs/signatures、cookies、passwords、secret/key-file paths 和个人邮箱会替换为明确占位符。稳定的 stage/cache/request/call/reservation ID、科学指纹、wire hash、来源身份哈希和项目生成科学材料保留。rights-bound full_text、raw_paper、paper_text 等字段及 evidence atom 对应值会替换为 `[OMITTED_RIGHTS_BOUND_FULL_SOURCE_FIELD]`。

SQLite、原始 SSE/transport 流、worktree、tokenizer 二进制、__pycache__、第三方或未授权论文全文不上传；standalone prior GUIDE full text 按要求只保留 hash/path reference。超过 600,000 bytes 的公开 UTF-8 数据只在字符边界切分，并由 `.parts.json` 重建校验。
