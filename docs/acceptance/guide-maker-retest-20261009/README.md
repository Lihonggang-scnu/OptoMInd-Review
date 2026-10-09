# guide-maker 修后复测公开归档（2026-10-09）

本目录是 `guide_maker_retest_20261009_10cny` 的获授权安全公开副本，固定受测源码为 `145d68788037b4aa31295253d0b0d06f7de88685`。源输出保持只读；本目录不含 SQLite、marker、密钥、完整 science archive、作者完整 messages 或本地 ZIP。

## 阅读顺序

1. `USER_REQUEST.md`：用户授权、测试范围、预算和公开边界；其中明确用户要求与本地助手评判的区别。
2. `cold_start_live/SOURCE_MANIFEST.json`、`MATERIAL_IDENTITIES.json`、`cold_start_live/stages/` 下的初始输入和请求证据：先确认七章输入、来源身份、真实请求、用量和结果。大文本按同名 `.parts.json` 规则重组，原始字节不加分隔符。
3. `cold_start_live/GUIDE.md` / `GUIDE.json`：本轮唯一真实生成的七章指南；`comparison/OLD_GUIDE.md` / `OLD_GUIDE.json` 是只读旧版对照。
4. `AUTHOR_PAYLOAD_PROJECTION.json` 与 `writer_preview/WRITER_PAYLOAD_CHECK.json`：证明七章 chapter assignment 被作者预览消费的公开投影，仅保留 manuscript guide、chapter assignment 以及材料计数/哈希，不含作者完整 science messages。
5. `cold_start_live/` 下的 GUIDE_RESULT、RESULT、RAW_RESPONSE 和 SSE actual wire parts，以及 READ_TRACE、USAGE、CLI/RUN metadata：核对真实生成证据。RAW_RESPONSE 和 SSE 是模型生成记录，已做 auth/secret/signed-link 模式扫描。
6. `free_tests/`、`BUDGET_FINAL.json`、`BUDGET_WRAPPER_REVIEW.md`、`CODE_PROVENANCE.json` 和 `COMMANDS.md`：核对免费控制、wrapper 和版本来源。
7. `ROOT_REVIEW.md`：最后阅读本地根评判。它是本地审阅意见，不是生产提示或用户既定方案；其中“未上传”保留为报告生成当时的历史状态，本目录是后来获授权的公开副本。

## 一页事实

- 本轮只发出 1 次 `qwen3.5-plus` 真实调用，费用 `0.499740` 元；没有真实补读，`reading_needs=0`，没有正文付费调用。
- 原账本累计 `1.016770` 元，`reserved=0`、`uncertain=0`；原 limit 和 marker 未变。本归档不执行模型，也不把剩余余额当成新授权。
- 免费专项测试记录为 91 项：90 passed、1 项 Windows 临时 SQLite 锁失败；原日志和失败原因按原文保留。
- writer preview 为免费准备检查，`model_calls=0`、`paid_dispatch_count=0`，在第一个未知作者输出处停止；完整七章分配由公开投影和 GUIDE 证明。

## 公开边界

`SOURCE_OMISSIONS.md` 列出每个省略类别及源文件 SHA-256。材料公开投影只含身份元数据、标题、DOI、来源字段计数/哈希、记录/章节计数和聚合哈希。完整 `FULL_BODY_INPUT.json`、完整作者 `MESSAGES.json`、本地账本、marker、credential/key 及完整正文输入留在本地受控包中。

`MANIFEST.json` 给出公开文件的用途、相对路径、字节数和 SHA-256；parts 文件按 `.parts.json` 中的顺序以 UTF-8 字节直接拼接即可恢复，并记录原文件哈希。`PUBLICATION_SAFETY.json` 不输出任何 key 或 secret 值；exact-secret 比对由根代理另行完成。
