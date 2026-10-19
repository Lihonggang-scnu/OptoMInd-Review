# 超表面光学成像 E2E 事故与恢复记录

**原记录生成：** `2026-10-18T23:25:01.974486+08:00`（本字段来自 `datetime.now(ZoneInfo(Asia/Shanghai))`，不是最终 receipt 时间）  
**本版追加核对：** `2026-10-19T13:22:23.498257+08:00`（仅文档修订）  
**最终截止：** `2026-10-18 19:05:07.977191 +08:00`；原始 receipt 为 `2026-10-18T11:05:07.977191+00:00`。  
**项目：** `e2e_metasurface_imaging_20261016`  
**最终源码：** `0133380ea82068ec8d9c28269bf9d0417d38bb04`  
**最终 binding SHA：** `b4e9f56f191f2d4e1de211db08f0f60a221f42c0068005776c6382b5e0cdd2ec`

本记录只整理本地已读 receipt、proof、正式报告和少量相关源码指针；不读 credentials/SQLite/整批论文全文/完整 REQUEST 或 raw response，不重新运行测试，不调用研究模型、不启动测试。用户已要求上传脱敏记录；当前待提供云端目的地，尚未上传。结构化问题与证据 hash 见同目录 `ISSUE_REGISTER.json`、`EVIDENCE_INDEX.json`。

## 截止状态与计数口径

最终状态是 `complete_with_diagnostics_local_review_draft`。交付为 66 页、7 章、36 BODY units、9 张表、199 条参考文献，PDF SHA-256 为 `ecc410afaac21bd6215318b9c1a8fb99e317d239d7be5b313774343b1e7848f0`。PDF 存在且 XeLaTeX returncode 为 0，但 `LATEX_BUILD_REPORT.json` 仍为 `failed`，未定义引用为 `532`、`635`，submission blockers 为 `latex_or_pdf_validation_failed` 与 `author_metadata_pending`。波长中的 `@` 被 natbib 误识别为引文，p47 表 9 与 p51 有实际可见问号，不是“仅日志警告”。第 40–41 页三处数学 literal fallback 各含 U+0008；历史 FF 已在输入阶段归一化为换行；fallback 控制字符不应记为 FF。`scientific_allclear=false` 是验收状态，不是待自动修复的普通 bug；public release 未开启。

费用按最终 receipt 快照：项目 cap 110，settled 100.821423，uncertain 0.1086312，reserved 0，可用 9.0699458；账户 cap 300，settled 158.6437595，uncertain 4.6254988，可用 136.7307417。项目 897 条 settled rows 不是 897 次物理调用；账户累计是跨任务口径，不能与项目金额相加。历史 `.885416`、`.462222` 等费用只能按各自 gate/call receipt 解释。

材料计数也分层：1715 初检 → 600 准入 → 548 基础 A/B（160 旧 + 388 新）；补材工作池 555 不是基础卡数。7 章有 37 个上层线索但最终是 36 units；V24 是 18 个 case batches（12 accepted、6 rejected/failed），124 是 selected contributions，107 是 unique source handles。共享 chapter list、A/B 卡行不能再次相加。

## 真实阶段时间线与本地修复对照

|阶段|已证事实/时间|源码或 receipt 指针|状态边界|
|---|---|---|---|
|M1–M3|1715 初检、600 准入、548 基础 A/B；M3 `gain_below_floor` 停止|`supervision/ROOT_M3_REVIEW.json`; early PDF guard `4c1a8e3…`|筛选/停止，不等于全池科学读完|
|V13|22 groups/6 targets，nested results=0、material records=0；付款前停止|`supervision/ROOT_V13_STOP_DIRECTED_HANDOFF_EMPTY.json`|空交接，不能当 provider failure 或成功|
|V19–V20|V19 两个 repair call 合计 .885416；V20 发现 P0395 A/B 已在 `candidate_materials[7]`，纠正 receipt `new_calls=0`；9d382… 的 .462222/191.313 s 是同一历史失败 repair|`ROOT_V19_LIVE_REPAIR_RESULT.json` (source `09e93c7…`); `ROOT_V20_NESTED_MATERIAL_CORRECTION_AND_ORIGINAL_ACCEPTANCE.json`|旧 raw/reject/费用保留；按不同call/gate归因，历史子项不再加到最终累计|
|V24|18 case batches：12 accepted/6 failed；124 用途贡献、107 unique handles|`ROOT_V24_CASE_STAGE_PARTIAL_ACCEPTANCE.json`; `HISTORICAL_AB_COUNTS.json`|计数语义，不是 124/107 次模型调用|
|V25|selection bare-ID collision；源 commit `eb153f87…`；58 targeted tests；+30 预算绑定为 110（账户 300）|`ROOT_V25_SELECTION_IDENTITY_CODE_ACCEPTANCE.json`; `preparation/budget110_v26/*`|scoped ID 已是通用修复；预算是 binding/guard，不是全局生产重写|
|V28|多 envelope 通用代码 commit `f4461637…` + 一次免费恢复；PID22544 期间存在 live/inflight 快照|`ROOT_V28_NATIVE_MULTI_ENVELOPE_RECOVERY_ACCEPTANCE.json`; V28 live/progress receipts|两层恢复；旧 rejection/pending 保留，不能将 snapshot 当最终退出|
|V29|assembly numeric alias resolver commit `002067c…`，P157/P206 显式映射 P0157/P0206|`ROOT_V28_EXPLICIT_NUMERIC_ALIAS_IDENTITY_PROOF.json`; `ROOT_V29_ASSEMBLY_ALIAS_CODE_ACCEPTANCE.json`|只修 assembly provenance，raw diagnostics/body/QC/seals 不改|
|V31|parts guard 将 finite material/context 上限提高；实际估算 263789 < allowance 950848|`ROOT_V31_FORMAL_PARTS_PREVIEW_ACCEPTANCE.json`; `ROOT_V31_PARTS_CONTEXT_GUARD_ACCEPTANCE.json` (source `6865eb…`)|完整语义/wire hash 保留；不改 prompt 科学材料|
|V32–V34|refs 201→199（source `42e97ad…`）；delimiter（source `f1876f…`）；literal fallback（final `0133380…`）|对应 preparation receipts、`ROOT_V34_FINAL_LOCAL_DELIVERY_ACCEPTANCE.json`|PDF 可生成仍不等于 publication/science green|
|最终|receipt `11:05:07.977191+00:00` = 上海 `19:05:07.977191+08:00`|`ROOT_V34_FINAL_LOCAL_DELIVERY_ACCEPTANCE.json`|当前进程 exited，fresh CIM absent；无由本次文档任务引起的重启|

