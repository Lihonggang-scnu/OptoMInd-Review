# 提示词定点修订：免费验证记录

日期：2026-10-09。基线：`6d48a3b0350a745d0a803d41f7ba5a340c5c4e33`。未发起付费调用，未生成新指南或正文。

## 自动测试

以下 8 个专项测试文件最终合计 **180 passed, 0 failed**（2.61 秒）：

```text
tests/upgrade3/test_guide_maker_contracts.py
tests/upgrade3/test_guide_maker.py
tests/upgrade3/test_guide_maker_cli.py
tests/upgrade3/test_guide_maker_dedicated_budget.py
tests/upgrade3/test_guide_maker_prompt.py
tests/upgrade3/test_guided_body_contracts.py
tests/upgrade3/test_guided_body_writer.py
tests/upgrade3/test_guided_body_cli.py
```

新增 11 项检查：6 项提示内容存在检查、1 项 JSON 示例结构检查、1 项真实预览阶段加载完整提示检查、2 项显式 Max 配置免费预览参数化检查，以及 1 项付费 Max 缺少许可时的拒绝检查。默认 Plus 不变；Max 的免费预览不需要付费许可，`--run` 时必须显式 `--allow-max`。测试禁止建立真实付费客户端和预算调用。`git diff --check` 通过。

这不是全仓测试通过声明，也不证明模型会遵守全部语义要求。提示短语断言用于防止后续误删指令；真实写作效果仍需本地模型测试。

## 真实七章归档的免费请求检查

从公开 B repair 归档的 `shared/canonical_input/FULL_BODY_INPUT.json.parts.json` 按顺序恢复 17 个分片，逐项校验字节数及 SHA-256。恢复总量 9,977,216 bytes，SHA-256 为 `8ad1755e8d8ddd5296fc96bc58589944e09a08391e1adebdf11b076d2d7d6b1b`。该材料仅用于离线 `--book` 预览，不能冒充本地真实 manifest 的付费运行。

使用最终提示、显式 Max 配置和仅含新增引用目标的 feedback，通过实际 CLI 生成 `maker_001` 请求；核实：

- system 文本与最终 `generate.md` 完整一致。
- 输入包含 7 章、221 条来源目录、初始 `materials=[]`、`prior_guide=null`。
- 用户的全文 150 篇去重目标通过 feedback 进入实际请求。
- model_calls、client_invocations、paid_dispatch_count 均为 0；没有生成指南。
- 输入未裁剪，材料请求流程与 schema 未改。

云端本次没有适用的本地 tokenizer，预览明确使用 UTF-8 字节保守上界，而非真实模型 token 计量。此预览验证接线，不用于预测实际费用或证明线上容量；本地必须重新核实模型、计量与预占预算。运行时价格/容量注册表沿用现有值，本次没有外部重新验证或修改。

## 人工范围审查

复核提示没有本题专有名词、固定 150 指标、每任务对应表、额外评审链或强制补读数。来源作用标注限定在合并/复用时易丢失或混淆的地方，避免逐篇重抄细纲。已修正测试交接中可能误读为“直接运行裸 CLI”的表述，明确旧 Plus-only wrapper 需要本地适配和免费验证后才可承担 Max 运行。

未改规划层、正文作者、科学材料、预算执行逻辑或历史归档。指南相对质量、最终引用数量与科学引用正确性均未在此宣称通过。
