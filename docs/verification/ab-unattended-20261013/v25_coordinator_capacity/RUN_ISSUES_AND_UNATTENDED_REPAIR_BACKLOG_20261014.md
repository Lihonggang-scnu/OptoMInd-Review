# 本轮运行问题与无人值守修复 backlog（2026-10-14）

本登记只覆盖 `E=F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny`、`S=E\worktree` 和 `U=E\preparation\ab_unattended_20261013`。唯一写入物是本 Markdown 与同名 JSON。未修改源码、运行源、gate、既有产物或账本，未调用模型，也未启动 pipeline。

`EXECUTION_STATE.json` 顶层快照时间为 `2026-10-14T14:23:46.237646+08:00`：V24 以 `be0abeaf2ae1bdc1e5a5188b471abb14faec775a`、`RUN_BINDING_PLANNING_V24_OPTIONAL_FOCUS_20261014.json`、PID 552 继续 native resume，阶段为 `chapters_tools`；快照记录 `paid_calls=1012`、`actual_cny=7.7108251`、`reserved_cny=0`、`uncertain_cny=4.5168676`。这些费用/调用数是历史观察值，当前费用与 paid-call 判定以当前 `budget.sqlite` SQL 为准。快照表示当时运行可继续，不表示无人值守稳健性或科学质量已通过。

状态含义如下：

- **正式修复并真实采用**：当前 source-bound lineage 中有代码修复，并有绑定运行或 native consumer receipt 证明采用；离线验证和生产采用分开写。
- **人工恢复/监督**：root/delegated 停止、恢复、拦截或拒绝使运行能继续；人工科学拦截不算程序自主检查。
- **仅离线**：读检查或合成 probe，没有付费 provider call，也没有生产状态变更。
- **仍未修**：有临时 guard 或保守策略，但通用无人值守能力仍缺失。

## 登记项

### UIR-001：focus、section、chapter ownership 曾被 generic fallback 吞掉

- **触发**：directed read/supplement 带有非空 focus/section/assigned_chapters，adapter 却先走 generic requirement fallback。
- **实际影响**：10 个具体请求在未付费 gate 前变成无章节归属的 generic reading；论文身份和原文仍在，但证据可能送入错误章节或用途。
- **临时处理**：停止 V19 unpaid gate，保留旧 raw/cache，在新的 source-bound namespace 恢复；没有手改科学内容。
- **正式能力状态**：`c6a5f01e` 加入 chapter alias、focus→question、section note 和 task signature；当前 V24 的 `be0abeaf` 包含该 lineage。保存的 harmonized projection 10/10 通过，但该 projection 是离线检查，不是 provider call。
- **证据定位**：`preparation/ab_unattended_20261013/ROOT_V19_FOCUS_REPAIR_ACCEPTANCE.json` 的 problem/change/verification；`worktree/optomind_research/runtime/upgrade3/progressive_review_plan.py:3538-3574,3679-3768`；`EXECUTION_STATE.json.current_execution_v24`。
- **raw/付费/拒绝事实**：验收前后 settled actual 为 CNY 6.6494815、reserved 为 0、uncertain 为 CNY 4.5168676；没有注入科学答案。
- **建议修复/验证**：继续把这些字段作为不可变请求合同，缺少声明的 chapter ownership 时 fail closed；用同时含 focus、section、assigned_chapters、显式问题和 generic reading 的 bound fixture，验证消息字段、归属、task signature 和零 provider call。

### UIR-002：中文或不可检索的 optional focus 使 planner 停止

- **触发**：中文说明、停用词或标点被直接交给本地 FTS，当作可检索 query。
- **实际影响**：V23 在 `read_local_capture -> planning_material_search.search -> fts_query` 以 `query_has_no_searchable_terms` 停止；之前已付费的成功结果仍有效，但阶段无法无人值守前进。
- **临时处理**：保留 V23 failure backup，退休 V23，不自动重启、不改科学答案。V24 仅对 optional focus 捕获该错误，保留原问题候选和既有 partial，并记录 omission。
- **正式能力状态**：`be0abeaf` 已绑定 V24；测试覆盖中文 focus、停用词、标点、空 focus 和 partial 保留，且 main question/low-level FTS 仍严格抛错。V24 已越过此前故障。
- **证据定位**：`preparation/ab_unattended_20261013/DELEGATED_V23_TOOL_IO_SUPERVISION_20261014.md:28`；`query_empty_failure_20261014`；`worktree/optomind_research/runtime/upgrade3/planning_material_triage.py:891-927`；`worktree/tests/upgrade3/test_planning_optional_focus_query.py:13,40-107`；`DELEGATED_V24_TOOL_IO_SUPERVISION_20261014.md:1-13`。
- **raw/付费/拒绝事实**：V23 失败不是付费科学成功，成功项保留且 unknown/ledger 未释放；V24 一次恢复性重复 query 得到 useful partial，bandwidth >100 nm 仍缺失。
- **建议修复/验证**：保持 optional-focus 边界，只吞掉该类 empty query，输出机器可读 omission，不能被下游当成已满足；注入 optional empty、真实 index error 和 partial case，确认只有前者被收敛。

