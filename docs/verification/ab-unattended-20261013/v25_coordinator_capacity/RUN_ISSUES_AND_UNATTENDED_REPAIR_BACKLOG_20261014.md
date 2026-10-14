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
- **实际影响**：五个已保存 chapter packet 和各自批准事实仍保留，但没有产生 `whole_plan_improvement` response；全局协调阶段未完成，后续 arrangement/body 不能因五章已保存而宣称整条链完成。这是原 V24 容量/上下文接缝阻塞，不是对五章内容质量的失败判定。
- **临时处理**：原始 V24 `RUN_FAILURE.json`、`RUN_STATE.json`、五章 packet 和 root review 由人工 freeze 保留；root 核对实际输入组成后，以同一输出/账本、复用兼容阶段和五章响应启动 V25 `--resume`，未重跑已有五章。人工 freeze/restart 是监督处置，不是程序自主恢复。
- **正式能力状态**：`formal_repair_produced_and_used`。source commit `8ded10187c83c23a690f6ef0a88e0e0946d7beb5` 在 whole-plan 消息中只对完全相同的大型 JSON 值做内部 JSON Pointer 共享：首次出现完整保留，后续引用首次位置；不按同 ID 合并不同内容，不裁剪科学条件。6 项新增检查与 77 项既有检查通过；V26 已在真实请求前通过容量门，并取得 root 批准的 coordinator response。容量/响应接缝已在真实运行验证，但 owner 路由仍受 UIR-020 影响，不能据此宣称整条链或无人值守稳健性完好。
- **当前边界**：原失败身份为 source `be0abeaf2ae1bdc1e5a5188b471abb14faec775a`、V24、PID 552，原始超限与人工 freeze/restart 记录继续保留。当前 V26 source 为 `dc1b73aa7d95e6ac4a4277559fdf4b11763ca889`、binding 为 `RUN_BINDING_PLANNING_V26_CHAPTER_CONSUMERS_20261014.json`、生产 PID 29780；V26 coordinator response 已由 root approve，provider elapsed 为 `88.21899999999732s`，且 `scientific_answers_injected=false`。随后 owner 未付款门由 root 安全停止；监督停机与新 binding 仍不能当作自治证明。
- **证据定位**：原 `worktree/outputs/optics_e2e/07_progressive_review_plan_docling_20261013/RUN_STATE.json` 与同目录 `RUN_FAILURE.json` 的超限事实；`preparation/ab_unattended_20261013/coordination_capacity_fix_20261014/ROOT_CAPACITY_PROOF.json`；`worktree/docs/verification/ab-unattended-20261013/v25_coordinator_capacity/README.md` 的修复范围、6+77 免费检查、143 处共享、lossless roundtrip 和容量测量；`execution_sources/planning_resume_v25` 的 commit `8ded10187c83c23a690f6ef0a88e0e0946d7beb5`；`CURRENT_PROCESS_PLANNING_V26.json`、`RUN_BINDING_PLANNING_V26_CHAPTER_CONSUMERS_20261014.json`；`preparation/ab_unattended_20261013/ROOT_V26_COORDINATOR_REQUEST_REVIEW.json` 的 gate `38e897ed441b4263943b4c6d0fdf2842e80c90a8b5c0ebc644e1a1395dc739f5`、root approve、`373656` measured tokens、`426687` estimated input 和 `2.886396` estimated reservation；`preparation/ab_unattended_20261013/ROOT_V26_COORDINATOR_RESPONSE_REVIEW.json` 的 `phase=response`、`decision=approve`、response hash `656152314b10de281b870b2c3a508bdd44e5b9a544915f5bff1216557a5be38c`、`88.21899999999732s` provider elapsed 和 `scientific_answers_injected=false`。
- **raw/付费/拒绝事实**：V24 原记录仍是 `ProgressivePlanError` 的付费前预检退出，不是 provider response 或 provider reject；该 whole-plan attempt 没有 whole-plan model output。修复证明是离线重建：143 处完全相同值共享后，实测消息 token `893042→375371`，保留计量 margin 后 input `428608`、总容量 `477760`，thinking/output 仍为 `16384/32768`，且 lossless roundtrip/scientific payload unchanged。V26 的真实 coordinator response 已 root approve，provider elapsed 为 `88.21899999999732s`，source-sharing 真实调用已验；顶层费用由 `9.3730111` 到 `10.9642751` 的聚合变化按当前 SQL/账本为准，不把该聚合快照拆成单次费用。owner gates 尚未付款，未启动 owner provider，不写成 owner provider reject 或全链质量通过。
- **建议修复/验证**：把 8ded101 的 exact-value pointer sharing 保持为正式能力，并继续拒绝同 ID 异内容合并；保留 V26 coordinator response、解引用一致性、provider elapsed/receipt 和 stage state。继续验证同一五章 source/binding 下不重跑五章、不裁剪条件、不降低 thinking/output；修复并验证 UIR-020 的 known-chapter owner 路由后，再以 root review 和后续状态证据判断整条链，不能把已通过容量/协调 response 门等同于无人值守认证。

