# 正文最小修复的免费验证

本次云端没有调用付费模型，没有生成新正文，也没有更改冻结指南或上游检索。

## 专项

以下七组测试共 132 项通过（1.80 秒）：guided_body_contracts、guided_body_writer、guided_body_cli、guided_source_handoff、guided_delivery_format、guided_body_plus_only、guided_writer_prompt_requirements。独立只读审查另跑相关组合 86 项通过。`compileall` 与 `git diff --check` 通过。

- 实际旧 B 第七章 RAW_RESPONSE 经原解析器读取，完成元数据正常；真实 GUIDE 的章节标题与外层正文标题一致。
- 只对交付副本去掉外层 Markdown 包装，原 RAW_RESPONSE、FULL_BODY、阶段 BODY、科学文本和引用身份序列不改。
- 使用 MarkdownIt 渲染：原章进入 `<pre><code class="language-markdown">`，修复副本生成正常 `<h1>`、`<p>`，不再是整章代码块。
- 夹具覆盖第一/中间/最后章、原始前文逐字传递、正常 Python 代码块、未知引用、标题不符、短示例、嵌套/未闭合围栏、4 空格或制表符代码、LF/CRLF、缺完成标记的部分交付、空预览。
- Plus-only 覆盖错误配置在访问账本/客户端之前拒绝、与 Max 许可互斥、后续调度配置变化时再次拒绝，以及恢复策略身份绑定。
- 数量要求来自输入指南，生产提示词未写死某个论文数量；未新增自动质量判分或科学审稿。

这验证工程与显示行为，不证明本轮尚未生成的正文质量或150篇目标已达成。

## 更广回归

`tests/upgrade3`：2,264 项通过、17 项失败、10 个 subtests 通过（170.24 秒）。17 个失败测试身份与保留的上一轮基线清单相同（材料目录历史字段5项、交付历史夹具/路径缺失12项），没有新增失败测试，但不是全套全绿。本轮未重新执行旧基线。
