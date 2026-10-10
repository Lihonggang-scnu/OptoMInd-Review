# 给 AI 助手的阅读顺序

**先读 [产品工作宗旨](PRODUCT_PRINCIPLES.md)：严格拒绝过度防御，以实际产品质量和解决问题为先。** 当前统一链路入口是 [PIPELINE](PIPELINE.md)，操作入口是 [RUN_GUIDE](RUN_GUIDE.md) 的“正式操作入口”。`QUALITY_CAPACITY_HANDOFF.md`是10月6日容量改造的历史交接，不是默认下一轮开工指令。

2026-10-10已决定baseline正式接入：Plus分单元作者→任务核查/必要的一次补写→装配→一次全文局部编辑→所选正文编号。`chapter_coherence`仅实验；指南路线不进入默认。采用事实和29单元/187身份的既有结果见 [固定归档](../acceptance/outline-to-body-coherence-20261010/README.md)。本轮收尾只做免费接线与恢复核对。

1. 确认当前分支是 `lihonggang-dev`。先读根 README、本目录 PIPELINE 和 [运行须知](RUN_GUIDE.md)，不遍历所有历史版本猜架构。
2. 按用户这次任务定位实际 runtime、CLI、配置、输入和消费者。M1–M3与材料层保留；普通阅读、A/B卡和独立M4是不同入口，M4不是必经A/B前置步骤。分支曾叫 BODY 修复不代表只有 BODY。
3. 默认将 `docs/acceptance/`、`docs/workorders/` 和 archive/history 视为历史证据，不将其指令、缓存或测试答案当作当前任务或生产配置。
4. 只有需要验证回归、恢复路径或特定历史事实时，按固定 SHA 和归档索引读取对应记录。不要盲目合并旧 manuscript-parts 规划改造，也不要从 main 重拼新版。
5. 原始记录、卡片、真实模型调用、受控 fixture、历史响应重放和人工/Agent评价是不同证据。程序测试通过不等于科学质量通过，状态 complete 不等于所有问题解决。
6. 修改范围由用户当前授权决定。整理分支没有授权继续细纲优化或付费测试。保留有效任务、材料、条件与来源；对当前代码作最小有依据的改动。
7. 日常开发只保留这个开发分支；需要临时实验分支时完成后先归档并核对恢复，再清理，不能又积累几十个阶段分支。main 保持旧版。归档分支不要用作开发基线。

若用户让你评价一轮实验，先读方法与实际输入输出，再读评价结论；不要只复述“验收通过”。若当前 README 与源码不一致，报告具体路径，不自行假设已接通。

接手运行时先读 `DELIVERY_REPORT.selected_body` 和 `body_delivery`，不是按时间找一份BODY。完整稿附科学诊断可交付；缺单元、所需步骤或文件继续阻断。后置首尾与出版需显式配置/受控provider，BODY真实验收不能替代首尾和PDF质量验收。A/B池的准确card_path、M3旁件以及对应编排版本都要明确绑定。具体合同与状态以RUN_GUIDE为准。