### UIR-003：closed stage 被重新进入并产生多余 outline 费用

- **触发**：adaptive tool collector 在相同 input contract 下重新进入已关闭阶段。
- **实际影响**：重复 outline `d44047f4` 被 unpaid hold，但仍新增不需要的实际费用 CNY 0.0083584（7.0427231→7.0510815）。completed stages、raw 和预算未被科学覆盖。
- **临时处理**：停止 PID 4268，保留新 attempts snapshot，恢复旧 tool snapshot；退休 gate 与 11 个旧 snapshot 仅作历史，没有科学手改。
- **正式能力状态**：`02dce0d4f77580ff6598f092c561304a2ae0c3df` 是正式的 completed-adaptive-stage resume 修复，已在绑定 lineage 采用；其 32 项专项检查、真实 V23 八阶段零新调用复用，以及 V24 九阶段复用均有记录。本条早先多余调用仍是实际费用接缝；尚无证据表明所有未来 changed-contract/stale-marker 路径都有不可变 closed-stage guard。
- **证据定位**：`execution_sources/planning_resume_v23` 的 commit `02dce0d4f77580ff6598f092c561304a2ae0c3df` 中 `docs/verification/ab-unattended-20261013/README.md` 的 32 项检查与八阶段零新调用；`ROOT_V23_CHAPTER_NEED_REQUEST_REVIEW.json` 的 `actual_v23_resume_reused_stages=8`；`EXECUTION_STATE.json.current_execution_v24` 与顶层九阶段 `completed_stages`。
- **raw/付费/拒绝事实**：多余 CNY 0.0083584 与旧 uncertain CNY 4.5168676 分开记录；重复 outline unpaid hold，原 raw 未覆盖。
- **建议修复/验证**：保留正式修复并补齐 dispatch 前的 stage、输入 contract hash、已接收输出 hash、run binding 检查；相同 closed contract 只产生 zero-cost resume event，changed contract 才开新 stage。用同/变更 hash fixture，以及 32 项验收、V23 八阶段零调用、V24 九阶段复用 receipt 验证，不把复用通过当成科学质量通过。

### UIR-004：V23→V24 完全相同的 query refinement 未按内容去重

- **触发**：V24 与已批准 V23 的 system/user/wire body byte-for-byte 相同，仅 call ID、source、binding、pool generation 改变。
- **实际影响**：没有新材料或新科学用途仍开了新的付费调用；回复虽为 useful partial，却遗漏 bandwidth >100 nm，重复付费没有关闭缺口。
- **临时处理**：root/delegated 只批准这一次 V24 recovery duplicate，保留 unmet 条件；后续 exact repeat 应 hold 给 root。
- **正式能力状态**：当前 cache 对 call envelope/source generation 敏感，尚无以完整内容、任务语义和材料世代为 key 的正式 duplicate dedupe。
- **证据定位**：`DELEGATED_V24_TOOL_IO_SUPERVISION_20261014.md:5-13`；`ROOT_OPTICS_VS_GUT_PLANNING_20261014.md` 的重复 query 记录；`EXECUTION_STATE.json.current_execution_v24`。
- **raw/付费/拒绝事实**：V24 response 为 HTTP 200/complete/useful partial，不是 transport reject；没有从 aggregate actual 中虚构单次费用。
- **建议修复/验证**：用完整 wire body、source semantic identity、material generation、task signature、output contract 组成 canonical key；相等时复用 approved response，只有有理由的 override 才付费。不同问题/材料/输出合同仍须可新调用。

### UIR-005：模型 exclusion 的 `handle` alias 未被通用 resolver 识别

