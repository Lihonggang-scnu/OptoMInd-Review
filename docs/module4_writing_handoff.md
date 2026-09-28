# M4 写作素材交接

`handoff` 是离线打包步骤，不再次调用模型，也不把未通过验收的阅读结果改成合格。用于将既有 `PAPER_READING_DOSSIER.json` 中的结论、完整 context、quantities、归属和原模型核验结果一起交给后续写作。

```powershell
python scripts/upgrade3/module4.py handoff --dossier <PAPER_READING_DOSSIER.json> --snapshot <snapshot目录> --output-dir <新的输出目录>
```

增加 `--dry-run` 可只验证输入与 snapshot。已有输出不会覆盖。生成 `WRITING_HANDOFF.json` 和便于人工查看的 `WRITING_HANDOFF.md`。

下游按 `facets[].card_ids` 选择卡片，再读取每张卡完整的 `text_with_conditions`、`original_unit` 与所需 source_bank 条目。不要只抽出 statement，也不需要把整份包重复塞进每个写作请求。未关联 Facet 的材料保留在 `unassigned_card_ids`，可用于方法、背景和局限补充。

`related_section_context_ids` 补充原引用同节的图注和表格，目的是方便找统计限定；不表示这些块自动支持卡片中的所有主张。引用时应选实际支持该主张的原文块。过去模型的 supported 判断不是人工认证，原始 not_ready 不会被覆盖。

2026-09-21 验证：四份真实输入的 219 条原始单元及嵌套字段均完整保留。该步骤只保证交接不丢数据；尚不能保证下游模型一定把限定写进正文。额外的模型自由改写实验未被采用，原始实验与个人复核见 outputs/module4/20260921/final_handoff/FINAL_REPORT.md。
