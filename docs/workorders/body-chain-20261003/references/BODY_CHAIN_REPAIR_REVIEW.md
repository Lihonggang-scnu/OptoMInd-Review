# OptoMind BODY 链路修复审核

分支：review-v2-body-chain-repair-20261003  
锁定提交：93d82392aab03718cf463fee09751b8b19a743fa  
代码提交：f067eeaf7ea6c157765bebaab88ef1839f50614f；93d82392为验收资料归档，二者核心代码无差异  
审核日期：2026-10-03（UTC+08）

## 放行意见

**当前不建议放行完整付费测试。修复方向正确，38项原有局部离线测试通过，但新增失败路径复现证明，恢复与精读的真实下层调用仍有阻塞接缝。先修下列确定问题，补少量离线集成用例，再做受影响章节和单元的小范围真实验证，之后才考虑完整运行。**

不建议撤销整轮修改或重构架构。负责人材料闭包、显式恢复、按任务区分精读、局部补写都是有价值的方向；问题在于部分新判断没有与下层缓存、存储和校验规则闭合。

本轮未修改生产代码，未合并、未调用付费模型、未运行真实全链或整套tests/upgrade3。必要的仓库读取和安装离线测试依赖有网络操作；测试模型边界均为本地fake，未读取模型密钥。

## 一 审核依据与通过的部分

入口及固定来源：
https://github.com/Lihonggang-scnu/OptoMInd-Review/tree/93d82392aab03718cf463fee09751b8b19a743fa/advisor/body_chain_repair_20261003

按入口检查了PUBLICATION、ROOT_LOCAL_REVIEW、LOCAL_REPAIR_DELTA、负责人真实材料闭包记录、CH03/CH04恢复记录、补写真实预览与模拟输出，以及生产函数、CLI和对应测试。LOCAL_REPAIR_DELTA只含四份先前快照的本轮差异，不把e100到当前全部变化当作本轮修改；新增writer CLI也单独检查。

公开messages已删原论文片段。本报告不把它们当原始字节录音，不以公开删节推断生产输入丢失。真实材料预览证明选定任务和A/B可进入输入；fake证明接线，不能证明补写科学质量。

确认保留的设计：

- `run()`仍在章节细化之后接显式恢复，然后协调/负责人修订，再正式案例附加，最后交给编排与writer；没有加回案例后的整章采纳。
- 恢复结果单独写stages/recovered_chapters，没有覆盖原始stages/chapters。
- P0574型正向场景可按明确引用从当前池补到负责人输入，没有灌入整池。
- 精读新增task signature，并保留历史question_material；不能因此就宣称新任务已能真实补读，见R2/R3。
- 补写默认离线预览，真实执行需`--run`；原BODY保持为前缀，另写新产物；失败、pending及length结果不会替换原文。
- 原有五个局部测试文件合计38通过：case_chain、chapter_recovery、transport_retry合计17；material_reuse 13；writer_completion 8。没有跑完整回归。

## 二 必须修复项

以下均为代码实际执行的离线复现，不是根据注释猜测；但不意味着这些故障都已在本题真实运行中发生。示例温度、论文ID和文本为工程夹具，不是科研结论。

### R1 再次 resume 会丢掉已经成功的负责人结果

**严重度：阻塞恢复链验证。**

位置：`progressive_review_plan.py`：
- `recover_compatible_chapter_details()`约313–323行
- `_post_case_review()`中`revise_one()`约4607–4649行

当前检测到本次输出目录已有complete owner结果，会以`current_successful_owner_has_priority`跳过恢复，但返回的仍是传入的早期章节包，没有装入成功的updated_plan及其材料。随后owner缓存按整个revision_payload比较，因基线变回粗稿而不命中；重试失败又覆盖成功缓存。

