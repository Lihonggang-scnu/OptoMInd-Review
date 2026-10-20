# 调研发现与后续方案输入

本文全部源码行号默认属于 S=0133380ea82068ec8d9c28269bf9d0417d38bb04；标注 adapter 的是 N/preparation 下实际监督接缝。R=612cdce… 的函数清单与是否存在独立列在 `source/SOURCE_INDEX.json`，不能套用 S 行号。证据路径为包内相对路径；报告不实施任何建议。

## 1. 基线与修复落点

**事实。** `evidence/baseline/VERSION_AND_ENVIRONMENT.json` 保留分支/完整 SHA/既有 tracked 修改、未跟踪数量及相关 source diff；无凭据或环境变量。R 的 4 个修改均为旧验收文档，不是本轮改动。S tracked clean，preparation 不在 git 中，必须单独哈希。E2E034 V28 的执行 binding 和原生预算附件是不同角色：wrapper 绑定 f446 当前执行源码，native shared-project-binding 仍保留 V27 不可变预算授权，不能为了“统一版本号”强改两个附件。

**实际路由。** adapter `downstream_supervision.py:36–46,87–107,115–153` 选 binding.source_worktree，BODY→`run_review_harness.py:1570–1599`→`scripts/upgrade3/legacy_unit_writer.py:311–344`→`review_delivery.py:1166–1175`→`legacy_unit_route.py:506–970`→`review_unit_writer.unit_messages:1866–1896/run_unit_writing:2945–3080`。`writer_candidates` 只提供工厂/锁等接缝，不能把其另一套执行器或 fullbody_writer 缓存机制当成本次 baseline 的直接调用链。

**状态矩阵（现状，不是修复计划）。**

| 问题/能力 | S 现状 | R 当前落点/证据 |
|---|---|---|
| chapter+local unit scoped ID | 已修且有历史验证证据；eb153f87…，实际7章36单元/后续运行 | R 不含同等后续路由模块，不能称已集成 |
| 实际 nested material/owner 发送来源报告 | 已修且有历史验证证据；7b76cc27…/d23acb55… | 不因 S 修复推断 R 已有 |
| 普通 author 多 envelope 最后完整稿免费恢复 | 已修且有历史实际0调用证据；f4461637… | R writer 为旧1245行版本，缺 S 这条恢复函数 |
| unit-scoped alias 装配/真实 derivative provenance | 已修且有历史验证证据；002067c…/e896f31…/d7cecb6… | R 无同等 alias 辅助函数 |
| 补写 full rewrite/overlap/重复追加保护 | **仍存在缺口** | R 无 unit_realization/legacy_unit_route 同等入口；不能借缺模块判断其生产等价行为 |
| 拒绝结果显式 reparse 跨入口消费 | **仍存在风险**；正常v29未发生≠所有入口安全 | R 等价覆盖现有材料无法确认 |
| 本文 bibliography-only 参考文献计数 | 已修且有保存验证；42e97ad… | 不等于编号策略修复 |
| 生成TeX算子分隔/控制损坏公式literal | 已修且有历史验证；f1876f39…/0133380… | 只证明渲染边界，不证明数学还原；R不同旧路径 |
| @波长误引、缺作者、摘要去可见引文/引言首引排序/重复叙事 | **仍存在** | 旧报告中的方向不等于实现；没有本轮改动 |
| review_delivery 与 f446 的其余差异 | 代码已改；没有逐项独立验证材料的差异不计已验证 | 源码摘录/提交可供云端继续判别 |

已验证指**历史证据**，本轮未重跑任何测试。断言/测试文件存在不是执行通过证明。详细状态和提交见 ISSUE_INDEX。

## 2. E2E-034：CH02:CH02_U03 补写→拒绝→恢复

### 2.1 保存的事实链

`evidence/completion/gate_request.json` 是实际请求，允许补写任务仅 CH02_U03_P01，来源 P0292/P0253/P0352/P0470；包含完整原稿和缺口反馈，不是人工补科学答案。messages SHA=99f3a2eb1efacd5d516b351197267219c51cd5fc043776a7d79775fa3db16f88。

