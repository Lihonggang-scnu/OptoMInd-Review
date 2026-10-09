# compact auto-metadata 七章公开归档（2026-10-09）

本包记录 compact auto-metadata 路线的七章正式测试，源码绑定 `737ef95aff9f6ffb8061427489ca7e52fade5a5b`。七章已完成；原始结果、自动 metadata 恢复、complete 插入和 root 评价分开保存，整体质量边界与不理想处见 ROOT_FINAL_REVIEW.md。

## 建议阅读顺序

1. [ROOT_FINAL_REVIEW.md](review/ROOT_FINAL_REVIEW.md)、[ROOT_BEFORE_AFTER.json](review/ROOT_BEFORE_AFTER.json)、[ROOT_UNCITED_ASSESSMENT.json](review/ROOT_UNCITED_ASSESSMENT.json)
2. [七章原始正文](routes/compact_auto_metadata/FULL_BODY.md) 与 [DELIVERY_BODY.md](routes/compact_auto_metadata/DELIVERY_BODY.md)
3. [编号读者稿](review/records/NUMBERED_READER_BODY.md)、[编号映射](review/records/NUMBERED_READER_MAPPING.json)、[REFERENCES.json](review/records/NUMBERED_READER_STAGE/REFERENCES.json)
4. [FINAL_OFFLINE_AUDIT.json](review/records/FINAL_OFFLINE_AUDIT.json)、[FINAL_CITATION_AUDIT.json](review/records/FINAL_CITATION_AUDIT.json)、[FINANCE_SUMMARY.json](FINANCE_SUMMARY.json)

本轮有 9 个实际 provider calls：7 个 author、1 个 metadata、1 个 complete；已知实际使用费用 8.6193176 元，账本累计结算 41.0338328 元，未决 0。引用审计分开记录：原 FULL_BODY 有 409 个括号位置，DELIVERY 格式稿有 423 个位置（其中 14 处为裸 P 规范化）；两者对应 117 个身份，另有 194 个 semantic supplies 和 3 个 alias。

自动 metadata 判断本身成功且保留原文，但旧合并逻辑仍保留错误待办，触发了 1 次额外 1.724088 元补写；不能把本轮写成自动判断直接恢复完成。

所有实际 messages、raw responses、usage、metadata 恢复、complete 插入、前文 hash、生产回归、官方三文档、源码锁、配置与 guide hash、wrapper 和免费 audit 均按原始/整理边界保存。旧 GUIDE 全文只保留 `GUIDE_REFERENCE.json` 中 commit `24681415f913f40da818b649aa186d4684a3e0f0` 的 repository path/URL，不重复复制；实际请求记录仍按要求保留。

认证值、密钥、签名下载链接、cookies、密码、secret/key-file 路径、SQLite、SSE 原始传输和未授权论文全文不公开。共享 FULL_BODY_INPUT 只保存一份；所有公开数据先脱敏，再按不超过 600,000 bytes 的 UTF-8 字符边界分片。ARCHIVE_BUILD_MANIFEST.json 记录脱敏前来源 SHA/bytes 与公开副本 SHA/bytes。

请运行 `VERIFY_PUBLIC_ARCHIVE.py <this-directory>` 和 `VERIFY_CANONICAL_PAYLOADS.py <this-directory> --index` 复验索引、JSON、分片和敏感扫描。
