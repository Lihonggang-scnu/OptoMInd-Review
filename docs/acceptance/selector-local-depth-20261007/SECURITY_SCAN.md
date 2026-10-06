# 安全扫描

状态：**PASS**。扫描公开副本文件，未发现具体凭据值。

- `sk-...` 命中项全部位于 `deep_read_material.task_id`，是业务任务 ID，不是 API key。
- 另一组宽松正则命中普通叙述中的 `sig` 子串，没有 URL 或赋值上下文，按误报处理。
- 未上传 SQLite、密钥文件、环境文件、PDF、pyc、Authorization/Bearer 凭据或签名 URL。
- 明确的全文/`local_passages` 字段以原 SHA256 和长度标记替换；A/B、模型生成消息、细纲、raw/result 保留。

详情见 [SECURITY_SCAN.json](SECURITY_SCAN.json)。
