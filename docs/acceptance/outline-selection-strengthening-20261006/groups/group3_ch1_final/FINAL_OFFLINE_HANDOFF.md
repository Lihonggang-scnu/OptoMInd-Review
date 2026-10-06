# Final offline handoff — Ch1 U4 completion

状态：已完成最后 Ch1 U4 按需加强并停止付费；以下均为本地离线核验。

- 完整候选：7 章 / 29 单元；原始候选与最终副本：`F:\OptoMind-Review-2\outputs\outline_selection_20261006\FULL_CHAPTER_PAYLOADS.json` → `F:\OptoMind-Review-2\outputs\outline_selection_20261006\on_demand_group3_ch1\CURRENT_CHAPTER_PAYLOADS.json`。
- selector 选中 13 个单元，合并 Ch5 后共 6 个 owner 请求；原始 16 个未选单元逐字保留。
- 最后一组：`selection-ccb81c5cf0852794`，editable 仅 Ch1 `U4_证据阶梯与转化边界`，Ch1 其他 3 单元只读保留。
- Ch1 access：¥0.538426；Max owner：¥4.111632；本组：¥4.650058；Max 返回 `qwen3.8-max`、`stop`，prompt 327,984 / completion 4,884 / total 332,868。该候选保留供审阅，暂不自动覆盖现有细纲。
- 选择层 4 逻辑组共 12 次调用（6 access + 6 owner）实际 ¥28.408072；本次共享 40 元轮余额以账本/运行报告为准，未再启动后续组。

## 证据文件

- `Ch1/stages/ACCESS_RAW_STAGE.json`, `Ch1/stages/OWNER_RAW_STAGE.json`：阶段原始响应，可恢复解析；供应商原始正文另在 `Ch1/access_raw_responses/` 与 `Ch1/owner_raw_responses/`。
- `Ch1/RESULT.json`：校验通过的 `updated` 结果，`structural_errors=[]`。
- `ROOT_QUALITY_REVIEW.md`：根智能体对本组候选的独立后验审阅；未进入任何模型请求。
- `CURRENT_CHAPTER_PAYLOADS.json`：完整 7 章合回副本。
- `FINAL_MERGE_SEMANTIC_DIFF.json`：逐单元 exact/semantic/format-only 差异、未选单元和材料池保留统计。
- `ARRANGEMENT_INPUT.json`、`ARRANGEMENT_PROJECTION_VERIFY.json` 与 `ARRANGEMENT_PROJECTION_ALL_CHAPTERS.json`：对全部 7 章调用既有本地投影函数，逐单元核对任务字段、条件字段和章节关系字段，未调用编排模型或 writer。

## 说明

源码快照的 `source_diff_sha256` 只覆盖 git diff；未跟踪的 `outline_selection.py` 等文件单独纳入 `SOURCE_SNAPSHOT_MANIFEST.json`。

source_materials 可能因负责人明确采用的新材料发生增量/字段合并；逐层子树核验显示每个原始 A/B/精读/补充/local/tool 值仍存在，原始稳定身份均保留，candidate/tool/navigation/identity channels 做了保留核验。换行和引号等仅格式变化在差异报告中单独归类，不当作实质改进。
