# OptoMind 全文写作候选方案调研与建议

2026-10-07。以完整 BODY 为交付与比较单位。本轮只做公开代码、提示词和已有试验资料研究，没有修改生产代码或调用付费模型。

## 决断

建议保留四个有区别的候选：**全文受托作者、连续作者、长文工作台、陌生读者驱动的全文修订**。前三个解决全文怎样写出来，第四个在已形成的完整稿上增加有目的的改进。它们共用已批准细纲与科研材料，不重新设计正文planner。

最值得优先尝试的是：**把现有细纲交给一个对整篇文章负责的作者，用读者理解的推进来组织正文；能一次写完就一次，容量不允许时保持同一篇文稿连续写。** 多次调用不等于多个独立作者，更不必然需要最后再请一个昂贵模型合稿。

本轮检查了12组公开项目或skills，以及2份正式写作规范，包括研究原型和工程项目。没有发现可以直接替换OptoMind、已经证明能产出顶刊综述的通用组件。真正值得借鉴的是若干具体方法：累计正文续写、分层上下文、原始需求直达作者、读者视角的解释，以及新上下文读者对成稿的检验。

## 一 先纠正比较单位

先前测试的整章Plus、分单元Plus与Max章级编辑，只回答局部写作问题。它们没有完成新的整篇BODY，因此不能评出全文方案的优胜者。

此次候选必须全部交付：

- 已批准大纲中的全部正文章节与必要图表内容
- 连贯的全文、正确的来源身份、保留的重要条件和反例
- 所有作者、续写、记忆更新、统稿、修订及核验调用的实际记录和总费用
- 明确选用的最终文件，以及原稿、改稿和恢复关系

文件拼接、保存与编号本身可以不调用LLM；它们也不会自动形成跨章论证。若方案依赖额外全文统稿，必须把它作为正式步骤及费用列出来。首尾独立模块仍是另一个阶段，本报告不把它混进BODY写作比较。

## 二 广泛调研后最有用的发现