原稿 `original_body_copy.md`：1657 Unicode码点，SHA=abf89ffa015bf3d956ee4b6faac276a2cd2bb4b496a99f032ce0150df0503dbd。不是1657个汉字，也不是UTF8字节计数。QRoot 的 original_body_sha256=ad70f3b9… 与该文件字节哈希不同：本轮已核实它等于 SHA256(json.dumps(原稿字符串, ensure_ascii=False, sort_keys=True, default=str).encode(UTF8))；是JSON字符串序列化身份，不能混用作原始文件SHA。身份 payload_sha256=166f677c…；完整值在 request_metadata_copy.json/identity。

返回 envelope 自报 status=appended、covered_task_ids=[CH02_U03_P01]，body=1883字符、4563UTF8字节、SHA=9e6b078a5b0890a4fe3c245ce2a2a51ca54aa08d9fa964ebdde99fe6290d0531。完整答案、原传输逐行脱敏 SSE 和 `BASE_VS_REJECTED_ANSWER.diff` 保留。**appended 是模型字段与解析器状态，不是工程证明它只追加。**

`gate_response_decision.json`/`gate_response_reviewer.json` 持久化 reject：返回整稿重写违反 append-only。native COMPLETION_RESULT 为 pending/model_status=pending/parsed_via=unparsed/call_error=SupervisionError:response_rejected，fragment 空；QUALITY_RESULT assessed_with_pending、changed=false、scientific_acceptance=false。`completion_completed_body.md` 和正常 v29 使用的稿件均保留1657字符原稿。根审直接从 DELIVERY_REPORT 的实际 assembly result_path 找到原 V27 quality/assembly_derivative 并核对（NORMAL_HANDOFF_DIRECT_PROOF.json），不是猜 normal attempt 下的 stage 子目录。根审直接从 DELIVERY_REPORT 的实际 assembly result_path 找到原 V27 quality/assembly_derivative 并核对（NORMAL_HANDOFF_DIRECT_PROOF.json），不是猜 normal attempt 下的 stage 子目录。旧 paid/raw/reject 未改。

精确费用链：gate441e0051…→provider chatcmpl-22a7eec5-de88-9f09-8826-1aec80fb28bc→raw/SSE SHA 2f20224b01d9f2a33902c34e8d8637927ea455b03bc5c3b1e188b5680d4a5be0→双账 mapping/settled。预留1.095632元，actual_cny0.0256664元，elapsed42.031秒。usage17827prompt/2376completion；预览measured17817与providerusage分开。拒绝不退回已发生费用。

### 2.2 静态合同与缺口

- `review_unit_writer.py:1113–1128` append-only合同；1169–1202拒绝重复/未知task；1233–1286提供任务/来源闭包，拒绝缺必要材料。请求中无显式 base_hash/body_version 字段，外层完整messages哈希绑定原稿文本；quality identity `unit_realization.py:459–519` 还含原稿/输入/profile/prompt/experiment以及可选body_version。**缓存身份约束与返回补写合法性检查是两个层面。**
- 请求/响应保存：`unit_realization.py:379–456` 以profile+messagesSHA缓存并先落request；监督 `supervised_client.py:319–368` 先落 INVOCATION，再保存RESPONSE，再核response decision。拒绝文件与响应分开，哈希绑定，不能凭有完整SSE就默认接受。
- 消费：`review_unit_writer.run_unit_completion:1563–1778` 检查任务/表/status/citations；1745–1771 仍可执行 existing_body+fragment，没有通用 baseecho/fullrewrite/overlap guard。`write_unit_completion:1781–1863` 写候选/complete；`unit_realization.py:570–588` 采用complete或保pending；`legacy_unit_route.py:760–840` 接质量derivative/candidate边界；`review_delivery.py:847–886,1077–1115` 处理交付。
- 恢复：`unit_realization.py:379–399,490–509,526–588` 的显式 reparse_saved 可以从保存内容重读。该路径不等价于监督client再次核相同reject。**正常v29没有再消费主案例，只是一条历史观察；不能推导所有恢复/装配入口均检查拒绝。**目前未见所有消费者共享的全局reject/consumed登记。
- 一次completion限定减少同一quality路径内重复；改变身份/output/namespace可换成另一次逻辑请求，不能作为跨路径重复追加保护。未找到真实重复采用/拒绝后再采用样本；静态风险不写成已发生事故。

