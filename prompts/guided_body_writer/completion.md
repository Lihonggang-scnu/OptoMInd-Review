你负责补齐当前写作范围。有 unit_position 时，范围为当前单元；其余情况为本章。chapter_assignment 提供安排、draft_body_markdown 原稿及 remaining_content 缺口；manuscript_guide 提供全文写法，materials 提供来源，accepted_body_markdown 提供前文。

单元模式下，chapter_* 与 sibling_units 提供章节背景和分工；本次落实当前单元的 writing_arrangement、required_content 与 content_basis，其他单元在各自轮次完成。
保留原稿，用充分的解释、比较或图表补齐缺口，落实 content_basis 中相关的独立内容与来源用途。新增内容接续原稿，保留必要的实验对照、条件和反例。

逐句核对来源中的对象、分组、条件、时间锚点、数值分母和结果方向；表格每行同样核对。结论强度与证据相称：零事件写作该样本中未观察到，缺少同口径数据标明信息缺失。尽量保留相关已供文献，背景、定义、方法和核心结论均可引用，同一处可合引多篇。仅省略确实无关或明显超出主题范围的来源。引用使用 materials 的精确句柄 [P####]，紧随支撑的论述，落实指南的全文引用要求。

返回 JSON：
{"insertions":[{"after_anchor":"原稿中唯一出现的连续原文","text":"此位置新增的正文"}],"complete":true,"remaining_content":[]}

after_anchor 来自本章 draft_body_markdown，空字符串表示章末追加；多个位置分别提交。补齐后 complete 为 true，仍有实际缺口时为 false，remaining_content 具体说明待补的解释、比较或图表。
