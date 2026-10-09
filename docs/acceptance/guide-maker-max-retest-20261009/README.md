# Qwen3.8-Max guide-maker retest（公开验收归档）

本归档固定对应源码提交 `d834d79461591e1c3dc1ba00f6084314d5aa0d3b`，记录 2026-10-09 的两次 Qwen3.8-Max guide-maker 调用。归档准备只读 acceptance 产物和 retest worktree，没有再次调用模型、读取账本或修改生产代码。

## 结论与事实边界

正式冷启动的原始状态是 `response_invalid` / `complete=false`：第二次提供商返回正常 `stop`，但 JSON 中有 8 个未转义的内嵌双引号。模型声明 `complete=true`、`reading_needs=[]`，完整指南内容已返回。格式恢复仅插入 8 个反斜杠；恢复后的 guide 结构与生产保留的 `DRAFT_GUIDE` 完全相等，Markdown 字节也相等。原始失败状态、原始响应、DRAFT、RESULT、费用和缓存没有被改写，也没有把原运行重写成 success。详见 `recovery/FORMAT_RECOVERY_RECORD.json`。

本地亲读质量报告推荐保留本轮 Max 的指南内容，优于上一轮 Plus；这只是内容选择建议，不改变原运行状态。上一轮 Plus 已在公开 commit `3e1f4a285b30575701908c375cf373676e6bdc29`，本归档只提供链接：https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/3e1f4a285b30575701908c375cf373676e6bdc29/docs/acceptance/guide-maker-retest-20261009。

## 费用

本轮实际两次调用合计 `6.239664 CNY`；累计 `7.256434 CNY`；有效上限 `10.517030 CNY`；未结算/未决 `0 CNY`；剩余 `3.260596 CNY`。这些数字沿用本地账本的供应商用量估算口径，不是供应商发票确认。作者预览为免费 metadata-only preview，模型调用和付费 dispatch 均为 0。

## 内容与重建

`guide/` 含完整生成指南、保留 DRAFT 和格式恢复审阅副本。`run/requests/maker_001/` 是允许公开的第一条真实 MESSAGES，含冻结细纲与 light catalog，并按原始字节拆分。第二条真实请求含 P0593/P0090 的完整 paper-derived materials；公开目录用带 identity、source path、source-card hash、byte count、packet hash、atom count 和 record count 的 projection 替代，并明确省略完整内容。完整第二请求仍只在本地源目录保留。

所有原始响应均按字节拆分为不超过 180 KB 的片段；请先读取对应 `.parts.json`，再按 `index` 顺序直接拼接，不做换行转换。`MANIFEST.json` 对除自身外的每个公开文件记录路径、字节数和 SHA-256。

## 导航

- 质量报告：`review/ROOT_REVIEW.md`
- 恢复偏移与 replay：`recovery/FORMAT_RECOVERY_RECORD.json`、`recovery/MODEL_RESPONSE_ESCAPING_ONLY.json`、`recovery/replay_format_recovery.py`（可执行的八偏移重放校验）
- 请求/响应哈希与 parts 索引：`run/REQUEST_PROVENANCE.json`、`run/requests/`、`run/responses/`
- 材料身份和省略：`provenance/MATERIAL_PROJECTION.json`、`SOURCE_OMISSIONS.md`
- 作者预览安全投影：`author/AUTHOR_PREVIEW_PROJECTION.json`
- 测试与 wrapper：`tests/`、`wrapper/`
- 全树校验：`MANIFEST.json`

本目录是公开文档归档，不是生产 final、success cache、正文输出或版权论文仓库。
