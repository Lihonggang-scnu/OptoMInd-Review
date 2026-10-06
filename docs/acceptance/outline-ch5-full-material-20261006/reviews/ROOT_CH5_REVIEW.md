# Ch5 首章真实加强：根审阅

## 决定
暂停下一章付费，不把 updated 状态视为加强成功。原始细纲及新返回分别保留，不覆盖生产原件。

## 实际运行
模型 qwen3.8-max；输入331422 tokens，输出15163 tokens（其中reasoning7056）。实际生效配置32768思考、65536回答，max_completion_tokens98304；finish_reason=stop。账本按原价用量结算4.522932元，本轮新增40元已累计7.401266元，未新增未决占额时剩32.598734元。

## 内容对照
五个单元的15条paragraph_briefs字典全部完全相同，包括point、development、source_handles及任务身份。五单元substantive_point、ordered_development、case_groups、synthesis、transition也未实质改变。

实质差异是U1 supporting_studies删除P0585（JCOG2007辅助生物标志物研究）；这有助于收紧干预试验章节范围，但没有转化为新的分析任务。其他已观察变化主要是引号格式；不能记作组织提升。

原有初治、耐药、菌株制剂、安全性与转化职责仍保留，没有发现整项职责删除。但原计划内部已有的不一致并未因本次加强消失，例如同一MRx0518任务的case描述2名RCC加1名NSCLC获得PR，paragraph development仍写2/12；这里只作为根审阅观察，不作为模型下一轮输入答案。

## 已核对
RESULT.messages与已审FULL_MAX_MESSAGES逐项相同。返回实际模型为Max且正常结束；本次并非输出长度截断。完整raw保留，接下来只做离线核实请求、解析和保存链，避免把程序复用或后处理行为误当模型表现。

## 判断边界
此次整章直接完整材料路径没有得到值得按原方案铺开的收益；这不推翻先前按需Max两单元的实质收益，也不足以将原因全部归为输入长度或整章粒度。下一步优先查明确执行差异，禁止自动重试或人工提供本题科学修订清单。