- **触发**：模型用 `{handle: ...}` 表示 source exclusion，而 Mapping resolver 只读取 `source_handle`、`paper_id`、`canonical_paper_id`。
- **实际影响**：本轮 exclusion 未生效，程序保守保留材料，避免误删但可能过量纳入、错路由并诱发重复阅读。
- **临时处理**：root 记录 alias contract，保留材料，不手改科学结果；direct read 的局部 handle 分支不能替代通用 exclusion 检查。
- **正式能力状态**：当前 source 仍未在 `_resolve_source_handle` 的 Mapping 分支读取 `handle`；这是仍未修，保守 retention 不是正确执行 exclusion。
- **证据定位**：`ROOT_V22_FINAL_SCOPE_RESPONSE_REVIEW.json` 的 residuals；`worktree/optomind_research/runtime/upgrade3/progressive_review_plan.py:871-875,894-901`；`ROOT_OPTICS_VS_GUT_PLANNING_20261014.md`。
- **raw/付费/拒绝事实**：没有为 alias 修复支付新调用，没有科学答案改写；scope 带残余获批。
- **建议修复/验证**：所有 source list/exclusion/direct read/known paper 统一走 alias normalizer；未知或冲突 alias 产生不付费的可审计 fail-closed 诊断。等价 alias fixture 应只排除目标 source 一次。

### UIR-006：schema 通过但严格科学阈值判断错误

- **触发**：V24 证据写明恰好 1 cm，却被判为满足 `孔径>1cm`；candidate selection 又用 speculative text 和窄带 632.8 nm surrogate 代替 achromatic + measured-efficiency + NA 联合条件。
- **实际影响**：delegated/root 拒绝 triage 与 candidate selection；raw、候选列表和费用保留，没有下游付费读取或手改科学答案，目标缺口仍未满足。
- **临时处理**：监督写入 `response_rejected/provider_retry`，停止把该 response 当 fulfillment；这是人工科学拦截，不是程序自主检查。
- **正式能力状态**：仍未修。JSON 完整和字段解析没有 enforce comparator、联合条件、实测/仿真类型或 speculation 边界。
- **证据定位**：`DELEGATED_V24_TOOL_IO_SUPERVISION_20261014.md:40-46,115-120`；`ROOT_OPTICS_VS_GUT_PLANNING_20261014.md:66`；`ROOT_V24_27F_TRIAGE_REQUEST_REVIEW.json`。
- **raw/付费/拒绝事实**：response 是 HTTP 200/complete 但因语义质量拒绝；candidate response 亦 raw-retained 且未开下游 paid read。
- **建议修复/验证**：加入 units、严格比较符、联合条件、证据类型和不确定性语言的确定性 validator，在 direct_use/selection 前单独给出程序结果。exactly 1 cm fixture 必须不能满足 `>1cm`。

### UIR-007：问题与材料身份/任务语义的对齐仍需人工拦截

- **触发**：问题点名 P0004，但 material_found 给了 P0050；P0187 的任务要求 OPM 定义，而供应材料没有该定义。
- **实际影响**：本轮 native response 保留了身份差异并留下 unresolved，但无人值守 direct-use 若只看主题相似度可能替代论文或把 source-absent 定义当成已答。
- **临时处理**：监督 hold mismatch；focused read 后补到 exact P0004；P0187 只接受 MDL/metalens useful partial，OPM 不手填、不换源。
- **正式能力状态**：source identity pin/handle 已有，但尚无通用的 claim-type 与 expected-source semantic compatibility check。
- **证据定位**：`DELEGATED_V23_TOOL_IO_SUPERVISION_20261014.md:15-18`；`ROOT_V23_CONTINUATION_NOTES.md`；`ROOT_OPTICS_VS_GUT_PLANNING_20261014.md:33-34`。
- **raw/付费/拒绝事实**：P0004 follow-up 是 useful partial，P0227 efficiency 仍缺；P0187 OPM 未解决；没有注入科学修正。
- **建议修复/验证**：每个 triage/read 携带 expected source ID、object、metric、condition 合同；direct_use 前逐项匹配，失败输出 source-mismatch/unmet，不能用 thematic neighbor 替代。用 P0004/P0050 与 source-absent OPM fixture 验证。

### UIR-008：不确定传输费用只能 hold，尚未无人值守收敛

