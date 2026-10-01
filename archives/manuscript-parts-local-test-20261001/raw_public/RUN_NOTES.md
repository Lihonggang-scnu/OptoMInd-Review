# 本地真实验收运行记录

基线远端分支：review-v2-manuscript-parts
基线提交：ad173f9677cc2e4c0f765b7cd923d3546b1d1943
本地测试分支：review-v2-manuscript-parts-local-acceptance
实验总预算：100 CNY，同一 budget.sqlite；旧项目账本只读。

## 输入与比较边界

新版生产输入为真实上游 PLAN + 585 篇完整 A/B 材料，旧规划/旧正文仅作比较。
本地材料索引：591 篇、123494 段；复用实际已有定向精读，不复用旧章节规划。
旧真实比较稿：20260927_run01，6章23单元176篇参考文献；其摘要和结语有人工编写参与，不能视为旧自动首尾能力。

## 首次真实暴露的问题

首次 provisional_scope 正常结束，但漏掉 manuscript_parts_plan；516766 输入 token，11503 计费输出 token（其中报告 reasoning_tokens=8143），实际估算 2.343136 CNY。
整次失败保留于 attempt01_missing_parts，不作为新版有效规划。
实际开关已传递，问题是系统返回字段仍列旧合同，职责卡只追加在系统尾部，用户交付尾部也没有明确合同。
最小修复：返回字段一致化，在全量材料后重申通用职责卡形状及具体性要求，说明当前自动应用支持 standalone。不生成领域专用示例，不手写或补造模型漏掉的职责卡。
修复提交：f708dc4；同提交接通现有首尾模块的最小真实客户端，保留 Conclusion→Introduction→Abstract 依赖，同账本预算。
离线专项：102 passed；不据此宣称模型质量通过。

## 运行方式

RUN_PLAN.ps1 -Stage level1：真实小测。
RUN_PLAN.ps1 -Stage full -Resume：小测亲审后续跑完整规划。
后续 BODY、首尾和来源内容检查尚待本轮真实运行，未提前判定通过。

## 一级纲要局部修复（4e266f4）

第二次小测的 v0/真实工具结果已保留。根代理亲读发现 v1 把两项局部缺口提升为全篇中心，并自行构造 Grade B/C 分级；因此未直接升级全量。修复只调整一级纲要的材料权限与通用提示：原问题/Facets/上游主题库存驱动全局，工具反馈作用于对应部分；禁止无标准的科学等级。当前一级输入是上游材料摘要，而不是再次输入全部 A/B。

新增阶段提示版本只使 level1 缓存失效，v0 和工具结果复用。旧 v1 与原始响应保存在 attempt02_level1_before_adjustment，未手工修订模型结果。legacy 输入合同保持不变。Luna 定向测试 105 passed，根代理复核代码后提交本地分支。正在执行最小一级真实重试。

## 小测决定与升级

最小一级重试仅新增 level1 调用，成本 0.0360424 元；累计 5.0311096 元，无占额。根代理已亲读完整 v1。原问题的临床关联、菌群/代谢特征、机制、干预、转化五条 BODY 方向保留；说明恢复中文，自行 Grade B/C 分级消失；Introduction 明确为入口，不详讲基础机制。局部缺口仍偏重、一些检索缺失表述过强，不能宣称彻底解决。鉴于正文覆盖没有被删除，且后续章节实际读取材料深化，升级完整真实测试；将这一剩余风险纳入最终质量比较，未手工改写职责卡或大纲。

## 验收目标澄清

用户再次强调验收对象是通用编排链路，而非本题测试稿。采用以下判断：修复必须发生在通用输入组织/提示词/模块交接；真实稿只暴露问题与检验修复，不手工改成优秀产物，不把领域答案写入提示词。最终分别报告机器的机制改善、单次模型输出表现、尚未经跨主题真实验证的限制。完整真实运行不用于额外润色本题。

## 停止付费：查询交接缺失

根代理已停止完整规划 Python 进程（PID 33560；命令行确认是本测试实例）。Luna 只读检查确认：部分 harmonized/proposal supplement_requests 缺 targeted_queries；工具队列因此产生空 round_specs 并在外查之前 stopped_no_query。不能把这些视为已完成检索或领域没有资料。真正带查询的三个 chapter_need_analysis 请求能够正常调用工具，证明应修 producer-consumer 边界。