### 2.3 合法历史追加样本

CH03:CH03_U01：546字符原稿，已有唯一合法fragment；fragment文件1804字符，去4个嵌入换行后1800，combined2348。这是历史真实补写与native装配证明，不是对通用guard已修的证明。文件 `ch03_original_body.md/ch03_fragment.md/ch03_completed_body.md/ch03_completion_result.json` 保留。未重新科学审查其条件或quote。

## 3. 请求身份、跨路径重复购买与状态

### 3.1 现有机制，不另造一套

本次实际 active seam 是 `source/adapter/planning_transport.py` 的 `_GatedClient.__call__:377–438`：identity包含nativecall_id、raw_response_dir、完整messages、profile、semantic_kwargs。profile含model/thinking/output/json/stream/timeout/temperature、source_commit、bindingSHA、module/role、ledger路径、cap、direct/key/retry和wire preview；transport_observer明确非语义排除。call_id后缀为digest(identity)。supervised_client.call_directory:96–100按stage+call_id哈希定位gate。任务/来源/章节scope经完整messages进入；路径、源码binding与namespace相关身份进入其他层。**题目相同或 messages 相同不等于现有合同同一请求。**

实际 unit route：`legacy_unit_route.py:552–576` RUN_IDENTITY含wholebookSHA/model/profile/budget/prompt/body_version/candidate_manifest；609–613 unitstageKey=hash(identity,messages)。671–708仅在UNIT_RESULT/RESULT_SEAL/bodyhash完整匹配后cachehit；713–739无合法缓存且run才新尝试。qualitykey见上一节。raw transport每次invocation用新随机证据文件名（module4/runtime.py:1016–1023），只防旧证据被覆盖，不是免费复用或全局调用去重。

`provider_transport.cache_key:76–83` 另有endpoint/body/schema/policy/credentialfingerprintscope的完整响应缓存机制，`chat_completion:280–399` 有不同重试policy；**它不是本次QwenDirectClient主路径的替代证明**。native ledger `budget_ledger.py` 的 logical attempt/unknown/frozen/linkedcache机制也只证明其调用者范围。不能看到通用类就宣称本次跨namespace全覆盖。

### 3.2 状态分支

| 分支 | 直接行为与证据 | 尚未证明的部分 |
|---|---|---|
| 科学/人工拒绝 | active supervisor `_wait:290–317` 对绑定decision/reviewer reject抛错；native补写保pending。规划6科学拒绝依赖匹配SupervisionError缓存保partial，旧费用留存 | 显式reparse/换身份是否所有路径再核reject未证明 |
| 格式解析失败、已有完整响应 | 普通author可同请求免费SSE恢复、多envelope/受控candidate恢复；unit_realization:379–456恢复保存响应；不能当成模型没调用 | 每个入口都先离线恢复未得到通用证明；请求/namespace变更可能新key |
| 传输失败 | supervisor:338–365 有INVOCATION无RESPONSE时禁止自动重试；QwenDirect runtime:1259–1359保留异常/unknown，native最大重试参数0 | 通用类可配置retry，不能套成本次实际重试；未记录耗时/计费不可填0 |
| 结果不确定 | runtime:1243–1252缺usage记uncertain；1268–1273/1343–1355异常后保unknown；ledger统计reserved+uncertain占额，dual_budget:104–196保双账mapping | 换新gate/rawpath不是原marker；预算hold防超额但不是全局幂等，不能把unknown视作从未调用 |

`evidence/calls/EXACT_MESSAGE_REPEATS.json` 有20组50gate同messages哈希，其中仅10组kwargs相同；profile/source/output等仍须逐层比较。897个已关联provider request ID彼此唯一；同nativecall_id重复组31不能用作唯一全局键。未证明这些重复messages是违规重买；也不能据此保证跨namespace不重买。

