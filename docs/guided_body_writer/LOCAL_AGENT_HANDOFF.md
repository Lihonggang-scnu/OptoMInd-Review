> 2026-10-08 接缝修复后说明：以下是历史试写操作与复核方法，不是新的付费授权。公开 A/B 归档不可改写。代码或提示词变化必须新输出目录；旧 B6 只能作离线回归夹具，不能静默迁移成新缓存成功。

# 新写作层本地测试交接

## 本轮使用这个入口

使用 `scripts/upgrade3/guided_body_writer.py`，不要再按旧 ZIP 的实验适配运行旧 `evidence_body_writer.py`。

用户保存的旧文件 `F:\OptoMind-Review-2\临时文件夹\Writing_Guide_Experiment_20261008.zip` 可以留作参考。现在两份指南的可运行版本在仓库 `docs/guided_body_writer/guides/guide_A.json` 与 `guide_B.json`，无需再拼提示词或创建启动器。

本轮只测试 A 和 B 的完整正文，先不跑旧包的无指南 C0。新作者以指南为主要输入；无指南基线要另行设计，不能拿空指南绕过接口。

规划、细纲、材料来源保持原样，不开发自动中间层，不增加科学评价链，不建立逐任务对应表。完成后亲读比较整篇文章。

## 开跑前

拉取交付提交后检查工作区，不覆盖本地其他改动。先运行新专项测试，核实本地输入和原账本都存在。

```powershell
python -m pytest -q tests/upgrade3/test_guided_body_contracts.py tests/upgrade3/test_guided_body_writer.py tests/upgrade3/test_guided_body_cli.py
```

沿用第二轮真实 `PREPARED_MANIFEST.json` 及其绑定的原始文件；公开脱敏归档不能替代正式付费输入。

历史位置：

- manifest：`F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json`
- 账本：`F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite`
- tokenizer 与密钥：使用本地之前已验证的真实路径，不将密钥写进代码或归档。

2026-10-08 A/B 归档累计已结算 18.3609232 元、held 为 0，历史剩余 41.6390768 元。现在先核实实际账本；实际状态优先。累计上限仍是 60，不是新增 60。严禁复制成新账本、重置历史费用或释放未决请求来腾额度。

## 先免费预览 A 和 B

```powershell
python -X utf8 scripts/upgrade3/guided_body_writer.py --manifest "F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json" --guide docs/guided_body_writer/guides/guide_A.json --config config/guided_body_writer/plus_first.json --tokenizer "<现有 tokenizer.json>" --output "F:\OptoMind-Review-2\outputs\guided_body_trial_20261008\A"
```

B 使用 `guide_B.json` 和不同的 B 输出目录，其余配置相同。不要运行旧的 `--route` 或 `--prompt-dir` 参数，新入口不需要这些参数。

检查真实消息只有 `manuscript_guide`、`chapter_assignment`、`materials`、`accepted_body_markdown` 四个顶层字段。原细纲段落任务、单元规划和完成 task ID 不应混入。第一章前文为空，后章使用实际生成前文。

预览只有首个可构造请求，后章需要前文，不能把首章容量通过写成七章都已实测。若实际容量阻断，停止该组并报告，不自行截断、压缩科学材料或恢复原任务切片。

## 付费试写

预览合格后，在相同命令追加：

```text
--run --budget-ledger "F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite" --budget-limit 60 --key-file "<现有本地密钥文件>"
```

A/B 共用这个原账本。入口要求原账本和 `.evidence_round2.json` marker 已存在且匹配，否则停止。`--budget-limit 60` 是原终身累计上限，不改成余额。

按当前 plus_first.json，沿用 Plus、思考 16384 + 正文答案 49152，实际总输出预留 65536 tokens，以及既定超时、回读配置。此处纠正旧文档口径，不改变生产配置。不自动升级 Max，不自动付费重试，不为凑完整比较绕过余额限制。成功缓存正常复用；指南或配置改变时使用新输出目录，不能把旧成功稿冒充新组。

主写作每次返回当前章正文；若模型自己说明还有具体内容缺口，最多追加一次局部补写。这里没有额外的查漏或科学审稿模型。补写失败仍保留已经生成的正文。

## 评阅

先分别读完整稿，再对照。优先比较：跨章重复是否减少、论述是否连续、章节是否各有增量、小标题是否自然、后部是否形成综合、机制和关键案例是否保留深度。保留几个能够说明判断的具体段落和跨章对照。

标题冒号数、小节数和文本相似度只用于找到问题，不能直接决定胜负。不要因为完成标记齐全就标记写作质量通过。遇到明显事实退化如数字串线，记录即可，不扩大成本去做全面新审稿。

记录 A/B 的所有实际请求、响应、模型设置、指南哈希、费用与未决占额、成功/失败状态、完整或部分正文，以及亲读结论。所有失败也是有效结果。归档不含密钥、原账本数据库或未授权论文全文；回传方式按已有明确授权执行。

结论允许一份更好、各有优劣、无明显改善或两份都不合格。现在仍是检验人工中间产物与新作者配合的阶段，不能据此提前声称 Qwen 自动中间层已经可用。

## 完成记录遗漏的无付费恢复

正常结束但缺失/无效 metadata 的可读稿现在明确停在 `metadata_unresolved`，`FULL_BODY.md` 保留原文，不因 `finish_reason=stop` 自动完成，也不自动重写本章。检查 `required_action.declaration_binding` 及对应保存正文，人工决定 `complete` 与 `remaining_content`。将 binding 原样加这两个字段，保存为 JSON 数组，续跑原同代码/同配置命令时传 `--metadata-declarations <文件>`。例如：

```json
[{"stage_id":"author_006","cache_key":"<原结果绑定>","raw_response_sha256":"<原响应文件哈希>","body_sha256":"<原正文哈希>","complete":true,"remaining_content":[]}]
```

声明只补元数据，精确绑定阶段请求、原响应字节和原文，不覆盖 RAW_RESPONSE/RESULT 缓存；每次恢复需显式传声明。`complete:false` 可声明真实缺口并沿用一次插入式补写，补写仍需要本次已授权的执行模式。metadata 恢复本身零模型调用；后章或补写是否调用模型取决于原命令 `--run`/离线 `--responses`，正常预算守卫与收费记录不变。哈希不匹配或截断/传输失败拒绝恢复。

## 正文与交付引用

`FULL_BODY.md` 和 `body_markdown` 是原始已接受正文，仍用于精确前文回放。编号/交付使用新 `DELIVERY_BODY.md`（结果含 `delivery_body_path`/哈希）：只将已知精确 P#### 身份的裸号、圆括号/全角方括号兼容成 `[P####]`；不改代码块、链接目标或较长标识符，不推断未知来源，未知编号保留并报告。此格式转换不证明引用支持相应论断。未完成稿的交付副本仍是未完成稿。

作者每次可见全部章节 writing_arrangement；全局指南中已逐字嵌入的安排不重复附加。当前章 required_content 仍只属于当前 assignment。科学投影递归去除 outline_action，保留发现、条件、限制及来源版本差异。通用提示词强调关系与现象主语、真实前文决定重复/重返以及自然过渡；没有添加任务清单或审稿链。
