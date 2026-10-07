# 安全与公开范围检查（最终）

归档内全部文件及可解析的嵌入 JSON 字符串已检查。

- 凭据格式命中：0；本地 qwen key 精确匹配：0。
- 脱敏后仍存在的非空受限全文字段：0；已用 SHA256/长度占位的受限字段：73。
- 解码检查的嵌入 JSON 字符串：1999；绝对本地路径残留文件：0。
- 生成的 A/B、模型返回、细纲和 messages 保留；未纳入 SQLite、PDF、认证 header、signed URL、密钥或版权不明全文。
- 逐文件 hash 与复制来源见 `SHA256_MANIFEST.json`、`COPY_PROVENANCE.json`。
