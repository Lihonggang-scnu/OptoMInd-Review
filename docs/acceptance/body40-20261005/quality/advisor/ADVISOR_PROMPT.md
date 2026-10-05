# GPT-6 Pro：投稿质量与下一阶段实现指南

你是外部导师顾问。请依据实际稿件、代码和范例提出下一阶段方案，不预设我或另一位AI的结论正确。当前只审阅与规划，不执行代码，不发起模型调用。

## 固定阅读入口

以下均在GitHub的review-v2分支，main保留旧技术报告版本。

- 阅读包入口：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/advisor/20260930/README.md
- 最新状态：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/docs/REVIEW_V2_DELIVERY_STATUS_20260930.md
- 真实六章稿：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/advisor/20260930/delivery/real_manuscript/REVIEW_DRAFT.md
- 最新46页集成PDF：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/advisor/20260930/delivery/offline_fixture_integration/main.pdf
- 30篇范例索引：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/advisor/20260930/exemplars/study/INDEX.md
- 另一位AI意见：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/advisor/20260930/feedback/LOCAL_AI_FEEDBACK.txt

核心代码：

- 统一交付入口：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/optomind_research/runtime/upgrade3/review_delivery.py
- 现有全文局部编辑器：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/optomind_research/runtime/upgrade3/article_text_editor.py
- 编辑提示词：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/prompts/article_text_editor.md
- 题名/结语/引言/摘要：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/optomind_research/runtime/upgrade3/manuscript_front_back.py
- 图表和引用：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/optomind_research/runtime/upgrade3/delivery_citations.py
- LaTeX出版器：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/optomind_research/runtime/latex_publication_renderer.py
- 冻结规划层：https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/review-v2/optomind_research/runtime/upgrade3/progressive_review_plan.py
- 原任务编排/写作：https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/review-v2/optomind_research/runtime/upgrade3
- 交付测试：https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/review-v2/tests/upgrade3

GitHub页面截断大文件时，改用raw.githubusercontent.com/Lihonggang-scnu/OptoMInd-Review/review-v2/加同一文件路径，或下载阅读包ZIP。不要把“网页没展示全文”写成“材料不存在”。如确实读不到，列明缺件。

## 先辨认材料，再评价

1. 阅读最新状态及阅读包README；真实六章正文来自9月27日，23个单元、176篇参考文献。历史首尾也有人工整理，不是最新冻结链重跑的全篇。
2. 先独立阅读这篇正文，再读另一位AI意见交叉印证。逐项判断其批评是否成立，不能以AI互相同意当成事实核验。
3. 最新46页PDF沿用真实正文，但编辑、首尾和示意图使用明确标注的fixture，证明消费链和出版行为。请检查格式，但不要把演示摘要/引言的质量算成Qwen能力，亦不要据此推断整套规划失效。
4. 冻结03真实小样在delivery/content_handoff_03_real；根代理ASTRA_REVIEW优先于助手HUMAN_COMPARISON，揭示工况/条件错配及结果遗漏。它是另一学科的小样，不属于六章稿。
5. 30篇记录中27篇有本地可读全文和结构，P03/P06/P09只有出版入口。先看结构研究结论，再检查实际范例的题名、摘要、引言、正文展开与图表。区分叙述性综述、系统综述/荟萃分析、roadmap等文体，不能把不同期刊要求拼成万能模板。记录实际读到哪些，禁止假称阅读30篇全部全文。

## 任务一：达到投稿要求的形式与表达

这部分是重点，不只是排版美化。请检查题名是否表达范围与中心思想，摘要是否给出真实的综合结论与必要边界，引言是否从背景、已有认识推进到本文独特的问题/贡献；同时检查标题层级、内部任务语句、章节衔接、结语、图表说明/正文指引、参考文献格式与PDF布局。

对照真实范例，指出我们哪些做法值得保留，哪些使稿件像“程序任务书”，哪些属于明确工程错误，哪些必须通过更好的内容提炼解决。举出具体片段和可迁移写法，不只列抽象要求。现阶段尚无确定目标期刊，请区分“通用可投稿稿件”与以后期刊模板适配，不能宣称一个模板保证任何期刊接受。

题名、摘要和引言应重点给出可落实的生成/修改任务：所需输入、模型应读哪些全文或规划信息、输出长什么样、怎样主观验收。不要把范例内容写进领域专有提示词。检索方法说明只能依据实际运行资料，不能为显得正规编造系统综述程序、检索截止时间或完整性。

