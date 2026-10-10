# 最终接入检查

源码：`638eb93cdb37fc95cb917130f69a5b16df3549b7`。

## 有界入口与回归

执行者：chapter_coherence_integration 工作代理；根智能体已核对最终生产与测试 diff。

```powershell
python -X utf8 -m pytest tests/test_chapter_coherence.py tests/test_dual_budget_cli.py tests/test_legacy_unit_route.py tests/test_legacy_unit_route_review.py tests/test_unit_realization.py tests/upgrade3/test_review_unit_writer_completion.py tests/upgrade3/test_review_delivery_entry.py tests/upgrade3/test_fullbody_cli.py::test_normal_body_entry_loads_existing_manifest_export_without_another_writer -q
```

结果：169 passed / 8 skipped。跳过项为需要显式路径的历史可选 fixture，未把跳过算通过。测试覆盖正常 main 与真实子进程默认 baseline、显式实验选择、原作者消息、任务/材料保留、选用编辑稿与编号稿、未决诊断保留、真实测试双账本限额、失败历史与零调用恢复。模型传输为标明的合成 SSE，不冒充真实生成质量。

## 原生完整资产免费检查

根智能体在最终源码执行：

```powershell
python -X utf8 ../coherence_round_20261010/validate_formal_defaults.py
python -X utf8 ../coherence_round_20261010/validate_full_cached_entry.py
```

- 正常 `run_review_harness.py --delivery-start body` 不传质量/version开关，全29单元输入预览实际默认 baseline + quality_control + article_edit；Plus 8192/32768，0调用，原输入SHA不变。不存在的密钥路径未读取。
- 原完整七章稿通过相同产品运行库的历史 CLI 恢复，29/29完成，实际选用原全文编辑稿，187个编号论文身份，0新增调用/账本预留，原始响应及采用正文哈希不变。
- 第二项保存的 CLI exit=2 对应原有未决质量诊断；完成单元、选用可读稿、费用与诊断分别记录，没有把诊断清零或人工填状态。

完整命令、路径与结果在同目录两个 FORMAL_*.json 和 EVIDENCE.zip/run_records 中。

## 语法与文件

根智能体对实际改变的入口、作者、质量和交付模块执行 `python -X utf8 -m py_compile`，通过。`git diff --check` 通过。

真实质量证据来自固定五单元、两组新生成的请求及正文。旧完整稿恢复与免费预览仅验证接入，和真实对照分开记录。