停止时已结算 8.1638152 元，另有 0.0271692 元 query-refinement 调用占额，保留不重置。旧中间产物已复制到 attempt03_query_loss。将只修通用查询传递/缺查询初始化与局部恢复，不重跑已有效的全池与材料分配。

## 查询交接修复（7e6e5a4）

Luna 实现，根代理复核并补齐旧模式隔离：仅新规划模式保留同身份缺口的既有 queries/round_specs；新章节需求提示明确要求可执行查询；首轮确实缺查询时沿用现有 query-refinement 回调。生成的方向在外部调用前保存，同一需求恢复不重复生成。工具阶段局部版本只使 level2/chapters 重新进入工具队列，不使 v0/v1、source routing 和 proposals 失效。旧模式保持原提示/载荷与默认行为。

根代理使用 UTF-8 实跑相关七套件 116 passed（包含工厂函数执行、真实七个缺查询 ID、三个自带查询请求、更新查询不被旧 round_specs 覆盖、中断恢复、空查询不重复付费、阶段局部恢复）。未使用本题科学答案修订代码。有限真实恢复已启动到 level2，累计账本保持 100 元，先前未知占额 0.0271692 元继续保留。

## 有限恢复真实结果

level2 查询恢复正常结束，新增 0.6392526 元，累计已结算 8.8030672 元，保留旧未知占额 0.0271692 元。已有查询得到保留，缺查询的需求生成了可执行的对象+关系检索式。实际返回 TACITO IIa 随机试验，负责人 finalize_scope 已吸收主要终点未达显著性、中位 PFS 改善、OS 无显著差异等内容。根代理亲读原始 READING_VIEW 的摘要和研究设计：45 人进入全分析集（23/22），12个月 PFS 主要终点 p=.053；中位 PFS 24/9个月 HR .50 p=.035，不能作为 III期证据。

本次仍观察到生成规划把“补检索未找到”强化为“领域确认不存在”，并过度强调两个局部缺口。这属于仍待最终内容判断的链路表现，未手工修订模型规划。完整规划已从有效缓存继续，日志 FULL_RUN_AFTER_QUERY_FIX.log；未重做全池阅读或材料路由。正在只读核查局部检索文字中的 P0591 归属是否为工程映射问题。

## 停止付费：共享缓存的运行内编号冲突

Luna 只读确认并向根代理报告：共享索引内旧 UBA6 论文 CorpusId:242322079 与当前肌苷 Phase II 论文 CorpusId:272644866 均保留 P0591。当前 level1 身份映射中 P0591 只属于 Phase II；当前池中的 UBA6 CorpusId:252309032 为 P0582。level2 local triage 实际同时把不同 paper_id 标为 P0591，故这不是单纯模型措辞问题。根代理已核对本测试完整命令并停止 PID40940。累计已结算9.23218元，未决占额0.0536652元继续保留。

修复要求：共享索引保留旧资料，检索结果进入当前运行时依据稳定身份/DOI转换为当前池编号。P号是run内编号，不能用全局唯一约束或删除共享资料掩盖问题。正在准备通用边界修复与污染结果局部恢复方案。未手工改写测试稿。

## 当前来源身份修复与恢复（6b8e1ad）

Luna 实现通用当前池 paper_id/唯一DOI 绑定，根代理复核并补充中断恢复。本地池外论文保留稳定身份和原文，通过现有池纳入函数获得当前编号；同DOI别名不重复纳入；旧预印本不冒用正式稿或RCT编号。judge同时看到稳定身份；focused local reading沿用同一映射。旧模式无映射时保持原行为。

根代理UTF-8实跑39项通过，包含工厂→judge输入→本地池纳入→章节工具材料，以及本地纳入后恢复不重复调用且编号保留。提交仅在本地测试分支，未推送未合并。

只读扫描发现level1起已有旧编号污染，故根代理决定保留v0全量材料理解，从level1工具结果及其派生规划开始重新生成，不能承诺旧routing/proposals继续有效。完整旧new_plan已复制到attempt04_identity_before_recovery；已下载材料原位置不动（共享索引仍可读取），只移除13个已归档阶段/队列/partial缓存文件。预算账本不动，累计9.23218元+0.0536652元未决占额。具体恢复清单IDENTITY_RECOVERY_APPLIED.json。最小level1真实恢复日志LEVEL1_IDENTITY_RECOVERY.log已启动。