### UIR-019：V25 恢复未把已完成的 chapters_tools 接入 closed-stage consumer cache

- **触发**：V25 `--resume` 的 closed/`accepted_tool_cycle_cache` consumer 映射未登记 `chapters_tools`。虽然该阶段已 complete，且五个已保存 chapter packet 已实际消费对应 tool need/material，恢复仍重新进入 query-refinement；未付款 gate `b036fe1f390253bfa3db32743ac8134842d8f92944af6171b78b94928970fc0b` 没有 decision/INVOCATION。
- **实际影响**：恢复路径真实暴露了对已关闭阶段重复进入的风险，并使 V25 在 query-refinement 之前中止；本次没有启动 provider、没有改变账本，也没有新增费用。随后冻结的 V26 复用了前阶段和五章，未重复 query-refinement，直接到 root 批准的 whole-plan request；这证明了本 seam 的真实复用行为，但不等于全无人值守认证或科学协调完成。
- **临时处理**：root 依据 `V25_UNPAID_STOP.json` 安全停止 PID 9376，保留所有结果和预算；未付款 gate 不继续 dispatch。随后以新 V26 source/binding 启动 PID 29780。停机与新 binding 仍是 root 监督操作，不是程序自主恢复。
- **正式能力状态**：`formal_repair_produced_and_used`。commit `dc1b73aa7d95e6ac4a4277559fdf4b11763ca889` 已提交并绑定 V26：要求五章完整有效 packet 与 tool need 的所有非-handle 字段逐项一致；仅在稳定 `paper_id` 且其他来源字段完全相同时允许 handle 重绑定；随后仍由 `_stage` 检查当前输入 contract，新的任务、材料、模型或显式重试使缓存失效。10 新用例 + 既有 13 + coordinator 6 共 29 项，以及真实 16 条 tool rows/5 packets 的免费 proof 通过；V26 真实运行复用前阶段和五章并未重复 query-refinement。变更合同失效已有离线证据，但全无人值守认证仍未完成。
- **当前边界**：V25 的真实 re-entry 和人工 unpaid stop 已发生；`V25_UNPAID_STOP.json` 明确 `provider_started=false`、`ledger_changed=false`。V26 source/binding 为 commit `dc1b73aa7d95e6ac4a4277559fdf4b11763ca889`、`RUN_BINDING_PLANNING_V26_CHAPTER_CONSUMERS_20261014.json`、PID 29780；真实 whole-plan request `38e897...` 已 root approve，记录 `373656` measured tokens、`426687` estimated input、`2.886396` estimated reservation；该 request-review 文件是请求阶段快照，后续 `ROOT_V26_COORDINATOR_RESPONSE_REVIEW.json` 已记录 coordinator response approve。response 与复用行为均不构成 owner 路由完整或自治认证。
- **证据定位**：`preparation/ab_unattended_20261013/coordination_capacity_fix_20261014/V25_REENTERED_CHAPTER_TOOLS_RUN_STATE.json` 的 complete `chapters_tools`/重入状态；同目录 `V25_UNPAID_STOP.json` 的 gate、PID9376、安全停止、无 provider/账本变化；`ROOT_CHAPTER_CONSUMER_CACHE_PROOF.json` 的五 packet hashes、`accepted_chapter_tool_consumer_cache=true` 及 `_stage` 必须再比对当前 effective input contract；`worktree/docs/verification/ab-unattended-20261013/v25_coordinator_capacity/README.md` 的恢复接缝说明、10 项新增检查与 16 条 tool rows/5 packets 免费验证；`execution_sources/planning_resume_v26` 的 commit `dc1b73aa7d95e6ac4a4277559fdf4b11763ca889`；`CURRENT_PROCESS_PLANNING_V26.json`、`RUN_BINDING_PLANNING_V26_CHAPTER_CONSUMERS_20261014.json`；`preparation/ab_unattended_20261013/ROOT_V26_COORDINATOR_REQUEST_REVIEW.json` 的 root approve whole-plan request 与容量测量。
- **raw/付费/拒绝事实**：`b036fe1f390253bfa3db32743ac8134842d8f92944af6171b78b94928970fc0b` 是未付款停止 gate，没有 decision/INVOCATION；`provider_started=false`、`ledger_changed=false`，不归因 provider reject、paid call 或协调 response。V26 request-review 快照中的 `response_sha256=null` 只表示该文件处于请求阶段；后续 coordinator response 已由 root approve，owner 路由另见 UIR-020。无重复 query-refinement call 的复用事实与协调 response 分开记录。
- **建议修复/验证**：保持 dc1b73aa 的 `chapters_tools` consumer contract：完整 packet/tool-need 非-handle equality、稳定 paper identity 下的严格 handle rebind、每次恢复前的 `_stage` 当前输入 contract 检查。验证同一 contract 只复用一次且零新调用，任何任务/材料/模型/显式 retry 改变都失效；V26 已验证真实复用路径并另有 coordinator response review，仍需 owner 路由修复、stage state 与 root review，不能把 root 停机/换 binding 当自治通过。

