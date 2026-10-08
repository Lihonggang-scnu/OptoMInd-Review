# 中间层独立复测交接（2026-10-08）

本次修改只针对细纲→指南的写法决定和专用预算入口；规划冻结、材料不重跑，正文作者不修改。当前交付只做免费测试，没有真实模型调用或自动写正文。旧冷启动归档保持原样，见 `docs/acceptance/guide-maker-cold-start-20261008/`。

## 先确认代码与输入

在新的隔离 worktree 中使用交付提交，不覆盖另一个助手的工作区。运行：

```powershell
python -m pytest -q tests/upgrade3/test_guide_maker_contracts.py tests/upgrade3/test_guide_maker.py tests/upgrade3/test_guide_maker_cli.py tests/upgrade3/test_guide_maker_dedicated_budget.py tests/upgrade3/test_guide_maker_prompt.py
```

继续用原真实 manifest：

`F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json`

核验 manifest、依赖文件及 tokenizer 哈希；规划、95 项原任务、表格和独特条件均不得裁剪。首次 materials=[]、prior_guide=null，不传人工 A/B 指南、旧正文、人工手选材料或旧评价反馈。使用新输出目录，不覆盖或恢复旧提示生成的指南。

## 免费预览

```powershell
python -X utf8 scripts/upgrade3/guide_maker.py --manifest "F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json" --config config/guide_maker/plus_first.json --tokenizer "F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json" --output "F:\OptoMind-Review-2\outputs\guide_maker_acceptance_20261008_30cny\cold_start_prompt_fix"
```

检查实际请求确实使用本次提示、完整细纲和来源/工具导航，核实 token 计量与输出预留可容纳请求。未预先保证所有补读材料都能放入；保持完整材料分批和容量失败停机规则。

## 独立 30 元账本，不是新增 30 元

只使用已经存在的：

`F:\OptoMind-Review-2\outputs\guide_maker_acceptance_20261008_30cny\guide_budget.sqlite`

及其原始 `.guide_maker.json` 身份标记。不得新建替代账本、复制成新预算、修改上限、清空花费或丢弃未决占额，不借用另一个助手的 60 元账本。

归档 `BUDGET_FINAL.json` 记录累计实耗 0.517030 元、未决占额 0、当时剩余 29.482970 元。这个数只属于归档时刻。真实运行前必须读取上述原 SQLite 账本，重新核对 limit、actual、reserved/uncertain 和 remaining；若账本/标记缺失、身份不符、已有未知费用或余额不足，停下报告，不重建账本。CLI 的 budget_before/budget_after 应记录本次真实状态。

仅在用户允许进行真实复测后，在已通过的预览命令追加：

```text
--run --budget-mode dedicated --budget-ledger "F:\OptoMind-Review-2\outputs\guide_maker_acceptance_20261008_30cny\guide_budget.sqlite" --budget-limit 30 --key-file "<原有本地密钥文件>"
```

默认模型配置不变，不自动升级模型、不隐式重试未知花费。保留原子预占、结算和恢复记录。相同代码/提示/输入/配置的恢复可复用缓存；改动这些内容必须另建输出目录，仍绑定原账本。legacy 默认仍按原 60 元规则，作者入口原样保留。

## 亲读与复测边界

逐章核对：重复材料的主讲位置、后章增量与回指是否具体；解释是否利用原细纲的关系和条件而非统一模板；比较是否有维度、设计或适用条件；承接是否推进认识，而非重复证据不足；指南是否真有作者可执行的安排而非细纲摘要。

阅读需求必须指向尚未确定的写法选择，记录实际交付材料与其带来的可观察调整。不要要求固定阅读篇数，也不要把零补读本身判失败。complete 只是模型声明与结构状态，不等于质量验收通过。当前免费测试不能证明真实生成效果。

完成后可先用本轮 GUIDE.json 做作者免费预览。不得把 DRAFT_GUIDE 或旧目录残留文件当成新终稿。指南专用 dedicated 参数不能直接传给未改动的正文作者；付费正文是另一项授权及预算安排，不在这次复测中自动执行。

保留全部请求响应、指南版本、阅读轨迹、费用、失败/缓存状态和亲读结论。此交接不授权公开上传；分享须另行按用户授权，排除密钥、原账本数据库及未授权全文材料。
