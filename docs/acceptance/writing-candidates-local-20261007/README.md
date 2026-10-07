# 写作候选：完整章节真实测试与质量记录

本包供云端独立阅读、判断产品默认路线。**整章 Plus 已有实用价值；分单元 Plus 有局部展开能力；两次 Plus 统稿几乎无收益；Max 统稿恢复部分关键条件和反例，仍有重复及材料支持问题。** 没有重跑规划、BODY 全文或首尾模块，没有将 root 的科学意见输入模型。

## 优先阅读

1. [用户目标与执行选择](records/USER_SCOPE.md)，区分用户方案与本地落实。
2. [最终报告](records/FINAL_REVIEW.md)，路线、费用、问题和建议。
3. [采用的完整任务与材料公开投影](inputs/CHAPTER_INPUT_PUBLIC_PROJECTION.json)及[逐字段送达检查](records/PROJECTION_CHECK.json)。
4. [A 整章 Plus 稿](runs/chapter_live/format_repair/ADOPTED_CHAPTER_BODY_PROPOSAL.md)、[B Plus 初稿](runs/units_edit_live_after_timeout_fix/20261007T124218Z-4c3f3755a1/DRAFT_BODY.md)、[B balanced 编辑稿](runs/units_edit_live_after_timeout_fix/20261007T124218Z-4c3f3755a1/CHAPTER_BODY.md)、[B Plus reasoning 编辑稿](runs/units_edit_live_after_timeout_fix/20261007T130126Z-5772883350/CHAPTER_BODY.md)。
5. [本轮代表性选用 Max 稿](FINAL_SELECTED_CHAPTER.md)，未经人工修改；[选择记录](records/ROOT_ACCEPTANCE.json)。
6. [A 亲审](records/ROOT_REVIEW_A.md)、[B 亲审](records/ROOT_REVIEW_B.md)、[Plus reasoning 亲审](records/ROOT_REVIEW_PLUS_REASONING.md)、[Max 亲审](records/ROOT_REVIEW_MAX.md)、[11 项来源追溯](records/QUALITY_TRACE.json)。请自行复读，不必接受本地评价。
7. [实际差异证明](records/COMPARISON_PROOF.json)：三次编辑器请求的 messages 文件字节一致；reasoning 编辑只删除 `, P0584`。
8. [最终费用](records/FINAL_BUDGET.json)、[运行与恢复说明](REPRODUCTION.md)、[文件与省略清单](MANIFEST.json)。

`runs/` 保留独立运行和原始模型输出 `MODEL_RETURN.json`、actual request、usage、完整阶段/缓存/尝试路径。不要把旧失败报告、初稿、编辑稿或顶层最新选择混读。

补齐了 `*_preview` 离线准备记录及原始 [124 项基线检查日志](records/offline_acceptance.log)、[启动准备快照](records/PREPARED.md)。修后 126 项候选检查及 23 项超时控制记录在 `runs/units_edit_live/cancelled/TIMEOUT_FIX_VERIFICATION.json`。预览与录制回放没有新增模型调用；其中未完成或 placeholder 返回不是科学质量结果。预算和采用结果请以最终报告为准，不以启动快照中的旧余额为准。

## 锁定代码与输入

- 云端基线 `fe2f1c2adde414e71341287845d0520325e602c6`。
- 本地格式修补 `14257f4274e1deeb77aa6d125447bfb2a5d4c7d3`。
- 实际 Bv2 与编辑源码 `32772fbe1a1d3de0969f4d2e27b0fd5cf725b939`。
- 两处源码修改只处理完整 JSON 内引号和流式超时，不改生产提示词或 BODY 算法；源码随本归档的 Git 父历史保留。
- 使用前次批准的完整 Ch2 编排、对应视图与材料；4 单元、12 任务、32 来源。不是重新规划，也不是把两单元结果当作全章。

## 收尾选择与限制

选用 Max 稿只表示本次比较中有代表性的实际改善，不意味着默认引入 Max 或已达到投稿质量。正式离线装配结果在 `assembly/`，只消费这个单章，不构造虚假的逐单元产物、不重写或编号正文。

C 分簇路线只预览、未付费测试：本章容量够，没有额外分簇的必要。不能据此淘汰 C。跨领域和其他完整章节未做真实比较。

账本最终新增结算 **3.5213712 元**、未确定 **7.237796 元**、合计占用 **10.7591672 元**，余下 **49.2408328 元**没有消耗。旧 90 条账目全部保留。Max 结算＋占额 **7.72626 元**。`FINAL_BUDGET.json` 优先于旧 prepare 估算；计价来自客户端实际用量记录，未核对提供方发票或缓存折扣。

公开投影保留已有模型生成的 A/B、全部任务、身份与参数。权利不明的论文精读/补充原文字段以哈希省略，精确原请求、SSE、原材料和 SQLite 账本在本地完整保留。**公开消息不是逐字可重放输入。** 密钥、认证信息、论文 PDF/XML/HTML 全文未上传。

建议云端下一步先判断：A 的哪些章节推进值得复用，B 的哪些细节值得保留；编辑器为何在充分材料下几乎不改稿，Max 的条件恢复怎样通过自主编辑能力实现而不依赖人工科学清单。不要先重跑模型或重做上游细纲。
