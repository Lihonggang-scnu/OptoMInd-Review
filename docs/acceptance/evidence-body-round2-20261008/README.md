# 全文写作第二轮：四路线真实验收归档（2026-10-08）

锁定受测源码：`f1b78750fae6ff47dbc4d4694f88af3b53c8cf9c`，正式入口 `scripts/upgrade3/evidence_body_writer.py`，Plus配置 `config/evidence_body_writer/plus_first.json`。本轮未修改算法或重跑上游，复用同一批准的7章、29单元、95任务与配套材料。完整新预算60元；最终结算6.6382292元，预留/未决0，余53.3617708元；费用为供应商实际用量按本地价表估算。

## 优先阅读

1. [根智能体完整亲审与选择](review/ROOT_ROUND2_REVIEW.md)
2. [四路线事实与费用](review/FINAL_EXECUTION_SUMMARY.json)
3. [推荐的完整连续初稿](packed_continuous/FULL_BODY.md)
4. [自主编辑后完整稿（未整体采用）](routes/scoped_revision_live/selected/FULL_BODY.md)，对照 [原稿](routes/scoped_revision_live/selected/ORIGINAL_FULL_BODY.md)
5. [本地恢复入口](review/RECOVERY_ENTRY.md) 与 `PUBLIC_FILE_INDEX.json`

| 路线 | 实际状态 | 新增费用（元） |
|---|---|---:|
| packed_whole | 容量预览阻断，未调用 | 0 |
| packed_continuous | 7次Plus完整成稿，根选用为初稿 | 4.347772 |
| dossier_author | 策展正常返回但3个不同atom ID无效，作者未调用 | 1.341644 |
| scoped_revision | 读者+2编辑；5组地址无效待处理、2组5块技术应用；根未整体采用 | 0.9488132 |

连续初稿保留技术细节和条件，费用低于上一轮；任务书口吻、重复及局部综合不一致仍在。策展没有成稿，不能凭接口失败淘汰科学方案。编辑有衔接收益，但新增来源错配和过强筛选规则；程序接受修改不等于内容正确。实际问题与推断在亲审文档中分开。亲审意见未注入任何模型请求，全部修改由正式自主读者/编辑链产生。

## 复现和资料边界

实际请求、消息、原始返回、有效参数、用量、失败与恢复记录位于各路线目录。单份公共输入在 `packed_continuous/inputs/canonical_input/`，科学证据档案在 `packed_continuous/inputs/evidence_book/`；其他路线引用同一快照，不重复上传。原本地输入10,099,935字节、证据档案25,908,835字节，原件SHA和脱敏副本SHA分别记录。默认模型视图是有轨迹的选择，不能声称整池原样入模；原材料完整档案可回读，本轮未触发实际回读。

脱敏凭据、认证头、签名下载URL、密钥文件定位等；不上传原SQLite、网络传输流或权利不明论文全文。非认证的缓存指纹和科学哈希保留；具体范围见 `REDACTION_NOTES.md`。公共材料是项目生成A/B、精读和补充记录，不冒充完整论文原文。原件留本地，公开副本是审查资料，不能无条件作为生产缓存。

大文件按UTF-8字符边界拆分。`VERIFY_CANONICAL_PAYLOADS.py <本目录>` 严格验证265个索引文件及26份分片清单；不允许损坏字符或任意换行归一化来过哈希。上传使用原生Git保留字节，远端另作逐项校验。本次只新增/更新本验收目录，生产源码与其他分支保持不变。新增调用已经停止，交云端独立复读判断。