耗时只在 receipt 明确给出时引用；未单独统计的阶段不从跨日 wall-clock 推导模型耗时。commit 对照只写实际指针；没有证明的时间/commit 标为未知。

## 验证范围（不合并重叠计数）

V25 selection identity receipt 报告 58 个 targeted tests；budget110 receipt 报告 3 个 focused worker tests；V32 refs/parts 相关 proof 报告 17 个 focused tests；V33 renderer proof 报告 8 个 focused tests；V34 literal-math receipt 报告 12 个 focused tests。这里不重跑这些测试，也不把它们相加为一个覆盖率；每个数字只代表对应 proof 的范围。V25/V28/V29/V31/V34 的免费或本地 proof 中的 0-call 只适用于相应阶段，不抹除历史真实模型调用。

## 关键因果实录

### E2E-018 / E2E-019：P0395 误判与 repair 费用分界
V19 的停止和六章缓存保留是真实边界；但 V20 全读证明原 CH06 batch2 request 的 `candidate_navigation.candidate_materials[7]` 已有完整 A/B，原先“缺材”是 registry/root review 错误。原答在严格免费恢复后为 `accepted_partial`，不是 final chapter。V19 两个 repair call 为 0.440152 + 0.445264 = .885416；V20 纠正 receipt 中 9d382… 失败 repair 为 .462222、191.313 s，且 V20 `new_calls=0`。这三项事实保留，不能把 .462222 说成 V20 又买一次，也三项repair由不同call/gate标识区分，但都是项目总费用中的历史子项，不能再加到最终累计。root 承担误判，旧 reject/raw/fee 不改。
**证据：** `E31,E32,E33`

### E2E-049：V25 selection identity collision
qualified refs 36 与 unique bare IDs 27 的投影中出现 `selection_unit_id_duplicate:U01`，下层 selected/readonly bare IDs 和 legacy readonly refs 也有碰撞风险。`eb153f87…` 的修复保留 chapter namespace 和 legacy readonly IDs；7 chapter plans、科学内容、12 accepted/6 rejected case caches 均保留。root receipt 记录 58 targeted tests；隔离 proof 记录 0 physical calls。scoped identity 是已经完成的通用修复，不是未来要另建的系统计划。
**证据：** `E60–E64`

### E2E-050：+30 项目预算与 hardcoded-80 guard
用户显式将项目 cap 从 80 加到 110，账户 cap 仍 300。extension receipt 记录 reservation rows 795、前后 hash 相同、account cap unchanged、extension 期间 no provider calls；native acceptance 记录 3 个 focused worker tests、0 provider/0 reservations。该证据证明 binding/preparation guard 对 110 生效，不证明全局 production budget 被改写，也不把 +30 授权本身当作已消费。
**证据：** `E65,E66`

### E2E-051：owner 实际发送材料与报告投影
早期 owner report 实际把全池预解析/未发送行虚称为已读来源；d23acb55… 将 `owner_material_resolution` 改为在 final supplied channels 上重算，pool-only P0003 留在 unresolved，supplied P0002 才进入报告，owner cache projection 不变。某些历史说法中的“323/191 条已读”没有从最终 receipt 独立证实；V25 receipt 的 323.032 是 elapsed seconds，不能改写成 row count。该修复纠正报告投影，不补任何科学来源。
**证据：** `E67–E69,E79,E82–E84`

### E2E-052：directed claim 未闭合
五个 directed gate 缺少 `REQUEST_DECISION/INVOCATION/RESPONSE/INVOCATION_ERROR`。attestation 明确 `_wait(request)` 在 INVOCATION 之前，marker 之后才进入 physical client call；无 decision/marker 不代表已 dispatch，也没有 fee release/ledger change。V13 的 22 groups/6 targets/0 nested results/0 material records 在付款前停止。原始任务签名、Docling config projection、stable candidate ID 必须作为边界证据，不能以缺 marker 触发 restart 或 rebuy。
**证据：** `E70–E72`