- **触发**：RemoteDisconnected、stream incomplete/error event、read timeout 在 provider invocation 后发生，但没有可用 response 或确定 charge status。
- **实际影响**：当前运行记录仍保留 8 笔 unknown、合计 CNY 4.5168676，并计入 80 CNY project cap；剩余额度仍可用于其他已授权工作。历史 A/B manifest 另有 6 笔 unknown、合计 CNY 0.3121156，其中 batch-relevant RemoteDisconnected 为 CNY 0.0854376；历史 latest transport hold 为 CNY 2.102376。这些记录不可盲加。未知工作不能安全重试，也不能当成功。
- **临时处理**：bounded SSE/raw preservation 保存 call id/partial stream，hold 这 8 笔 unknown，禁止对歧义 work item 自动 retry/re-buy；没有把后续所有 reservation 一律停掉，raw、paid、rejected 事实分别保留。
- **正式能力状态**：传输安全 guard 已真实采用，但 end-to-end reconciliation 仍缺失。只有 provider 官方明确支持 status/idempotency 时才可使用相应能力；本登记不假定 provider 存在该接口。
- **证据定位**：`preparation/throughput_recovery_20261013/AB_RECOVERY_MANIFEST_V7_20261013.json` 的 ledger/uncertain_paid_failures/resume_guard；`EXECUTION_STATE.json.latest_transport_failure` 与费用快照；`worktree/optomind_research/runtime/upgrade3/module4/runtime.py:1245-1271,1310-1354`。
- **raw/付费/拒绝事实**：unknown reservation 保留 invocation-observed/response-unobserved，不算 settled success；usable AB response 与失败/reparse artifacts 分开；当前 unknown 继续占用 cap。
- **建议修复/验证**：若 provider 官方支持，按 call/invocation ID 使用 status 与 provider-recognized idempotency，并以证据完成 unknown→settled/refunded；不支持时保留 unresolved hold 并要求人工决定，不伪造 endpoint。注入断连、partial、timeout、确定性 reject，验证不会盲重试或重复扣费，且无关授权工作仍可在剩余额度内继续。

### UIR-009：`finalized_complete` 与 pending/running 状态冲突

- **触发**：transport stop 后 BATCH_STATE 仍保留 `stop_reason=finalized_complete`，但 pending/running 仍存在，且 final report 缺失。
- **实际影响**：无人值守控制器若信任标记，可能跳过工作、错误宣布完成或未 reconciliation 就 resume。观察到 success 122、reused 5、pending 117、running 3、failed 4、insufficient 19。
- **临时处理**：人工规则要求 pending/running 全空、AB_RUN_REPORT 存在且进程退出；单独的 stale marker 不权威；观察没有改 live state。
- **正式能力状态**：记录中的 batch state machine 仍未修。该条是历史 A/B snapshot，不是当前 V24 的新失败；当前 preflight/source pin 已匹配。
- **证据定位**：`preparation/throughput_recovery_20261013/STATE_OBSERVATION.json`；`AB_RECOVERY_MANIFEST_V7_20261013.json` 的 after_stop；`EXECUTION_STATE.json.ab_preflight` 与 `current_execution_v24`。
- **raw/付费/拒绝事实**：state observation 没有新 paid call；pending/running 和缺失 report 按事实保留，未改成 success。
- **建议修复/验证**：把完成定义为 row states、进程退出、report hash、ledger reconciliation 的派生不变量；载入 stale marker 时先失效并写 reconciliation event。用 stale fixture 验证 fail closed、无重复 reservation。

### UIR-010：同一 A/B output root 的跨进程并发写入不安全（仅离线复现）

- **触发**：两个独立 child process 同时写同一 batch_root/state/aggregate/pool/bridge 路径。
- **实际影响**：离线 probe 在替换 `BATCH_STATE.json` 时真实复现 `PermissionError [WinError 5]`；设计上还存在 last-writer-wins、重复 attempt 和 pool/bridge 覆盖。
- **临时处理**：probe 使用合成本地数据，0 paid transport、未触碰 ledger/live batch；当前运行约束为每个 root 一个 owner，独立实验分 root。
- **正式能力状态**：没有跨进程 owner lock 或 per-workkey lease。本条是离线复现，不声称当前 live A/B 已发生并发写失败。
- **证据定位**：`preparation/ab_concurrency_20261013/IO_AUDIT.md:12,30-37`；同文件 `41-69` 的单 writer、bridge race 和 owner lease 建议。
- **raw/付费/拒绝事实**：probe 记录 `paid_transport_calls=0`、`ledger_touched=false`；没有修改 live card、gate、ledger 或 source。
- **建议修复/验证**：run 与 bridge/pool promotion 共用 owner lease，attempt 前做 per-workkey claim；同 root 双进程应只允许一个 owner、无 WinError/重复 attempt/覆盖，独立 roots 仍可并行。

