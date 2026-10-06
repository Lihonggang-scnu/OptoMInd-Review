# 公共副本安全扫描

- 状态：通过；扫描文件数和具体模式命中见 [SECURITY_SCAN.json](SECURITY_SCAN.json)。
- 未发现具体 API key、access token、password、私钥、认证头、Bearer 凭据或签名 URL 值。响应中的 `signature` 字段按普通元数据处理。
- JSON/raw 中的 `local_passages` 与 fulltext 类字段已用原始 SHA256、路径和字节数标记替代，详情见 [indexes/REDACTIONS.json](indexes/REDACTIONS.json)。
- SQLite、PDF、api_keys 和认证凭据文件未纳入；原始运行目录未修改。
