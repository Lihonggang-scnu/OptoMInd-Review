# 公开省略项与安全边界

本目录只收录文档证据、模型生成的指南与按字节拆分的请求/响应记录。以下项目仍保留在本地源目录，可用路径和 SHA-256 受控核验，但未复制到公开树：

- `**/inputs/**/FULL_BODY_INPUT.json`：完整科学输入与可能受版权或权利范围限制的材料；公开替代为 `provenance/MATERIAL_PROJECTION.json`。
- 第二次真实 `MESSAGES.json` 的完整 P0593/P0090 paper-derived packets：公开替代为 `run/requests/maker_002/MESSAGES.projection.json.parts.json` 与 `provenance/MATERIAL_PROJECTION.json`，保留 identity/path/hash/byte/atom/record counts。
- 作者预览完整 `MESSAGES.json`、完整输入、正文输出：公开替代为 `author/AUTHOR_PREVIEW_PROJECTION.json`；本轮作者调用为 0。
- SQLite ledger/marker、锁文件、key files、tokens、passwords、signed URLs、request headers、完整工作树、full papers 和 rights-unclear source packets：均未复制。
- 上一轮 Plus 指南不重复复制，见 `DEDUP_INDEX.json` 中的固定公开 commit 链接。

模型原始响应按 UTF-8 原始字节拆分，每片不超过 180,000 bytes；索引包含顺序、偏移、片段哈希与重建规则。原始第二响应保持 `response_invalid` 事实，格式恢复记录只说明插入 8 个反斜杠。
