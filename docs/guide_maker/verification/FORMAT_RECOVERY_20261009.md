# 格式恢复验证（2026-10-09）

基线为公开归档提交 `45e519cd583b5e9824c20dd3752a8bcdda8a61c7`。本次没有模型付费调用、没有修改原验收档案或账本。

## 专项

指南、作者及新增恢复共 10 个测试文件 **240 passed, 0 failed**，最终运行 3.02 秒。测试命令见 [使用说明](../FORMAT_RECOVERY.md)。覆盖：

- 本次完整原响应恢复，与归档手工转义副本对象完全相等。
- 正常 JSON 不改、重复键/非有限数拒绝、引号/控制符/尾逗号/完整围栏/BOM。
- 截断、缺逗号、需要补字段或内容等超出范围的错误不伪造完成。
- provider 错误/未结束、模型未完成、待补读、缺章和 schema 错误均不能发布最终指南。
- 自动两阶段流程、补读处理、缓存复用、原始响应不变和恢复记录持久化。
- 免费离线 CLI 不初始化模型/账本，不覆盖已有目录或写入旧运行目录。
- 原生 Max 请求实际带 `response_format={"type":"json_object"}`，默认 Plus 未变；没有将它误报为严格 JSON Schema。
- 新恢复实现及其复用的 `chapter_arrangement.py` 引号 helper 均进入运行/CLI 源码哈希。

独立复核另检验了 7 种嵌套 provider 失败状态，首次运行及缓存恢复均不发布 GUIDE，缓存恢复没有新增调用。`git diff --check` 通过。

更广的 `tests/upgrade3` 回归：**2163 passed、17 failed、10 subtests passed**（164 秒）。17 个失败在独立 `45e519c` 基线工作树逐项全部复现，test IDs 和路径归一化后的错误行相同；未发现本补丁新增失败。既有失败包括 `material_visibility_catalog` 的 5 个 `source_supplied_locators` KeyError，以及 `review_delivery` 的 12 个历史测试夹具/硬编码 Windows 路径缺失。没有为清零这些无关失败扩大本次修复，不宣称全仓全绿。

## 原始返回的零费用恢复

按归档 parts 清单恢复 maker_002 RAW_RESPONSE，逐分片和总量哈希通过。原响应文件 SHA-256：

`21d9cd5cd15f31cd6e28eb018c092f826f751e17c401684b7dbb241e9fe392ec`

最终库后端 `json-repair 0.63.5` 配合受限机械检查，产生恰好 8 个 `escape_inner_quote` 编辑。原 content SHA-256 为 `32c5335ea5db84f8574ed07e6466fd7580fe9ee3fdc9eb1e073e06c2b6711a59`，规范化 content SHA-256 为 `d28507176b46c0853df27190eb5ae135afa3756e16e2f57d8cb1a83d1d46ecae`。恢复后的完整响应对象等于归档 `recovery/MODEL_RESPONSE_ESCAPING_ONLY.json`；GUIDE 等于其中的 guide，也等于原保留草稿。原文件哈希未变。

独立离线入口验证结果：`complete=true`、七章齐全、`model_calls=0`、`paid_dispatch_count=0`、`original_run_modified=false`。此完成只表示该响应通过原协议与结构检查，不重写原失败运行，也不声明科学质量或正文完成。

## 两阶段录制回放及计量边界

采用原 maker_001/maker_002 两份响应、冻结七章 book 与原 feedback，完整走新流程。第一阶段提出的两份材料请求得到处理；第二阶段自动修复 JSON 后完成，`read_history` 记录 P0593、P0090；最终 GUIDE 精确等于归档 oracle。再次运行复用缓存，录制客户端新增调用数为 0。整个回放没有网络模型调用，费用为 0。

云端没有该模型的本地 tokenizer。第一次完整 CLI 回放使用默认 UTF-8 字节上界，在第二阶段报告容量不足，未截断材料；该失败记录保留。随后两阶段集成回放显式采用合成测试计量器，仅验证流程与解析，**不能用其结果证明真实线上容量或费用**。离线单响应恢复不依赖 token 计量；此前本地真实运行的输入/费用仍以原归档为准。

这次没有重新运行 Qwen 来验证 JSON Object 模式的实际生成效果；仅验证最终请求接线，并依据官方能力说明启用该现有模式。语法、schema 和科学质量仍分开评价。