| 项目或规范 | 实际阅读的实现或提示词 | 适合借鉴的部分 | 不直接移植的部分 |
|---|---|---|---|
| [LongWriter的AgentWrite](https://github.com/THUDM/LongWriter/blob/447539b356a8b09760b51eca876e19b6fc1f2dd7/agentwrite/write.py) | 顺序写作循环与write prompt | 每次读原始要求、完整计划和已写正文，再继续；无需默认统稿 | 固定段落/字数计划、旧小输出额度、不充分的截断和缓存处理 |
| [Re3](https://github.com/yangkevin2/emnlp22-re3-story-generation/blob/3a97ebde04e3333962c2825146897efe1dc87dd8/story_generation/draft_module/beam_candidate.py) | construct_prompt及候选生成 | 较远上下文、近期信息与最近原文分层提供 | 每段多候选与控制器、小说事实生成、有损左截断 |
| [DOC及DOC v2](https://github.com/facebookresearch/doc-storygen-v2/blob/d092b42686bfc29ef2d610d5563e822ebf9d1451/storygen/story/story_writer.py) | 大纲折叠、当前节点/祖先/后继上下文、生成与评分循环 | 当前写作部分详细，已完成部分保留结构定位；前后文协同 | 多候选与大量评分调用、固定短段长度、把计划事件当成已写事实 |
| [Dramatron](https://github.com/google-deepmind/dramatron/blob/2e7c36afadacf8321b77a468940024371b7a8c7a/colab/dramatron.ipynb) | generate_text与generate_dialog | 单次输出不够时，带已生成前缀继续 | 主要按场景组织，不等于全文连续作者；不能把触及上限写成完成 |
| [WriteHERE](https://github.com/principia-ai/WriteHERE/blob/817b489008e4b5dd50d5008803992dcf81c4fa50/recursive/agent/agents/regular.py) | report writer及regular代理 | 真实累计稿件进入下一次请求，证据参与论述 | 递归重规划、额外搜索/分析、超过固定字数强制拆分 |
| [AutoSurvey](https://github.com/AutoSurveys/AutoSurvey/blob/5e8f389f3d51b29bad16dc6ae75db3e8a45a3b65/src/agents/writer.py) | subsection writer、局部连贯性编辑 | 读相邻实际文本后修衔接 | 摘要级供材、小节最低字数、局部编辑冒充全文整合 |
| [GPT Researcher](https://github.com/assafelovic/gpt-researcher/blob/0957c301ed06c2a5857b834358c7227c739041d4/multi_agents/agents/writer.py) | report生成、DetailedReport、multi-agent writer与publisher | 原始问题、上下文与明确报告职责进入作者 | 某条多代理路径的writer只写首尾，BODY仍是拼接；绝对禁止内容重现也不适合综述 |
| [Open Deep Research](https://github.com/langchain-ai/open_deep_research/blob/1b7d2e80db9faa586165c60e09096dbbfd483a64/src/open_deep_research/deep_researcher.py) | final_report_generation与最终提示词 | 一位作者基于用户要求和全部研究结果整合成报告 | 压缩结果替代完整依据、超限时截取材料前缀、可自由折叠原纲要 |
| [DeerFlow](https://github.com/bytedance/deer-flow/blob/a4427625cfc0759021736cca51426bfc2759d6c8/skills/public/consulting-analysis/SKILL.md) | consulting-analysis的已有框架输入合同；旧reporter | 已有分析框架和材料直接进入写作，不必再次规划 | 商业咨询特有推断链、图表配额；旧实现的计划字段遗漏 |
| [OpenScholar](https://github.com/AkariAsai/OpenScholar/blob/0e9b8fb912273d3dae39e593da86e4f6d3bf8de1/src/open_scholar.py) | generation、feedback和editing的实际插值 | 跨来源解释异同，保留未修改的有用内容 | top-N短回答；一个反馈模板未真正接收当前证据；字数保留比例不能代替内容判断 |
| [Anthropic doc-coauthoring](https://github.com/anthropics/skills/blob/main/skills/doc-coauthoring/SKILL.md) | Stage 2及Stage 3读者测试 | 用未参与写作的新上下文读者阅读成稿，发现假定知识和断裂 | 反复让用户策划、逐问题派新代理和无限循环 |
| [Self-Refine](https://github.com/madaan/self-refine/blob/main/src/responsegen/task_iterate.py) | feedback与iterate实际提示词和循环 | 反馈必须指出具体表达问题与修订目的 | 短对话结果不代表长文能力；反复打分和保留全部历史会增加成本 |
| [Google Technical Writing](https://developers.google.com/tech-writing/one/audience) | Audience与Documents课程 | 写作帮助读者从已有知识到需要掌握的认识 | 不能把面向文档的固定格式强加给所有综述 |
| [Rust RFC模板](https://github.com/rust-lang/rfcs/blob/master/0000-template.md) | guide-level、reference-level、rationale | 先教会读者理解，再说明精确细节、取舍与边界 | 不移植RFC栏目，也不把建议中的机制写成既有事实 |

### 三个值得强调的代码发现

**AgentWrite并非独立写几段再拼接。** 它在每次调用中传入所有已生成正文，再追加新的内容。WriteHERE也将真实累计文章传给作者。这提供了一个与我们上一轮独立单元写作不同的路线。

**代理名称不能说明做了什么。** GPT Researcher某条多代理路径的Writer生成引言、结论、目录和来源，Publisher仍然直接连接各正文块。AutoSurvey的连贯性编辑主要围绕同章相邻小节，且实际提示词没有全文论证合同。不能因为项目有“Writer/Editor”就认为它解决了全文一致性。

**复杂路线的成本常藏在图里。** Re3和DOC有多候选、评分和编辑步骤；DOC v2的一次局部候选生成还可能带大量评价调用。它们的上下文方法有启发，但不能整体搬入已有深细纲的OptoMind。

## 三 单章试验给这次设计的启发

可用的局部观察是：Plus有能力写出具体机制与条件；分单元写作可能丢掉整章一次写原本保留的内容；两次Plus通用统稿分别不改正文、仅删除一个引用；Max能补回部分条件，但也会重复或丢限定。

因此，新候选不应该继续围绕“每块写完，再多找一个更强编辑”展开。应先改变作者的工作对象和上下文：**从领取一份局部任务，改成推进一篇完整文章。** 其次改变评价视角：读者究竟理解了什么，而不是模型是否认为自己完成了任务。

另一个已确认的小接缝须在后续实施时修正：当前共用编辑提示词把“不得声称完成全章通读”的窗口限制也放进整章请求。全文、章级和窗口级职责必须明确，不能共用互相矛盾的范围说明。

## 四 候选一 全文受托作者

### 核心变化

主要创新在任务表达。不要让模型扮演逐项交作业的单元执行者，而让它作为作者，为明确读者写完这篇完整综述。原细纲仍规定科学任务，作者决定怎样把这些任务落实为自然的解释和论证。

### 输入

- 用户原始问题、范围、目标读者与语言
- 最终完整细纲、全文主线、章节分工、案例用途和条件
- 所需来源的完整材料及身份；精确重复只供一次，互补材料保留
- 一份简短的写作职责指导，可加少量与测试领域无关的表达示例

这里的完整材料指完整保留当前可用的相关内容，并非强制每篇来源都有自己的A/B或论文全文；有用的综述转述与明确引用身份仍正常、同权参与。

不能只喂一级目录，也不能把当前章节当作唯一任务。将细纲中的写作目的和材料中的事实依据区分开，防止把“准备论证什么”当成已经得到的科学事实。

### 工作与输出

输入和完整成文的输出容量都足够时，一次生成全部BODY。成稿段落不受任务ID数量约束；任务与引用身份可保留在伴随记录中，不能要求每个brief必须对应一个可见小标题。

输出为完整BODY及可追溯的来源/任务记录。没有默认额外编辑调用。一次停在某章，或者将后半篇压成摘要，都不能算该候选完成。

### 新的提示词主旨

> 你是这篇综述的正文作者。读者没有看过任务书和材料卡片。沿用已批准的大纲与科研材料，写成一篇持续推进理解的完整正文。
>
> 写作应让读者理解发现之间的关系：一个结果为何重要、差异怎样产生、哪些证据能够相互解释、哪些条件改变判断，以及这些认识怎样引出下一层问题。任务书规定要完成的工作；正文应直接完成解释，不向读者宣布“需要比较、汇总或分析”。
>
> 把前文已建立的认识作为后文起点。同一研究可以再次使用，但应承担新的解释作用。重要条件在后文概括、表格和比较中仍然有效。不要让每章重新介绍相同背景，也不要让每段以相同的免责声明结束。
>
> 按知识需要选择段落、句序与详略，不固定每段的修辞步骤。具体材料决定科学表述能写到哪里；任务中的概括若强于证据，应以证据支持的程度完成解释。完整写出所有授权正文章节，不以目录、待办或简略后半篇代替成稿。

这是新拟的通用职责，不包含本题科学答案。它与来源协议配合，不是把更长的一串禁令叠到旧prompt上。

### 价值和代价

这是最轻的全文候选，正常只需一个作者调用。主要风险是长输入下注意力分配和输出不够；即便章节齐全，也要看是否真正展开。不能因想保住“一次调用”而压短后半篇。

## 五 候选二 连续作者

### 核心变化

**允许多次调用，但始终是同一篇文稿的连续写作。** 第一次写出的真实正文进入下一次请求；模型从已经形成的论证继续，而非仅看到“上一章负责什么”的说明。

可以把它理解为一个作者分几次完成一篇文章，而不是几个人分别写几篇短文。即使容量勉强能容纳，若全文一次生成持续把后文写薄，也可以按自然论述任务安排连续写作；这种质量分段应明确记录，不能冒称供应商超限。

### 每次真正看到什么

- 完整原始要求、全文主线和已批准大纲
- 当前需要展开的完整细纲任务和相关材料
- 已接受的真实正文；能容纳时保留全部前文
- 明确的写作位置、尚未完成的职责和接下来的内容边界

未送入当前请求的材料仍在同一材料池，按已有任务来源关系完整读取；不是永久丢弃。已写正文提供连续性，不替代原始材料成为新的事实来源。

### 执行方式

1. 从全文开头开始，优先一次写出尽可能完整、自然的正文
2. 如需续写，保存原始响应、实际用量与准确断点
3. 下一次调用带前文继续，避免重复开头、重复定义或提前总结全文
4. 直到全部BODY完成，才交付最终稿

程序追加文本、保存文件和检查重复接缝不需要LLM。该候选默认没有“全部写完再让另一个模型统稿”的隐藏步骤；若后来加入编辑，另列实际调用和费用。

### 相比旧分单元路线的关键差别

验收必须检查下一次请求中是否真的出现前面生成的文章，以及模型是否被授权继续同一篇文章。只增加一个全文标题或章节职责表，仍然让各章独立生成，不算实现本方案。

### 成本

作者调用为K次，K由实际全文和容量决定。重复输入前文可能增长较快：相同材料重复送、越来越长的历史稿都要计费。提供方前缀缓存只按实际命中统计，不假设免费。

它省掉的是常规事后合稿，不是让所有续写变成零成本。全文质量与总费用一起比较。

## 六 候选三 长文工作台

这是候选二在输入确实过大时的扩展，不建议在小稿上为复杂而复杂。

### 核心变化

全文保存在可读取的文稿工作区，作者始终知道全文在讲什么，但不在每次调用里重送所有远处文字与所有论文。采用多层上下文：

- 固定保留用户要求、全文主线、术语和来源身份
- 当前论述的细纲、关键条件与相关材料完整提供
- 最近正文保持原文，用于语气、指代与衔接
- 较远正文保留结构与已经建立的认识索引，可随时回看原文
- 后续大纲可见，避免提前耗尽别章职责

优先用现有纲要层级和稿件定位组织导航。若额外调用模型生成远处正文摘要，就单独计费，并把摘要作为导航，不能让它取代科研证据。原稿更新后，相关导航也要更新。

### 为什么它仍是全文写作

本轮目标始终是完成同一份完整文稿；所有章节都要写完。当前窗口只是作者此刻的工作台，并不是最终交付范围。最后文件写齐不等于已经连贯，要亲读跨章论证、同概念复现用途和前后条件是否一致。

### 借什么 不借什么

借DOC/Re3的多层上下文，不借小说人物事实生成、不借多候选搜索、不借固定字数分段，也不把旧内容简单截掉。按完整任务组织材料，跨块必须比较的来源应一并看见。

### 成本和使用条件

成本是K次作者调用，加实际发生的S次记忆整理或其他模型调用。能直接回读文件和确定性组织索引的步骤不需要模型。只有候选二的历史重送已经明显昂贵或超限时，才优先测试本候选。

## 七 候选四 陌生读者驱动的全文修订

完整方案是：**候选一、二或三写出整篇BODY，再加一次有目的的阅读诊断和必要的局部修订。** 不是只测试编辑器，也不把底稿成本从方案总价中抹掉。

### 为什么换成读者

上一轮Plus统稿已经看见资料，却几乎照抄初稿。“请把文章改得更好”容易变成风格判断与宽泛自检。这里让一个没有参与写作的读者根据成稿重建认识：它到底读懂了什么，哪里必须自己补关系，哪些内容重复却没有推进。

### 阅读请求

读者主要读取完整正文、目标读者定位和原始研究问题。它不读取作者自评、我们的人工问题清单或先前候选的好答案。必要阅读问题来自已经批准的读者目标，不额外花一轮模型重新策划问题。

> 请作为首次阅读本文的目标读者，说明你从这篇完整正文中形成了什么认识，各部分如何共同回答研究问题。不要用你自己的领域知识替作者补上文章没有解释的关系。
>
> 找出真正影响理解的具体位置：必要联系未讲清、前后条件不一致、重复没有新作用，或结论似乎跳过了依据。给出文本定位、实际读出的意思以及妨碍理解的原因。优先处理值得修的问题，允许没有问题；不按段落配额挑刺，不给空泛评分，不直接重写全文。

读者调用拥有充分思考与回复容量，不再用极小输出上限逼它把问题说明压扁。最终列多少问题由价值和成本决定，不规定统一条数。

### 修改请求

对成立的问题，写作者读取原文、对应细纲与有关完整材料，做局部修改。它可以根据实际原文和材料驳回不成立的意见，不需要服从读者的一面之词。与问题无关的好内容保留。

独立读者能发现阅读断裂，不会自动证明所有事实正确；涉及科学判断的修改，必须回看相应材料。可以复用现有定点修订和版本管理能力，不另起复杂审稿平台。

### 全部成本必须列出

完整方案费用 = 底稿实际生成费用 + R次读者诊断 + P次修订 + V次核验（若使用模型核验）。

通常先测试一次全文阅读和一批必要修改；没有值得修改的问题就不再调用作者。若相关材料仍超限而分批，实际次数照实计入。复用现有底稿不再向账本重复收费，但候选方案比较要列明共用底稿成本和新增成本。

## 八 OptoMind值得形成的独立特色

不是再给系统多加几个角色名，而是把三个优势连接起来：

1. **已有科研理解足够深。** 作者拿到的是最终问题、解释关系、来源用途和条件，不必再从零搜资料或发明全篇主线
2. **全文认识可以累积。** 系统区分“细纲安排了什么”和“文章实际上已经讲清什么”，让后文从前文推进，而不是反复开始
3. **读者的理解失败成为可操作反馈。** 修改具体的解释缺口和冲突，不靠泛泛的更专业、更深入、去AI味要求

文风的创新首先是任务视角：把“按任务写满”改为“让读者沿全文理解一个有依据的认识体系”。不要把所有段落写成统一五步法，也不要让所有文本都像咨询报告、教程或争议评论。

可少量使用与目标领域无关的虚构示例，展示从罗列到解释的转化。示例只教写法，不是当前事实、模板或期待答案。无需每次重喂35篇综述，也不把模仿某期刊措辞当成质量目标。

## 九 下一轮实际安排

先修清章级/窗口级编辑范围指令，并准备共同的**全文输入**：锁定的最终细纲、全篇任务、统一来源身份和可回读材料，保留原有上游成果。

建议按收益依次推进，避免四个方案全部重跑三种模型：

1. 全文容量能容纳时，先试候选一的Plus完整BODY；将真实全文作为最轻路线
2. 单次输出或输入不足，或者一次生成把后文写薄时，试候选二。输入与输出容量分别处理，不靠少写来完成
3. 候选二因历史上下文成本或容量受限，才启用候选三
4. 对已有完整稿试候选四，观察它相对该稿的实际增益；不要重跑底稿来测试一个编辑步骤

仍以Plus优先，Max只处理已经定位的重要困难，显式批准配置和费用。之前60元中尚余49.2408328元的口径来自本地最终账本；本次研究未消耗该预算，也未启动新的真实运行。

### 先验收落实 再读全文

- 模型是不是实际收到完整的文章级要求和对应材料
- 所谓连续作者有没有收到真实前文，还是仍只看章节职责
- 超限分批有没有保留任务与关键材料，是否另付了摘要/选择费用
- 最后交付的是全部BODY，还是一个被改名的章节文件
- 正文有没有把关键关系写透，重要条件在后文仍成立，跨章重复是否有新用途
- 各方案所有生成、修订、恢复与评价费用是否完整

不追求严格科研对照或零错误。全文亲读能清楚看到改进，并且输入/执行靠谱，才值得继续投入。单章可用于排查接口或局部失败，不能再次代替完整BODY的质量判断。

## 十 可追溯来源

以下是本轮实际读取的主要源文件。旧版实现与当前skill分别标明，研究性生成项目只取可用机制，不把其论文成绩当成OptoMind质量保证。

1. AgentWrite顺序续写和实际提示词，LongWriter `447539b356a8b09760b51eca876e19b6fc1f2dd7`：https://github.com/THUDM/LongWriter/blob/447539b356a8b09760b51eca876e19b6fc1f2dd7/agentwrite/write.py ；https://github.com/THUDM/LongWriter/blob/447539b356a8b09760b51eca876e19b6fc1f2dd7/agentwrite/prompts/write.txt
2. Re3分层上下文，`3a97ebde04e3333962c2825146897efe1dc87dd8`：https://github.com/yangkevin2/emnlp22-re3-story-generation/blob/3a97ebde04e3333962c2825146897efe1dc87dd8/story_generation/draft_module/beam_candidate.py
3. DOC上下文折叠，`9d727cdbae40c72169ab03b729bff4419a113dac`：https://github.com/yangkevin2/doc-story-generation/blob/9d727cdbae40c72169ab03b729bff4419a113dac/story_generation/draft_module/beam_candidate.py ；DOC v2生成与评分 `d092b42686bfc29ef2d610d5563e822ebf9d1451`：https://github.com/facebookresearch/doc-storygen-v2/blob/d092b42686bfc29ef2d610d5563e822ebf9d1451/storygen/story/story_writer.py
4. Dramatron生成续写与场景输入，`2e7c36afadacf8321b77a468940024371b7a8c7a`：https://github.com/google-deepmind/dramatron/blob/2e7c36afadacf8321b77a468940024371b7a8c7a/colab/dramatron.ipynb
5. WriteHERE真实累计文章与report writer，`817b489008e4b5dd50d5008803992dcf81c4fa50`：https://github.com/principia-ai/WriteHERE/blob/817b489008e4b5dd50d5008803992dcf81c4fa50/recursive/agent/agents/regular.py ；https://github.com/principia-ai/WriteHERE/blob/817b489008e4b5dd50d5008803992dcf81c4fa50/recursive/agent/prompts/report/writer.py
6. AutoSurvey小节生成与相邻编辑，`5e8f389f3d51b29bad16dc6ae75db3e8a45a3b65`：https://github.com/AutoSurveys/AutoSurvey/blob/5e8f389f3d51b29bad16dc6ae75db3e8a45a3b65/src/agents/writer.py ；https://github.com/AutoSurveys/AutoSurvey/blob/5e8f389f3d51b29bad16dc6ae75db3e8a45a3b65/src/prompt.py
7. GPT Researcher writer/publisher，`0957c301ed06c2a5857b834358c7227c739041d4`：https://github.com/assafelovic/gpt-researcher/blob/0957c301ed06c2a5857b834358c7227c739041d4/multi_agents/agents/writer.py ；https://github.com/assafelovic/gpt-researcher/blob/0957c301ed06c2a5857b834358c7227c739041d4/multi_agents/agents/publisher.py
8. Open Deep Research全文报告路径，`1b7d2e80db9faa586165c60e09096dbbfd483a64`：https://github.com/langchain-ai/open_deep_research/blob/1b7d2e80db9faa586165c60e09096dbbfd483a64/src/open_deep_research/deep_researcher.py
9. DeerFlow当前咨询skill `a4427625cfc0759021736cca51426bfc2759d6c8`：https://github.com/bytedance/deer-flow/blob/a4427625cfc0759021736cca51426bfc2759d6c8/skills/public/consulting-analysis/SKILL.md ；旧reporter `13a25112b1cb858aa56e2d77c385a28ff95f83ea`：https://github.com/bytedance/deer-flow/blob/13a25112b1cb858aa56e2d77c385a28ff95f83ea/src/graph/nodes.py
10. OpenScholar生成与反馈，`0e9b8fb912273d3dae39e593da86e4f6d3bf8de1`：https://github.com/AkariAsai/OpenScholar/blob/0e9b8fb912273d3dae39e593da86e4f6d3bf8de1/src/instructions.py ；https://github.com/AkariAsai/OpenScholar/blob/0e9b8fb912273d3dae39e593da86e4f6d3bf8de1/src/open_scholar.py
11. Anthropic doc-coauthoring，2026-10-07读取Stage 2/3：https://github.com/anthropics/skills/blob/main/skills/doc-coauthoring/SKILL.md
12. Self-Refine实际feedback/iterate，2026-10-07读取：https://github.com/madaan/self-refine/blob/main/src/responsegen/feedback.py ；https://github.com/madaan/self-refine/blob/main/src/responsegen/task_iterate.py
13. Google Technical Writing读者与文档规范：https://developers.google.com/tech-writing/one/audience ；https://developers.google.com/tech-writing/one/documents
14. Rust RFC正式写作模板：https://github.com/rust-lang/rfcs/blob/master/0000-template.md
15. 本轮OptoMind实际单章证据，锁定`80180d9c6fc975e9dfa3430adcfd3a89b361697a`：https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/80180d9c6fc975e9dfa3430adcfd3a89b361697a/docs/acceptance/writing-candidates-local-20261007

本报告中的中文职责段是新写的设计候选；并未安装或复制这些项目作为生产依赖。未来若引入源代码或原文skill，应逐项检查相应版本许可证，不能把所有公开仓库都视为同一种可自由移植的授权。