### E2E-053：监督漏 decision 与 inflight/exit 误报
V28 live receipt 仍见 PID22544 和 native rows，随后 progress receipts 记录 partial/pending；最终 freshness check 才证明 process exited。旧 snapshot 的误报没有被证实为重启原因，实际没有因本问题重启。今后需要 fresh CIM/process observation 和显式 decision marker；旧状态文件继续作为历史证据。
**证据：** `E72–E75`

### E2E-054：case shared-list 过计
V24 的 124 是 selected contributions，107 是 unique source handles，另有 18 个 case batches（12 accepted/6 rejected）。shared chapter list、A/B card row、canonical handles 是不同语义；canonical 124/107 不可与 chapter_results 或 7/36 结构相加，也不直接代表物理调用数。
**证据：** `E26,E76,E77`

### E2E-026 / E2E-034：BODY pending 与 U04 derivative
CH01 的两个 candidate quality 项保持 pending/`quality_task_unverified`；U04 的原始 335 字 rejection 仍是失败历史，只有真实 1553 字 sealed derivative 可以进入装配。ordinary cache、candidate、quality derivative、article edit 不能互换；历史 QC `model_calls=1/2/3` 必须原样保留。
**证据：** `E04,E21,E23,E25`

### E2E-028：多 JSON 两层恢复
V28 不是单一一次性手工恢复：source commit `f4461637…` 先提供通用 multi-envelope 代码，之后另有 0-call free recovery 选择完整实际 envelope。旧 rejection/paid history、raw/body hash 均保留；这不等于 scientific acceptance。
**证据：** `E25,E80`

### E2E-038 / E2E-039 / E2E-040：出版残余
三处 fallback span 的实际非空控制字符是 U+0008；U+000C=0，FF 历史已被换行归一化。native compile returncode=0/PDF 66 页仍不能清除 532/635 undefined citation、p47/p51 问号或 author_metadata_pending。author metadata 是缺输入/投稿条件，系统不自动补事实。
**证据：** `E08,E02,E07`

## 全量条目索引（不是因果缺陷计数）

本版共58个结构化条目：incident=39、acceptance_boundary=10、control=5、recommendation=1、user_requirement=3；不是58个独立代码缺陷。更新日志和重复引用证据不另计条目。055–057是明确待办，尚未实施。