### UIR-020：coordinator 的 cross_chapter_adjustments 未完整路由到 owner feedback

- **触发**：V26 coordinator 的完整 response 已由 root approve；模型返回 5 条 `cross_chapter_adjustments`，以 `concept`、`primary_chapter`、`re_appearance_value` 给出明确主讲/再现分工。但 owner 路由 `feedback_by_chapter` 只按 `chapter_id/id`，未识别这些 entry；CH1/CH2/CH3 的实际 REQUEST 只收到 scalar `thesis/objective`，5 条职责建议全部丢失。
- **实际影响**：V26 的付费 coordinator 结果保留且不重跑；当时 CH1–CH3 owner 没有收到完整跨章节职责反馈，后续 owner revision 不能视为已执行完整 coordinator 调整。root 在 3 个 owner 未付款 gate 前安全停止 PID 29780；该路径没有 owner provider 或人工科学反馈。V27 修复后，实际新 REQUEST 已分别收到 3/3/5 条原模型角色意见，full A/B、deep、candidate 等材料仍保留；最终 owner 质量仍待审。
- **临时处理**：保留 V26 coordinator raw gate/response、遗漏输入和 unpaid-stop 记录；root 对未付款 gates `d7a157f4e77439d57ee52a57b556ffd9df4549baaefb7301c0ebe72eb5f8331d`、`dc663c95b9c9f40822c14ac2055ef381921641b6a56247f10345942b9ff2059f`、`b716af549208f0d62bf45decb753ae28250c15a433b966d69a9574fe3694b847` 安全停机，不继续 dispatch。随后以 V27 native resume 复用已付 whole-plan 结果且不重付，在 root 审核三份 owner request 后并行推进；无人为 scientific/editorial feedback 注入。
- **正式能力状态**：`formal_repair_produced_and_used`。commit `33a2d2297f94b2659920cb3229b264f5e676a383` 已推送并绑定 V27；通用路由按 known chapter IDs 保留完整原模型条目，5 项新路由测试与 43 项既有 handoff/contract 测试通过。V27 真实 REQUEST 已验证角色意见 3/3/5 条、原字保留及材料保留；unknown/ambiguous 入口的 fail-closed 行为仍按修复合同继续验证，不能把 owner request approve 写成最终 owner 质量通过。
- **当前边界**：这是 V26 已付 coordinator response 后暴露并已修复采用的真实 owner routing defect，不是 coordinator quality rejection。V27 source 为 `33a2d2297f94b2659920cb3229b264f5e676a383`、生产 PID `29964`、binding `RUN_BINDING_PLANNING_V27_COORDINATOR_FEEDBACK_20261014.json`；三份 owner request 已 root approve，但 `ROOT_V27_OWNER_REQUEST_REVIEWS.json` 是 request-phase review，最终 owner response/质量与后续全链无人值守结论仍待验证。
- **证据定位**：V26 原始证据仍见 `preparation/ab_unattended_20261013/ROOT_V26_COORDINATOR_RESPONSE_REVIEW.json`、同目录 `coordination_capacity_fix_20261014/V26_OWNER_FEEDBACK_UNPAID_STOP.json` 及 `ACTUAL_OWNER_CH1/2/3_INPUT.json`；正式采用与真实验证见 `preparation/ab_unattended_20261013/ROOT_V27_OWNER_REQUEST_REVIEWS.json`（CH1/CH2/CH3 `role_entries_delivered=3/3/5`、`manual_feedback_empty=true`）和同目录 `coordination_capacity_fix_20261014/ACTUAL_V27_OWNER_CH1_INPUT.json`、`ACTUAL_V27_OWNER_CH2_INPUT.json`、`ACTUAL_V27_OWNER_CH3_INPUT.json`；输出根目录 `CURRENT_PROCESS_PLANNING_V27.json`、`RUN_BINDING_PLANNING_V27_COORDINATOR_FEEDBACK_20261014.json` 记录 source/binding/PID；`execution_sources/planning_resume_v27` 对应 commit `33a2d2297f94b2659920cb3229b264f5e676a383`。
- **raw/付费/拒绝事实**：V26 coordinator response gate `38e897ed...` 已 root approve，已付结果保留、不重跑；三个 owner gates 是未付款安全停止，`owner_provider_started=false`，没有 owner decision/response/费用，也不是模型拒绝。V27 native resume 复用了已付 whole-plan，未重复付款；三份 owner request review 已 approve，request-phase 的 `response_sha256=null` 不等同 owner response 或质量通过；没有人工 scientific/editorial feedback 注入，也不假定 provider state/idempotency 能力存在。
- **建议修复/验证**：保持 known-chapter 路由的完整原文传递，保留 `concept/primary_chapter/re_appearance_value`、full A/B、deep、candidate 等原始内容，不合并同 ID 异内容、不写科学答案；unknown/ambiguous 条目继续 fail closed/hold 并给出可审计诊断。已用 V27 复用同一 coordinator response 验证 CH1–CH3 收到 3/3/5 条职责意见；下一步核对三份 owner response、stage state、最终 owner 质量与 root review，再判断全链自治边界，保持 prompt/model/budget 不变。