**复现：**
1. 构造原始章1条brief、兼容历史成功章2条brief。
2. 首次显式恢复后让本地fake owner成功，产出`SUCCESSFUL_CURRENT_OWNER`、2条brief。
3. 同目录`run(resume=True)`，让下一次owner返回异常。
4. 实际结果：又调用owner；第二次输入为`ORIGINAL`；最终只剩1条brief；原成功owner缓存改写为failed。

既用聚焦stage夹具复现，也用未替换生产stage/cache/recovery/assembly的微型离线流程复现；后者仅替换模型客户端，不依赖人为绕过stage缓存。

**最小修法：**
- “当前成功结果优先”必须意味着验证兼容后真正加载成功计划及对应材料，而不是只跳过历史恢复。
- 使成功结果的输入身份与当前任务、范围、材料关联；不无条件信任任何旧complete文件。
- 成功基线与最新失败尝试分开保留，失败不能抹掉最后有效成果。无需重建规划器。

**补测：**首次恢复成功→再次resume零重复owner调用；确需修订但失败→保留兼容成功基线并记录未完成；输入发生实质变化→不得盲用旧成功结果。

### R2 上层识别了新精读任务 但真实阅读器仍阻止补读

**严重度：阻塞新任务精读验证。**

位置：
- `progressive_review_plan.py`，`make_directed_reading_runner().read_one()`约6810–6896行，尤其6877输出目录
- `directed_reading.py`，`DirectedReadingStore.add_task()`约721–746行、实际读取约4327–4344及4376–4385行

签名不同会进入reader，这一步正确。但输出目录仍只有`phase_root/directed/paper_id`，没有区分任务；底层持久化存储还有“没有新gap不批准新任务”的规则。

**两个独立复现：**
- 同论文首次读取成功；同phase换问题并换gap：第二次失败`output_directory_already_contains_a_reading`，没有发生第二次fake provider读取。
- 改用新目录，同论文同gap但改required_outputs：仍失败`needs_explicit_new_gap`。

复现经过真实adapter、真实run_directed_reading、真实SQLite store，只注入本地fake模型；不是把reader整个替换成一个永远成功的函数。

**最小修法：**
- 让任务签名/有效task identity参与输出目录，保留不同任务的历史记录。
- 对齐adapter与store的任务身份语义：问题或要求产物确实变化时可建立可追踪的新提取任务；完全相同任务仍复用。不要为了绕过store而伪造科学gap，也不要取消所有防重复规则。
- 保留按独立论文计数的共享阅读额度及付费预算限制；新输出要求不应凭空重置额度。

另有一个直接兼容回归：`_directed_task_requirements()`约2750行，对合法的字符串问题不再回退purpose，真实请求构建报`question_purpose_missing:Q01`。恢复原来的purpose/use/required_output/question回退即可，不需新字段。

### R3 无实际答案仍能被复用并宣称 fulfilled 空结果重试也被磁盘缓存挡住

**严重度：阻塞需求满足状态的可信性。**

位置：
- `progressive_review_plan.py`：`_directed_material_compatible()`约2812–2822；`_material_current_question_rows()`；自适应`external_closure`约6660–6698
- `directed_reading.py`：既有结果复用约4340–4350

当前把“question_material数组非空”当成“当前问题有答案”。如下夹具没有任何实质答案，却能复用：

```json
{"material_ready":false,"question_material":[{"question_id":"Q1","examples":[],"explanation":"","remaining_points":["Not found"]}]}
```

只要绑定当前签名，实际adapter返回`reused_prior_deep_read`；自适应闭环把这个数组JSON转成非空字符串后可返回`fulfilled`。底层已有`has_practical_content()`对它判false，两个判断未对齐。

即便让空结果进入reader重试，已有同task的DIRECTED_READING.json也会被直接返回，不检查是否有可复用的有效结果。离线两次读取得到partial/partial，fake模型只调用一次。