- **E2E-001 [incident]** `scope/M1-M3/范围与选择`；自动范围修复不能替代用户范围；改为根验收后的显式交接。 状态：closed_guarded；证据：E01, E45。
- **E2E-002 [acceptance_boundary]** `M3/候选停止`；初始M3在gain_below_floor下只选出169篇；用户因A/B备选过少明确要求最低送读600篇。免费修复后实际选出600篇（169旧+431新），后续基础A/B验收为548篇（160旧+388新），不凑600篇科学卡。 状态：generic_fixed_with_diagnostics；证据：E01, E45, E96, C04, C05, C06, C07。
- **E2E-003 [incident]** `材料解析/解析与缓存`；native parse 非零停止；记录保留，具体底层解析原因未从 receipt 证明。 状态：recovered_once；证据：E03, E46。
- **E2E-004 [incident]** `材料获取/PDF身份`；同一无障碍指南 PDF 被声明为两个研究 paper；下载后身份校验拒绝。 状态：generic_fixed；证据：E01, E46。
- **E2E-005 [incident]** `AB/科学来源边界`；FDTD 示例与另一器件实测被 A 合并；原答复排除且不进 planning。 状态：manual_rejection_preserved；证据：E01, E47。
- **E2E-006 [acceptance_boundary]** `AB/M3/阅读深度边界`；候选池分层且未逐一根读全部全文；不能把卡片/HTTP当科学验收。 状态：accepted_with_boundary；证据：E01, E45, E47。
- **E2E-007 [incident]** `planning/本地公式增强`；P0123/P0480 有全文仍因 5.10GiB allocator OOM，非材料缺失。 状态：bounded_recovery；证据：E01, E48。
- **E2E-008 [incident]** `planning/审判响应解析`；语义已批准但 judge JSON parse 失败；保存付费响应，decoder/sidecar 零调用复核。 状态：replayed_zero_call；证据：E01, E49。
- **E2E-009 [incident]** `planning/策略补丁`；scope/policy 补丁不得改写原科学输入；仅采用显式且对齐的工具约束。 状态：closed_guarded；证据：E01, E42。
- **E2E-010 [incident]** `V10/索引与投影`；多语索引修复保留身份，但监督投影有 replacement/mojibake；未证明 source bytes 同步损坏。 状态：accepted_with_diagnostics；证据：E01, E42。
- **E2E-011 [acceptance_boundary]** `V9-V10/语义拒绝与重试`；科学拒绝、模型误归因、provider failure 必须分态；同输入不自动重买。 状态：protected_boundary；证据：E01, E42, E43。
- **E2E-012 [incident]** `V5-V13/缓存/重买`；managed path/sidecar changes once made a byte-identical request look new; that path seam has a guard, but a unified scientific-rejection/cross-namespace rebuy prevention is still unproven. 状态：generic_fixed_with_diagnostics；证据：E01, E40, E41, E44。
- **E2E-013 [incident]** `V13/directed handoff`；22 groups/6 targets 但 nested results=0、material_records=0；付款前停止。 状态：recovered_once；证据：E39, E40。
- **E2E-014 [incident]** `V14/source identity`；P0480 内容被归到 P0006，P0123 被判 unavailable；拒绝章节并保留 raw。 状态：manual_rejection_preserved；证据：E01, E38。
- **E2E-015 [incident]** `V15/chapter quality`；CH01-CH03 generic、缺 P00468、方法/场景/来源混用。 状态：manual_rejection_preserved；证据：E01, E37。
- **E2E-016 [incident]** `V16/chapter source binding`；不同 paper 的波段/器件/效率被拼接，实际 source bindings 却正确。 状态：manual_rejection_preserved；证据：E01, E36。
- **E2E-017 [incident]** `V17-V18/partial batch contract`；source-local batch 生成 full-chapter 形态并带未供应 handles。 状态：generic_fixed；证据：E34, E35。
- **E2E-018 [incident]** `V19/native failure boundary`；V19 P0395 failure stopped locally while six chapter caches remained; V20 later proved complete A/B was already in candidate_materials[7] and corrected the historical missing-material interpretation. 状态：generic_fixed；证据：E32, E33。
- **E2E-019 [incident]** `V19-V20/bounded repair`；V19 two repair calls totalled .885416; gate 9d382… separately records .462222/191.313 s; V20 correction itself had 0 new calls, so no unproven fee addition. 状态：recovered_with_cost；证据：E31, E32。
- **E2E-020 [incident]** `V21-V22/late closure/owner projection`；付款前 chapter map 为空；resolved_from_current_pool 无 substance；CH06 32→30 计数记录修正。 状态：generic_fixed_with_diagnostics；证据：E50, E51, E52。
- **E2E-021 [acceptance_boundary]** `V23/chapter metadata hold`；CH06 6 units/30 cases/18 tasks 原结构保持，routing metadata 仍 hold。 状态：accepted_with_diagnostics；证据：E53。
- **E2E-022 [incident]** `V24/case enrichment`；12 accepted/6 failed batches、107 unique handles；失败含 source conflation、unsupported numeric、coverage/strictness。 状态：accepted_partial；证据：E26, E27, E28。
- **E2E-023 [incident]** `V25/scope over-rejection`；CH03/CH04 合法跨波段方法例被当作绝对 scope violation，根重读后接受原 raw。 状态：manual_reconsideration；证据：E19, E29。
- **E2E-024 [acceptance_boundary]** `V25/outline handoff`；7 chapters/36 units handoff accepted，但 classification 是 initial_draft，不是 submission final。 状态：accepted_initial_draft；证据：E19, E30。
- **E2E-025 [incident]** `V25/arrangement reexport`；由 7 个实际 raw response formal reexport/body_ready，0 calls；不是手造 manifest。 状态：recovered_zero_call；证据：E03, E20。
- **E2E-026 [incident]** `V26/BODY quality contract`；U02 混合两组条件；U03 QC false covered；U04 335 字且缺 P02/P03。 状态：accepted_with_diagnostics；证据：E21, E23, E24, E87, E88, E89, E90, E91, E92, E93, E94。
- **E2E-027 [acceptance_boundary]** `V27/quality reparse`；第二次 quality diagnosis 仍认可条件混用/矛盾焦距；保留 strict rejection，scientific allclear=false。 状态：protected_pending；证据：E01, E23, E24。
- **E2E-028 [incident]** `V28/multi-envelope`；V28 multi-envelope handling has a general source implementation at f446… plus a separate free recovery selecting the complete actual envelope; old history and hashes were preserved. 状态：generic_format_fix_plus_run_specific_recovery；证据：E25, E91, E92。
- **E2E-029 [incident]** `V28-V30/supervision status`；partial/complete/diagnostic、stale status 与缺 decision 曾混报；最终分开 generation/assembly/quality/article/science。 状态：generic_fixed_with_diagnostics；证据：E01, E15, E17。
- **E2E-030 [incident]** `V28-V29/numeric citation alias`；P157/P206 只在 unit-scoped、DOI/paper_id 唯一匹配时映射 P0157/P0206；raw diagnostics 不改。 状态：generic_fixed；证据：E16, E17, E18。
- **E2E-031 [incident]** `V30/derivative alias provenance`；quality/format derivative 只能通过同 unit/attempt ancestor、hash、body/result seal 读取原 sidecar。 状态：generic_fixed；证据：E16, E17。
- **E2E-032 [incident]** `V30/BODY article edit`；selected body 来自 article_edit，model_calls=1；numbering 0 call，scientific acceptance 仍 false。 状态：accepted_with_diagnostics；证据：E04, E15。
- **E2E-033 [incident]** `V30-V31/post-BODY capacity`；旧字符 guard 拦截 73 records；实际 token estimate 263789 < allowance 950848。 状态：generic_fixed；证据：E13, E14。
- **E2E-034 [incident]** `V28/补写整段重写与拒绝SSE安全重放`；CH02:CH02_U03的唯一补写以status=appended返回整段重写。监督拒绝采用，原生保留1657字原稿并assessed_with_pending/changedfalse；run_unit_completion尚缺通用全稿重写/overlap guard。 状态：contained_production_guard_unfixed；证据：E85, E88, E90, E91, E92。
- **E2E-035 [incident]** `V31/checker response`；首轮 paid checker invalid JSON；完整 raw 保存，0 proposals adopted，用户拒绝付费格式 repair。 状态：unpaid_stop；证据：E11, E12。
- **E2E-036 [incident]** `V32/publication references`；bibliography scope 修复 201→199；reader SHA/科学正文不变。 状态：generic_fixed；证据：E09, E55。
- **E2E-037 [incident]** `V33/TeX delimiter`；renderer 的 \cdotn 命令/变量未分隔；只修 delimiter，旧 compile failure 保留。 状态：generic_fixed；证据：E10, E55。
- **E2E-038 [incident]** `V34/控制字符数学`；3 个数学定界 span 各含 U+0008；U+000C=0，历史 FF 已归一化为换行；literal fallback 记录 offset/hash，不猜测公式。 状态：generic_fixed_with_literal_fallback；证据：E08, E55。
- **E2E-039 [incident]** `V34/波长@引文`；natbib 把波长 @ 识别成 citation，undefined 532/635；p47 表9、p51 有可见问号。 状态：final_residual；证据：E02, E07, E11。
- **E2E-040 [acceptance_boundary]** `V34/作者元数据`；author_metadata_pending 保留；PDF 存在不代表 submission-ready。 状态：final_residual；证据：E02, E07。
- **E2E-041 [acceptance_boundary]** `V34/PDF/交付判定`；DELIVERY complete、XeLaTeX returncode 0、PDF 66页与 LATEX_BUILD_REPORT failed 可同时成立。 状态：accepted_with_diagnostics；证据：E01, E02, E06, E07。
- **E2E-042 [control]** `全链路/监督声明边界`；897 settled rows 不是 897 physical calls；JSON/tests/cache/PDF 也不等于无人值守科学全绿。 状态：evidence_boundary；证据：E01, E02, E04, E05, E06。
- **E2E-043 [control]** `全链路/费用与时间`；只按最终 receipt 报 project 100.821423 settled + .1086312 uncertain、reserved 0、cap 110、available 9.0699458；account 158.6437595 + 4.6254988、available 136.7307417。 状态：closed_reporting；证据：E01, E02, E11, E27, E31, E32。
- **E2E-044 [acceptance_boundary]** `final/科学接受`；36 units/7 chapters/199 refs 不改变 scientific_allclear=false；pending/quality diagnostics 保留。 状态：final_residual；证据：E01, E02, E15, E23。
- **E2E-045 [control]** `V34/自动化生命周期`；current process exited、fresh CIM absent、wrapper returncode 0、automation deleted；历史 CURRENT_PROCESS 文件不证明活跃。 状态：closed；证据：E01, E02。
- **E2E-046 [acceptance_boundary]** `V34/PDF可读性`；视觉页无空白/越界，文本无 html/mojibake/ref leak，但 literal 数学和 532/635 仍是读者可见问题。 状态：accepted_with_diagnostics；证据：E02, E07, E08。
- **E2E-047 [control]** `final/安全与脱敏`；本记录只写 N/S 相对路径、SHA 和字段摘要；不读 credentials、SQLite、完整论文、完整 REQUEST/raw。 状态：closed_guarded；证据：E01, E02。
- **E2E-048 [recommendation]** `cloud handoff/修复优先级`；后续应优先统一 identity/cache、partial/final contract、source/condition pairing、status projection、checker fail-closed 与 publication guards；本记录不声称已实施。 状态：open_plan；证据：E01, E02, E17, E35, E41。
- **E2E-049 [incident]** `V25/selection identity`；qualified refs 36 projected to 27 unique bare IDs; downstream U01 and legacy readonly IDs collided/risked being dropped. 状态：closed_guarded；证据：E60, E61, E62, E63, E64。
- **E2E-050 [incident]** `V26/budget binding/guard`；user +30 CNY changed project cap 80→110 while account cap stayed 300; preparation guards needed to stop hardcoded-80 assumptions. 状态：closed_binding_guard；证据：E65, E66。
- **E2E-051 [incident]** `V21-V25/owner evidence projection`；owner-facing report falsely represented unsent pool rows as read; d23 final projection reports supplied channels only. 状态：generic_fixed_with_diagnostics；证据：E67, E68, E69, E79, E82, E83, E84。
- **E2E-052 [incident]** `V13/directed handoff`；five directed gates lacked REQUEST_DECISION/INVOCATION/RESPONSE/INVOCATION_ERROR; V13 stopped before payment at 22 groups/6 targets/0 nested results/0 material records. 状态：generic_guarded_undispatched_claim_recovery；证据：E70, E71, E72, E86。
- **E2E-053 [incident]** `V28-V30/supervision lifecycle`；continuous supervision omitted decisions or reported inflight as exited; fresh CIM/process checks later established actual state. 状态：diagnostic_boundary；证据：E72, E73, E74, E75, E94。
- **E2E-054 [control]** `V24/case count semantics`；124 selected contributions correspond to 107 unique source handles, with 12 accepted/6 rejected batches; shared lists could overcount. 状态：closed_reporting；证据：E26, E76, E77。
- **E2E-055 [user_requirement]** `V34/editorial follow-up/摘要与引用`；用户新增要求：摘要不得出现可见引文，但内部证据/来源句柄仍需保留。只读核对发现当前摘要724字、14组方括号句柄、13个unique P句柄；实际context writing_requirements为空，现行策略只在明确给出禁引文时禁止，parser只校验结构/文本/关键词/allowed handles。 状态：deferred_user_requested；证据：E95, C02。
- **E2E-056 [user_requirement]** `V34/editorial follow-up/全文引用编号`；用户要求移除摘要可见引文后，按从引言开始的实际首次正文引用顺序做旧号到新号映射，更新现有全文标记和参考文献表而不改写内容或论文身份。现有delivery_citations.py已按final_handle_draft全篇首次出现建canonical map；当前引言前14个编号为1,14,15,16,17,2,2,3,4,5,15,16,18,3，非连续本身是摘要先占号与回引的结果。 状态：deferred_user_requested；证据：E95, C02。
- **E2E-057 [user_requirement]** `V34/editorial follow-up/摘要与引言职责`；摘要与引言首句实际完全相同54字。只读核对确认两张职责卡均覆盖物理极限/工程约束/材料效率/制造/计算/应用等主要轴；摘要可看到先前introduction/conclusion。现有结构/非空/句柄校验与位置应用没有验证最终读者职责是否真正分开，最终checker轮又因invalid JSON未采用建议。 状态：deferred_user_requested；证据：E95, C02。
- **E2E-058 [incident]** `V28/supervision runtime/监督与运行时`；read_thread API直接返回的2026-10-18 01:32–04:32+08十个heartbeat回合均为failed，错误文本为usage limit；这些回合没有assistant结果，不能把heartbeat失败当成provider/model调用、科学失败或生产进程退出。 状态：diagnostic_boundary；证据：C01。