### UIR-021：owner 编辑对象歧义已由 V29 v2 修复，V27/V28 失败事实仍保留

- **触发**：V27 CH3 owner 的 `updated_plan` 只有五个 `substantive_threads`，没有原有效四个 `units`；V28 recovery 仍返回线程布局并猜旧 ID，native 判定 `structural_unit_change_old_units_missing_stable_ids` 与 `unit_id_remap_references_unknown_unit`。根因是 chapter 旧五线程与当前四-unit `chapter_plan` 的编辑对象歧义。
- **实际影响**：V27/V28 的 CH3 修订均未质量采纳，首轮 partial/raw 和原四-unit 计划保留。V29 v2 将 `chapter_plan.units` 设为唯一可编辑对象、旧 chapter 五线程仅作只读背景后，真实 response 返回 `errors=[]`、4 units、12 briefs、20 cases，各 unit 来源集合与原 current plan 相同且条件保留；native 已继续进入 case_groups，无需手改状态。最终是否可交付由完整有效章节及后续加强实际输出/消费者校验决定，当前尚未到 final。P0157/Alvarez 旧错误仍保留为来源事实，未向模型注入纠错答案。
- **临时处理**：V28 在 CH2 未付款门 `1185b6f0ad7810697c9eef47d8bdaacc1a844e0da7c6d83caacdbcf0e9d67e7d` 安全停止 PID `11176`，保留原生状态和已保存的 CH1 case batch，project reserved 为 `0`；v1 付费失败/claim 未改写。随后以独立 v2 source/binding 做一次 V29 recovery，保持完整材料、不手写字段、不重跑 coordinator 或其他 owner。
- **正式能力状态**：`formal_repair_produced_and_used`。commit `8602e1c0cd3273067e773b6a63aa22af3c03d041` 已推送并用于 V29；48 项专项检查与 root 30 项边界检查分别通过，不把两组相加冒充全仓覆盖。root 审阅 V29 request/response，确认同一 current plan/material fields、只读旧线程背景、动态 unit 结构和无人工科学反馈；V29 native response 已 updated 且无 structural errors。该接缝已真实修复，但其他无人值守问题和后续最终交付质量仍未认证。
- **当前边界**：V29 source 为 `8602e1c0cd3273067e773b6a63aa22af3c03d041`、PID `8860`、binding `RUN_BINDING_PLANNING_V29_OWNER_AUTHORITATIVE_INPUT_20261014.json`（SHA256 `0ace86577986b6ec091ba36c70947580bc87289590470aeffbf0799c8115a790`）；gate `5cf3168293b130453da4ef3a3f1bb5d24b95ab27595b4a57ae035bd65da3a002` 的 request/response 均已 root 审阅。V29 response 的 substantive 改动有限，仍记录过宽比较/措辞与 material label 问题供后续 autonomous steps 处理；完整 current plan、case_groups/default 加强和消费者校验仍由 root 继续审。
- **证据定位**：V27 原始事实见 `preparation/ab_unattended_20261013/ROOT_V27_CH3_OWNER_UNRESOLVED.json`、`worktree/outputs/optics_e2e/07_progressive_review_plan_docling_20261013/stages/affected_chapter_revision/CH3.json` 与 `ROOT_V27_OWNER_RESPONSE_REVIEWS.json`；V28 失败与安全干预见同目录 `owner_contract_recovery_20261014/ROOT_V28_REPAIR_RESPONSE_REVIEW.json`、`V28_UNPAID_STOP.json`、`CURRENT_PROCESS_PLANNING_V28.json`；V29 request/response 见 `owner_contract_recovery_20261014/ROOT_V29_REPAIR_REQUEST_REVIEW.json` 与 `ROOT_V29_REPAIR_RESPONSE_REVIEW.json`（elapsed `117.110s`、prompt `229622`、completion `6288`、known repair `0.5347`）；输出根目录 `CURRENT_PROCESS_PLANNING_V29.json`、`RUN_BINDING_PLANNING_V29_OWNER_AUTHORITATIVE_INPUT_20261014.json` 与 `execution_sources/planning_resume_v29`。
- **raw/付费/拒绝事实**：V27/V28 response 在批准 gate 下交给 native validator 但未质量采纳，结构拒绝不是 provider rejection；V28 unpaid stop 未付费且保留状态。V29 request/response 在 gate `5cf3168293...` 下完成，实际 repair 费用 `0.5347` 元，V29 process 继续 native case_groups；预算快照为 project `14.7210831`、account `24.0155579`、unknown `4.5168676`、reserved `0`，按当前 SQL/账本解释，不拆 unsupported 单次金额。没有人工科学改写，原失败和 partial 记录未清零。
- **建议修复/验证**：保持 v2 唯一编辑对象合同：`chapter_plan.units` 权威，旧五线程仅只读背景；动态说明 `cases/ordered_development/paragraph_briefs/relations_limits/substantive_point/transition`，完整保留材料和条件，禁止旧线程猜映射或生成占位字段。继续以 case_groups/default 加强、`post_planning_handoff/load_complete_packets` 的实际任务/材料消费者校验和 root 对 `CURRENT_PLAN` 的审阅判断交付边界；不要把 V29 seam 通过等同全链无人值守认证。