**最小修法：**
- 统一“有实质内容”“当前任务覆盖”“历史材料保留”的判断，至少不让显式material_ready=false、空解释或仅remaining_points成为已满足答案。
- 优先复用已有实质内容检测与任务/输出标识；部分答案保留为partial，不能因为旧材料有内容就把未覆盖需求变fulfilled。
- 底层只自动复用符合条件的有效结果；不完整结果应支持显式、有界重试，保留原记录，不无限重读。还需处理RAW_RESPONSE.json的复用规则，不能只删除DIRECTED_READING.json后又命中同一份无答案原响应。

**补测：**签名相同但无答案不算fulfilled；部分答案保留；完全相同且有效零调用复用；空结果在明确重试下确实能到达fake模型，而不止到达reader函数入口。

### R4 恢复兼容性只比较A/B 可能把已更正的补充材料恢复成旧版本

**严重度：阻塞“材料仍相同才恢复”的保证。**

位置：`progressive_review_plan.py`：`_recovery_material_matches()`约209–220行；`recover_compatible_chapter_details()`约414–431行。

**复现：**同论文身份与A/B不变，历史成功计划使用补充结果“80 C”；当前池已更正为“25 C”，当前章暂缺该source row。恢复仍判compatible，然后直接复制历史source_row，返回旧“80 C”补充材料并标recovered。

这是确定的材料版本检查缺口，不是证明本题已有某项温度错误。恢复章依赖精读/补充材料时，只证明A/B相同不够。

**最小修法：**把该计划实际依赖的精读、补充、本地片段等实质材料纳入兼容比较或稳定内容指纹；剔除时间戳、路径等纯运行元数据。发现实质内容变化时，将对应章标为需重新协调，不把历史正文主张强接到更正材料上。导入缺失来源时使用经确认的当前权威材料，不直接覆盖成历史旧行。

无需要求整池字节相同，也不应因无关来源变化让所有章失效。

### R5 负责人闭包的 unresolved 与后续来源校验尚未对齐

**严重度：阻塞“负责人实际获得所引材料”的完整保证。**

位置：`progressive_review_plan.py`：`_resolve_owner_source_materials()`约2080–2120；`_validate_owner_plan_update()`约1997–2024。

**已复现两个边界：**
1. 来源行只有source_handle/paper_id/title，卡片文件缺失：闭包视为already_present，不报unresolved；校验将有句柄的行都算available，owner结果仍被接受。
2. P0999仅出现在`paragraph_briefs[*].source_handles`，池中不存在：闭包明确报unresolved，但validator没有扫描paragraph_briefs；owner返回updated仍被判有效。

这里的部分validator问题原已存在，并非全由本轮新引入；但新闭包接上它之后仍未兑现材料可用性保证。

这不是建议凭文字正则搜出所有P编号。问题是两个阶段对同一结构化引用的覆盖范围不一致，以及身份元数据被当成了科研材料。

**最小修法：**
- 显式结构化引用收集与验证共用同一遍历范围，覆盖paragraph_briefs及现有支持的案例字段。
- 区分“有身份行”和“有可用A/B、精读或其他实际材料”；不要要求每篇必须同时有A和B，但不能仅靠title算完成闭包。
- unresolved要进入有效性判定或明确限制相关更新；保留原有效计划，不能取消来源校验放行。

补测不能只用unit顶层source_handles和完整卡片；要覆盖嵌套brief、身份-only行、卡片缺失以及有效deep-only材料。

## 三 非阻塞限制与较小问题

这些不应压过R1–R5，也不要求本轮建设新的审稿平台。

### N1 shared_outline合法列表形态被恢复拒绝

恢复函数约340–343行要求双方outline必须为Mapping。正常链路也接受列表；完全相同的列表仍被判`shared_outline_unavailable`，未恢复任何内容。给定真实预览是Mapping，故不是当前预览失败的原因。

最小修法：复用已有outline规范化/读取方式，比较语义相同的合法结构，仍拒绝缺失scope或真正不兼容内容。补一条列表对照用例即可。

