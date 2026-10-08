# 中间层本地测试说明

本轮新增“细纲转写作指南”。现有人工 A/B 指南转正文正在测试，先让它们按原冻结版本运行并结算，不中途更换代码或提示。

中间层测试稍后单独进行。规划不改，材料不重跑，使用同一真实 manifest。初次测试不传人工 A/B 指南，不提供手选文献清单，默认不传旧稿反馈，检验真正冷启动。

## 免费检查

先在新的隔离 worktree 或确认干净的工作区拉取交付代码，运行：

```powershell
python -m pytest -q tests/upgrade3/test_guide_maker_contracts.py tests/upgrade3/test_guide_maker.py tests/upgrade3/test_guide_maker_cli.py
```

原始输入历史位置：

`F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json`

先检查该文件及依赖仍存在，再做实际 tokenizer 预览：

```powershell
python -X utf8 scripts/upgrade3/guide_maker.py --manifest "F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json" --config config/guide_maker/plus_first.json --tokenizer "<之前已验证的 tokenizer.json>" --output "F:\OptoMind-Review-2\outputs\guide_maker_trial_20261008\cold_start"
```

检查第一次请求有完整细纲和来源导航，materials 为空、prior_guide 为空，没有旧正文或人工指南。预览只验证当前可构造请求，不预先保证后续所有材料都能放入上下文。

## 付费生成指南

等当前 A/B 测试完成、费用结算清楚以后，读取原账本实际余额。历史 53.3617708 元只是 A/B 开跑之前的预计余额，现在不能继续当作可用余额。

原账本：

`F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite`

在上述预览命令追加：

```text
--run --budget-ledger "F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite" --budget-limit 60 --key-file "<现有密钥文件>"
```

所有试验共用原累计上限，不能新建账本、清空花费或丢弃未决占额。预算不足、容量不足或未知花费时保留草稿并报告，不为获得成功结果自动重试或升级模型。

本轮先做一次冷启动生成。仅在看清其具体问题、预算允许时，再考虑 `--feedback "<UTF-8 阅读评价文件>"` 的另一个新输出目录；不要默认形成多模型、多提示排列组合。

## 检查指南和选读行为

读最终指南，并查看阅读轨迹：需求是否真的帮助组织和解释；候选来源是否由当前题目决定；是否读取了比较对象、关键实验及影响含义的条件；补读是否改变了具体写法；是否只是复制细纲或重新写了一套细任务树。

不要求它选择与人工版本完全相同的文献，也不以字面相似程度评分。没有补读但指南已足够、选择不同材料但更有帮助，均可能是合理结果。重复读取、无效需求和失败也要保留。

## 再交给同一正文作者

只有本次 `GUIDE_RESULT.json` 显示完成，且 `GUIDE.json` 对应本次版本时，才用它调用新作者。先免费预览：

```powershell
python -X utf8 scripts/upgrade3/guided_body_writer.py --manifest "F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json" --guide "F:\OptoMind-Review-2\outputs\guide_maker_trial_20261008\cold_start\GUIDE.json" --config config/guided_body_writer/plus_first.json --tokenizer "<同一 tokenizer.json>" --output "F:\OptoMind-Review-2\outputs\guide_maker_trial_20261008\generated_guide_body"
```

预算允许且预览合格，再加同一组付费参数。不得将 DRAFT_GUIDE 或旧目录残留的成功指南冒充本次完成结果。自动指南生成成本与后续全文写作成本都计入比较。

最终与人工 A/B 的完整正文比较阅读流畅性、跨章增量、综合能力和细节保留。记录哪份指南实际生成了哪份正文，使用相同作者配置，避免把模型设置变化误认为指南差异。

归档完整成功/失败状态、实际请求响应、阅读轨迹、指南各版本、全文、费用及未决记录和亲读结论。回传遵循用户已明确授权的方式，不上传密钥、原账本数据库或未授权论文全文。