### UIR-011：完整响应仍可能因 paper-card 格式契约失败

- **触发**：响应有内容但使用 alias，或负面 finding 写在 warning；item 105 把 `[finding]` 放进 conditions 而缺少 finding key。
- **实际影响**：A/B snapshot 有 102 native success、3 native format failures；此前 5 个 saved format failures 通过 free reparse 恢复。余下失败没有被当 material exclusion，也没有付费 retry。
- **临时处理**：INPUT/PROMPT/RAW/USAGE/FAILURE 字节不改；saved-response validator/free replay 只生成派生卡，失败留给 stage-end acceptance。
- **正式能力状态**：alias/negative-finding free recovery 已真实采用，包括 2 个 native free recoveries；其余 semantic/contract failures 表明 versioned contract 和 consumer gate 仍需完善。
- **证据定位**：`preparation/AB_REVIEWER_PROGRESS.md:120-126,205-216`；`PARSER_CONSUMER_COUPLING_20261013.md:23-29`；`ab_saved_reparse_validation/v5_new_format_failures_20261013/INVESTIGATION_RECEIPT.json`；`native_batch_free_recovery_20261013/NATIVE_BATCH_FREE_RECOVERY.json`。
- **raw/付费/拒绝事实**：3 current native failures 与 5 recovered saved failures 分开计数；free replay physical_calls=0，原 reject/raw/input hashes 保持。
- **建议修复/验证**：版本化 output contract，严格 alias/negative normalization/conflict rejection；stage-end 明确 format recovery、scientific reject、evidence success。isolated saved replay 必须 zero network、原 hash 相等、无 paid retry。

### UIR-012：parser consumer binding 曾可能报告错误 source version

- **触发**：A/B 参数读新 parser binding，但 `SupervisedFactory` 固定读旧 binding，request 可被标成旧 source。
- **实际影响**：即使内容正确，card 的 source/binding provenance 不可相信，release 可能混合 lineage。
- **临时处理**：`factory` 接受显式 `binding_path` 并由 A/B 透传；17 项 free tests 通过，未新增模型调用；历史 source-pin mismatch report 停止而未认证。
- **正式能力状态**：显式 binding seam 已在 resumed A/B 使用，当前 preflight `source_pin_matches=true`。仍应把 source/binding hash mismatch 保持为 hard start gate。
- **证据定位**：`preparation/PARSER_RESUME_HANDOFF_20261013.md:21`；`preparation/throughput_recovery_20261013/AB_RUN_REPORT_V7.json` 的 source_pin_mismatch；`EXECUTION_STATE.json.ab_preflight` 与 `current_execution_v24`。
- **raw/付费/拒绝事实**：seam repair 无模型调用、不改既有 card/ledger；V7 report 因 mismatch 未认证，仍是历史证据。
- **建议修复/验证**：所有 factory/client 显式接受 binding object，每个 request/response/report 写 source/binding hash；故意 mismatch 必须在 reservation 前停止。

### UIR-013：Docling worker 崩溃后需 CPU resume，区域不确定性仍存在

- **触发**：两个 native Docling worker 在 repair 前崩溃，部分 PDF regions 返回 uncertain/error。
- **实际影响**：历史 regional/Plus recovery snapshot 记录 38 个 basic PDF snapshot 需 CPU resume，区域为 400 recognized、23 uncertain、14 error、0 pending；这些旧区域计数不代表当前 parser route，只说明若按全成功计数会掩盖公式/表格缺失。
- **临时处理**：历史 V6 使用独立 CPU snapshot，保留 105 native usable cards 与 3 saved failed responses，未新增 paid request；当前采用 local Docling＋local CodeFormulaV2 做定点 deep read，error/uncertain 继续随 reader message 传递。
- **正式能力状态**：crash-resume/raw preservation 已用于 E2E material chain；当前采用路径是 local Docling＋local CodeFormulaV2。region-level unresolved 与 per-region unattended reconciliation 仍是后续候选，不把 error 统一转 recognized。
- **证据定位**：`EXECUTION_STATE.json.local_docling_resume/materials_progress`（历史 crash/CPU/区域计数）；`preparation/PARSER_RESUME_HANDOFF_20261013.md:38-46`；`preparation/PARSER_CONSUMER_COUPLING_20261013.md:9,23`；`preparation/ab_unattended_20261013/PREVIEW_CHECKLIST.json`（当前 docling_basic/local CodeFormulaV2 hook）。
- **raw/付费/拒绝事实**：resume 的 new_paid_requests_started=0；原 PDF、failed response、uncertain/error regions 均保留。
- **建议修复/验证**：候选修复是持久化 per-region lease/retry budget，区分 worker crash 与 content recognition failure，按下游 claim 重要性处理 unresolved region；重要项可用 local CodeFormulaV2 定点增强，不另加全元素完美或统一 block-publication 门槛。注入 worker kill，验证无重复 region call、raw 保留和 deterministic reconciliation，普通解析局部错误仍可见。

