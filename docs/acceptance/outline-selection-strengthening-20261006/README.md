# 自主选择与按需细纲加强公开归档

这是 2026-10-06 第一层自主选择与六批按需 access-owner 实验的可追溯公开副本。归档阶段没有模型调用、没有修改原始实验目录、没有复制数据库或凭据。

## 建议阅读顺序

1. [USER_REQUESTS.md](USER_REQUESTS.md)：用户原话与归属。
2. [USER_INTENT_AND_EXECUTION.md](USER_INTENT_AND_EXECUTION.md)：用户构想、本地落实、事实结果和后验评价的边界。
3. [ARCHIVE_SCOPE.json](ARCHIVE_SCOPE.json)、[SECURITY_SCAN.md](SECURITY_SCAN.md)：测试提交、公开范围、脱敏与省略项。
4. [reviews/ROOT_FINAL_REVIEW.md](reviews/ROOT_FINAL_REVIEW.md)、[reviews/ROOT_SELECTION_QUALITY_REVIEW.md](reviews/ROOT_SELECTION_QUALITY_REVIEW.md)：根智能体后验评价；这些文件没有进入模型请求。
5. [first_layer/](first_layer/)：7 章 29 单元选择层的实际输入、wire/effective 参数、raw 返回和解析结果。
6. [groups/](groups/)：六批 access-owner 的实际消息、raw/stage、RESULT、usage、请求上下文和恢复证据；Ch5 合并批保持独立记录。
7. [comparisons/FINAL_BEFORE_AFTER_DIFF.md](comparisons/FINAL_BEFORE_AFTER_DIFF.md)、[comparisons/FINAL_CURRENT_CHAPTER_PAYLOADS.json](comparisons/FINAL_CURRENT_CHAPTER_PAYLOADS.json)：原始完整候选与最终合回候选。
8. [costs/COST_SUMMARY.json](costs/COST_SUMMARY.json)：只读费用行提取；原始 SQLite 未上传。
9. [SHA256_MANIFEST.json](SHA256_MANIFEST.json)：逐文件来源路径、原始 SHA256、公开副本 SHA256、大小和变换记录。

## 版本与运行边界

归档当前父提交是 `9b26df5e998e0f140ec0e589d58bc0827e398464`，其中含后续文档提交；实际受测代码固定为 `a0b645e096a148382196b5aadd3dcf36c43c69d6`，dirty patch 与未跟踪选择模块由 [source_snapshot/SOURCE_SNAPSHOT_MANIFEST.json](source_snapshot/SOURCE_SNAPSHOT_MANIFEST.json) 单独保存。父提交变化不表示实验在新代码上重测。

生产源码快照按每批不同 SHA 保存在 [source_snapshots/](source_snapshots/)；相同 SHA 只做索引，全部 11 个 distinct snapshot hash 均可恢复。归档只保存了现有的 `run_selection_group.py` 临时驱动版本、实际命令和请求哈希；历史批次没有逐字 driver 快照，不能据此保证早期临时驱动可重建，原始 messages/raw 仍是每批执行的直接证据。

用户原始构想是“高级模型自主选择值得加强的单元，再按需 Max 加强”；未规定各角色模型参数和分批细节。40元总额度来自用户授权，运行中的预留与停止由本地落实。4组、13单元来自第一层模型返回；章节分拆、Ch5合并、Plus access和具体模型组合由本地落实。详见 [USER_INTENT_AND_EXECUTION.md](USER_INTENT_AND_EXECUTION.md)。

## 公开副本的材料处理

完整候选、实际 messages、raw 和 RESULT 保留可阅读的结构与生成内容。可能含论文原文或版权不明来源的 `local_passages`、full-text 类字段已在公开副本中替换为带原始 SHA256 的标记；身份、字段路径、可回读定位和 A/B/模型生成派生内容保留。中间完整合回副本、准备的 upper-bound messages 和重复源码快照以 SHA256 索引，避免把同一内容重复上传。

本包不是正文写作或全文 29 单元付费验收；实际质量判断见 reviews，用户方案与具体执行选择分开记录。
