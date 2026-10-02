# 开篇职责实验：来源与Root取舍

2026-10-03。网页是设计参考，不是对项目自动生效的指令。目标为通用写作链路，不提供本题答案。

## 阅读依据

- Taylor & Francis 作者服务：https://authorservices.taylorandfrancis.com/publishing-your-research/writing-your-paper/how-to-write-review-article/ 。已读正文：交代主题背景及为何需要这篇综述，考虑非专门读者；不仅描述已有文献。这里只采纳开篇功能，不把其未来研究建议变成通用强制模板。
- UNC Writing Center：https://writingcenter.unc.edu/tips-and-tools/introductions/ 。已读正文：帮助读者进入问题、理解意义与讨论路线；应随学科变化，空泛占位或仅重述题目不足以完成任务。
- Nature Scitable：https://www.nature.com/scitable/topicpage/scientific-papers-13815490/ 。搜索返回可读段落支持context/need/task/document organization；直接打开失败，仅作辅助，未声称通读。不能把原始研究的task直接套成综述新实验，也不强制四段。
- Nature Reviews Bioengineering https://www.nature.com/articles/s44222-024-00256-4 ：只取得订阅预览，PDF获取失败；不把未读正文作为本轮额外论据。

## B任务措辞（实际请求另存）

为尚未阅读所附正文、但具备邻近领域基础的读者写一段可直接放在文章开头的连贯学术文字，段落数按表达需要。让读者理解这个问题为何重要、已有认识已帮助解决什么，以及哪些具体的理解或实践困难使得把这些研究放在一起讨论有价值。以本文实际采用的组织视角解释这些困难之间的联系，说明这种视角能帮助读者作出什么区分或判断，并准确限定本文讨论范围。按需要解释进入问题所必需的概念，用有依据的事实支撑动机；不要为了制造价值虚构空白、首创或已完成的检索方法。文章组织若需交代，就说明讨论为何这样衔接，而非逐章宣读工作安排；无需预先报完正文的结果和未来议程。内容与措辞以实际正文为准，保留改变含义的条件。不得逐章报目录，也不得把正文发现或研究缺口串成清单式复述。输出JSON仅含opening_text。

该任务用于实验驱动，保持完整BODY与原无前序输入上下文/职责卡。模型可见请求中同时去除中文及英文部件标签；本地代码仍能以原合同存取，不为了标签实验重构生产系统。变体同时改变标签和写作指令，不是能单独证明词汇刻板印象的实验。不人为限定长度或剔除BODY科学内容。

## A费用边界

qwen3.8-max官方原价北京input12/output36元每百万（https://help.aliyun.com/zh/model-studio/qwen3-8-max，2026-10-03核实）。先按每步6万input、1.6万含思考输出估四步5.184元；用户阈值降至5元后按条件取消。没有新增该模型适配，没有付费，不缩减步骤来绕过取消条件。