## 更新请求的补检索输出目录修复（c298081）

身份修复后的真实level1调用完成，新增0.045075元，总计9.277255元，但根代理亲读v1发现补检索报告OutputDirectoryError。原因是补充模块要求新/空目录，而合法重试仍使用旧gap目录。此为恢复工程失败，不能视为没有研究。新增revision-only attempt_NN目录选择，结果/index均消费新路径，原输出与快照路径保留。Luna40项回归通过，根实跑专项通过并审diff后提交。

目录失败的本次结果归档至attempt05_directory_before_recovery，保留v0和原始论文路径，重置5个受影响stage/journal/partial缓存文件，预算不重置。真实恢复日志LEVEL1_OUTPUT_RECOVERY.log。当前尚未有最终新BODY/首尾，不宣称验收完成。

## 修复后小测决定

LEVEL1_OUTPUT_RECOVERY正常结束，新增0.27173元，累计9.548985元，旧未决占额0.0536652元。根亲读完整v1：五条BODY方向（临床、微生物特征、机制、干预、转化）仍在，首尾职责独立且具体，工具新带回的SCFA定量材料确实写入纲要。真实local triage检查的12组handle未见不同DOI冲突，目录错误未复现。

仍存在内容风险：review_argument把NSCLC ORR80%与双ICI背景65%毒性放在相邻综合句而未明确后者属黑色素瘤；多处把本次未检出强化为confirmed_absence。未手工纠正模型稿，也未为本题再加专门答案提示。允许完整规划继续，用后续真实章节材料和最终BODY检验；不能提前宣称科学质量通过。完整日志FULL_RUN_AFTER_IDENTITY_FIX.log。

## 新章节提案抽读

根代理亲读当前CH03和CH04提案：机制保留DC/T细胞、分子模拟、代谢物、屏障/易位；干预保留FMT、LBP、饮食、标准化。深度BODY职责未被轻量首尾挤掉。

仍见科学综合错误：CH04把MITRIC描述为GI肿瘤背景，而原始材料是12名耐药者中9名黑色素瘤+3名其他癌症；部分FMT毒性陈述仍需具体人群。先记录观察，不手改提案、不嵌入本题答案，最终核查新BODY是否纠正或继承。完整测试仍在运行。

## 跨章协调输出截断恢复

harmonize_scope HTTP200但finish_reason=length，JSON在directed_reads中途结束。输入183869、completion20572（reasoning2565），实际结算0.614602元。累计11.662011元，旧未决占额继续保留。Luna诊断与根代理原始响应核对一致，不是网络错误或科学内容修补。运行配置输出上限18000改32000，保持同一任务/材料/提示词，resume复用已完成阶段。未拼接截断JSON或手工改研究内容。

## 小时检查：章级上下文超限，付费已停止

2026-10-01 04:56根代理确认进程已退出，日志 planner_context_preflight_exceeded:chapter_details:input=1129991:output=16000:thinking=2048。CH01/02/04/05细纲已真实生成并结算，CH03门禁前停止，无新正文。累计结算17.1379492元；旧reserved .0536652元及uncertain .51844元保留。正在委托Luna只读检查章级payload，要求最小通用输入组织修复，不手改科学内容、不绕过1M门禁、不重复已完成付费章。

## 章负责人A/B输入投影修复

Luna定位CH03唯一来源418篇，A/B占输入约94%，不是同论文重复。revision-only模型消息去除A重复概述、B用途建议与身份元数据；研究范围/方法/findings+conditions/贡献与限制、B贡献与边界、未知实质字段、deep/supplement继续保留；完整packet/cache输入不变。根审diff并补fallback：缺实质发现/贡献的A仍保留概述。专项3passed、Luna关联35passed。418来源构造的材料视图估算987293→776641（此为离线构造，未包含正式全部额外字段，正式门禁仍执行）。仅本地提交，当前四章细纲缓存不改、不手改科研内容。resume日志FULL_RUN_CONTEXT_PROJECTION.log。

## 恢复时发现缓存误失效，立即停止付费

