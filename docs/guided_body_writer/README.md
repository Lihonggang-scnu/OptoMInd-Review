# 指南驱动正文作者

这是独立的新写作入口。作者消费写作指南，不再把原始细纲任务作为正文生成指令。复用已有的章节材料、真实前文、传输、缓存、恢复与预算机制；不开发自动指南生成器，不重跑规划。

## 输入到输出

每次作者请求只有四个顶层内容字段：

- `manuscript_guide`：全文写作指南。
- `chapter_assignment`：当前章节的自然语言写法、可选必须完成内容。
- `materials`：完整相关科学记录、来源身份、工具补充与回读导航。
- `accepted_body_markdown`：实际已写前文。

不传原 `tasks`、单元指令、章节规划框架或原任务完成 ID。不存在新增的“指南责任到 95 任务”对应表，也不增加覆盖评价模型。章序与材料章身份的简单检查用于找到正确材料，不检查每个段落应如何写。

现有封存 book 仍可作为来源容器。兼容适配器按原有章节来源连接提取材料，不生成新的对应层。作者看不到这些旧任务连接。指南可用可选 `source_handles` 补充指定来源；回读可从完整来源池取回相关科学记录。

输出是当前章的自然正文以及简短伴随记录：

```guide_writer_metadata
{"complete":true,"remaining_content":[]}
```

若作者报告具体内容缺口，最多进行一次局部插入补写。缺口使用自然语言，不是任务 ID。补写也保留同样四类输入；原稿及缺口放在 `chapter_assignment` 中。

## 保留的执行能力

按指南章序连续写，实际完整前文进入下一次请求。科学记录保留完整值与来源条件，不做固定字符截断。支持按来源或已知证据原子回读；已提供的材料请求不会产生无意义的重复读取调用。

完整成功稿、有效部分稿和补写进展分别保存；失败补写不覆盖原稿。缓存依赖真实指南、输入、提示、代码与前文内容。没有默认付费重试，没有隐藏全文编辑。预览和录制回放不读取凭据、不收费。

用户要求沿用原 60 元账本剩余额度，因此此入口只接受已存在、带原 round-two marker、累计上限为 60 的账本；不会创建新额度。保留原已结算费用与未决占额。

## 本版边界

本版以完整章节为写作单位。真实请求超过容量时，保留完整输入并阻断，不偷偷回到旧任务切片，不截断材料或前文。尚未实现指南内自然小节的自动分批；这是明确边界，不需要为了本轮七章样例先增加一套分批规划。

运行完成标记表示模型返回和章节状态，不替代全文阅读。这里没有新增科学审稿器、事实评分器或任务覆盖证明。

## 指南文件

`guides/guide_A.json` 和 `guides/guide_B.json` 已适配新接口。对应 Markdown 可直接阅读。这两份保留人工指南的实质写法，删除了旧版要求填写任务 ID 的交付句子；不需先解压旧 ZIP 才能运行。

通用格式：

```json
{
  "schema_version": "optomind.guided_body_guide.v1",
  "manuscript_guide": "完整写作指南",
  "chapters": [
    {
      "chapter_id": "与来源容器对应的章节身份",
      "title": "章节标题",
      "writing_arrangement": "本章怎样解释与比较",
      "required_content": ["可选的自然语言内容要求"]
    }
  ]
}
```

章节数量、名称、主题和论述结构都由输入决定，代码不包含医学领域的特定结构或规则。

## 运行

新入口：`scripts/upgrade3/guided_body_writer.py`。

```powershell
python -X utf8 scripts/upgrade3/guided_body_writer.py --manifest "<真实 PREPARED_MANIFEST.json>" --guide docs/guided_body_writer/guides/guide_A.json --config config/guided_body_writer/plus_first.json --tokenizer "<现有 tokenizer.json>" --output "<新的 A 输出目录>"
```

默认预览。没有 `--dry-run` 参数，也不需要原实验包的 `PROMPT_ROOT` 启动适配。`--responses` 为离线录制回放，`--book` 仅用于封存输入的离线检查。

正式测试加入 `--run`、原账本、`--budget-limit 60` 和现有密钥文件。详见 [本地测试交接](LOCAL_AGENT_HANDOFF.md)。

## 验证

专项测试覆盖任意非医学章节、四字段输入、递归指令隔离、条件保留、回读、部分正文与插入补写、缓存恢复、容量阻断、原账本保护和 CLI。

另用第二轮七章真实归档，对 A/B 各作免费录制回放：实际前文逐章一致、四字段输入检查通过、原七章文本重现，随后恢复均零新调用。回放计数器为测试替身，不代表真实 tokenizer 容量；重现旧正文也不意味着新指南已产生质量提升。详见 `verification/ARCHIVED_REPLAY.json`。

最终回归数字与版本见 `verification/VALIDATION.md`。
