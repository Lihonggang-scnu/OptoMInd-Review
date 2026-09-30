# 全稿汇编运行报告

- 状态：`complete`
- 预计单元数：23；已载入：23；缺少：0
- 实际使用论文数：176；未使用论文数：5
- 全文统一表号：10 张
- 未能映射的原始引用 handle：无

## 单元状态

| 章节 | 单元 | 状态 | 正文字符数 | 结果路径 |
| --- | --- | --- | ---: | --- |
| CH01 | CH01_U01 | written | 1280 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH01_U01\CH01_CH01_U01\UNIT_RESULT.json` |
| CH01 | CH01_U02 | written | 1512 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH01_U02\CH01_CH01_U02\UNIT_RESULT.json` |
| CH01 | CH01_U03 | written | 1578 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH01_U03\CH01_CH01_U03\UNIT_RESULT.json` |
| CH02 | CH02_U01 | written | 3217 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH02_U01\CH02_CH02_U01\UNIT_RESULT.json` |
| CH02 | CH02_U02 | written | 2544 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH02_U02\CH02_CH02_U02\UNIT_RESULT.json` |
| CH02 | CH02_U03 | reused | 2857 | `F:\OptoMind-Review-2\outputs\unit_writing\20260927_astra_plus_trial\CH02_CH02_U03\UNIT_RESULT.json` |
| CH03 | CH03_U01 | written | 1798 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH03_U01\CH03_CH03_U01\UNIT_RESULT.json` |
| CH03 | CH03_U02 | written | 1857 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH03_U02\CH03_CH03_U02\UNIT_RESULT.json` |
| CH03 | CH03_U03 | written | 1818 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH03_U03\CH03_CH03_U03\UNIT_RESULT.json` |
| CH03 | CH03_U04 | written | 2723 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH03_U04\CH03_CH03_U04\UNIT_RESULT.json` |
| CH04 | CH04_U01 | written | 1228 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH04_U01\CH04_CH04_U01\UNIT_RESULT.json` |
| CH04 | CH04_U02 | written | 2141 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH04_U02\CH04_CH04_U02\UNIT_RESULT.json` |
| CH04 | CH04_U03 | written | 1479 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH04_U03\CH04_CH04_U03\UNIT_RESULT.json` |
| CH04 | CH04_U04 | written | 1701 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH04_U04\CH04_CH04_U04\UNIT_RESULT.json` |
| CH05 | CH05_U01 | reused | 1506 | `F:\OptoMind-Review-2\outputs\unit_writing\20260927_astra_trial\CH05_CH05_U01\UNIT_RESULT.json` |
| CH05 | CH05_U02 | written | 1318 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH05_U02\CH05_CH05_U02\UNIT_RESULT.json` |
| CH05 | CH05_U03 | written | 1979 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH05_U03\CH05_CH05_U03\UNIT_RESULT.json` |
| CH05 | CH05_U04 | written | 1389 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH05_U04\CH05_CH05_U04\UNIT_RESULT.json` |
| CH05 | CH05_U05 | written | 1035 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH05_U05\CH05_CH05_U05\UNIT_RESULT.json` |
| CH06 | CH06_U01 | written | 2646 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH06_U01\CH06_CH06_U01\UNIT_RESULT.json` |
| CH06 | CH06_U02 | written | 1652 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH06_U02\CH06_CH06_U02\UNIT_RESULT.json` |
| CH06 | CH06_U03 | written | 2240 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH06_U03\CH06_CH06_U03\UNIT_RESULT.json` |
| CH06 | CH06_U04 | written | 1946 | `F:\OptoMind-Review-2\outputs\full_review_draft\20260927_run01\units\CH06_U04\CH06_CH06_U04\UNIT_RESULT.json` |

摘要与结语由主代理结合六章主线及正文抽读补写；这不是逐条科学事实核查。

## 本轮真实写作与主代理检查

- 新写21单元，复用2单元；新增调用全部使用Qwen3.5-Plus直连、最多3并发，均正常stop结束，无重试、无截断。
- 新增实际输入702,052 tokens，接口返回completion 75,894 tokens（包含思考等接口计量部分，不等同于正文长度）。
- 模型批次约488秒（8.1分钟），不包含准备、汇编和人工检查时间；单次约41–84秒。
- 新增已结算0.9259328元，没有新增未确定请求；共享上限150元，累计结算45.5108046元，历史reserved 2.7541948元、uncertain 3.271762元仍保留，可用98.4632386元。
- 总稿包括摘要、六章23小节、10表、结语和176条参考文献。正文含约30,042汉字（不含文末参考文献，非中文字符另计）。
- 已安排但本轮未用的5个handle：P0026、P0271、P0298、P0370、P0076；未为凑数硬塞引用。
- Astra检查了全篇层级与引用编号，并亲读引言、代谢机制、宿主背景、展望等部分，保留前轮已读的两份试写。已处理明确的语言噪声、任务式章首提示、长标题与表号；未声称逐篇核对全部事实。
- 可作为后续工作的完整初稿。仍有部分长段、章节间案例重复、上游继承的偏强概括；本轮没有重开规划/检索/精读，也没有进行大规模全篇重写。
- 参考文献使用本地已有身份字段；有题名、年份与DOI/其他可用身份。缺失的作者或期刊信息未编造，尚非统一期刊投稿格式。

## 后续使用

优先阅读 REVIEW_DRAFT.md；程序继续加工可用 REVIEW_DRAFT_HANDLES.md 和 REFERENCES.json；单元原始正文及实际模型响应在 units/ 与两份复用路径保留。标题和表题调整保存在 TITLE_OVERRIDES.json 与 TABLE_TITLES.json，重汇编无需再次付费。