### UIR-014：GROBID 健康不保证公式/表格 fidelity

- **触发**：real PDF→TEI 服务健康且 parse complete，但 equation 4 被 flatten，源 PDF page 8 table 没有 table nodes。
- **实际影响**：历史 GROBID parse 虽 health/complete，却 flatten equation 4 且 page 8 无 table node；health/element count 可能误导为完整快照，受影响公式/表格不能直接支撑 quantitative synthesis。
- **临时处理**：保留原 PDF 与独立 TEI；当前采用 local Docling＋local CodeFormulaV2 做定点 formula/table deep read，旧 regional Plus 路径只作历史记录，GROBID 作为 body fallback 等待 claim-level fidelity review。
- **正式能力状态**：service readiness/real parse 是历史可用性证据；当前 local Docling＋local CodeFormulaV2 路径已采用，但 parser fidelity 按 claim 处理，未形成全元素通过；没有因 health 或 element count 替换 checkpoint。
- **证据定位**：`preparation/DOCKER_GROBID_REPAIR.md:42-54`（历史 health/flatten/table）；`EXECUTION_STATE.json.parser_repair`（历史 route）；`preparation/PARSER_CONSUMER_COUPLING_20261013.md:3`；`preparation/ab_unattended_20261013/QUALITY_GATE_README.md:54`（当前 local CodeFormulaV2 hook）。
- **raw/付费/拒绝事实**：validation parse 未替换原 PDF/acquisition checkpoint；parser health 不等于付费科学修正。
- **建议修复/验证**：候选修复是对高重要性公式结构、table cells、caption/page 定位做抽样核对；不确定元素写成 explicit gap，由 local CodeFormulaV2 定点增强，不把普通解析局部错误升级为统一 publication block。固定含分式、根式、表格的 PDF fixture，验证 health-only output 不能静默成为 verified claim。

### UIR-015：supplementary-only material 曾被提升为 fulltext

- **触发**：一份 supplementary PDF 在 A/B material route 中初始被标为 main article/fulltext。
- **实际影响**：读者可能把 SI-only claim 归给主论文并扩大 scope；在 unpaid next gate 前被发现，未产生付费 retry，但暴露了 document-role 可静默失真的风险。
- **临时处理**：停止 owned process，改为 truthful `supplementary_plus_abstract`，明确 main article missing，旧 gate/binding 不重启。
- **正式能力状态**：source-bound material resume 已修复该记录，当前 scope 真实；通用 document-role inference 仍需成为 hard validation。
- **证据定位**：`preparation/AB_REVIEWER_PROGRESS.md:54-64`；`EXECUTION_STATE.json.ab_material_scope_hold`；`preparation/PARSER_RESUME_HANDOFF_20261013.md:9,15,19`。
- **raw/付费/拒绝事实**：该 gate 在 unpaid/unapproved 时停止；SI、abstract 和缺失主文状态保留，既有 unknown 未动。
- **建议修复/验证**：要求 manifest/provider metadata 给出 document role，并将 scope 写入每个 reader packet；main article 不可由 filename/provider assumption 推断。用 main/SI/abstract/metadata fixtures 验证下游 claim limits。

### UIR-016：chapter_details 的 shared_outline 只有全局论证字典，不是完整五章分工列表