## 处理分类与真实边界

- **已通用修复：** PDF identity guard、managed cache/rebuy path seam、partial-batch role、native failure isolation、owner final-channel projection、scoped selection identity、assembly-only numeric/derivative alias、parts finite limits、bibliography scope、TeX delimiter、C0 literal fallback。每项以对应 commit/receipt 为准；不表示整条科学链路全修。
- **本次恢复：** directed 空交接后的可追溯缓存重接、V19/V20 历史结果隔离、V25 arrangement reexport、V28 envelope recovery、selected BODY handoff。恢复不等于科学改善。
- **人工科学决定：** AB 的 FDTD/实测边界、V14–V16 错源/跨设置拒绝、V24 failed batches、V25 CH03/CH04 误拒重审、V26/V27 条件配对/短稿判断。旧 raw/reject 保留。
- **接受瑕疵：** 用户允许的 Qwen 局部瑕疵、pending quality、literal 数学、532/635、author metadata。接受只代表停止低收益重复付费并交付 local review draft。
- **仍未修：** 补写整段重写/overlap guard与拒绝SSE安全重放未通用修复；同一科学拒绝的跨namespace防重买/自动重试仍缺统一稳健修复；波长 `@` 引文仍有读者可见问题。author metadata属于缺输入/投稿条件，scientific_allclear=false属于接受状态，不能列成应由算法自动“修好”的bug；局部source/condition问题按用户容忍尺度保留。不能把 scoped ID、缓存边界或一次性恢复宣传为未来系统已全部解决。