### N2 身份冲突时可能拼出旧身份加新A/B

闭包约2102–2113只补空字段，随后强制用当前卡A/B覆盖。若传入source row和当前池对同handle对应不同paper_id，结果会保留旧paper_id/title/DOI而装入新论文A/B。

已用夹具复现；未证明当前真实池正常分配会产生该冲突，故标条件性风险。最小修法是检测并报告身份冲突，不生成混合来源。不要通过放宽身份检查解决。

### N3 正式案例仍有元数据冒充材料的旧风险

`_attach_case_groups()`约6030–6048：卡片缺失时build_local_material_payload仍可返回含身份的非空dict，案例便被正式附加，A/B均为空。该风险是既有路径，并非本轮新引入。

保留正式案例追加顺序；若在本轮顺手共用R5的“实际材料可用”判据，可以小范围修正。不能据此重新加回案例后的整章采纳，也不宣称实际142来源都存在这类问题。

### N4 补写的结构检查还较弱

已用本地fake复现：

- `_markdown_table_check()`约1096–1111把Markdown代码围栏内的表格字符串也判valid；最终仍是代码块，并非渲染表格。
- 一次请求两个表任务，只返回一个表，`run_unit_completion()`仍pending=false；当前检查只问是否存在任意表。
- 补段引用未提供的[P9999]也直接追加，issues为空；普通writer的`write_unit_output()`已有unknown_citations记录，而补写报告没有复用。

最小建议：忽略围栏内的表格候选或只安全去除完整外层围栏；记录新增片段的已知/未知引用；当前真实小测每次只选一个明确漏项，避免把“有一个表”解释成多个任务都已完成。

这里pending=false只说明片段已拼接，不是科学质量通过。多个任务合并成一张表有时合理，不能单凭表数构建全面语义验收。段落是否真正完成仍需亲读；不能靠covered_task_ids、词频或引用数量自动判定。

### N5 暂无自动识别所有漏项和自动出版接线

本轮提供的是显式`--complete-task`入口，不是自动任务审查闭环；这符合小改动范围。补写后的COMPLETED_BODY是单独产物，进入最终装配前仍应核对原文保持、真实新增内容和引用身份。不要把“提供入口”说成“已经自动补完旧稿”。

### N6 输入量和费用仍需真实预检

局部补写只选相关来源是优点，但并不必然很小。公开CH03预览有8来源，CH05有3来源；本地报告的23.8万/10.7万上下文估计需结合tokenizer方式理解，不能直接当实际供应商token或已发生费用。原片段删节也使公开副本不适合字节级成本重放。

完整质量和跨题泛化没有验证。本轮未重跑完整回归，也未重新核对原12项历史基线失败集合，不宣称其状态仍完全一致。

## 四 为什么38项通过仍不能放行

测试主要证明局部函数和正向场景，遗漏了以下组合：

- 成功恢复之后再resume，并让后续尝试失败
- adapter放行新任务后，真实reader与SQLite store是否允许它执行
- question_material有行但没答案，以及底层不完整结果缓存
- A/B不变但补充材料已更正
- 仅在嵌套brief引用的缺失来源，以及身份-only材料行

新增微型离线复现能够稳定触发这些故障。它们不是完整科研质量测试，却足以说明现在花钱跑完整流程可能再次测到恢复/交接问题，而不是模型展开能力。

## 五 最小修复与验证顺序

1. 修R1，建立兼容的最后有效章节基线；失败尝试不覆写有效结果。
2. 将R2与R3一起贯通adapter、reader、store及结果缓存，不能只再改上层一个if。
3. 修R4/R5，对齐材料版本、实际可用性与结构化来源覆盖。身份冲突时明确拒绝；不灌整池。
4. 保留当前阶段顺序，补跑原38项和新增定向失败用例。先验证重复resume不损失、不同任务能到达reader、未回答不假称fulfilled。
5. 用户授权后，只真实测试受影响章和一个补表、一个补段。初次不同时换模型、改prompt、重做池或全题重跑。
6. 上述结果能兑现后，再申请完整流程测试。此次审核本身不构成付费或合并授权。

