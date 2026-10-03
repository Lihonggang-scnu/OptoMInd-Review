请接手本地真实验收发现的两处本地供材损失。先阅读，后修复，不进入04–07。

归档分支：
https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/body03-local-lookup-real-acceptance-20261003

依次阅读此分支的 `docs/acceptance/body03-local-lookup-real-20261003/README.md`、`ASTRA_ACCEPTANCE.md`、`NEXT_REPAIR.md`、四次模型RESULT与对应REQUEST_METADATA，以及既有 `docs/acceptance/body03-20261003/records/local_lookup_repair.md`。REQUEST_METADATA是删除论文原文后的衍生记录，不是完整原始请求；先区分实测事实、只读归因和建议。

受测生产代码为 `2df2a2719b17b05a9ccdfc1574b0649c7bacf586`。请从归档分支创建独立修复分支，保留归档。不合并，不修改main/review-v2/lihonggang及原修复分支。

本地4次真实qwen3.7-flash调用共0.01428元：指定身份和checkpoint确实改善，但无指定论文时，read_focus让原问题中的相关论文退出窗口；指定论文时读到错误章节，已有A/B认识和上下文也未送入。不要把这些归为“LLM不够聪明”，也不要仅提高数量或报测试全绿。

严格按NEXT_REPAIR分两项完成：①原问题候选作为主锚，read_focus只补充；②指定论文提供有界的相关正文片段、已有A/B认识及该论文上下文，并定位原始FTS对点段为何被后续选段丢掉。维持一次扩大和现有输入预算；保留身份、partial、失败尝试、缓存修复。保持普通首轮阅读默认路径、BODY提示词/执行顺序、案例、编排与writer不变。禁止按具体论文、学科或答案硬编码。

每项先写失败反例，再修改，再检查实际发送材料与交付内容；增加另一学科同型用例。云端不接密钥，不真实模型调用，不下载论文，不跑全稿。无法访问本地数据库的限制明确记录。留下分项records、实际消息前后对照、SHA和命令，提交推送独立分支，停在READY_FOR_ASTRA_03_LOCAL_LOOKUP_R2，交本地Astra用同问题真实小测。不要自行宣称科学质量通过。