费用CSV按唯一provider_request_id且usage完全相等关联897行；47个gate无这种mapping、1个uncertainmapping独列。消费记录不是费用状态：paid≠accepted≠consumed。无证据的consumption_state保留not_deduced_from_gate_alone。主案例单独覆盖为实际拒绝/空fragment/正常handoff保原稿。pending没有独立账本列，held为reserved+uncertain的定义统计；不将未知费用释放，不上传数据库。

## 4. 出版层最小确定性复现

### 4.1 波长 @ 被二次解释

`evidence/publication/WAVELENGTH_AT_REPRO.json` 提供当前raw/handles/reader行与生成TeX行；同目录canonicalmapping显示 P0549→canonical DOI→显示[6]，P0006→[1]、P0301→[14]。论文身份映射正确不代表后续文本解析正确。原532/635波长旁的@进入Pandoc，`latex_publication_renderer.py:3186–3195` Markdown扩展+--natbib 未禁默认citation语法；已有body/main.tex将其变成 `\citet{532}`/`\citet{635}`。references.bib空，main.log原1128/1131/1134/1138/1163报告undefined。原PDF47/51页可见问号（PNG+原页文本均保留），未重编译。

输出校验确实在 renderer:3276–3310提取undefined并影响compile_ok；**有PDF/TeX命令return0不等于校验通过**。现有测试覆盖算子分隔/引用表计数，不等于已有@波长回归用例。可独立修复边界是Markdown→TeX字面量/引用token处理及渲染校验，不应重写正文或重新调用模型。

### 4.2 数学控制字符首次出现

`MATH_CONTROL_PROPAGATION.json`/`CH06_U02_ORIGINAL_RETURNED_CONTENT.txt`：CH06:U02原始保存回答content中有3个单反斜线 `\bar` 和4个 `\frac`，尚无对应字面控制字符；已保存UNIT_BODY首次可见3个U+0008(BACKSPACE)和4个U+000C(FORM FEED)。这是7个控制字符，不是7个公式或7条pending。后续INPUT_BODY/FINAL/handles/reader中的换页符已表现为换行，仍有3个U+0008，不能把只数U+0008误报为全部控制损伤。

静态链 `review_unit_writer.py:2635–2654,2673–2701` 只将非法JSON转义加倍，合法JSON b/f转义被保留并json解码；因此LaTeX命令可能变控制字符。此处未执行项目parser，原始响应和已保存前后稿提供变化证据。`publication_integrity.py:274–339` 和 renderer:460–530 后来把损坏公式literal化/留audit，三处fallback span不等于三字符。PDF40/41页显示公式被当literal，未被数学恢复。

已修且有验证的是**不崩溃、审计literal保留的出版边界**（0133380…）；损坏的科学公式仍存在。未来可独立修复结构化返回的转义/输出诊断，不能无依据猜原公式。本轮不改、不调用模型补公式。

### 4.3 作者缺失

`AUTHOR_METADATA_PROJECTION.json`：实际PUBLICATION_METADATA只有title/abstract/keywords，未提供authors/affiliations/ORCID/通讯信息。TeX第33行pdfauthor={}、第37行author={}；不能从本机账户或引用论文作者补身份。renderer:1698–1749创建metadata，2489–2512过滤placeholder；metadata_resolver:314拒占位/2047+合并已有字段，不自动推断真实作者。边界是配置→metadata→template的数据通路与缺项校验；缺少用户明确作者资料，因此本轮只登记。

## 5. 摘要、引言与引用

实际完整请求 `evidence/parts/ACTUAL_*_MESSAGES.json`，完整计划ACTUAL_PARTS_PLAN和原生成文本均在包内。serial_manuscript_parts:256–304是各部分职责/正文优先/此前部分次级表达的规则；308–330组messages，407–525串行循环。顺序**构思卡→结论→引言→摘要**；摘要能看到已经生成的引言。serial_parts_application:318–346/387–463只应用ownedspan，正文不重写。

`ABSTRACT_INTRODUCTION_COMPARISON.json`：首句同为“超表面光学成像技术正从实验室原理验证向实际系统部署过渡，但性能评估标准的碎片化导致技术路线选择缺乏统一依据。”，54 Unicode码点（含标点），对应 stage10 MANUSCRIPT_FINAL.md:10/19。摘要724字符，14次内部handle/13去重；引言1162字符、3段。字面整句交集只有首句，不表示没有改述式重复。

