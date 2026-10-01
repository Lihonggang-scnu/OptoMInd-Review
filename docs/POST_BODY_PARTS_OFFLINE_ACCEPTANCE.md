# 后置首尾模块：离线验收

日期：2026-10-01。基线：e100e2066028bd60f56796e3b31849293bca1536。

## 已验证

- 专项测试：162 passed。
- 全部 tests/upgrade3：241 passed，12 failed；基线为79 passed，失败集合完全相同。
- git diff --check、compileall 通过。
- 四个受保护正文文件无差异：progressive_review_plan.py、chapter_arrangement.py、review_unit_writer.py、scripts/upgrade3/full_review_draft.py。
- 独立审查发现并修正：错误绑定的旧录音重放、inline-code marker 被误认作所有权、CH01/CH01.1 定位混淆、身份字段冲突与命名空间碰撞、缺失引用后的出版继续执行，以及 CLI 参数覆盖与元数据回归。
- 归档提交516af116的真实 body_plus 正文作为离线输入，四阶段使用明确标注的人工fixture：输入文件字节不变，去除文章题名和本模块owned spans后的BODY文本完全一致。BODY SHA-256：7582b76fe9c1727a96a1c5467a0db3f5d5b2d849f54fee25df863ce5d8b4d29e。人工首尾是测试文本，不是科学内容质量证据。

## 没有验证

没有发起真实或付费模型调用，没有新生成的科学稿件质量结果，没有确认跨领域写作质量改善。没有PDF视觉验收。Git取回与发布属于普通网络操作，不等同于模型调用。

v1只自动装配standalone；现有owned结语原位替换，新结语放在第一个明确参考文献标题之前。没有自动检索、细纲单独定稿或断点续跑；每次使用新输出目录。

## 与基线相同的失败

这些测试依赖仓库未包含的历史运行产物或固定Windows工作目录，本次不修改：

- tests/upgrade3/test_review_delivery_editing.py::test_front_back_inserts_all_parts_into_plan_draft
- tests/upgrade3/test_review_delivery_editing.py::test_front_back_updates_existing_parts_and_inserts_missing
- tests/upgrade3/test_review_delivery_editing.py::test_intro_changed_version_keeps_only_new
- tests/upgrade3/test_review_delivery_editing.py::test_intro_located_by_role_when_title_lacks_the_word
- tests/upgrade3/test_review_delivery_editing.py::test_intro_repeat_runs_do_not_accumulate
- tests/upgrade3/test_review_delivery_editing.py::test_text_edit_second_run_is_no_change
- tests/upgrade3/test_review_delivery_editing.py::test_text_edit_stage_applies_fixture_to_real_draft
- tests/upgrade3/test_review_delivery_entry.py::test_harness_parser_has_delivery_branch_and_help_is_offline
- tests/upgrade3/test_review_delivery_figures_citations.py::test_real_draft_map_and_reader_rendering
- tests/upgrade3/test_review_delivery_figures_citations.py::test_stage_cross_refs_captions_and_map_all_agree
- tests/upgrade3/test_review_delivery_integration.py::test_history_full_chain_via_cli_subprocess
- tests/upgrade3/test_review_delivery_integration.py::test_plan_restricted_chain_via_entry