## 历史与当前边界

- V24 `RUN_STATE/RUN_FAILURE` 的 `whole_plan_improvement` 预检退出仍是保留的真实原始阻塞；8ded101 已形成并绑定 V25 的正式容量修复，V26 已通过真实容量门并取得 root 批准的 coordinator response，但 owner 路由仍不完整，五章 chapter_details 的批准/保存或容量/response 证据不能覆盖整条链边界。
- V25 `chapters_tools` consumer cache 重入是新的真实恢复接缝；本次在未付款 gate 被人工安全停止，未形成 provider/账本变化。dc1b73aa 已提交并在 V26 真实复用路径验证，V26 request review 已通过但 root 停机/换 binding 仍属监督操作，不能据此宣称全无人值守认证。
- V26 coordinator response 已 root approve 并保留付费结果，但 owner `feedback_by_chapter` 丢失 5 条 primary/reappearance 职责建议；CH1–CH3 owner 在未付款门前被安全停止。commit `33a2d229` 已形成并在 V27 真实采用，三份 owner REQUEST 已核对收到 3/3/5 条角色意见；最终 owner 质量与全链自治仍 pending。
- V27 CH3 owner response 被 native 合同正确标记为 unresolved：只给五个 `substantive_threads`、缺少原四个 `units`；V28 有界 recovery 仍猜旧 ID，两个失败与 partial/raw 均保留。CH1/CH2/CH4/CH5 已通过，不能把 CH3 partial 自动等同 final 必然阻塞。
- UIR-021 的 v2 唯一编辑对象修复已推 commit `8602e1c0` 并在 V29 source/binding/PID `8860` 真实采用；V29 返回有效四-unit plan、无结构错误并继续 case_groups。最终交付仍取决于完整有效章节、加强输出和消费者校验，其他无人值守风险未认证。
- V23 `query_has_no_searchable_terms` 是已记录的旧故障；V24 使用 `be0abeaf` 的 optional-focus 修复并继续 native execution。
- V7 A/B source-pin mismatch 与 stale `finalized_complete` 是历史 A/B 证据；当前快照的 V24 source/binding 已配对，AB preflight 的 `source_pin_matches=true`。
- `WinError 5` 是零付费、未改 live batch 的离线 synthetic reproduction，不等同于当前 live A/B 已发生并发写失败。
- GROBID service health 与 CPU/parser resume 是可用性证据，不是公式/表格 fidelity 通过；原始 PDF、raw response、付费/拒绝/unknown 记录继续作为审计边界。
- `chapter_details` 的 `shared_outline` 只是背景 handoff 完整性风险；CH1–CH3 gates 已批准，本轮没有证明重复或 chapter quality failure。
- arrangement 模板的 `--key-index` 是执行前入口预检发现；只做了 wrapper/help 检查，不能写成 native arrangement 已失败或被 provider 拒绝。

完整机器可读字段、证据定位、raw/付费/拒绝事实及验证建议见同目录 JSON。后续修复应在新 source-bound worktree/预算账本中单独验证；本登记不在本轮展开大改。
