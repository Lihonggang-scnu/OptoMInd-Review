# A/B无人值守恢复与光学池放行（2026-10-13）

生产源码 e8dbc42f0ab474376540cd7ac9360131c2a17678。历史32并发调用源码/费用见同级 ab-concurrency-20261013。恢复是基础能力，科学内容不由程序填充。

## 本轮修复

- 正式paper_reading_batch默认先免费验证已保存的完整RAW_RESPONSE，不依赖调用者记得恢复开关。验证失败保留错误，不落低质量固定字段，也不买重复回答。
- 明确的regional_dispatch_stopped_before_reservation可有界重试一次；未知/在途费用仍保留，不据超时假定免费。
- 同一output-root的run及会写状态的preflight都持有进程级文件锁，重复进程明确返回batch_already_running；进程退出由OS释放锁。HTTP请求仍32并发。
- 光学桥接器兼容历史读取投影：校验COMMIT全部文件、身份与topic，并用当前原始manifest/blocks/assets/references加原保存reading_packet重算原snapshot hash。真实材料或参考文献更改仍拒绝，旧卡不改写。

## 验证和质量

54项有界恢复/锁控制通过，含跨进程冲突与预览同根拒绝。真实244张卡交接全部通过，其中5张历史投影通过上述证明；改变参考文献的反例仍拒绝，零模型调用。

244卡内容全量扫描无空卡、占位模板、整卡重复及身份串用；独立亲读9张完整A/B，5份完整输入，另核对关键源段。root另亲读两张理论卡。允许的局部模拟/实测归属、FWHM含义等错误单独记录，未将评价或科学答案输入后续生成。

剩余7份历史failed与19份无可读材料保持原状态；规划只消费已验证卡，失败缺口必须显式绑定并记录，不能改成成功或metadata。独立验收JSON与root放行记录附于本目录。

## 使用

正式入口仍 scripts/upgrade3/paper_reading_batch.py；保留同目录、同manifest、同材料、同预算及模型配置恢复。--reparse-saved-responses保留旧调用兼容，免费重校验现已默认执行。成功产物保持缓存复用，既有账本不能清零。

本地光学运行根目录 F:/OptoMind-Review-2/outputs/e2e_optics_20261012_80cny；preparation/ab_unattended_20261013保存逐项证据和下游预览。开发期人工supervision不是产品默认运行前提：审读应使用正式decide接口，不手拼receipt，也不因生产已支持的无损别名误拦。


## 正式保存响应恢复补充

字段兼容使用原文：key_findings内明确的warning/result/observation等内容字段可作为finding别名；冲突仍拒绝，conditions-only不填充。单个显式[finding]标记可无损拆分条件与发现。格式恢复与真实内容质量分开。

正式run_paper_reading_card(saved_response_dir=...)也验证历史投影兼容，严格保留原输入、prompt、runtime和来源。72项相关检查通过。两份真实历史RAW已通过该正式入口免费恢复（一个projection_compatible=true、一个false），原RAW哈希不变，无客户端/账本创建；证据见REAL_SAVED_RECOVERY_CHECK.json。

规划已使用锁定244卡池开始运行。以上两份新恢复派生产物未悄悄改入正在运行的输入，批状态和显式排除回执保持启动时一致，后续按正式恢复合入。


## 运行传输与状态

正式规划现显式使用已有module4流式传输，保持模型、科学prompt和思考/回答容量。异常同步RUN_FAILURE与RUN_STATE.failed，记录安全的异常类型，不再让已退出进程显示in_progress。

仅连接级SSE断流允许沿调用者已有max_retries有界重试；整体deadline/解析/拒绝仍停止。每次未知费用保留，下一次另预留且服从同一封顶。当前开发验收明确physical retries=0，仍不会隐藏自动重试。每次stream invocation使用独立raw文件名，保存旧partial而不覆盖。相关离线34项通过（另28项直接unittest有重叠，不相加）。

此前非流式RemoteDisconnected与一次SSE早断均无最终答案，旧未知占额保留。用户关闭TUN后，V15相同244B/同模型同容量直连135.953秒完整返回，root全文亲审provisional_scope后放行。五个正文知识章节，未插入引言；目前继续level1_tools，尚未完成完整综述。

开发监督运行源码34c333602a09cd18cc51273050b227c1542f31d0；后续连接重试/证据文件保护提交4629d30b见Git历史，未混称已真实触发自动重试。supervised_runtime保存本轮监督接线源码，它是本地验收adapter，产品正常入口仍scripts/upgrade3/progressive_review_plan.py，不依赖人工审读gate。显式failed_exclusions允许冻结244合格卡及7真实失败/19无材料的边界，不篡改原批状态。

残余已知测试：test_legacy_prompt_proves_reuse_but_cannot_hide_current_change在本次与未改34c基线均失败（旧精读兼容会重新执行而非缓存复用），保留记录，不强行放宽真实材料兼容。

## 默认本地供材与检索fallback接缝（续）

受测源码27018c5d13a570d66f743dc7db7531a832d65b9a。V15已在未付款门停止，scope与真实补充成果保留；V16在同目录/同80元项目及300元账户账本恢复。