## 任务二：提升内容，优先改装下游

判断作者观点、章节职责、比较、案例、条件、阴性结果和论证是否足以服务完整综述。允许修冻结上游显而易见且收益明确的问题，但必须指出真实触发、根因、最小修改位置、验收方法与预期收益。不要默认重跑上游或让所有阶段都返工。

优先评估现有article_text_editor的改装空间：目前它主要处理少量重复/术语/衔接，不是强力科学审稿。读其真实输入、提示词、返回解析与应用函数后，建议如何扩大职责及补足审稿所需材料。可以复用旧版main中有用资产，但要说明改装点，不能拿旧逻辑覆盖冻结实现。

追求能让稿件明显变好的修改，不建设繁重的证据绑定、哈希验收或逐句完美审计平台。综述中转述的原始研究应保留归属与引用，但不得仅因通过专家综述读到就降为二等材料。无需为所有引用再下载/阅读一次原研究全文。

## 任务三：百炼原价模型实验怎么选

后续计划在阿里百炼中选择较强、但价格合理的审稿模型。请查询当前阿里官方模型/定价文档并附直接链接和查阅日期，排除优惠券、免费额度和折扣口径。列少量真正值得试的候选；不编造模型是否上架、版本、价格或能力。

估算应覆盖实际全文长度的上下文价格档位、输入、思考/输出、必要复审与实际修改成本，而非只看“每百万输入”报价。明确价格地区/部署形态/缓存假设，不用GPT-6 Pro对话产品价格冒充百炼API价格。

给出小额实验的具体题目/片段选择、输入材料、输出任务和比较方法。目标是绝对有用的审稿与改稿能力，不是公平比赛：发现重大真实问题、给出具体且正确的修改、改善题名摘要引言及综合论证、避免改坏好段落。你的审稿意见可作参考，但不视为唯一真值；候选可提出你未发现的有效问题。不追求便宜模型“找一两个错”便算过关；昂贵模型若增益不足也淘汰。说明怎样确定质量/费用平衡与停止，不发起真实调用。

## 任务四：评估我的局部替换设想

我设想审稿模型像导师直接输出JSON：

```json
{"edits":[{"old_text":"原稿连续旧片段","new_text":"改好的新片段","reason":"替换原因"}]}
```

程序先找原文精准替换，避免模型重输未改的全文。若old_text发生少量漂移，可以语义查找近似原文，低于阈值则作废。请判断该设想哪些值得保留、哪些应改变，不能把它当已批准的最终设计。

请具体提出：旧片段范围与章节定位、多个命中怎样选、多个编辑重叠/先后依赖如何执行、模型少量文字漂移怎样匹配、语义检索是否仅用于找候选还是足以授权替换、相似度阈值怎样用真实样本确定（不要凭空给一个通用0.9）、否定/数字/条件高度相似时如何避免改错。用最少的字段和程序逻辑解决实际问题，不扩张为复杂审核制度。

能够用连续段落替换的题名/摘要/引言重写可沿用该协议；新增段落、重排章节、形成贯穿全文的综合框架等不能勉强伪装成单个三元组，请另给轻量方式。结合当前编辑器给出最小改装位置与测试，说明审稿如何看到必要材料、什么时候确实需要原卡片而不是只看稿件。

请在真实稿上示范少量高价值三元组，old_text逐字来自选定版本，并标明选用文件；不能给fixture摘要做科学返修，也不能编造事实/数字/引用来让新段落更好看。示范同时用于判断协议是否易执行，而不是要求本轮自动修改正文。

## 交付

在对话中简要说明：稿件主要优点/短板、最有收益的下一步、应保留的实现、暂不值得做的改动。另生成一份完整Markdown《NEXT_PHASE_SUBMISSION_AND_REVIEW_GUIDE.md》，包含：

- 实際已读材料及其限制。
- 投稿格式与题名/摘要/引言改进方案，引用实际范例观察。
- 少量按顺序可执行的工单：核心文件、输入、实现方法、交付物、内容质量验收、阶段停点；不能只以测试数量验收。
- 强模型候选实验及计算完整流程费用的方法。
- 最小编辑JSON及匹配/执行策略、真实三元组示范。
- 上游少量明确修复与下游优先路线的取舍。

先复用上传的真实稿件、材料和测试产物打通下游，全部模块过关后再考虑一次完整新题E2E。总体目标是高质量完整文献综述；提示词与机制必须能泛化，不坍缩到微生物组、肿瘤或电池领域。
