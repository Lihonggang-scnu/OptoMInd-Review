# 当前BODY→独立首尾→最终编号→出版实测

先读ROOT_REVIEW.md，再看GENERATED_PARTS_READABLE.md与05_publication/tex/main.pdf。正文保持，首尾是真实模型生成，未人工改写。

## 结果与入口

- 首尾阅读副本：GENERATED_PARTS_READABLE.md。
- 原始首尾完整稿：03_front_back/attempts/attempt_002/MANUSCRIPT_FINAL.md。
- 免费恢复相同稿：03_front_back/attempts/attempt_003/MANUSCRIPT_FINAL.md。
- 最终数字稿及唯一映射：04_figures_citations/MANUSCRIPT_READER.md、REFERENCES.json。
- 出版Markdown：05_publication/MANUSCRIPT_PUBLISHED.md；TeX与49页PDF：05_publication/tex/。
- 全部首尾模型请求/RAW/传输/解析：03_front_back/cache/；原格式错误与独立格式修复均保留。
- 生成与恢复报告：GENERATION_REPORT.json、REPLAY_REPORT.json、REPLAY_VERIFICATION.json。
- 输入、引用身份和PDF检查：ROOT_INPUT_AUDIT.json、ROOT_NUMBERING_AUDIT.json、PDF_VISUAL_ACCEPTANCE.md。
- 启动、续跑、源稿与配置：LIVE_STARTUP.json、FORMAT_REPAIR_STARTUP.json、SOURCE_INPUTS.json、INPUT_CONTEXT.json。最终源码和哈希见SOURCE_RECEIPT.json。
- 最终编号/出版驱动：run_final_delivery.py，直接调用既有04/05生产函数，不含模型或论文特定修订。

## 费用与结论

qwen3.5-plus：4逻辑阶段＋1次职责卡结构修复，实际新增0.356588元；同目录恢复0新增调用。原项目累计17.9225288元，剩余42.0774712元，未决0；双账本和原累计封顶保持。

题名可用，摘要具备初稿价值；引言第二段目录化、摘要/结语重复和语义强化仍需要通用链路改进。本轮不再付费重抽。BODY保持7章29单元、187个去重论文身份。没有手工科学问题清单、旧首尾答案或整池B卡输入。

## 运行与恢复

正式首尾CLI：scripts/upgrade3/manuscript_parts.py --live；默认免费预览，加--run才调用。初次预览固定项目增量预算基点；恢复继续同目录、同输入、同账本，不能换目录重新起算。原子装配在新attempt目录，实际模型响应逐阶段持久缓存。--repair-invalid显式允许每阶段至多一次结构格式修复，不自动重购失败请求。

跨机器按LIVE_STARTUP中参数映射本地路径；凭证仅本地安全文件读取。详细占位符命令在源码docs/POST_BODY_MANUSCRIPT_PARTS.md。账本数据库与密钥不属于可公开归档内容。