- **触发**：当前 `finalize_chapter_scope.json` 的 `response.shared_outline` 只有 `statement`、`review_argument`、source/status；完整五章对象在 `response.chapters`，own chapter/material、相邻标题衔接和全局论证通过 chapter-details/后续 whole coordination 另行供给。
- **实际影响**：这是背景完整性与 consumer contract 风险：arrangement/whole coordination 若把 shared_outline 当成完整分工图，可能重复全局背景或漏掉 handoff。本轮没有证明实际重复，也不宣称 chapter-details 质量失败。
- **临时处理**：CH1、CH2、CH3 的真实 request gates 分别为 `1f215d9d26c76be839472b8a1cb9369e01d157d004b4432ebf65f89d16cfd132`、`bae97f93b8636c0b56c1c23808502a1ea131356483a1fde52e517af0a7030cfa`、`aa1594fe15d60d192432ffdd08d394967a331206b0964aab89962b31d9e3e7ab`，root 已批准继续；full-card materials 仍权威，whole coordination 负责跨章校准，没有手改。
- **正式能力状态**：finalize 已有五个 chapter objects，三个 V24 chapter-details request/response gate 也已批准；shared_outline 本身尚不是完整 division contract，仍待实际 whole consumer 观察，属于背景风险而非当前质量失败。
- **证据定位**：`worktree/outputs/optics_e2e/07_progressive_review_plan_docling_20261013/stages/finalize_chapter_scope.json` 的 `response.chapters`/`response.shared_outline`；`ROOT_V24_CHAPTER_DETAILS_REQUEST_REVIEW.json`；三个 `supervision/calls/<gate>/REQUEST.json` 的 shared_outline＋chapter 输入。
- **raw/付费/拒绝事实**：三个 request gates 均为 approve，response review 是可继续的中间 chapter plan；无 gate rejection、人工科学答案或 final-outline 完成宣称。
- **建议修复/验证**：定义 versioned handoff，将 `global_shared_outline` 与含全部 chapter ID/title、own scope、前后 handoff 的 `chapter_sequence_context` 分开；whole coordinator/arrangement 只校验实际需要的 contract。用五章 fixture 检查每章一次 own scope、全局论证不变成重复 ownership，作为候选 contract check，不作为 publication block。

### UIR-017：development arrangement 模板转发 native parser 不支持的 key-index

- **触发**：兄弟准备文件 `DOWNSTREAM_SUPERVISION_ARGV_20261014.json` 的 arrangement 模板在 wrapper separator 后带 `--key-index 1`，但原生 `scripts/upgrade3/chapter_arrangement.py` 的 `_parser` 没有该参数；该文件 binding 还是旧 V19，root 正生成正确 V24 argv。
- **实际影响**：若照旧模板正式执行，native parser 可能在 arrangement 前拒绝 unknown option；本轮没有正式执行该 arrangement，因此不能说真实编排失败，也没有 provider call、paid reservation 或生产变更。
- **临时处理**：launcher 仅保持 development-only 并做 wrapper/help 检查；root 生成 V24 argv，PlanningTransport 通过 active binding 强制 identity index 1；没有从旧模板启动 provider run。
- **正式能力状态**：这是执行前发现的准备入口 contract 错误，不是 production arrangement failure；最终 V24 argv/parser compatibility 仍需在授权执行前核对。
- **证据定位**：`preparation/downstream_supervision_20261014/DOWNSTREAM_SUPERVISION_ARGV_20261014.json` 的 development-only scope、旧 binding 与 arrangement templates；`worktree/scripts/upgrade3/chapter_arrangement.py` `_parser` 约 59–90 行（有 `--key-file`/`--run`，无 `--key-index`）；`preparation/downstream_supervision_20261014/VERIFY_20261014.json` 的 wrapper/help、network=0、paid_calls=0。
- **raw/付费/拒绝事实**：没有从该模板执行 native arrangement provider request；不归因 arrangement charge、reject、output、gate 或 ledger mutation。实际事实只有离线 argv/parser mismatch。
- **建议修复/验证**：从 active V24 binding 与 native parser contract 生成最终 arrangement argv，去掉 unsupported option，保留由 PlanningTransport/binding 正式强制的 identity index 1；加 `--run` 前完整 forwarded argv parse/help 检查。

### UIR-018：五章 chapter_details 保存后 whole-plan improvement 在付费前预检退出