**功能核实（对文本职责，不重做物理真伪验收）：**

| 角色 | 已有内容 | 缺口/重复 |
|---|---|---|
| 摘要 | 对象=超表面成像；范围=路线/材料制造/计算部署；组织贡献=物理硬边界＋工程约束双视角；有条件化主要结论 | 可见引文较多；用多项细节和正文证据承担摘要叙事，宏观贡献与结论层次需统筹打磨 |
| 引言 | 展开动机/现有进展/缺少可比基准的缺口；结尾有第1—7章阅读路线 | 与摘要共用同一开场、双框架/材料效率/制造/计算边界的相近叙事；不应只改第一句 |

两次输入实际same actual_body（序列化106324字符）、samecontext（401300）、samewholepartsplan（2581），任务相关字段SHA完全相同；引言prior仅结论，摘要prior含结论+引言。职责卡确实存在，不能说“没有prompt”。**事实**是共享任务轴、共享大输入、串行可见前文；**推断**是易发生措辞锚定和职责重叠，未做ablation，不能声称已证明唯一模型内部根因。未来建议先分清各角色的信息抽象层/验收，再考虑防复述，保留共有科学证据，不用删除输入量充当收益。

摘要禁可见引文：当前system只在显式writing_requirements时要求遵守，实际CONTEXT没有该字段；没有全局摘要hide-citations策略。内部[Pxxxx] provenance应保留，未来可在reader投影隐藏，不等于删证据。`delivery_citations._identity_key:115–131` S用DOI→paper_id→namespacehandle；`build_delivery_citation_map:71–190`按final handle draft阅读顺序（含摘要）建number；render_reader_citations:215–241和stage:432–508此时固定数字再交TeX。现有编号已从摘要开始，**不是从引言重建**；LaTeX模板无法自动更正已经写死的[n]正文。可独立做canonical→handle→显示序号→文末entry投影，不需要大幅重新整理或模型重写。

## 6. 完成状态：36与18分别代表什么

`evidence/states/UNIT_STATUS.csv` 完整列36个qualified单元；`PENDING_GROUP_INDEX.json`将18组通过同一nativeissues结构匹配到章节，不以bare U01全局猜测。18是writer_issues分组，也对应18个不同问题单元；组内1—6条issue，共数不等于18；不能叫18篇、18任务或36科学通过。

正常DELIVERY_REPORT字段：generation_complete=true/complete_units=36/missing=[]；36row.status=complete；author cache_hit36、quality cache_hit36、当前quality.model_calls全部0。assembly.status=complete/loaded_units36，但assembly.problems_resolved=false，report.assembly_complete=false，status=restricted_draft；每单元scientific_acceptance=false。quality状态19 assessed_pending_human_review+17 assessed_with_pending。历史sealedQUALITY_RESULT.calls分布0×7、1×20、2×2、3×7，合计55，不能将当前cachehit0新调用误称历史核查0调用。交付report top-level.model_calls=1是全文edit等该次动作，不是36quality计数。

谓词边界：full_review_draft:1555–1605分loaded和resolved；review_delivery:824–875/1077–1119允许带诊断的artifactgate；manuscript_delivery:94–141和checker:610–723仍区分checker/science。部分字段名称有歧义、上层“complete”表示artifact存在，容易被摘要报告误读，但现有字段没有把科学验收硬改true。生成完成≠装配加载完成≠质量问题解决≠投稿可用。建议云端保留层级状态并明确展示，不以全局阻断瑕疵或手填success来简化。

## 7. 零新增调用、依赖与性能

### 7.1 已有恢复能力

