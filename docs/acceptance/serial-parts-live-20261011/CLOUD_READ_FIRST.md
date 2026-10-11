# 云端背景对齐：当前BODY后的真实首尾与出版

本页汇总2026-10-11本地真实运行及2026-10-12下一步研究需求。请先阅读实际输入输出，再判断原有评价；本次上传没有重新生成论文。

## 当前实现与版本

- BODY正式采用baseline：Plus分单元作者→任务核查/必要的一次有界补写→装配→一次全文局部编辑→选择实际成稿。采用来源见[上一轮固定记录](../outline-to-body-coherence-20261010/README.md)。
- 完整规划CLI默认开启后置细纲加强及相关无人值守交接修复：`7a18d0e904db8b81f3d64e3a5f173a2301456bd8`。本地此前尚未推送，本次一并上传。
- 后置首尾真实客户端及出版投影修复：`508cf724e5c6896fa1994d2bb7c83214ffe82959`。主要源码为`serial_parts_live.py`、`scripts/upgrade3/manuscript_parts.py`和`latex_publication_renderer.py`。正文规划及写作能力没有在本次首尾试跑中改造。
- 真实流程为实际BODY→重新构思职责卡→结语→引言→摘要/关键词/题名→装配→图表/引用编号→出版。独立首尾CLI支持`--live`；预算、响应保存与恢复已接通。04/05由本包`run_final_delivery.py`调用既有生产函数。不要把这个实跑驱动写成一个所有上游均自动运行的总CLI。
- 运行开始时是`7a18d0e...`加工作区改动；启动文件保留当时diff哈希，补丁保留修改，完成后的源码锁定为`508cf724...`。不要把所有物理请求直接标成干净的`7a18d0e`提交执行。

## 建议先读

1. [实际首尾文本](GENERATED_PARTS_READABLE.md)，再读[本地亲审](ROOT_REVIEW.md)。题名可用；引言无引用且后一段偏组织预告；摘要和结语重复较多，部分压缩强化了原判断。请独立复读，不以这些评价代替看稿。
2. [完整出版Markdown](05_publication/MANUSCRIPT_PUBLISHED.md)和[49页PDF](05_publication/tex/main.pdf)。BODY为已认可7章29单元、187个稳定身份去重来源；本轮没有重写BODY。
3. [首尾之前的实际选中BODY](input/SELECTED_BODY_HANDLES.md)、[对应引用目录](input/SELECTED_BODY_REFERENCES.json)、[原交付报告](input/BODY_DELIVERY_REPORT.json)、[实际全局输入](INPUT_CONTEXT.json)。`SOURCE_INPUTS.json`记录原运行来源与哈希。
4. `03_front_back/cache/`中每阶段的实际REQUEST、RAW_RESPONSE及传输文件；`03_front_back/attempts/`保留失败、生成和恢复结果。`FINAL_DEPENDENCY_AUDIT.json`核对各阶段全文、职责卡及前序部件的真实传递。
5. [生成报告](GENERATION_REPORT.json)、[恢复核对](REPLAY_VERIFICATION.json)、[源码回执](SOURCE_RECEIPT.json)、[PDF视觉记录](PDF_VISUAL_ACCEPTANCE.md)。原回执中的`pushed:false`是创建时状态，原文件保持不变；本页所属归档提交包含源码及资料的正式上传。

实际使用`qwen3.5-plus`，思考16384、回答空间32768，实际总输出上限49152，流式直连。4个逻辑阶段＋1次职责卡结构修复，共0.356588元；恢复新增调用0，未决0。模型返回的boundary字符串通过一次明确结构修复改成数组，原失败响应保留，没有人工科学答案输入。

## 用户的新方向与本地落实

用户提出：先让云端跨领域阅读几十篇高水平综述，研究引言、摘要、结语的真实写法，特别是引言引用；在研究完成后再讨论首尾改造。另外增加阅读全文的成稿校验员，自主发现几类问题，返回逐字旧/新片段JSON，由程序替换，失败反馈最多两轮。

用户没有要求把当前稿手工改好，也没有决定固定三段引言或统一跨领域模板。上述方向来自用户；五类检查范围、草案提示词及接缝建议是本地Agent的具体设计，仍可调整。

本地已经完成[全文盘点与研究/校验准备](../manuscript-checker-preparation-20261012/README.md)，**尚未实现或运行新校验员，也未修改首尾生产写作提示词**。跨领域新调研尚未开始。云端下一步适合先对齐这些事实，然后开展该研究。

本包复用原记录、请求、返回和产物；未包含密钥或账本数据库。省略重复初版PDF渲染及TeX中间文件，保留最终PDF、TeX、联系图和实际响应。绝对本地路径保留作原始运行证据；跨机器执行需映射到本机路径，不能把本地路径当云端已存在文件。