- **触发**：V24 下 CH1–CH5 的 `chapter_details` 均已由 root 批准并保存后，流程进入 `whole_plan_improvement`；付费调用前的 planner context 预检以 `input=1069227`、`output=32768`、`thinking=16384` 退出，`RUN_STATE` 进入 `failed`。
- **实际影响**：五个已保存 chapter packet 和各自批准事实仍保留，但没有产生 `whole_plan_improvement` response；全局协调阶段未完成，后续 arrangement/body 不能因五章已保存而宣称整条链完成。这是当前容量/上下文接缝阻塞，不是对五章内容质量的失败判定。
- **临时处理**：保留初始 `RUN_FAILURE.json`、`RUN_STATE.json`、五章 packet 和 root review；root 正在核对实际输入组成。当前不绕过模型容量，不降低配置的 thinking/output，不删科学条件，不手写协调答案，也不重跑已有五章。
- **正式能力状态**：`still_unfixed`。当前预检能把超限暴露为 `ProgressivePlanError`，但尚无已生产采用的 whole-plan 上下文组成/容量治理修复，也没有 root 后续修复 receipt；预检能退出不等于无人值守链路已恢复。
- **当前边界**：失败运行的身份记录为 source `be0abeaf2ae1bdc1e5a5188b471abb14faec775a`、V24、生产 PID 552；`RUN_STATE/RUN_FAILURE` 是该 whole-plan 失败的直接证据。顶层 `EXECUTION_STATE` 曾在并发 chapter worker 期间显示 `chapter_details`，不能用它覆盖这次已记录的 whole-plan 失败。五章上游批准不等于全局协调完成。
- **证据定位**：`worktree/outputs/optics_e2e/07_progressive_review_plan_docling_20261013/RUN_STATE.json` 的 `status=failed`、`current_stage=whole_plan_improvement`、`last_error=planner_context_preflight_exceeded:whole_plan_improvement:input=1069227:output=32768:thinking=16384`、`stage_outputs=null`；同目录 `RUN_FAILURE.json` 的 `ProgressivePlanError` traceback；`preparation/ab_unattended_20261013/ROOT_V24_CHAPTER_DETAILS_REQUEST_REVIEW.json` 与 `ROOT_V24_CHAPTER_DETAILS_RESPONSE_REVIEW.json` 的 CH1–CH5 approve rows/gates；`worktree/outputs/optics_e2e/07_progressive_review_plan_docling_20261013/stages/chapters/CH1.json` 至 `CH5.json` 的保存 packet；`preparation/ab_unattended_20261013/PLANNING_V24_LAUNCH.json` 的 source/binding/PID 记录。
- **raw/付费/拒绝事实**：这是 `ProgressivePlanError` 的付费前预检退出，不是 provider response 或 provider reject；按初始失败记录，本次 whole-plan attempt 未进入 paid reservation/provider call，也没有 whole-plan model output。既有 chapter paid calls、raw packets 和批准事实保持独立，不能合并成 whole-plan 成功。
- **建议修复/验证**：后续正式修复先对五章输入做可审计的组成分解，随后设计保留科学条件与配置的 thinking/output 的容量安全协调协议（可包含有证据的分段/hand-off/复用，但不得静默剪裁条件或改写答案），并在付费前输出可行动的 deterministic preflight receipt。用同一五章已保存 fixture、同一 source/binding 重放离线预检，验证组成报告、零付费失败路径、既有五章不重跑；修复 receipt 完成后再做一次授权生产验证。

## 历史与当前边界

- `RUN_STATE/RUN_FAILURE` 当前记录的 `whole_plan_improvement` 预检退出是本轮仍未修的真实阻塞；在 root 提供修复证据前，五章 chapter_details 的批准/保存不能覆盖该状态。
- V23 `query_has_no_searchable_terms` 是已记录的旧故障；V24 使用 `be0abeaf` 的 optional-focus 修复并继续 native execution。
- V7 A/B source-pin mismatch 与 stale `finalized_complete` 是历史 A/B 证据；当前快照的 V24 source/binding 已配对，AB preflight 的 `source_pin_matches=true`。
- `WinError 5` 是零付费、未改 live batch 的离线 synthetic reproduction，不等同于当前 live A/B 已发生并发写失败。
- GROBID service health 与 CPU/parser resume 是可用性证据，不是公式/表格 fidelity 通过；原始 PDF、raw response、付费/拒绝/unknown 记录继续作为审计边界。
- `chapter_details` 的 `shared_outline` 只是背景 handoff 完整性风险；CH1–CH3 gates 已批准，本轮没有证明重复或 chapter quality failure。
- arrangement 模板的 `--key-index` 是执行前入口预检发现；只做了 wrapper/help 检查，不能写成 native arrangement 已失败或被 provider 拒绝。

完整机器可读字段、证据定位、raw/付费/拒绝事实及验证建议见同目录 JSON。后续修复应在新 source-bound worktree/预算账本中单独验证；本登记不在本轮展开大改。
