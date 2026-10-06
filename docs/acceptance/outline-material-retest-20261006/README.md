# 按需细纲材料可见性复测公开归档

本包用于云端复核 2026-10-06 的两单元细纲小测。建议按以下顺序阅读：

1. `ARCHIVE_SCOPE.json`、`SECURITY_SCAN.md`：确认测试提交、费用边界和公开范围。
2. `reviews/ROOT_COMPARISON_REVIEW.md`、`reports/EXPERIMENT_REPORT.md`：查看独立质量判断与事实执行记录。
3. `comparisons/THREE_ARM_CURRENT.md`：逐字阅读原计划、完整材料 Plus 与修后语义目录 Max 的三组对照；`THREE_ARM_PRIOR.md` 是此前已保存的三组证据，单独标为 prior。
4. `inputs/`、`materials/`：核对原输入指纹、29 个来源/2 个工具和语义字段可见性。
5. `groups/full_material_plus/`：查看 Full Plus 实际 wire messages、返回、effective 参数和编排输入。
6. `groups/semantic_catalog_max/`：按 ACCESS → RESOLVED_TRACE → OWNER_REQUEST/WIRE → OWNER_RESULT → ARRANGEMENT 顺序复核按需链路。
7. `SHA256_MANIFEST.json`：核对每个归档文件的来源、用途和 SHA256。

## 测试边界

归档目录基于当前公开分支提交 `5620ee419a9581d66f5f90638b0f647d01ff1bc2` 保存；本归档以该提交为父基线，归档新增文件不代表重新运行实验。受测源码固定为 `4d81a772bdecc4da18a84a8bc90298fb1a38f625`。用户后来提供的 `2b389c82a21ac2e4b13489061ee2a92b9ab66c31` 未用于本轮请求；公开分支后续前进也不代表本轮在新源码上重测。

输入是两个可编辑单元、两个只读邻近职责、29 个相关来源和 2 个工具材料。Full Plus 直接发送 29 个相关来源和 2 个工具材料的完整材料 payload。按需 access/Max 使用覆盖全部材料通道的完整语义目录，Max 另收到 access 选出的 5 条完整记录；目录中的未读取记录仍可按协议请求补读。按需 access 返回的 5 条用途全部指向只读 U3/U4，这个原始模型结果保留在包内，未人工改写或重试。

本轮只运行细纲强化，不写正文、不启动 29 单元全量、不修改普通 BODY/writer。三次调用实际账本估值为 `2.878334 CNY`，新增未结算占额为 `0`；共享账本从 `45` 显式升至 `85`，历史费用和 reservation 保留。费用是本地原价账本估值，不宣称供应商最终扣费。

## 文件完整性

`SHA256_MANIFEST.json` 列出全部归档文件的来源和 SHA256；manifest 自身的 SHA256 记录在 `SHA256_MANIFEST.sha256`，该校验文件按惯例不自列。
