# 内容单元写作指南：真实本地验收（2026-10-10）

当前状态：**blocked，只有临时DRAFT_GUIDE，不能当作正式GUIDE。** 本轮新增7.942884元；原30元账本累计15.199318元，未决0，余14.800682元。没有生成正文。

初始受测源码742bef4a9134da9cbccc4e407ee7a2e36d5eab5f；同论文补读别名小修源码d1ab9c0b73817a97cea459bf582030206cbbf0a0。归档提交与受测源码区分。原manifest及完整编译输入不变；没有向冷启动输入旧指南、旧正文、人工科学问题或手选论文。用户追加30元累计授权沿用原guide账本，未新建或清空账本。

## 优先阅读

1. [ROOT_REVIEW.md](ROOT_REVIEW.md)：独立亲读、具体优点与问题、精确费用和下一步建议。
2. [补读后的完整临时指南](alias_recovered_live/DRAFT_GUIDE.md)，与[首次指南](cold_start_live/DRAFT_GUIDE.md)及[旧对照](old_comparison/GUIDE_RECOVERED_FOR_REVIEW.md)比较。
3. [GUIDE_RESULT](alias_recovered_live/GUIDE_RESULT.json)、[READ_TRACE](alias_recovered_live/READ_TRACE.json)：11份完整材料送达，随后重复请求相同材料，未完成。两个GUIDE内容差异只有Ch4一处“与/和”，不是实质完善。
4. stages中实际MESSAGES、RAW_RESPONSE、USAGE；input/FULL_BODY_INPUT.json及原manifest，reads中11份实际完整包。大文本只分片，原字节可恢复。
5. [AUTHOR_SUPPLY_README.md](AUTHOR_SUPPLY_README.md)、[摘要](AUTHOR_SUPPLY_SUMMARY.json)及全部29单元实际消息：95项内容、表格、条件和原背景均送达，自动恢复0；前文均为空，不是连续写作测试。
6. [ALIAS_FIX_VERIFICATION.md](ALIAS_FIX_VERIFICATION.md)、[恢复回执](ALIAS_RECOVERY_RECEIPT.json)：按已有稳定身份折叠同论文别名；原首次返回字节与指南不变，免费恢复，没有重复第一笔收费。

## 云端优先判断

内容连接结构值得保留；目前未选该草稿进入付费作者。请结合真实输入输出判断：当前materials与read_history/reading_needs的交接是否清楚；补读是否被当作泛化事实审查而非写作选择；主讲声明与保留任务的重复张力及统一模板怎样有界改善。不要把删掉重复需求再改complete=true当作模型已完成。不需要重跑BODY或向生产提示词注入本题答案。

## 大文件恢复

每个 *.parts.json 列出原文件哈希和按序分片。按列表连接分片的UTF-8字节，验证SHA256后写回 original_file，即可取得完整原始JSON。ARCHIVE_FILES.json记录源文件字节及哈希；未裁剪科学材料。完整作者PAYLOAD可从对应MESSAGES的用户消息读取，避免重复上传同一大载荷。

密钥、SQLite数据库、原始论文PDF及其他原始全文文件不在本包。账本仅上传费用快照。受测代码修复只涉及合同解析与测试，生产提示词、规划和BODY不变。公开归档包含A/B认识、精读/补充材料及实际请求，供独立复读。