## 文档包边界

此包只记录问题和证据，不包含论文公开发布、source patch或新binding。用户已要求上传本脱敏记录，当前缺少目的地；本轮不实施生产修复或重跑，Root按文档白名单打包。原失败、unknown、取消、repair、pending、人工纠正和接受瑕疵均保留。

## Root 最终核对补记

### E2E-034 补写拒绝与普通续跑

CH02:CH02_U03 已有1657字有用原稿。其一次补写完整SSE把整段重写标成appended，监督拒绝采用，native保持assessed_with_pending、changed=false并继续其他单元。`run_unit_completion` 截止版本仍缺通用full-rewrite/overlap guard，回执明确production_fix_implemented=false。`--reparse-saved` 可能重新消费这份完整但已拒的SSE，故正常续跑使用不含该标志的future命令；特定拒绝stage必须先审再重解析。它是工程消费接缝，不应靠更多付费重抽解决；也不应把可用原稿和pending误作全书停止条件。证据 `E85`。

### E2E-052 已证明未dispatch claim的显式恢复

commit `24695cf6b53474efd34514712bbe9b54010c0225` 已实现严格的正常run+resume恢复路径：核完整attestation、旧owner身份/进程、原子claim版本，保留原claim和终态receipt。验收记录9项focused tests及实际五任务隔离proof；无全局清claim、无账本更改、无假成功。缺marker本身不等于可任意重试；必须证明尚未进入physical client并通过既有保护。证据 `E86`。

