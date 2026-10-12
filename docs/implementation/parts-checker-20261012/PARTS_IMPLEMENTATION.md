# 任务一：后置首尾写作方法

实现基准：`1981c116faaf7f27ad0fd5bb6d849b1bf8b93975`。实现提交SHA由统一交接记录锁定。

## 改动及取舍

- 三阶段 `_DUTIES` 改为从实际BODY选择问题、辨认关系、保留条件、形成阅读用途的具体操作；完整变化见 [PARTS_PROMPT_DIFF.md](PARTS_PROMPT_DIFF.md)。`_COMMON`、`_CONCEPTION`、职责卡JSON、调用顺序及BODY作者不变。
- 不照搬候选的固定段落序号；引言可以导航，但需解释章节功能关系；摘要交付分类轴或有条件判断；结语先看已有收束，再决定净新增任务。没有词汇互斥、统一段数、固定引文数量或强制展望。
- 引言事实判断必须由许可材料支持并就近引用；没有程序级“零引文一律失败”。引用身份校验不等于科学支持校验，也不会根据来源题名自动补文献。
- `writing_requirements` 是可选上下文对象，只允许 `audience`、`article_type`、`journal`、`confirmed_format` 四个非空字符串，总JSON不超过10,000字符。不新增规划schema。`shared_scope`仍可承载已有范围信息；独立字段让读者、文体及已确认刊规无需混入科学范围。未知刊规不猜测，不读取期刊名便推定已知规则。
- 四阶段完整提示合同哈希进入live运行绑定。只修改后续任务段也会在购买请求之前拒绝旧绑定，不等到职责卡完成才遇见缓存冲突。
- 43篇材料、代表深读与真实问题仅用于开发，不进入生产任务文本。历史回答不是新提示效果证据。

## 正式入口与输入输出

沿用 `scripts/upgrade3/manuscript_parts.py`。输入为正式选用的句柄BODY、问题、匹配范围/细纲、来源身份及显式材料。输出仍为职责卡、各阶段请求与回答、`FRONT_BACK_REPORT.json` 和成功时的 `MANUSCRIPT_FINAL.md`。新校验及最终交付接线见统一交接文档。

示例context片段（其余字段保持实际本轮输入）：

```json
{"writing_requirements":{"audience":"领域研究者","article_type":"叙述综述","confirmed_format":"已确认的摘要栏目、长度及引文要求；未知项目不填写"}}
```

这里的文本只是配置说明，不要将“已确认”占位文字当成真实刊规。没有要求时省略整个字段。

## 免费四阶段消息与历史fixture验收

[parts_evidence/PARTS_ACCEPTANCE.json](parts_evidence/PARTS_ACCEPTANCE.json) 汇总三例：材料分类且已有Outlook、教育条件判断且提供结构化要求、真实历史回答作为fixture。两个人工异领域目录 `run/messages/{conception,conclusion,introduction,abstract}.json` 是实际生产builder输出，正文及前序部件完整保留； `FIXTURE.json`、`PROVENANCE.json`、`run/responses/`、最终稿和哈希供回查。为避免重复提交四份长历史BODY，历史例仓内保存明确标记为 abridged_preview_not_exact_request 的任务预览、原BODY路径、完整消息SHA和依赖核对；完整实际payload已在免费离线验收中执行并保留于验证工作区，可用下列导出命令逐字重建。预览不是伪装成完整请求的替代品。所有例子0外部请求，`scientific_acceptance=false`。

历史fixture使用2026-10-11已保存的有效职责卡和首尾回答，未重写或覆盖原记录。旧引言仍无引用：保留它正是为了区分结构回放与新提示质量。它不具备新消息的历史exact-recording绑定，故走明确标注的fixture通道，不能把它冒充新请求的模型响应。

在已安装项目运行依赖的环境，从仓库根目录重新导出到新目录：

```sh
PYTHONPATH=. python docs/implementation/parts-checker-20261012/preview_parts.py --output /tmp/parts-methods-new-evidence --summary-output /tmp/parts-methods-summary
python -m pytest -q tests/upgrade3/test_serial_manuscript_parts.py tests/upgrade3/test_serial_parts_application.py tests/upgrade3/test_serial_parts_live.py tests/upgrade3/test_serial_parts_writing_methods.py tests/upgrade3/test_serial_parts_delivery_integration.py
```

导出器遇已有目录停止，不覆盖证据。首尾装配继续验证BODY字节、CRLF、外层标题、关键词单处、管理区与冲突；新测试验证实际阶段任务、引用要求、跨领域数据交接、显式要求边界、提示哈希变化及旧绑定早期停止。fixture不证明模型遵循这些写作方法；真实文字、证据支持、自主性与成本仍由本地试跑评估。

## 本地固定BODY比较与恢复

使用实际既有项目及账户账本，不初始化、复制或重置账本来增加预算。先将旧归档绝对路径映射到当前本机实际文件，选用的BODY、材料、模型及参数保持一致。

```sh
python scripts/upgrade3/manuscript_parts.py \
  --draft "$SELECTED_BODY" --context "$CONTEXT" \
  --identity-catalog "$REFERENCES" --body-report "$BODY_REPORT" \
  --output "$NEW_OUTPUT" --live --language zh \
  --ledger "$PROJECT_LEDGER" --budget-limit "$PROJECT_LIFETIME_CAP" \
  --account-ledger "$ACCOUNT_LEDGER" --account-budget-limit "$ACCOUNT_LIFETIME_CAP" \
  --account-mapping-dir "$EXISTING_MAPPING_DIR" \
  --incremental-budget "$APPROVED_INCREMENTAL_CAP" \
  --thinking-budget 16384 --output-tokens 32768 --tokenizer "$LOCAL_TOKENIZER"
```

以上仅预览conception及预算绑定，不是四阶段完整消息预览；后续消息依赖真实前序输出。审阅后本地在相同命令增加 `--run --key-file "$PRIVATE_KEY_FILE"` 才允许真实请求；本轮云端没有执行该调用。模型沿用 `qwen3.5-plus`，没有升级或额外评价模型。

同一版本、同一输入、相同输出目录再次执行相同命令，按已有保存响应免费重装配；已失败/不确定物理请求不会自动再购买。职责卡格式不合格时，可明确增加 `--repair-invalid` 启用已有一次结构修复，原响应保留，不补科学答案。

提示词、BODY或运行绑定变化时使用新输出目录，但必须继续同一原账本和原生命周期额度；新比较仍需要本地批准的增量预算。旧版本目录遇 `serial_parts_live_binding_changed:retain_budget_and_cache` 时保留原样，不能删绑定绕过。历史版本的原目录恢复须使用对应旧代码；本次升级不覆写旧绑定或旧回答。

不声明提示变化已提高实稿质量；验收关注输入、消息、缓存隔离与消费者合同是否真正执行。

## 本轮验证环境

项目官方运行依赖 agentscope 2.0.9 与 pytest 安装后，使用正常包导入执行首尾四组测试，191项通过。此前仅为诊断曾隔离无关runtime初始化；最终证据由正常导入重新生成，未使用stub。包含共享delivery的联合测试由任务二接线完毕后统一重跑，不能以首尾测试通过代替整条交付验收。
