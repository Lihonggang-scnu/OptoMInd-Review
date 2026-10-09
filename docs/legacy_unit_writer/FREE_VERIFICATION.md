# 免费验证记录

基于 `f5103e99c624951ab549a90b443ab090876ca301` 增加独立旧单元路线。云端没有真实模型调用，新增费用 0 元。机器可读结果见 [FREE_VERIFICATION.json](FREE_VERIFICATION.json)。

## 已验证

- 当前真实供材完整展开为 7 章、29 单元；92 段落任务、3 表格任务 / 14 行。段落、表格、负责人单元背景和单元说明逐项原值一致。
- 566 次单元来源记录供给，依赖来源身份并集 221；其中有仅身份或工具材料记录，221 不代表每篇都有完整精读。直接任务绑定的规范化身份为 194。
- 同一论文明确别名正确归并；多份实质材料保留，单纯身份规范化不重复发送整份科学材料。跨章来源快照不互相覆盖。
- 生效作者提示词 3805 字符，与十月五日 Ch2_U01 实际 system 消息逐字一致。
- 实际 29 单元免费 CLI 预览成功，无容量阻断、无调用。云端未配置 Qwen tokenizer，使用 UTF-8 保守上界；本地正式测试须用现有 tokenizer 复核。
- 全单元“保守输入＋每次最大输出和思考额度”的最坏费用和为 79.254968 元。这不是实际费用预测，不增加授权；正式测试仍按新的独立 30 元账本逐次占额、耗尽即停。
- 283 项相关免费测试通过；编译检查、`git diff --check` 通过。
- 两项历史测试依赖仓库没有提供的 `outputs/full_review_draft/20260927_run01/` 成品，因此最终集合明确排除。初次扩大检查还因稀疏检出缺少指南提示词产生三项失败，补齐仓库中的真实文件后全部通过，未改测试绕过失败。
- 未运行完整仓库测试套件，未验证本轮科学写作质量或是否恢复 173 篇。

## 测试覆盖

新入口 37 项针对性测试（包括独立复核）：完整任务字段、条件、长材料、递归来源依赖、别名互补材料、输入不变性、Plus 限定、独立预算及未知费用、账本丢失/损坏/封顶漂移、零收费预览、缓存完整性、成功不重复调用、原始响应免费恢复、封存文件写入中断恢复、失败后保留已有正文、部分稿诚实合稿、合稿引用别名、模拟输出隔离。

相关回归另外覆盖旧作者补写、容量、输出消费、可移植路径、引用身份与格式、合稿，以及保留的 GUIDE 合同和连续单元路线。模拟客户端的结果仅证明程序行为，不是新科学正文。

## 复现相关集合

```bash
python -m pytest -q tests/test_legacy_unit_route.py tests/test_legacy_unit_route_review.py tests/upgrade3/test_review_unit_writer_completion.py tests/upgrade3/test_arrangement_writer_quality_budgets.py tests/upgrade3/test_review_unit_writer_portable_paths.py tests/upgrade3/test_body06_writer_output_consumption.py tests/upgrade3/test_body06_formatted_citations.py tests/upgrade3/test_citation_prefix_consumption.py tests/upgrade3/test_body40_numeric_citation_identity.py tests/upgrade3/test_body40_citation_diagnostic_handoff.py tests/upgrade3/test_review_delivery_figures_citations.py tests/upgrade3/test_guided_continuous_units.py tests/upgrade3/test_guided_content_units.py tests/upgrade3/test_guide_maker_contracts.py --deselect tests/upgrade3/test_review_delivery_figures_citations.py::test_real_draft_map_and_reader_rendering --deselect tests/upgrade3/test_review_delivery_figures_citations.py::test_stage_cross_refs_captions_and_map_all_agree
```

部分历史回归依赖已在仓库归档的实际返回文件。使用稀疏检出时需要同时检出相应归档夹；新入口的两份测试文件本身使用独立合成材料。
