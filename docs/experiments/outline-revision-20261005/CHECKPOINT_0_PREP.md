# 细纲真实三臂实验：停点 0 准备 checkpoint

状态：只读发现与离线准备；尚未发起新的 Qwen 调用，尚未改变旧源码、主分支或旧产物。

## 实验簇

固定真实 BODY40 资产中的 Ch1 两个相邻且有组织关系的单元：

- `Ch1 / U1_跨癌种信号`：跨癌种观察性信号、队列异质性与治疗方案依赖；3 个 paragraph briefs。
- `Ch1 / U2_外部药物暴露`：抗生素/PPI 的时间窗、联合治疗方案对微生物效应的修饰/反转、指示偏倚校正；3 个 paragraph briefs。

选择理由：U1 的第三段与 U2 的第二段都使用 JCOG2007 `P0585`，但职责不同。U1 应回答“治疗方案如何改变关联方向/强度”，U2 应回答“外部暴露与联合方案怎样进入混杂/修饰校正”。这是可检验的任务分工问题，且保留了合理复用案例；不是只替换 BODY 句子。旧 BODY 的 `B0012/B0017` 位于同一关系附近，但本轮以真实计划单元 ID 为准，不把旧块号冒充细纲 ID。

## 三臂

1. `control_original`：原始 Ch1 计划的 U1/U2 副本，保留原任务、source handles、顺序和职责。
2. `R1_strong_once`：同一真实材料下，由一次 `qwen3.8-max` 细纲修订请求返回完整 U1/U2 任务对象、诊断与差异理由。
3. `R2_review_then_owner`：先用同款 `qwen3.8-max` 做独立审稿（只返回问题、依据、影响范围和补料需求，不写答案），再把该意见交给现有 `affected_chapter_revision`/章节 owner 合同，由同款 `qwen3.8-max` 返回完整 U1/U2 修订计划及采纳、拒绝、暂缓解释。owner 不是旧 post-BODY author 的改名；调用使用当前章节修订 prompt/合同和章节 owner 输入形状。

三臂随后共用同一离线 arrangement 映射、同一 `review_unit_writer` 参数和同一 writer 模型，分别写 U1/U2 局部正文。控制臂也实际写，避免把旧历史正文当公平控制。只读取/重写这两个单元，不重跑全 BODY。

## 真实材料与固定入口

- 原细纲：`<LOCAL_REVIEW2_PATH>
- 真实 BODY：`<LOCAL_REVIEW2_PATH>
- Ch1 writer packet：`<LOCAL_REVIEW2_PATH>
- Ch1 arrangement：`<LOCAL_REVIEW2_PATH>
- 旧实验材料包（只读复用 generated summary/身份字段）：`<LOCAL_REVIEW2_PATH>
- 现有 Qwen 直连和 owner/writer 代码快照：`<LOCAL_REVIEW2_PATH>
- 共享 10 元 SQLite 账本（沿用，不创建新钱包、不释放旧 uncertain）：`<LOCAL_REVIEW2_PATH>

当前只读账本快照：`settled=0.2785574` CNY、`uncertain/held=0.2137224` CNY、`reserved=0`；因此扣除现有结算和占额后的读数为 `9.5077202` CNY。实时账本是唯一费用依据。

## 材料投影

两单元直接 paragraph handles 为 `P0096,P0146,P0585,P0478,P0081,P0402`。两个单元的 supporting studies 共 25 个唯一 handle；候选请求将携带这 25 个材料的现有 generated summary/content fields、标题/年份/DOI/身份和贡献用途，同时携带原始 U1/U2 全部任务、Ch1 论断/章节职责、必要邻近职责、BODY 对应局部和全局章节映射。未把整本 536 材料池灌入请求，也不把题目特例答案写进 prompt。

材料内容约 151,313 个字符（25 个 supporting study 摘要；重复的 `text`/`content_fields` 只保留一份），加计划、BODY 局部与通用合同后预计约 45–55k input tokens/规划请求。三次规划请求输入相同；R2 的审稿意见另作为 owner 后续输入，不携带审稿答案以外的 hidden judgment。

## 费用预估与停止边界

保守预留：三次 `qwen3.8-max` 规划（R1、R2 reviewer、R2 owner）约 `2.4–2.9` CNY；三次同参数 qwen3.7-flash 局部 writer 约 `0.15–0.30` CNY；必要的两个匿名 oldswap 复评请求约 `0.05–0.15` CNY。合计拟预留 `<=3.6` CNY，实际以同一 SQLite 账本结算为准；任何单次预留失败、未知结算或总账本可用额不足立即停止并保留记录。

新调用必须先在该旧账本 `reserve`，请求完成后 `settle`；transport unknown 继续持有，不释放、不抵扣为零。root 审查本 checkpoint 前不付费。首个付费顺序建议为 R1 规划，成功且接缝通过后再运行 R2 reviewer/owner；不为 R1/R2 预设胜者。

## 首个离线验证

付费前运行 `driver --offline` 的最小接缝检查：确认真实计划中恰为 Ch1 两单元、6 个 paragraph briefs、25 个 supporting handles；每个候选副本保持原 source identity 与 unit IDs；R1/R2 请求材料投影相同；R2 reviewer 输出不能被当成 owner 答案；三个 arrangement 输入可生成同一 unit-to-task 映射；writer 输入只包含两个单元且引用 handles 有材料覆盖；控制、R1、R2 的 writer model/temperature/thinking/output 参数逐字相同。检查只写新实验目录，不读取或复制 key。