## 六 下一次完整测试重点比较什么

不要只对照176引用或56/64任务。旧稿人工反馈仍是不可忽略的混杂因素。

| 检查层 | 必须保存与比较的东西 | 放行信号 |
|---|---|---|
| 运行身份 | 锁定代码、dirty差异、实际模型参数、材料快照、恢复清单及输入签名 | 能解释每个阶段来自新算还是哪次有效复用 |
| 章节恢复 | 恢复前后具体brief的point/development/conditions/source用途；再resume结果 | 已完成且仍适用的任务不因失败退回；不盲拼不兼容历史 |
| 负责人材料 | 实际消息里的明确引用、对应A/B/精读、unresolved及身份 | 所用来源确有材料，缺失项不能被标已解决 |
| 精读 | 当前问题、required_outputs、task identity、旧/新内容、调用与需求状态 | 同任务有效结果零重复；新任务可执行；未答保留unmet/partial；历史有效内容仍在 |
| 正式案例与编排 | 案例落在哪个任务、用于什么解释/比较，来源是否仍可读 | 材料进入实质写作任务，不仅计数增加或全部塞表格 |
| writer | 实际messages、原始响应、解析后正文、明确任务的完成情况 | 机制、条件、方法比较及代表例子有足够展开；未完成不靠空issues掩盖 |
| 局部补写 | 原正文hash/字节、所选任务、来源、新片段与新产物 | 原正确内容保持，交成品表/实质分析，不能把任务说明当成品 |
| 全文质量 | 同功能章节对齐，检查旧强项及新强项、重复与范围 | 背景教学未误删、跨章再现有用途、限制随判断出现；不以引用配额通过 |

建议预先选几类跨领域通用任务形态做比较：机制链解释、概念/理论教学、方法前提、正反案例比较、边界条件与未来问题。具体本题案例只是测试载体，不写入生产规则。

**停止条件：**任一恢复损失、身份混合、未答假满足或明确任务失落再次出现，停止扩大运行范围，定位该边界；不要以继续全链“看看运气”替代修复。若局部及完整工程路径都稳定，但文字仍薄，再讨论模型和任务组织，而不预先归咎于模型。

## 七 复现记录与口径

本轮保存了三组离线复现：
- 恢复组4个pytest场景，其中包含一个仅fake模型边界的生产run/resume微型流程；4个均重现目标故障。测试断言的是“故障可观察”，不是“修复通过”。
- 材料组经过真实reader/store，复现新任务阻断、无答案复用、请求purpose缺失、嵌套引用与材料可用性边界；另单独核对自适应fulfilled和不完整结果重试。
- writer组3个fake响应：围栏表、两表只交一表、未知引用，均进入pending=false；原文前缀仍保留。

复现均使用临时目录/本地fake，未传模型密钥。报告中的函数和行号对应93d82392锁定源码。原38项测试通过与这些新故障能同时成立，不能混成“全部测试失败”，更不能说38通过已证明质量恢复。


### 可复用的原38项运行命令

在锁定提交的仓库根目录、已安装测试依赖的隔离环境执行以下局部文件即可，不要扩展到全套tests：

```bash
python -m pytest -q tests/upgrade3/test_progressive_review_plan_case_chain.py tests/upgrade3/test_progressive_review_plan_chapter_recovery.py tests/upgrade3/test_progressive_review_plan_material_reuse.py tests/upgrade3/test_progressive_review_plan_transport_retry.py tests/upgrade3/test_review_unit_writer_completion.py
```

新用例应按R1至R5的复现步骤构造；关键是只fake模型边界，保留真实reader/store/cache，不能再次把发生故障的下层替换成永远成功的stub。
