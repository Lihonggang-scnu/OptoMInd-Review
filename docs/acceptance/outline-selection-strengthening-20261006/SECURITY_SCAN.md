# 公共副本安全扫描

扫描范围是本归档目录内的公开副本；原始实验目录、SQLite 预算库、密钥环境和受限全文不在上传范围。

- 状态：通过。
- 扫描文件数、凭据模式命中数和省略字段清单见 [SECURITY_SCAN.json](SECURITY_SCAN.json)。
- 未发现具体的 API key、access token、password、私钥、Authorization/Bearer 凭据或签名 URL 值。模型响应中的 `signature` 字段是普通响应元数据，不作为凭据。
- 论文原文或版权不明全文字段（包括 `local_passages` 等）在公开 JSON/raw 副本中以带原始 SHA256、路径和字节数的省略标记替代；省略路径见 [indexes/REDACTIONS.json](indexes/REDACTIONS.json)。A/B、模型生成补充、细纲、请求结构和结果字段保留。
- SQLite、PDF、pyc 和密钥文件均未纳入归档。归档阶段没有模型调用。

安全扫描只说明公开副本未发现具体凭据，并不改变原始文件的访问权限或版权归属。
