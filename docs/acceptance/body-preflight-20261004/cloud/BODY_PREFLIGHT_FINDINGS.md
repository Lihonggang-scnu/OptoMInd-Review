# OptoMind BODY 完整实验前独立检查

代码：`d3a2419b213ddbbfbf8a550af03b0b60edab3025`。日期：2026-10-04。对应阶段与观察点见 `BODY_FULL_RUN_STAGE_MAP.md`。

## 判断

**暂不建议立即开始完整付费实验。** 06/07修复值得保留，但扩大到正式CLI、跨运行材料导入和中断恢复后，发现了此前短链未覆盖的确定接缝。优先补齐下面的小范围消费者/兼容检查，再以固定材料做代表章验证；不需要另建规划器，也无需继续围绕某篇稿件调科学措辞。

本轮没有改生产代码、提示词或分支，没有调用付费模型、真实检索下载、完整规划/正文或完整测试集。以下“已证实”指可构造的程序行为，**不表示已证明用户当前585篇材料或全部历史退化受它影响**。

## 一 已证实的问题

### F1 编排CLI缓存丢任务关系 独立writer又可绕过失败状态

**位置：** `scripts/upgrade3/chapter_arrangement.py::saved_payload_from`（456–508）、缓存恢复分支（362–387）；`scripts/upgrade3/review_unit_writer.py::main`与`build_unit_view`。

**离线反例：** 一个单元原有B1/B2。合并或拆分后的fresh结果携带`source_briefs`与`portion`，真实validator判定`arranged`。经`saved_payload_from`再校验后，这些字段丢失，结果变成`contract_failed / briefs_unclaimed`。原ID不变的普通任务仍可重新匹配，但原`issues`被静默清空。`chapter_argument`保留，不能误报为主线也全部丢失。

实际`build_unit_view`仍能从这种失败编排构造writer消息，合并/拆分的原任务明细不再完整。独立writer CLI没有与feedback/delivery相同的入口validation闸门。**已有受控反馈短链通过，不能证明这个CLI缓存路径通过。**

**最小建议：** 缓存保存当前validator真正消费的模型字段，包括`source_briefs/portion/issues`；恢复后不完整合同不得作为正常编排输出交给writer。独立writer或本地统一启动器在调用模型前消费相同validation。用fresh→cache→writer的合并、拆分、普通任务和issues四种情况验证；无需重写编排算法。