章材料投影提交50a50e8后，实际CH03门禁通过并创建调用，但CH01/02同样新调用。根定位缓存source_materials追加candidate_navigation，而比较对象payload只有assigned，故有导航附加材料的缓存永不命中。不是投影修改了原卡片。根停止唯一已核对的acceptance规划进程，避免后续CH04/05继续误重复调用。已发出的调用可能计费，账本预留不释放、不把总成本冒称已确定。要求Luna仅修assigned比较与独立candidate检查，新增同输入恢复不调用、实质材料改变仍失效测试。当前不存在新BODY，先工程修复再resume。

## 章缓存修复验收

Luna完成revision-only比较时排除packet追加candidate_navigation行，其余assigned原材料仍逐项比较，独立candidate_navigation/candidate_materials仍比较。真实生产fixture同输入resume不调用、assigned A变化和导航材料变化仍调用，专项4passed+planner35passed。根审diff与测试后本地提交。已中止旧进程，不清空预留，后续恢复日志FULL_RUN_CACHE_RECOVERY.log。语义内容不改，原四章缓存保留，实际是否复用以最新ledger调用为准。

## 05:55小时检查：五章细纲完成，案例材料接缝修复

FULL_RUN_CACHE_RECOVERY结束于case_groups前置材料构造 TypeError。根确认无进程、累计settled20.0215708元，reserved2.7100392与uncertain.51844保留（含已中止调用不清零）。五章细纲均真实生成。案例池回退调用helper错用deep_material_by_paper，实际传入是单篇value，应为deep_material。Luna仅改一处关键字，生产fallback回归保留真实A/B/deep/supplement，另测local passages。专项5passed，根审diff后本地提交。恢复FULL_RUN_CASE_FALLBACK_FIX.log，不重写材料/计划。

补充：Luna只读证明CH05上轮cache miss还有真实输入变化（3个stable identity发生active handle重绑定、一个工具need差异、候选有无card_path变化），不应忽略实质变化强行复用。窄过滤candidate附加行修复正确，但不等于任何旧chapter都可复用。

## 06:55小时检查：候选来源被旧准入漏判

当前最终plan已落盘，cases与whole-plan均完成，v2更新，但statuspartial owner_revision_unresolved CH05，错误updated_unit_sources_unavailable:P0614。Luna与根代理核查P0614=CorpusId288332815、DOI10.1136/jitc-2025-014702，真实卡片存在，CH05实际candidate_materials含A/B/local/supplement。错误来自只看原source_materials。修复允许已有真实candidate_materials；metadata-only不作为内容；采纳候选材料加入最终packet/material目录/章handle。相同输入cached真实响应按当前分类器重处理，原partial可在实际材料可用时合法采纳，不重调模型。根补原packet不被promotion浅复制改写，并实跑41项通过。仅本地commit，恢复FULL_RUN_OWNER_CANDIDATE_FIX.log。

根亲读最终结构：CH01/02修订退为ordered_development字符串而非逐段briefs，CH03/04仍有逐段任务；这是待BODY质量验收的问题，不手工改纲要或拿本题答案改prompt。目前累计settled25.8168756元，旧reserved2.7100392+uncertain.51844继续保留。后续优先实际生成新BODY验证。


## 真实验收结束（2026-10-01）

完整规划、新BODY和真实首尾已完成。最终本地SHA f96ef312c903bbb32eff6b323af41c945bb8b074；20/20单元、71篇实际使用、未知引用0。只重试一个引用不合合同单元，19份原响应离线恢复。主代理亲审发现正文广度下降、首尾仍由缺口主导、引言英文及科学归属/统计含义错误；不批准整体替换旧稳定链。没有手修科学稿。最终报告FINAL_ACCEPTANCE_REPORT.md，机器摘要FINAL_ACCEPTANCE_RESULT.json。已结算27.8396996元，reserved4.3938592、uncertain0.51844元，均保留。没有运行中的Python进程，未合并、未推送。小时检查在最终汇报时关闭。

最后离线补修cdde86d：language配置未写入首尾指令已通用修复，专项22通过，关联133通过/7缺历史fixture；无真实调用，无科学稿修改。最终本地SHA cdde86dfceb89a618db72f85aa1477ec7207022c。真实稿依据f96ef31运行；新语言指令尚未真实复测。小时automation已PAUSED。
