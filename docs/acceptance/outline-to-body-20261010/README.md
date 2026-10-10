# 2026-10-10 细纲→正文链路验收

源码及正式入口：`73f938e40ec6c46947d5cca34ad0eee542631107`，`scripts/upgrade3/legacy_unit_writer.py`。各实际生成阶段的源码不同，见 SOURCE_AND_RUN_HISTORY.json；本目录所在提交是公开归档提交。

优先阅读 [最终结论](ROOT_FINAL_REVIEW.md)、[采用的句柄稿](BODY_HANDLES.md)、[编号读者稿](BODY_READER.md)。[原作者稿](BODY_ORIGINAL_AUTHORS.md) 与 [全文编辑前稿](BODY_BEFORE_ARTICLE_EDIT.md) 可用于比较。根智能体完整阅读旧179篇稿、新29单元及全部发生修改的差异，评价文件从未作为模型答案输入。

云端本次审读请先看 [审读请求与本地判断](CLOUD_REVIEW_REQUEST.md)：用户要求以实际质量收益为中心，不把恢复等基础能力当主要成果；该文件区分用户目标、本地实施和本地建议，供云端独立裁决。

七章29单元，187个规范化引用身份、3表；实质改善来自自主补回漏写任务和部分条件纠正。仍有误判补写、来源任务错配及实验解释问题，报告保留受限状态。并非投稿就绪认证。

主实验12.0901596元，独立一次Max上探1.025472元，未知占额0；300元账户中的2.0077024元是主实验的子集，不能再相加。COST_EVIDENCE.json保留每次物理调用的实际费用/预留和证据定位，不上传数据库。

EVIDENCE.zip及EVIDENCE_MANIFEST.json包含本轮原请求、返回、材料投影、参数、质量/恢复轨迹、原稿快照、留出样本、Max上探和本地启动记录。压缩包不含密钥、SQLite账本或原论文PDF。包内绝对路径用于追溯原运行，换机需按来源准备材料及凭据；不可替换原私有账本来重新起算预算。

通用运行/恢复：[产品说明](../../writing_candidates/LEGACY_UNIT_QUALITY.md)、[账户预算接口](../../legacy_unit_writer/ACCOUNT_BUDGET.md)。直接运行正式CLI；旧临时launcher仅作为历史证据，不与正式双账本参数叠加。

本轮已经停止新增调用。后续建议留给独立审阅，不启动新实验。
