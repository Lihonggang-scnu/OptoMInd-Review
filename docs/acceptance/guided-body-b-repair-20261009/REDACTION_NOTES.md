# 脱敏、范围与省略边界

公开包保留真实请求、messages、返回、有效参数、正文、用量、阶段结果、三次 run、恢复声明、引用消费记录和 root 亲审文件。模型返回正文与项目生成材料尽量完整保存。

API keys、bearer/auth headers、signed URLs/signatures、cookies、passwords、secret/key-file paths 和个人邮箱被替换为明确占位符。稳定的 stage/cache/request/call/reservation ID、科学指纹、wire hash 与来源身份哈希保留。`full_text_path` 等生成正文定位可保留；实际 rights-bound `full_text`、`raw_paper`、`paper_text` 等字段及 evidence atom 的对应值会被替换为 `[OMITTED_RIGHTS_BOUND_FULL_SOURCE_FIELD]`。

SQLite、原始 SSE/transport 流、worktree、`__pycache__`、第三方或未授权论文全文不上传。没有为了缩短原始记录而截断模型返回；超过 600,000 bytes 的公开 UTF-8 数据只在字符边界分片，并由 `.parts.json` 重建校验。