- 默认local index从当前pool与已保存的真实补充材料自动建立；旧版本索引保留，显式用户index不写。精确snapshot/direct-reading绑定、错配卡隔离，独立可用转述仍接入。建库失败明确保存FAILURE并停止受影响路径，不伪装成本地无材料。
- 已付partial从journal恢复给working_pool/index，未把partial升级成fulfilled。全局scope有效输入合同保持完全相同，免费验证已确认可复用。
- 查询已有效返回2xx时，不因更早的S2限流把OpenAlex成功fallback判成整体outage；原provider_attempts保留。合法空选不会触发这种服务故障重试。全部provider失败仍保留原失败与有界重试。
- 免费实际索引：244基础论文＋1份新全文＝245paper、24769segment，建库3.969秒；三份已读source均可定位，精确快照3/3一致，原pool/journal哈希不变。245份均有可读document blocks，其中摘要仍按摘要深度使用，不能称为245份全文。假judge仅验证I/O，不计为真实科学判断。
- 有界本地控制56通过、1项缺既有fixture排除；root另验默认index6通过（与前项重叠）及补检索22通过，不相加冒充独立全套测试。编译、diff-check通过。

新的真实监督继续level1_tools；不重做已通过构思、不重跑A/B或BODY。ROOT_GAP01_HYBRID_JUDGE_REVIEW记录了正式判断器无需人工提示，自行区分MTF与效率并保留unmet/supporting的实际结果。局部模型概括错误单独留存，不手改科学产物。

## 未完成工具日志的正式消费者修复（V17）

源码091a6a38ae3b1c09a6237e67e4f85e6192f52adb。上节245条索引结果是离线供材验证；V16正式入口实际仍只有244条，未接入尚未完成的当前phase日志中的新增来源。这个差异在真实请求前检查发现，下一未付费门f9d6a211被保留并退役，原成功响应没有删除。

修复现在先恢复当前phase日志中的真实保存材料，再生成本地索引和当前任务兼容关系；同时将保存结果随runner返回交给外层pool merger。旧任务编号及“已满足”判断不复制到新任务，当前判断仍独立执行。跨目录或不存在的派生pool不静默采纳。

正式factory→返回值→外层merger的免费验证得到244→245，外部获取/API/账本写入均0，原pool/journal哈希不变。V17真实入口另已建出245论文、24769片段，三份保存来源均在精确快照目录；第一份实际请求完整亲读并批准，输入为本问题的原文片段、A/B认识及可用综述转述，没有人工科学答案。

当前继续level1_tools，尚未完成全规划或全文。V16两次实际局部判断的响应亲审记录保留，不能与免费fake judge混称。V17绑定、argv和首请求核对见本目录JSON；同目录恢复、原80元项目/300元账户双账与直连策略保持。

## 精读并发接线与材料显示（V18）

独立精读本来已有线程池，但 adaptive retrieval 逐项调用 reader，每次只交一项，实际运行仍是串行。本次在进入有状态的检索循环前，一次派发兼容性检查后仍需执行的精读任务；复用原 reader 的有界线程池，再按原任务顺序收回结果。检索日志、章节归属、材料池合并仍顺序更新，不并发写坏共享状态。同一论文的不同问题独立保存，论文名额只算一次。

正式参数仍是 `--reader-workers`，本次光学续跑显式设为32；同时运行数不会超过实际待执行任务数。开发监督 adapter 同步使用 per-call lock，避免人工审读锁把独立调用重新串行化。两任务并发与缓存复用的免费测试已经通过；这不等于已经实测32份精读同时运行。开发审读耗时与产品调用耗时分别记录。

本地 CodeFormulaV2 的 GPU 队列保持现有显存约束，不跟随远程精读并发启动32份GPU模型；已生成的公式材料继续复用。模型、完整材料、科学提示词和思考/回答容量不因提速降低。

本次同时修复补检索的 deadline 接线：当前调用的超时真正传到检索 transport、分页及等待，S2限流可以转已有OpenAlex fallback；已取得的结果及错误记录保留。旧式注入接口仍兼容，不把不支持新可选参数的 TypeError 当作外部服务故障。

原生XML的 MathML 通过本机已有 Pandoc 按论文批量生成可读数学视图，原始快照、raw XML、ID和定位保留；失败显式保留原表示，不猜公式或表格值。转换不依赖测试题目。已经有效的精读须通过原材料兼容证据复用，不能仅因显示方式变化重新购买。

V17在无在途付款、无运行中的精读claim时停止，保留4份有效精读（其中两份仍是partial）及明确失败记录。退役的未付款门不得再次批准；后续实际版本、PID及费用以V18绑定和运行记录为准。


## 开发监督格式复审（V18续跑）

五章一级纲要经root亲读接受，条件依赖、系统指标与替代设计均保留。路由前三批实际调用重叠，约196/286/278秒；这是source routing并行，不是32份精读并发实测。

本轮开发监督曾将可由原生json_repair读取的第三批响应误判为失败，阻断了生产解析并触发整批修复请求。该请求在付款前停止；原响应、原拒绝、原费用全部保留。新增review_response接口只允许root显式追加格式复审，绑定原request、response和decision哈希，随后同目录/同binding恢复，交原生解析器消费原raw。没有修改路由答案或生产解析算法。第一批确有重复来源handle，已经发生的一次原生修复照实保留，不混称免费。

监督时先检查生产解析能力，再决定是否存在实质质量问题；不得因可免费恢复的引号、围栏、合法别名触发额外付费。这个接口属于开发验收supervisor，产品正常运行不依赖root复审。旧科学拒绝响应不因此获准复用。两项离线检查验证原decision逐字不变、重放无provider调用，以及request/raw/decision变更仍拒绝。

当前执行沿同一生产提交f62ebf48154bdb5b91162963cb0c76d8118b8aa6和V18绑定；重启进程以CURRENT_PROCESS_PLANNING_V18_FORMAT_RESUME.json为准。只有native batch003和完整245路由正式落盘后，才报告分配恢复完成；后续各阶段仍逐I/O审读。
