# 修复后的 guide-first B 组：本地真实验收边界

用户授权日期：2026-10-09（Asia/Shanghai）。受测提交：145d68788037b4aa31295253d0b0d06f7de88685。独立 detached worktree，不修改生产提示词、原指南、上游规划或另一个 Agent 的指南生成产物。

只测试仓库人工 guide_B.json 的七章正文。模型仅 qwen3.5-plus；thinking_budget=16384、max_output_tokens=49152、实际 max_completion_tokens=65536，配置原样。禁止 Max、其他模型回退、自动升级、自动付费重试；正常有界回读和局部补写属于预算内生产路径。

沿用 outputs/evidence_body_round2_20261008_60cny/PREPARED_MANIFEST.json 和 new_round2_budget.sqlite，不重建、不清零、不释放未知占额。原 ledger/marker/CLI budget-limit 均为60。本轮最多新增40元，不使用另4元预备金或指南生成额度。首次只读核对24行settled=18.3609232、held=0；最终以不可重置的 CAMPAIGN_BUDGET_START.json 为 S0 权威记录。实例有效累计上限 min(60,S0+40)，每次物理调用由原子 reserve 守卫。存在未知或未结算时停止付费。

启动前：专项测试、编译、包装越额阻断/恢复不重算/非Plus拒绝的免费验证；正式首章预览；检查七章 writing_arrangement 可见、materials 中 outline_action 递归为零、首章 accepted_body_markdown 为空。后续实际请求逐一核对本组真实前文，不带入旧 B 正文或根评价。

完成标记遗漏：先阅读保存正文，区分传输失败、实质未完成与仅格式遗漏。仅后者按正式 metadata-declarations 接口、精确 stage/cache/raw/body 哈希做有依据的声明。记录判断依据，不一律 complete=true，不改原响应，不重写整章。必要补写仍受同一预算约束。

根智能体亲读完整新稿；与旧 B 的前六章比较共同范围，第七章单独评价，旧 A 七章仅辅助。评价自然承接、机制/实验深度、案例职责、P编号主语、章节预告、重复限制总结。轻微科学错误记录即可，不扩张为全面科学审稿。

FULL_BODY.md 保留原始正文及准确前文依据；DELIVERY_BODY.md 另作正式引用编号验证，格式转换与来源身份不得混为科学正确性。保存实际请求、原始返回、模型参数、材料和指南版本、用量、费用、占额、预算包装及恢复轨迹。

本轮只本地保存，不自动公开上传。完成这一组或发生无法安全继续的阻断后停止新增调用，交用户与云端评阅。