本记录原始版本共54条，细分incident38、acceptance_boundary10、control5、recommendation1；这是历史记录分类，不是54个独立代码缺陷。

---

## 2026-10-19 对话追加：覆盖与缺口

本版在10月18日54条记录、86条证据上追加，旧目录不改。API分页读到10/17 18:38后，cursor182161受单个超大回合限制；随后按同一thread的本地response_item.message补齐三天匹配公开文字层，共688条（含上下文和heartbeat，其中18条直接用户消息）。时间均转为Asia/Shanghai，只有选定短摘录进包。API原始分页限制仍保留；本地文字补充不等于reasoning、tools、完整模型I/O或论文全文审计。详见CONVERSATION_REVIEW_INDEX.json。

### E2E-055：摘要可见引文是本项目新增的后续要求

ROOT_EDITORIAL_FINDINGS 只读核对发现实际摘要724字，14组方括号句柄、13个unique P句柄；actual_context_writing_requirements为空，现行策略只在明确提供禁引文要求时才禁止，结构解析器检查形状、文本、关键词和allowed handle identity，没有摘要“无可见引文”门。用户现在要求摘要不显示引文，同时保留内部证据和来源句柄。这是本项目新的出版/读者呈现要求，不能倒写成旧模型调用已经失败；旧摘要和原始生成记录保留，状态为 deferred_user_requested。

处理边界是：后续生成或交付准备应把可见摘要文字和内部证据/provenance分开，验证摘要无可见引文，继续保留来源关系并维持 scientific_allclear 的独立语义。本次没有改摘要、prompt、source、cache、binding，也没有新调用或费用。证据 E95,C02。

### E2E-056：从引言起的编号映射

现有 delivery_citations.py 已经按 final_handle_draft 中的首次出现扫描canonical paper identity，再建立1..N map；因此当前引言开头出现 1,14,15,16,17,2,2,3,4,5,15,16,18,3，主要是摘要先占用了first-use编号并包含回引。这不能简化为“没有首次引用算法”。

用户要求在E2E-055移除摘要可见引文后，以引言开始的实际首次正文使用建立旧显示号到canonical paper/DOI再到新显示号映射，原子更新全文标记和参考文献表，保留所有科学文本和paper identity，不混淆 source handle 与 DOI。当前出版物主要是fixed [n] 文本，references.bib 只有换行；不能假定切换LaTeX样式就自动重排。未来验收是各canonical首次出现的新编号从1连续、所有复引用仍指向同一paper、ref表同步；正文数学区间、表号及其他数字不得全局替换。LaTeX native cite/BibTeX是待核实现有路径的可选方案，不能当作已经存在的修复。状态 deferred_user_requested，无新调用。证据 E95,C02。

### E2E-057：摘要与引言的职责重复

摘要与引言首句实际完全相同54字。读到的职责卡并非缺失：两者各有purpose/focus/boundary/finalize_from字段，另有conception阶段、prior-generated-parts降级为二级材料和反段落重复指令；但两个实际职责仍覆盖物理极限、工程约束、材料效率、制造、计算和应用等主要轴。摘要还能看到先前的 introduction/conclusion。现有计划校验和解析器只验证结构、非空和句柄身份，应用阶段核对BODY未改及位置，不能证明最终读者功能已分开；最终checker一轮又是invalid JSON，0 proposal adopted。

职责/输入/验收缺口是已证层。先前文本锚定或模型的generic-opening偏好是合理假说，无消融不能定为唯一根因；不能只加“首句不得相同”、做词面查重或为此追加全文重写。后续验收应分别看摘要是否预览review object、scope、organizing contribution与条件化主结论，引言是否提供阅读动机、prior knowledge、problem、review role和navigation；允许共享中心问题和术语。状态 deferred_user_requested，不改现稿、不收费。证据 E95,C02。

### E2E-058：heartbeat/API usage-limit 造成监督窗口空档

直接读取的第三页（cursor 187538）包含10个2026-10-18 01:32–04:32+08的heartbeat turn，均 status=failed，错误为 usage limit；选取代表回合 C01 保留完整用户消息SHA，正文只放脱敏短摘录。失败发生在assistant结果返回前，不能当作provider/model调用、science rejection或底层PID退出。是否导致同一时间段worker进度漏报没有从这些回合证明，必须回到native receipt/CIM；本记录不填未知费用或模型时长。

这条是监督/运行时诊断边界，保留旧失败记录，不恢复automation、不重启、不retry、不改账本。后续验收要把heartbeat/API failure、native process lifecycle、provider response和science decision分开；无decision/response不得自动restart或rebuy。证据C01；API分页限制与已补齐的公开文字层分别记录于CONVERSATION_REVIEW_INDEX.json。

### 已有条目的事实更新，不新增重复编号

