# selector-strengthening-20261007 公开审阅包

本包供云端审阅“第一层自主选择 → 既有按需 Max 加强”的真实证据。首层已有独立归档，本包只引用它并保存二层关键证据。

## 建议阅读顺序

1. [USER_REQUESTS.md](USER_REQUESTS.md) 与 [USER_INTENT_AND_EXECUTION.md](USER_INTENT_AND_EXECUTION.md)：用户授权、执行归属和后验意见边界。
2. [ARCHIVE_SCOPE.json](ARCHIVE_SCOPE.json)、[SECURITY_SCAN.md](SECURITY_SCAN.md)：范围与公开限制。
3. [first_layer/REFERENCE.json](first_layer/REFERENCE.json)：首层完整请求、raw、结果、参数和质量记录所在的相邻归档。
4. [second_layer/strengthening__FINAL_SUMMARY.md](second_layer/strengthening__FINAL_SUMMARY.md)、[second_layer/strengthening__FINAL_MERGE_AUDIT.json](second_layer/strengthening__FINAL_MERGE_AUDIT.json)：三组成功、Ch2 未运行组和稳定 ID 合并结果。
5. `second_layer/group*/`：每组实际 messages、raw、effective/stage、结果、费用和 root 后验。
6. [BUDGET_SCOPE.json](BUDGET_SCOPE.json)、[REPOSITORY_CHECK.json](REPOSITORY_CHECK.json)、[AGENTS_CHECK.md](AGENTS_CHECK.md)、[source_snapshot/SOURCE_SNAPSHOT_MANIFEST.json](source_snapshot/SOURCE_SNAPSHOT_MANIFEST.json)、[SHA256_MANIFEST.json](SHA256_MANIFEST.json)：费用、仓库和复现索引。

## 事实范围

第一层实际调用成功 1 次，返回 4 个同章逻辑组、5 个单元，费用 `1.420884 CNY`。第二层实际完成 3 组：Ch1 U1、Ch1 U3、Ch4 U03；Ch2 U01/U03 在付费前因容量与保守预算预检阻塞，没有第四组加强调用。不要把“第一层 4 组”写成“四组二层加强成功”。

三组二层结果均由既有按需 Max owner 路径产生，access、owner、读取轨迹和原始返回均保留；root 后验评价没有进入任何模型请求。当前 25 CNY 轮次 settled `17.612720`，剩余 `7.387280`；先前 `8.533428` uncertain 是用户明确豁免本轮占用计算的历史记录，SQLite 未公开，历史账本未重置。Ch2 的保守上界 `24.000580` 是预检估计，不是实际费用。

## 给云端的自主审阅提示

请仅依据本包的实际输入、输出、材料身份、阶段记录和 root 后验分离记录，判断：第一层是否真正自主选择了值得加强的单元；选择结果是否能无损接入按需 Max；稳定身份、互补材料、条件/限制和只读职责是否保留；Ch2 未运行是合理容量治理还是暴露了可改进的通用接口。请自行提出是否转正、适用条件和下一步成本/质量验证，不要把 root 后验科学意见或本包观察改写成模型输入答案。

归档整理阶段未新增模型调用；受测版本与归档提交分别记录，上传不代表重新测试。
