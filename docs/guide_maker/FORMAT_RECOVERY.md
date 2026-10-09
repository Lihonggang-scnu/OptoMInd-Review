# 指南结构化输出与格式恢复

## 为什么这次要修

Max 验收归档 `45e519cd583b5e9824c20dd3752a8bcdda8a61c7` 的第二次返回正常结束，但正文字符串中有 8 个双引号未转义。正式解析器直接拒绝；既有通用解析工具却能保留同一份指南草稿，造成“内容还在，流程停住”的接缝。手工副本仅插入 8 个反斜杠。本次以该原始返回作免费回归，生产实现不写死修复偏移、论文或章节答案。

## 复用成熟能力，而不新增 Agent 框架

1. **接口预防**：`config/guide_maker/explicit_max.json` 打开现有 `json_mode`，沿用 Qwen 适配发送 `response_format={"type":"json_object"}`。这约束 JSON 语法，不等同严格 JSON Schema，也不保证科学正确或内容完整。Plus 默认不改；不能把 Max 的 thinking/JSON 能力套到 Plus。
2. **程序修格式**：严格标准库解析优先；失败后复用已有依赖 `json-repair` 和仓库已有引号处理工具。项目只增加限制哪些机械改动可接受的薄层，不自行实现另一套完整 JSON 解析器。依赖要求 `json-repair>=0.63.5,<1`，本轮验证版本为 0.63.5。
3. **照常校验内容结构**：修复后的对象仍须通过原有指南字段、章节身份、来源身份、阅读需求和完成状态校验。格式恢复不能替代这些校验。
4. **保留结果与证据**：原始返回始终保留，修复候选和记录另存。无需重跑 Max 或额外调用低价模型。本次没有加入模型重写格式的调用；不能安全修复的结果继续保留草稿并报告。

我们参考了以下成熟机制，但没有迁移框架或引入其自动重试默认值：

- [Qwen 结构化输出](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output)：区分 JSON Object 与 JSON Schema；具体模型、thinking 模式和区域能力有差异。
- [LangChain structured output](https://docs.langchain.com/oss/python/langchain/structured-output)：原生结构化输出、工具式输出和验证错误反馈。
- [PydanticAI output](https://pydantic.dev/docs/ai/core-concepts/output/)：类型化结果、验证与受限重试。
- [json-repair](https://github.com/mangiucugna/json_repair)：复用其修复能力，但不开启会补默认值、丢字段或补全内容的 schema salvage。即使 `strict=True`，也不能将库返回结果无条件视为无损修复。

仅把 JSON 模板放在提示词里，仍然要求模型自己正确输出引号与括号。程序持有字典/Pydantic 对象并用 `json.dumps` 保存，才会由程序正确处理已有字符串的转义。不过长指南若仍作为一次 JSON 返回，接收时仍需上述接口约束与恢复；不能把“保存时序列化正确”误认为“接收时自动无错”。本轮保留现有全文生成与自主补读流程，不拆成七次模板填空。

## 自动恢复的范围

面向完整返回中的常见机械错误：完整外层 JSON 代码围栏/BOM、字符串内引号转义、字符串控制字符以及结构尾逗号。库候选须与受限机械候选一致，再进入标准 JSON 和原指南校验。正常 JSON 不走修改路径。

不补结束括号或缺失字符串，不填字段/缺章，不修改来源编号、数值、句子或 `complete`，不把有效的错误阅读需求删掉。重复键、非有限数值、截断、提供商错误、未完成状态或存在待补读需求不能借格式修复晋级为最终指南。这里允许拒绝某些罕见或有歧义的格式错误，不追求“什么都能修”。

## 新运行

正常使用 `guide_maker.py` 即可，合法的格式恢复无需额外开关或模型调用。成功修复仍会经过原来的读材料/更新指南/完成判断；不会提前结束自主补读。

有修复时，阶段目录另存：

- `FORMAT_RECOVERY.json`：原文与规范化文本哈希、机械改动、采用的方法及原始响应文件哈希。
- `NORMALIZED_RESPONSE.json`：可独立检查的规范化候选。

`RAW_RESPONSE.json` 不被替换。缓存和 CLI 恢复身份包括新的恢复实现文件；不能在旧运行目录静默使用改变后的代码和配置。格式成功、原协议完成与科学质量仍是不同状态。

## 已付费失败结果：离线恢复，不重新生成

本地从原运行中选择第二阶段的 `RAW_RESPONSE.json`，使用同一次运行的封存 `FULL_BODY_INPUT.json`。用原材料 manifest 也可以。输出必须是新目录，不能写入旧运行或尝试目录。

```powershell
python -X utf8 scripts/upgrade3/recover_guide_response.py --book "<原运行封存FULL_BODY_INPUT.json>" --response "<原第二阶段RAW_RESPONSE.json>" --output "<旧运行目录之外的新恢复目录>"
```

该入口没有 `--run`、模型、密钥或账本参数，不调用模型。自动检查原响应旁的 `ERROR.json`；另有错误记录可通过 `--error-file` 明确传入。原错误文件、原始返回、旧结果和旧成功/失败缓存都不改写。

- 完整指南通过原校验、原模型声明完成、传输完整且没有待补读时，输出 `GUIDE.json`、`GUIDE.md` 和 `RECOVERY_RESULT.json`，退出码 0。
- 有可读草稿但不能完成时只输出 `DRAFT_GUIDE` 和诊断，退出码 3。
- 输入或输出路径不合规等操作错误返回退出码 2。

这是原响应的独立派生恢复，不会重写原失败运行的状态，也不会虚构旧运行中尚未写入的阅读历史。恢复得到指南不代表作者已经生成正文或最终引用达标。

本次已有人工转义副本可作为比对对象；程序恢复版必须与其完整对象一致，不允许“顺手润色”科学文字。无需为验证这 8 个引号再付费生成一份指南。

## 免费验收

在原工程 Python 环境先确认 `json-repair` 版本；低于 0.63.5 时，仅升级该已有依赖即可，不必重装整个项目环境：

```powershell
python -m pip install "json-repair>=0.63.5,<1"
```

```powershell
python -m pytest -q tests/upgrade3/test_json_format_recovery.py tests/upgrade3/test_guide_format_recovery.py tests/upgrade3/test_guide_maker_contracts.py tests/upgrade3/test_guide_maker.py tests/upgrade3/test_guide_maker_cli.py tests/upgrade3/test_guide_maker_dedicated_budget.py tests/upgrade3/test_guide_maker_prompt.py tests/upgrade3/test_guided_body_contracts.py tests/upgrade3/test_guided_body_writer.py tests/upgrade3/test_guided_body_cli.py
```

先完成离线恢复并核对结果，再用恢复后的指南做作者免费输入预览。本修复不自动启动付费正文、不重置账本，也不需要因为格式问题新建一个科学候选。
