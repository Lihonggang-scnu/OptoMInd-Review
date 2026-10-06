# 细纲自主选择与按需加强：最终离线交接

## 先看

1. [ROOT_FINAL_REVIEW.md](../ROOT_FINAL_REVIEW.md)：根智能体对全部候选的最终质量判断。
2. [FINAL_OFFLINE_HANDOFF.md](FINAL_OFFLINE_HANDOFF.md)：本批费用、状态和停止点。
3. [FINAL_BEFORE_AFTER_DIFF.md](FINAL_BEFORE_AFTER_DIFF.md)：原始完整候选与最终合回候选的逐单元对照。
4. [FINAL_MERGE_SEMANTIC_DIFF.json](FINAL_MERGE_SEMANTIC_DIFF.json)：机器可读的语义差异、材料无损核验和费用范围。
5. [ARRANGEMENT_PROJECTION_ALL_CHAPTERS.json](ARRANGEMENT_PROJECTION_ALL_CHAPTERS.json)：全部 7 章的本地编排投影核查；没有调用编排模型或正文 writer。
6. [FINAL_ARTIFACT_MANIFEST.json](FINAL_ARTIFACT_MANIFEST.json)：本批文件 SHA256、逐次请求和费用记录。

## 范围

原始完整候选是 [FULL_CHAPTER_PAYLOADS.json](../FULL_CHAPTER_PAYLOADS.json)，最终合回副本是 [CURRENT_CHAPTER_PAYLOADS.json](CURRENT_CHAPTER_PAYLOADS.json)。共 7 章 29 单元；自主选择器选中 13 个单元，4 个逻辑组在合并 Ch5 后产生 6 个 owner 请求，16 个未选单元逐值保留。完整材料池也保留，负责人采用的新材料只作可追溯增量。

各批原始请求、响应、读取轨迹和合回结果仍在以下目录：

- [Ch3 首组](../on_demand_group1_ch3/)
- [Ch1/Ch4 外部暴露组](../on_demand_group2_external_exposure_ch1_ch4_boundary/)
- [Ch5 合并组](../on_demand_group3_4_merged_ch5/)
- [Ch6 组](../on_demand_group4_ch6/)
- [最终 Ch1 U4 组](./)

源码快照见 [SOURCE_SNAPSHOT_MANIFEST.json](SOURCE_SNAPSHOT_MANIFEST.json)。其中 `source_diff_sha256` 只表示 git diff；未跟踪选择模块已单独按文件纳入快照 manifest。

## 费用

本轮 6 次 access 与 6 次 owner 调用的账本实际费用合计 ¥28.408072；最后 Ch1 U4 一组为 ¥4.650058。历史账本未改写，未启动新的付费调用。
