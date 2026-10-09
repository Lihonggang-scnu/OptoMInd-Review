# 公开省略项与安全边界

以下内容未从本地测试包复制到公开归档。每个被省略的完整 science 输入仍可由源目录和 SHA-256 受控核验，但不在这里公开。

- 所有 `**/inputs/**/FULL_BODY_INPUT.json`：完整科学材料档案，可能含版权或权利范围不清的摘录；本轮三份输入副本均为 10,099,935 bytes，SHA-256 为 `6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7`。公开替代为 `MATERIAL_IDENTITIES.json`。
- `writer_preview/runs/20261008T175722Z-64691994dc/plans/author_001/MESSAGES.json`：1,294,176 bytes，SHA-256 `b673b196f5b4ce255a572f7e752f132b3be112b061647aae6e5f3e58ea45d7ad`；作者 payload 含完整科学 records，以 `AUTHOR_PAYLOAD_PROJECTION.json` 替代，仅证明 manuscript guide、七章 assignment、材料计数和哈希。
- `message_preview/**/plans/maker_001/MESSAGES.json`：与 canonical 初始 guide-maker messages 字节一致的免费预览副本，仅因去重省略；初始真实 messages 在 `cold_start_live/stages/.../MESSAGES.json.parts/` 中保留。
- `message_preview/FIRST_MESSAGE_PREVIEW.json`：855,428 bytes，SHA-256 `a95800bd22f3f2a47860ea6e915c13c89b32199b5f7a39cc20a5bbcb55bcdf9c`；免费预览 wrapper 的重复视图，仅因去重省略，`PREVIEW_CHECK.json` 与 CLI/request metadata 仍保留。
- `writer_preview/FULL_BODY.md`、`writer_preview/DELIVERY_BODY.md` 及其 run 副本：正文或正文候选输出，不属于本轮真实 guide-maker 结果。
- SQLite 账本、marker、`free_tests/**/synthetic.sqlite`、锁文件、Python cache、完整本地 ZIP 及其原始包元数据：本地运行状态或容器，不公开。
- `worktree/`：完整源码工作树不公开；版本、关键源码哈希和 wrapper 路径见 `CODE_PROVENANCE.json`。

`RAW_RESPONSE.json` 与 SSE actual wire 是模型生成记录，因用户明确要求保留而以原始 UTF-8 parts 公开；它们已在 `PUBLICATION_SAFETY.json` 中完成 auth/secret/signed-link 模式扫描。扫描报告不输出任何匹配值，exact-secret 比对由根代理完成。
