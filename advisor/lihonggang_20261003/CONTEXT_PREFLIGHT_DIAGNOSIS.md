# Context preflight diagnosis (offline)

本报告只重建生产 `_chapter_details` / `_messages_for`，没有模型调用、账本变更或生产代码修改。使用本轮真实索引 `planning_level1/planning_material_index.sqlite`。

`RUN_FAILURE.json` 报告 `chapter_details:input=1118136:output=16000:thinking=2048`。按串行文件顺序与独立生产构造器确认失败章为 **CH02**：CH01 后没有 CH02 detail raw/packet，CH03/CH04/CH05 随后成功。重建 CH02 为 local `991010` tokens、estimated input `1118124`；与真实报错相差 **12 tokens**。推算真实 local count 约 `991021–991022`，12-token 差异尚未完全解释，不冒称逐字复现。

## 各章重建与 provider 对照

|章|重建 local|estimated input|source 条目|真实 provider prompt|
|---|---:|---:|---:|---:|
|CH01|793,087|896,450|357|793105|
|CH02|991,010|1,118,124|432|未调用|
|CH03|569,029|645,505|229|569049|
|CH04|721,389|816,148|305|721406|
|CH05|282,331|324,403|74|282353|

CH01/03/04/05 的重建值与真实 raw prompt 接近，验证了构造路径和材料量级；CH02 无 provider 返回。

## CH02 真实组成

`source_materials` 为 `940,159` standalone tokens，占重建 local message 的 94.87%。`candidate_materials` 是空数组。

|顶层字段|JSON chars|standalone tokens|
|---|---:|---:|
|`source_materials`|2,646,621|940,159|
|`candidate_navigation`|101,561|25,754|
|`new_tool_materials`|35,316|15,323|
|`relevant_tool_feedback`|15,512|5,129|
|`chapter`|5,371|2,546|
|`shared_outline`|641|358|
|`citation_rules`|478|86|
|`required_behavior`|290|73|

`source_materials` 内部：

|字段|JSON chars|standalone tokens|占 source_materials 字段 tokens|
|---|---:|---:|---:|
|`planning_material`|1,108,321|386,352|41.35%|
|`study_summary_A`|1,016,637|380,627|40.73%|
|`routing_note`|245,420|103,477|11.07%|
|`supplement_materials`|57,843|15,812|1.69%|
|`supplement_material`|57,803|15,783|1.69%|
|`deep_read_material`|30,653|15,450|1.65%|
|`title`|48,739|10,956|1.17%|
|`source_handle`|3,024|3,456|0.37%|
|`material_depth`|4,420|2,530|0.27%|

A 与 B (`study_summary_A` + `planning_material`) 合计 766,979 tokens；routing_note 为 103,477 tokens，其中 interpretation_limits 51,147、specific_usable_material 42,734。它们不能在没有新契约时被视为无损垃圾。最重单条为 P0574（7,954 tokens，deep_read_material 5,812），随后 P0583、P0582、P0578、P0576；主因是 432 条 routed source 的累积。

## 跨章节重复

|章|source handles|standalone source tokens|
|---|---:|---:|
|CH01|357|760,568|
|CH02|432|941,025|
|CH03|229|504,139|
|CH04|305|643,999|
|CH05|74|177,588|

五章共有 1,397 个 source occurrences、583 个 unique handles；按每个 handle 的最大单章 token 计，重复跨章约 **1,801,412 tokens（59.5%）**。

## 离线情景（只供 root 选择接缝）

|情景|estimated input|total context|
|---|---:|---:|
|只去补充字段精确重复|1,101,898|1,119,946|
|导航只留未安排 candidate|1,115,094|1,133,142|
|导航保留身份、去 verbose local_search|1,089,901|1,107,949|
|完整 routing_note 留在本地 packet|1,001,760|1,019,808|
|去补充重复 + routing_note 移出|985,535|1,003,583|
|上项再压未安排导航|982,505|1,000,553|
|完整 B/route/deep/supplement/navigation，章节总览暂不送 A|691,338|709,386|

补充字段去重单独仍为 1,101,898 input；routing_note 移出并去重为 985,535 input，但 total context 仍 1,003,583；再压未安排导航仍 1,000,553。interpretation_limits 与 specific_usable_material 的去留需要显式审查。

## 处理方向

不随机删论文、不把每张卡截成固定长度、不永久丢 A 或 route。可审的最小通用接缝是：保留完整本地 packet，把超限 chapter_details 分成章节拥有的完整证据、紧凑身份/路由索引、以及按具体子问题读取的完整 A/B。B-only 总览情景的 CH02 离线预算为 691,338 input / 709,386 total；A 仍保存在本地，后续具体任务再送，不代表删除。

质量内容尚未重审；本报告只诊断工程上下文超限。
