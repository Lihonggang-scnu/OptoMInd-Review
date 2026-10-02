# 后置首尾提示词与输入实验资料包（2026-10-03）

这是历史实验与候选代码快照，不自动作为lihonggang生产默认或新的BODY规划输入。没有修改正文算法。参考代码基线dc661cf47bdb8ec70a348c02bc8f15a0b40253f4（lihonggang-serial-parts）；source_snapshot保存真实本地未提交改动后的源码、脚本和测试，不把它冒称一个已提交的生产版本。

## 先读这五项

1. extra_trials/ROOT_REVIEW.md：两个追加实验决定、最新开篇质量与费用。A Max组按新5元阈值取消、零调用；B真实一次生成。
2. extra_trials/B_neutral_opening/OPENING_TEXT.md：无“引言”标签、直接职责指令的真实新开篇；MESSAGES.json是实际完整请求。
3. focused_trial/ROOT_FINAL_REVIEW.md 与 ROOT_STAGE_REVIEW.md：先前六次真实小测，full/none均未整体通过，不能只看新结果报喜。
4. POST_BODY_PARTS_FOCUSED_RESEARCH.md：云端调研原报告；preparation/PROMPTS_FOCUSED.md为采纳后的真实通用指令。
5. source_snapshot/：对应源码与两个实验入口，LOCAL_CHANGES_FROM_DC661.patch为已跟踪文件差异，新脚本直接附完整源码。

## 运行与比较关系

同一固定BODY（5章20单元，实际引用120篇）与CONTEXT完整附于fixed_inputs。更早四步基线在同仓库advisor/lihonggang_20261003/serial_parts_live_baseline，锁定bb349c1149e6bccc0b1c14c6bcb5ae25fb217f6a。

focused_trial：一次新构思+一次结语，两组各生成引言/摘要。attempt-004是full最终稿，attempt-006是none最终稿；它们共用卡片/结语。每个attempt都保留实际messages、responses、生成部件和报告，cache/raw_responses保存真实返回以便区分复用与付费。

extra_trials：A_CANCELLED.json保留qwen3.8-max四步预算估算5.184元与按用户5元门槛取消的记录，尚无Max内容结果。B_neutral_opening复用focused none请求的正文/上下文/卡片，不提供前序成文；仅更换标签与写作任务，明确禁止目录化与清单式复述，真实调用qwen3.5-plus思考一次。联合变化不能证明效果只由名称引起。

B的MIXED_COMPOSITION_MANUSCRIPT.md仅插入本次开篇，其他首尾未重新生成；MIXED_COMPOSITION_OFFLINE.md是明确标注的固定离线样例。二者不可混同。所有原始模型结果未人工改稿。

## 费用与状态

更早4次0.2085536元，focused6次0.3225608元，本次B1次0.0550624元，累计0.5861768元，无未结算。均为API token按配置价格核算，不是云账单折扣核对；30元账本未重置。B的开篇有实质局部改善，摘要和结语没有随之验证改善，未冻结生产默认、未宣称投稿质量通过。

## 后续云端调研任务

请比较真实输入与文本，判断可迁回通用链路的有效职责要求，检查卡片/提纲中是否仍暗示逐章汇报。重点是让模型按读者任务组织知识，不按禁词凑表面合格。若建议新实验，少量、单独变化且说明收益。不要把本题的科学观点、章节顺序或结果写入生产提示词，不改BODY。

## 附件边界

附消息、响应、模型配置、完整生成文本、原始研究报告、费用快照与源码。未附API密钥、整个工作区、数据库、论文PDF或原始论文全文。正文为本项目生成的综述稿。既有源论文A/B和身份资料可从上一批archive索引查阅，fixed_inputs含本次实际身份目录。绝对本地路径仅为运行来源，不是云端有效路径；以本包相对文件为准。
