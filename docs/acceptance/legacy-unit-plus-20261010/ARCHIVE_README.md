# 旧分单元作者 Plus：七章完整测试与断点恢复归档

**已完成七章29/29单元。恢复只新增8次qwen3.5-plus调用，原21个成功单元复用，正文字节哈希不变。实际规范化去重引用179篇；3张计划表/14行在场。**

生产源码锁定 **054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512**，仍为干净独立worktree。本包是测试和恢复归档，不是算法更新；归档SHA与受测源码分开。未调用Max、未生成GUIDE、未重跑上游、未根据人工科学意见改稿。

## 优先阅读最终结果

1. [用户本次恢复原文](USER_RESUME_INSTRUCTION.md)与[原完整测试指令](ORIGINAL_USER_REQUEST.md)：区分用户方案、执行范围与本地落实。
2. [根智能体七章完整亲审](ROOT_REVIEW_FULL.md)：具体优点、退步、任务遗漏、输入冲突，以及173篇Flash/117篇Plus稿对照。
3. [完整句柄稿](assembled/REVIEW_DRAFT_HANDLES.md)、[读者编号稿](assembled/REVIEW_DRAFT.md)、[正式运行报告](RUN_REPORT.json)。29单元生成/装配完整，但四组诊断保留，状态仍为restricted_draft；没有将完成标记当科学质量通过。
4. [恢复最终审计](RECOVERY_FINAL_AUDIT.md)与[机器证据](RECOVERY_FINAL_AUDIT.json)：21正文哈希、8实际消息/参数/返回、结果封印、引用、表格、进程与源码。
5. [全稿引用审计](RECOVERY_CITATION_AUDIT.md)/[JSON](RECOVERY_CITATION_AUDIT.json)：179规范化身份、221目录身份及42未引用，不拿P编号数量冒充论文身份数。
6. [3.5漏任务实证](ROOT_TASK_CONSUMPTION_EXAMPLE.json)、[7.1漏任务实证](ROOT_TASK_CONSUMPTION_CH7.json)、[第6章表格分母实证](ROOT_TABLE_DENOMINATOR_CH6.json)、[TACITO输入身份冲突](ROOT_INPUT_IDENTITY_CONFLICT.json)。建议从实际请求到正文逐项核对。
7. [第六章超时原因](TIMEOUT_ROOT_CAUSE.md)、[恢复包装器差异](WRAPPER_DIFF.md)、[包装代码](recovery_wrapper.py)、[免费预算控制验证](RECOVERY_FREE_GUARD_CHECK.md)、[用户豁免记录](BUDGET_WAIVER.json)。

## 本轮实际链路

完整FULL_BODY_INPUT → 正式legacy_unit_writer → 逐单元qwen3.5-plus → 生产装配。7章/29单元/92段落任务/3表格任务保留。作者读取自身完整任务/材料、章节框架、兄弟单元与其他章节职责，**不读实际前文、旧正文或GUIDE**。

参数始终为thinking=true/8192、回答32768、实际max_completion_tokens40960、stream=true、读取超时1800秒/整体3600秒、max_retries=0。Qwen直连。最初Ch6_U1 HTTP200后只有646个推理SSE事件，0正文，随后1800秒无新数据超时。保持输入和参数的显式重试成功，没有用容量缩减或提示词变更规避。

恢复命令为本地`python -X utf8 recovery_wrapper.py --run`，包装器调用原CLI的`--run --retry-failed`。Ch6_U1原失败attempt_001及其推理流保留，新成功为attempt_002；其他七个未启动单元首次执行。旧21成功attempt从未重跑。

本轮原独立30元账本继续使用。恢复新增已结算1.5555488元，总已结算3.4918048元。旧未知1.639640元按用户指令不计预算，保留user_waived原金额/实际费用NULL；没有伪造已结算0元，没有新未决占额。账本数据库只在本地；公开记录提供审计导出。

## 内容判断

覆盖与机制解释值得保留：条件、阴性案例、DC/屏障关系及部分LBP临床细节具体，行文少些章节预告。**仍非全面更好**：3.5只展开三任务中的第一项，7.1只展开四任务中的第一项；任务已完整送达且正常stop。重复JCOG/FMT、部分验证实验未展开、表格总体/亚组分母不清和已有材料身份冲突未消解。完整亲审给出原消息/返回对照，不用引用数判胜负。

综述转述原始研究凭可用内容和明确身份照常参与，不要求自身A/B或全文；一张充分的表可以承担多个任务。

## 历史停点与版本选择

[首次21单元归档](https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/ff78d5ae3f85a6af305ebf76e4cfb1c37f6f14aa/docs/acceptance/legacy-unit-plus-20261010)不可变，保留原超时现场与部分稿。

包内`ROOT_REVIEW.md`、`ROOT_READING_NOTES.md`、`FINAL_RUN_STATUS.md`、`LIVE_AUDIT.*`、`CITATION_AUDIT.json`、`TABLE_TASK_AUDIT.json`、`LEDGER_SUMMARY.json`是**首次21单元停点**。其中132篇只属于前五章，不能作为全稿引用统计。当前最终状态以RECOVERY系列、ROOT_REVIEW_FULL、RUN_REPORT和assembled为准。

受测源码不是归档提交：生产源代码固定054f94a3；本次提交仅补传已有生成结果和评阅记录。旧173/117稿来源、版本和哈希见ROOT_COMPARISON_PROVENANCE.json；只用于事后比较。

## 请求、原件与恢复入口

`units/<章>/<单元>/<缓存哈希>/`保留实际UNIT_MESSAGES，`attempt_00N/`保存请求、原始返回、正文、结果和seal。大于600KB的JSON以UTF-8分片保存，按`*.parts.json`顺序拼接并核对SHA-256；PUBLIC_FILE_INDEX列出原件哈希与公开路径。

原始FULL_BODY_INPUT不重复上传：[75分片索引](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/f5103e99c624951ab549a90b443ab090876ca301/docs/acceptance/guide-content-units-20261010/input/FULL_BODY_INPUT.json.parts.json)，拼接SHA-256为`6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7`。STARTUP_RECORD保留原启动命令，USER_RESUME_INSTRUCTION与recovery_wrapper保留恢复步骤。

排除密钥、SQLite数据库、tokenizer原文件及未经授权论文全文。旧21单元不重复补传；恢复8单元另附UNIT_INPUT与成功SSE捕获，保留实际材料投影、请求与原返回。恢复前文本备份也保留，数据库备份不上传。详细边界见OMITTED_AND_RESTORE。没有将全部outputs复制进仓库。

## 交给云端独立复读

请先核对本地是否忠实恢复旧单元机制及比较口径，再读七章内容。建议优先判断两处正常结束漏任务的消费/结束判定接缝，同时分析身份冲突及重复证据用途。保住已有机制和反例，可自行决定后续修法；本报告中的具体科学例子仅为验收证据，不作为生产纠错答案。云端无需重新调用模型。