- E2E-026：V27 CH02_U02 的TiO2标签是未由摘要证明的本地标签，旧拒绝保留，按用户容忍免费恢复同一请求；V28 CH03_U01先恢复实际546字普通稿，QC明确P01 covered、P02/P03/P04 missing，再由唯一1800字append补足，合计2348字。P0355、P0244及比较范围等局部诊断保留，quality仍 assessed_with_pending、scientific_allclear=false。这不是把335字U04原始candidate当成功，也不是把历史QC model_calls改为0。
- E2E-028：多JSON两层恢复仍是通用f446实现加一次0-call实际缓存恢复；短稿普通恢复与后续append分别由原生QC/append回执约束，不能合并成“单次手工成功”。
- E2E-034：CH03_U01的append经过实际P02/P03/P04缺口和fragment不重复检查；CH02_U03被拒整稿重写的full-rewrite/overlap guard仍未通用实现。普通resume省略 --reparse-saved，不人工截取、不用一次成功标记覆盖失败历史。
- E2E-053：后段V28仍将native complete、quality pending、scientific_allclear false分开；局部replacement拒绝不会自动变成全书失败或重启。

### 追加后的分类与时间边界

本版58条结构化条目：incident=39、acceptance_boundary=10、control=5、recommendation=1、user_requirement=3。055–057是本轮用户明确待办（mandatory=true，status=deferred_user_requested），不是可选建议或已完成修复；058是一条历史监督运行边界。更新日志不另计条目，证据引用不作为问题数量。1715到600到548（160旧+388新）、补材池555、7章/37上层线索到36 units、18 case batches=12 accepted/6 rejected、124 contributions/107 unique handles、897 settled rows的旧语义继续有效，不相加为调用数或独立缺陷。

本版所有时间均以Asia/Shanghai显示；receipt时间仍按其原始UTC并单独转换，不能把跨天墙钟当模型耗时。文档写入时间来自真实 datetime.now(ZoneInfo Asia/Shanghai) 转换，未硬填。当前仅写新文档并准备脱敏上传包；云端目的地尚未给出，未执行上传。

### 云端审阅边界

云端若接收本版，应把已通用修复、一次性恢复、人工纠正、接受瑕疵和仍未修分开。055–057先作为deferred requirements；058只作运行时观测。沿用旧交接中的append/full-rewrite安全重放、拒绝缓存、科学拒绝跨namespace防重买、owner/directed生命周期与出版token化优先级；只停受影响路径，保留可用原稿和pending，不建立新的claims大框架、不把所有科学判断统一fail-closed，也不把Qwen局部瑕疵转成重复付费理由。


### E2E-002 对话回查补记：169→600→548

本地脱敏文字层补回了 API 分页无法安全返回的公开消息层。2026-10-16 23:01:29（Asia/Shanghai），用户直接问“A/B卡备选这么少？”（C04，原文 SHA 4d4a45a6e9b228db339d75696ea54e2507776f7075b15b7cf280dbb4f23f13e5）；23:11:54 明确要求暂停并保证最低600篇，不能由首篇15%分数阈值停止（C05，SHA 9b09a623d7482ed70b31c03f5195f535772da10f41a1e2e2de1ebd403b1626e3）；23:21:41 同意补卡并汇报质量（C06，SHA b04d97921fb5446360dcab9374955fe093c0d3aaa61e6c20b9a9a3dc8a4e3606）。

23:21:06公开助手报告暂停真实调用、处理旧400上限、免费重算169→600和5项检查；此报告早于用户23:21:41同意补卡，文字层结论不单独替代收据（C07，SHA 117bb2e691667535fa00f3630ecfe81f939e03c27c0550cf1a9b5d7762d30024）。权威本地收据 E96（ROOT_M3_MIN600_ACCEPTANCE.json，SHA 414185ccc8caaacc6331f91cf00f3091afc4fa9a85287077e013e4dcc1e9e954）确认：原169、new600、eligible3054、effective_min600、effective_max1000、paid/network calls均为0、5项聚焦检查通过。后续基础A/B接受池是548（160旧+388新），不能改写为600篇科学合格卡，也不能为凑数补卡。

因此 E2E-002 的工程问题是已验证的停止/数量合同修复，残余是送读、A/B采纳和科学验收仍分层。旧169/160、失败和差额记录保留；本次文档没有重跑或改生产。

### 旧肠道案例发布与本项目边界

10月16日早段对话中，用户要求去掉肠道案例摘要引文并改作者/日期，随后请求把该旧案例 PDF 放入既有网站；公开助手回报的是旧肠道案例提交和网站发布。该内容不属于本超表面E2E的作者输入或最终发布授权，不能把旧站点请求当作本问题记录包的云端上传许可。本地快照中最早匹配公开消息为10/16 17:41:41；这不证明此前没有工具执行。当前上传目的地未给出。

### 对话文字层覆盖说明

本地扫描索引 preparation/issue_record_append_20261019/ROOT_LOCAL_HISTORY_SCAN_INDEX.json（SHA b3cdff85500d09ab79f8ec7cd414d6e4e783d0c60412cce630bb316efc9abaa8）及其本地过滤文件（SHA 617fbdcf5a2ae83c094f43710b2273c42c589b0685f916837a88e57e27d95f21）覆盖2026-10-16 17:41:41至2026-10-18 23:26:48，共688条公开消息、上下文及heartbeat，其中直接用户消息18条，JSON解码错误0。该层跳过reasoning、tools、developer/system、inter-agent及attachment；完整过滤文件不进云包，只把C04-C07等短摘录放入本记录。API cursor182161的oversized-turn缺口仍保留，不能称为原始对话/I/O全审。