[缓存代码](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/scripts/upgrade3/chapter_arrangement.py#L456-L508) · [writer入口](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/scripts/upgrade3/review_unit_writer.py#L116-L165)

### F2 工具外层缓存会挡住内层本应重试的需求

**位置：** `ProgressiveReviewPlanner._tool_cycle`（4187–4224）、`_stage`（3647–3674）。

**受控局部执行：** 真实编排器和落盘函数，替换队列边界返回一个provider-failed/partial需求。首次队列调用1次；同输入resume仍为1次，直接返回旧失败。只改变本地索引标记也不进入队列。原因是外层stage把返回对象缓存并标记completed，不检查需求是否完成；外层合同也没有索引generation/外部许可配置。这里没有模拟真实供应商失败，也没有测试SQLite搜索本身。

内层需求缓存有较细的完成与索引校验，但外层返回早于它，保护可能根本不执行。新空目录首跑可避免历史stage复用，**不能解决同次长运行失败后的恢复**。

**最小建议：** resume时让工具collector实际运行，由既有内层缓存复用已满足需求、保留partial并处理失败；或对外层明确检查未完成状态及所有有效依赖。不要清空所有缓存，也不要增加自动付费重试循环。

[外层缓存](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/progressive_review_plan.py#L4173-L4242)

### F3 已取得的纯本地答案没有进入早期纲要反馈

**位置：** `_compact_tool_feedback`（5225–5262），消费者为level1_outline、harmonize、finalize及chapter_need_analysis。

**局部投影探针：** 真实`_tool_cycle`与检索适配器，受控本地triage结果。需求已answered；唯一答案标记存在于完整结果和`tool_materials_by_chapter`，但compact结果仅为：

```json
{"supplement_results":[],"directed_results":[],"phase":"level1","consumed_paper_ids":[]}
```

答案也不在材料主题增量里，因为原卡片/池未改变。后来提案、细化、负责人及最终packet有独立章级工具通道，仍可能看见它。**这是早期范围构思的特定信息损失，不是“全部材料没有送到writer”。**

**最小建议：** 给现有compact反馈保留来源绑定的本地答案、当前需求状态与限制，避免只投影外部补检索/精读数组。沿“本地回答→下一纲要真实messages”加检查；不要整池重喂或另建研究阶段。

[投影代码](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/progressive_review_plan.py#L5225-L5262)

### F4 首次章细化和历史精读导入的身份保护不一致

这是两个不同入口，应分别处理。

**A 已分配卡片：** 合成pool为paper-A/P0001，card_path明确指向paper-B卡片。真实`_chapter_details`发送的标题仍为A，A/B正文却为B；实际`_messages_for`含B的唯一标记。`build_local_material_payload`能拒绝同一矛盾卡片，但初始章细化直接读卡绕过它。后续refresh只标冲突，仍保留先前A/B。相同输入resume复用旧计划；纠正卡片内容后缓存会失效，这不是“修改任何卡片都不会刷新”。

**B 历史精读：** `load_prior_readings`按paper ID及题名接纳；实际`make_directed_reading_runner`在检查当前snapshot之前就按任务签名复用。两个探针分别提供“同ID/题名但DOI冲突”和“同ID/DOI但新旧snapshot标识不同”，均返回`fulfilled/reused_prior_deep_read/OLD_ANSWER`，reader与snapshot边界调用均为0。后者证明没有核验来源版本，不证明标识变化必然意味着科学内容变化。

**最小建议：**
- 初始assigned A/B也走既有身份检查；明确矛盾的内容不能继续作为可用材料送模型
- prior导入先比对身份和实际来源内容版本，再决定是否免读；同内容搬目录不应无故重读
- 在代码修复前，本地至少交付核对过的卡片/索引/精读来源清单，明确筛掉冲突或版本无法确认的免读捷径
- 没有自身A/B但具有可用综述转述及引用身份的原始研究，仍按既有政策使用；本项不能变成强制每篇补全文

[初始细化](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/progressive_review_plan.py#L5851-L5880) · [prior导入](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/progressive_review_plan.py#L7129-L7164) · [提前复用](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/optomind_research/runtime/upgrade3/progressive_review_plan.py#L8162-L8183)

### F5 可选feedback支线漏导出多来源工具认识

**位置：** `scripts/upgrade3/review_feedback_loop.py::FeedbackLoop.arrangement_runner`（356–361）。

真实适配器加受控模型返回的探针中，view有一条跨两篇来源的工具答案，编排validation正常；feedback导出只有source_catalog和unit_id_remap，没有`chapter_tool_materials`，实际writer请求缺该唯一答案。同一view经普通编排CLI `_export`会保留它。单来源入catalog的材料不能用来证明跨来源综合也保留。

**最小建议：** 复用普通导出的`compact_chapter_tool_materials(view)`。这是**可选反馈路径**的条件阻塞：若下一轮不用该支线，可以先隔离并记录；若自动返修要走它，应先修复验证。

[反馈导出](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/d3a2419b213ddbbfbf8a550af03b0b60edab3025/scripts/upgrade3/review_feedback_loop.py#L350-L366)

## 二 风险与内容限制 不冒充已发生的故障

- **案例后才首次采用某候选的deep可能缺消费者。** case选择能从独立read_materials看到答案，但`_attach_case_groups`的候选兜底只构造卡片材料，未传deep映射。多数已在packet的来源可避开此路；需本地挑实际“从未预载、后由案例采用”的来源核对，不能声称所有案例都丢精读。
- **案例缓存合同较弱。** 批次签名含任务和裁剪后的材料，却不含实时模型/输出设置及自动prompt哈希；换模型或只改被裁掉的后缀时可能沿用旧结果。新目录首跑隔离它，之后resume必须说明配置是否改变。
- **重要认识可能在模型merge/修订时压薄。** 材料与字段传到不等于充分展开；协调、细纲分批merge、owner、writer都须按阶段图比较。当前结构校验没有科学内容守恒证明。
- **普通无表writer可以交很短的正文而报告完成。** 合成21字符、一个句子的返回可为`complete/consumed`且无装配pending。它证明没有全面内容覆盖判定，不说明应加字数门槛或自动判所有短段失败。
- **数目仍有软牵引。** 没有强制585/176引用门槛，但case prompt保留“约150–200篇”的软目标及禁止凑数限定。应如实记录这项实验条件；本轮未改提示词，不用达标该数量代替内容评价。
- **真实输出仍有语义错误。** 06普通稿将输入里的“provider outage，当前不可判定”写成领域缺失；补表有跨试验数字归属不清、关联措辞变强。输入已包含相应提醒，不能全归因于程序丢材料，也不能靠格式修复宣称改善。

## 三 已排除或已有支撑的担忧

1. 当前没有案例后的再次整章采纳；函数旧名不代表实际顺序错误
2. 正常新目录启动不会自动恢复旧细纲；显式恢复参数和resume须主动选择。原始材料复用与规划结果复用应分开
3. 已修复的新问题/required_outputs任务身份、真实reader/store目录、partial保留、成功owner恢复、当前来源重绑定，在所选离线路径继续成立
4. 正常主线编排导出有真实source_catalog和多来源工具材料；合并任务fresh结果能携带原brief，不是所有编排都覆盖掉负责人内容
5. 独立成品表保留、原BODY前缀保留、pending装配不继续后置交付有06/07证据。用户刚报告本地07历史响应重放也通过，但该轮未提供公开归档，不当作云端新模型验收
6. 两任务共用一张有实质内容的表、综述转述原始研究只有引用身份，仍是允许的；本轮发现不构成收紧这些材料政策的理由

## 四 实际做了哪些验证

| 证据 | 执行与结果 | 不能推出什么 |
|---|---|---|
| 6项现有短链重查 | 跨阶段复用、partial输入、同论文新问题/新输出、owner→编排→独立表、正常resume；通过 | 不是新全文科学质量试验 |
| 8项缓存/分批控制 | 提案/路由有效输入、旧合同刷新、工具pool/PLAN合同、失败批次恢复、merge超限与无效批次；通过 | 没覆盖所有外层依赖，所以F2仍可成立 |
| 编排缓存定向探针 | fresh→缓存投影→真实validator→writer view；普通/合并/拆分及issues | 非真实LLM编排；没有付费生成 |
| 本地反馈投影探针 | 真实队列适配/工具stage，受控triage返回；答案只从早期compact消失 | 没验证真实检索排名 |
| prior两种反例 | 实际导入器/reader适配器；冲突DOI和不同snapshot；旧答案被复用 | 不证明用户现有prior文件已污染 |
| 工具外层失败缓存探针 | 真实stage/persistence；受控队列失败、索引标记变化；resume未重入队列 | 没模拟真实API outage或真实SQLite索引更新 |
| 章细化身份探针 | 实际pool loader、details、prompt、缓存、refresh；错误卡片到模型边界 | 不证明真实材料存在同样错配 |
| feedback跨来源探针 | 实际编排适配器、受控模型响应与writer payload；与正常导出对照 | 属可选支线，不泛化为主线全部丢失 |

所有探针都在临时目录使用合成材料，受测工作树保持干净。06第二轮是已有供应商响应重放，07本地此次也被用户明确说明为重放；没有把它们算成新的生成质量实验。

## 五 完整实验前的有限放行条件

**建议优先级：**

1. 先修/验证F1正式CLI缓存与写作入口状态、F2工具外层恢复、F3早期答案投影。这些影响实际编排或恢复可靠性；不需要改科学提示词
2. 对F4核对本地真实材料身份/版本，补上首次消费与prior免读的兼容保护；若先用人工已核验导入清单作为临时措施，应明确范围，不能宣称程序已保证
3. 若启用自动feedback返修，处理F5；不用则在本轮启动清单中关闭该可选路径
4. 本地给出实际批量脚本/命令、完整新输出路径、manifest/jobs生成规则、模型与token参数、索引及prior清单。当前仓库没有完整原始585输入与当次批量驱动，不能由云端替其宣布启动已准备好
5. 再做一个固定真实代表章的有限验证：至少包含合并/拆分任务、工具综合或partial、新案例及需要解释条件的内容。比较输入送达、原始回答和最终正文；先做fresh/cache离线等价，不为格式问题重复付费。验证后再由用户决定全量授权

**本地应补回的最少资料：** 新07验收归档或patch（含helper兼容改名）、拟用启动命令/批处理脚本、脱敏输入来源清单与版本哈希、每阶段生效模型/预算配置、代表章输入/输出及原回答。论文全文与密钥继续留本地，公开材料删节处明确标出即可。

不把小量LLM措辞问题当作工程必须停机条件；但错源、丢任务映射、失败被缓存为已处理、未完成仍写作属于明确工程边界。**目前可进入上述小范围核对，尚不建议直接放行完整真实实验。**