| 路径 | 源码/历史证据 | 拒绝状态边界 |
|---|---|---|
| 普通author最后完整多envelope | review_unit_writer:2058–2170/3017–3033；AUTHOR_MULTI_ENVELOPE_PROOF，f446的6focused历史测试与实际paidSSE0provider证明 | 保原raw/reject/候选hash，非科学自动通过；仅普通author，不能泛化completion |
| arrangement真实原答免费重导出 | chapter_arrangement:119–252/445–500；ARRANGEMENT_REEXPORT_RECEIPT | 7章原答/旧误拒均保留，root接受用途解释不冒自动科学检测 |
| alias/qualityderivative装配 | full_review_draft:270–453/650–877/1127–1207；ASSEMBLY_ALIAS_PROOF | ordinary cache和1553字真实derivative区别，不能把旧335字候选当成功；pending保留 |
| 正常sealed author/QC cache | legacy_unit_route:671–708；unit_realization:484–493 | 36+36实际命中；不是重新接受旧拒绝或抹去诊断 |

### 7.2 失效传播与可能保持的哈希

典型 CH02:CH02_U03 改任务或来源→自己的messages/quality payload/body身份变化；whole-book RUN_IDENTITY变化又使所有unitstagekey变化（legacy_unit_route:552–613）。该baseline没有证明“只改一unit只重跑一unit”的现成fastpath；不能据此承诺节省比例。其余35个科学内容/messages在未涉及章节共享frame/citationmap时**应可机械比较并保持原内容hash**，但当前包级identity仍可能阻旧stagehit；不能直接绕过。后续装配/selected_body/结论→引言→摘要/编号/TeX均依赖上游正文。parts在真正selectedbody产生前不能安全并发；概念无依赖的unit可并行只是拓扑上的可行性，本轮不改变并发（尤其账本/监督/共享output锁）。

### 7.3 完整输入构成与精确重复

CONTEXT_AND_TIMING.csv及UNIT_FIELD_HASHES统计36套actualtop-levelmessages，每套system+user，system3805字符×36；user prettyJSON共13412121字符、19177668UTF8字节，compactJSON10435496字符。字段compact字符：sources8801577、chapter_tool_materials1239546、paragraph_tasks112081、owner_unit_context90006、chapter_frame85958、other_chapters42471、citation_number_map32947、sibling_units9880、table_tasks6154。pretty与compact不可相加/互换token。无前序正文existing_body字段（只说明这36作者输入的实际构成，不否认QC/补写含原稿）。

1147来源记录；(sourceID,complete-recordJSONhash)共404唯一pair，单请求内完全重复0，跨请求197重复组/743额外出现。来源card深度包含fulltext/abstract_only/abstract_plus_snippets，resolved不等于全文。嵌套A/B/card/locator可能重复语义但本轮只统计原字段与完整hash，不做语义去重。共享来源可能是不同单元必需条件证据，跨请求共享前缀不等于单请求冗余，也不是providercachehit指标。大字段存在开销≠可删。

native saved token估计总5117081、中位157501.5、max232850=CH05:U05，tokenizer字段provided_token_counter；不是本轮重新tokenize。保守reservedinput6026060另含1.12 multiplier+8192framing，totalcontext7500620还含生成容量。providerusage36作者prompt5117441/completion95851/total5213292，是另一个口径；历史usage全项目含897付费stage，不可把作者36条与总prompt42575457相加。实际usage/价格结算不是发票。

### 7.4 耗时与OOM

36作者中28有全部运输端点，8缺失；已记录elapsed合计1566.388s、中位53.742s（逐请求求和非整轮walltime）。request_open_start→response_opened是HTTP连接/返回头延迟，response_opened→stream_complete是流式响应读取尾段；均不等于排队或隔离生成时间。CSV truequeue/isolatedgeneration/parse/assembly留空。TransportEvents保原事件，未用mtime估算吞吐/收益。944gate不是944次物理调用，INVOCATION单独存在不证明dispatch；唯一providerID/settlement分开。

原始完整CUDA异常见runtime两个stderr和ROOT_FORMULA_OOM_FINDING：P0123一次分配900MiB、GPU10GiB/free4.37GiB、allocated3.40GiB/reserved1.07GiB；P0480一次2.44GiB/free5.26GiB、allocated3.57GiB/reserved23.14MiB。两者输出均含5.10GiB allowed限制下的原OOM错误链；5.10GiB是允许上限，不是单次申请量，保完整stderr而非单行剪裁。实际精读batch32、CUDA、max8192MiB、单worker；普通Docling3，禁止与基础GPU并跑。parse_docling_batch._run_formula_batches:120–157已有adaptivehalving、434–450 formula worker=1、518–539 timeout/worker调度。后续恢复证明P0123[30,15,15]/peak6348MiB，不把后续4.96/4.98GiB日志当原5.10GiB。没有当时完整系统RAM/进程峰值/队列监控，不补造。包内原始配置/恢复与首错分别归档。

