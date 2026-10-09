# 脱敏、范围与省略边界

公开包保留本轮实际请求、messages、返回、有效参数、正文、usage、stage 报告、启动轨迹、root 亲审、引用映射、费用 JSON 与恢复决定。原始 source SHA 与公开副本 SHA 分开记录。

API keys、bearer/auth headers、signed URLs/signatures、cookies、passwords、secret/key-file paths 和个人邮箱会替换为明确占位符。稳定的 stage/cache/request/call/reservation ID、科学指纹、wire hash、来源身份哈希和项目生成 A/B/精读材料保留。rights-bound full_text、raw_paper、paper_text 等字段及 evidence atom 对应值会替换为 `[OMITTED_RIGHTS_BOUND_FULL_SOURCE_FIELD]`。

SQLite、原始 SSE/transport 流、worktree、tokenizer 二进制、__pycache__、第三方或未授权论文全文不上传；没有为缩短模型返回而截断正文。超过 600,000 bytes 的公开 UTF-8 数据只在字符边界切分，并由 `.parts.json` 重建校验。