### 7.5 resume静态I/O

wrapper source_pin:173+、check:207+先读binding/sourcegitpin；主入口重读bookmanifest并build每unitmessages，计算整本identity及unitkey；legacy_unit_route:671–708逐attempt读REQUEST/UNIT_RESULT/RESULT_SEAL/body并hash，合法cachefastreturn；unit_realization:466–510逐QCidentity/seal/body验证cachefastreturn；full_review_draft重新索引catalog/alias/单位sidecar/body并汇总引用/表；parts还读取完整selectedbody/context/sourcecatalog。大JSON和共享source材料会多次被读取，存在结构性I/O开销，但无文件read次数/峰值RAM/parse分段时长观察。不能宣称读一遍或量化加速。**本轮没有执行resume。**

## 8. 后续动态验证建议（本轮未执行）

仅供方案设计：在新独立sandbox、冻结sourceSHA、假provider、临时ledger且严格禁止网络下，建议运行现有局部测试：`python -m pytest tests/upgrade3/test_review_unit_writer_completion.py tests/test_unit_realization.py tests/test_legacy_unit_route.py`。副作用为pytest/cache/临时stage写入；若fixture未隔离可能触及项目，所以云端先审测试fixture和网络stub，不能直接在现有N执行。应新增exact-base/fullrewrite/overlap/拒绝reparse/跨namespace状态测试，但本轮不写修复或构造调用。

出版回归只应对本包副本/独立输出做Pandoc/LaTeX字面@与公式escape样本，可能启动本地编译进程并写aux/log/pdf，仍不得发模型/元数据网络请求；先明确禁外部联网。摘要/引用方案先用保存文字做确定性投影验证，需保持canonical内部provenance和正文hash，不能默认重写模型正文。实际完整流程/resume/reparse/被拒付费格式修复不在本次授权，另行方案批准后再考虑。

## 9. 缺项与停止线

未找到全入口共享accepted/rejected/consumed生命周期单据；没有合法复用的语义等价自动证明、真实被拒后再消费/重复append历史案例；没有独立发票、真实排队/隔离生成/解析/装配耗时、完整当时主机RAM。作者身份缺项。原物理条件拼接和QCquote对应问题仅登记，不展开。600送读、模型、并发、预算、验收尺度本轮均未动。

所有状态判断附source/sourceindex和原件hash；脱敏不是截断科学证据。云端先基于本包定修复范围/验证矩阵；此包不是自动执行任务。本轮上传后停止。

## 10. 根审补充定位与观测口径

scoped selection：outline_selection.build_selection_payload:198、selection_messages:669、validate_selection_response:777、project_selection_group:954；outline_strengthening.strengthening_messages:483、merge_owner_result_into_chapter_payloads:793；outline_strengthening_pipeline.load_complete_packets:114。owner/nested material修复函数的完整行号表在source/RELATED_FIX_FUNCTION_LOCATIONS.json（progressive_review_plan._material_content:174、_owner_cache_projection:257等），并附原始NESTED_MATERIAL_CORRECTION回执。当前S有历史运行验证，R不据此视为已同步。

STAGE_TIMESTAMP_OBSERVATIONS.json仅保存35份历史进程receipt中实际存在的时间字段；started_at不是queue开始，acceptance_at不当作finished_at。当前liveness以2026-10-20 fresh observation为准。出版LATEX_BUILD_REPORT.status实际为failed，虽已有66页PDF；不能把fallback成功或本地交付complete_with_diagnostics误叫无警告编译通过。

原生1657字最终保留证据与原始作者UNIT_BODY可能有既有引用括号规范化差异；根审比较的是被请求追加的quality原稿、completion保留稿与实际assembly derivative，未把不同阶段的字节hash混为同一。
