# Upgrade 3 task-directed deep reading

该通道为综述写作任务精读已入选全文，不负责文献检索或替代完整综述卡片。规划器先把章节问题拆成明确的问题、用途和所需输出；精读器随后只从指定的本地快照提取回答这些问题所需的材料。昂贵路线要求持久化 admission store、已批准的全文候选、有效 API key 和有限预算。全局核心名额上限为 40 篇，跨任务共享；综述类候选优先获得空余名额。`preflight` 是离线操作，不会调用模型或触碰预算账本。

## 请求与准入

从脚本生成有版本的请求，问题和输出定义应先由规划者审阅：

```powershell
python scripts/upgrade3/directed_reading.py plan `
  --review-id REVIEW_ID `
  --topic "综述主题" `
  --chapter-id CHAPTER_ID `
  --chapter-title "章节标题" `
  --questions questions.json `
  --required-outputs outputs.json `
  --candidates candidates.json `
  --output outputs/directed_reading/REQUEST.json
```

每个问题需要 `question_id`、具体的 `question`、用途 `purpose`，以及可选的 `required_output_ids` 和 `gap_key`。每个必需输出需要 `output_id`、`output_type` 和描述。核心候选需给出全文范围、足以解释提名理由的信息增益、核心名额依据、知识缺口和输出 ID，并明确 `approve_core: true`。

每个问题也可选带 `evidence_contract`，把直接证据资格与必须逐研究填报的字段固定在请求里：

```json
{
  "evidence_contract": {
    "eligibility": {
      "target_evidence_type": "任务需要的证据类型"
    },
    "reporting_fields": [
      {"field_id": "sampling_time", "description": "测量/采样时间"},
      {"field_id": "validation", "description": "验证设置与队列"}
    ]
  }
}
```

`eligibility` 只写任务实际需要的资格条件；未提供的维度不增加隐含限制，普通综述任务也不必写特定干预或排除条件。每个 reporting field 仍须逐单元明确报告：给出原文支持的 `reported` 值，或写 `explicit_unknown` 并说明在本次提供材料中未知、未报告或不适用的原因。不要用其他研究补字段。独立 verifier 对每个单元和契约问题重新判断 `pass|fail|unknown`，并逐字段核对同一单元的绑定来源。只有 eligibility 为 `pass` 且所有字段均有来源支持或明确状态的单元才能用于直接答案与 `planning_usable_direct_units`；缺失 verifier 项、来源未绑定、eligibility 非 pass 或漏字段都会阻断 `contract_ready`/完整 planning 交付。没有 `evidence_contract` 的旧请求照旧处理，输出不会出现 `contract_ready` 声明。新 contract 请求的旧 RAW 可离线重放作审计，但若 RAW 没有逐字段报告，系统会把它保留为不完整并阻断 contract-ready；需用新 prompt 重新读取才能补齐模型输出。

只有全文候选可以进入精读；短摘要只能保留为提名，不能启动昂贵路线。中文说明按汉字数量检查，不要求空格分词。

```powershell
python scripts/upgrade3/directed_reading.py admit `
  --request outputs/directed_reading/REQUEST.json `
  --store outputs/directed_reading/review.sqlite
```

Store 绑定 review ID、topic binding 和 topic 内容身份。改变主题或复用不同 topic binding 会拒绝。候选按 canonical paper ID 合并；同论文的新问题在与此前已完成阅读相同的快照上，不会因 gap 子集而被假定已回答，必须显式规划新缺口。Store 的事务锁保证并发准入不会突破 40 篇。

## 离线预检与付费运行

针对一个已入选候选，先做离线预检。`--paper` 应是该候选 JSON；`--snapshot` 应指向 `PreparedSnapshotProvider` 可读取的快照目录。

```powershell
python scripts/upgrade3/directed_reading.py preflight `
  --request outputs/directed_reading/REQUEST.json `
  --paper outputs/directed_reading/PAPER.json `
  --snapshot outputs/materials/SNAPSHOT_ID
```

若已完成选择器结果的人工检查，可显式将其生成的包用于一次选择式读取；省略该参数仍使用完整快照。选择包需由当前快照生成，并保留完整候选/遗漏审计：

```powershell
python scripts/upgrade3/directed_reading.py preflight `
  --request outputs/directed_reading/REQUEST.json `
  --paper outputs/directed_reading/PAPER.json `
  --snapshot outputs/materials/SNAPSHOT_ID `
  --selected-source-packet outputs/directed_reading/source-selection-02/SELECTED_SOURCE_PACKET.json
```

预检会绑定来源片段和文献表身份、构建阅读与 verifier 提示，并报告来源字节、来源块、输入估算和 verifier 保守上界。选择式 reader 预检分别列出选择包与全快照来源包审计；verifier 的上界仍按全快照估算。输入上界高于 900,000 token 时会在任何 provider 调用或 task claim 前拒绝。

只有负责人确定要执行真实读取时才运行 `run`。reader 使用 `qwen3.7-flash` 思考模式；独立 verifier 也启用思考，使用固定的 4096 reasoning-token 上限、JSON 输出模式，并与 reader 共用现有累计预算账本。预检和预算准入会计入 verifier 思考额度；预算上限必须是正的有限值，费用预估在 claim 任务前完成。

```powershell
python scripts/upgrade3/directed_reading.py run `
  --request outputs/directed_reading/REQUEST.json `
  --paper outputs/directed_reading/PAPER.json `
  --snapshot outputs/materials/SNAPSHOT_ID `
  --store outputs/directed_reading/review.sqlite `
  --output-dir outputs/directed_reading/attempt-01 `
  --key-file api_keys/qwen-api-key.txt `
  --budget-ledger outputs/directed_reading/qwen-ledger.sqlite `
  --budget-limit-cny 35
```

审核选择式包后运行时同样显式传入它：

```powershell
python scripts/upgrade3/directed_reading.py run `
  --request outputs/directed_reading/REQUEST.json `
  --paper outputs/directed_reading/PAPER.json `
  --snapshot outputs/materials/SNAPSHOT_ID `
  --selected-source-packet outputs/directed_reading/source-selection-02/SELECTED_SOURCE_PACKET.json `
  --store outputs/directed_reading/review.sqlite `
  --output-dir outputs/directed_reading/selected-attempt-01 `
  --key-file api_keys/qwen-api-key.txt `
  --budget-ledger outputs/directed_reading/qwen-ledger.sqlite `
  --budget-limit-cny 35
```

reader 会收到所选段落、必要邻段语境、所选表格的完整行及对应 REFERENCES；每个 handle 仍能回到原快照。selected/context 路由标签用于审计，不单独决定证据资格：只要 exact source binding 有效，所提供的相邻段或表格上下文也可作为候选证据；verifier 仍须独立确认原文在语义上支持对应主张和任务。不要因为材料来自 context 路由而人工降权、阻断或隔离。

reader 原始响应先完整保存到 `RAW_RESPONSE.json`。归一化阶段只有在直接答案明确列出 unit IDs，且这些单元都通过本地 direct scope、完整研究边界、逐字来源绑定、身份/引文检查时，才会把该答案重建为这些单元 `text` 的原样有序拼接。系统不补写或改述科学内容；原答案、原答案来源引用和重建方法保存在 `answer_repair_audit` 与逐答案 `answer_repair` 中，Verifier 的 prompt 仅接收重建后的答案。单个 extraction unit 格式或来源无效时，该单元会隔离并保留理由/原始材料审计；有效 sibling units 可继续核验，但失去的必要覆盖仍须显示为不完整。即使重建完成，最终 `planning_ready` 仍需独立 verifier 重新检查单元语义支持、研究边界和答案适配。不同研究须依据各自正文引用与研究边界区分；同一研究中彼此相关的多个观察和终点可以一起保留，但每项都要连着自己的条件和范围。

将 `--budget-limit-cny` 设置为该 review 已授权的累计上限，并沿用同一账本。成功的 reader 原始响应会在 verifier 开始前原子保存。verifier 的 `ready`/`source_ready` 只表示给出的引文支持检查通过；`planning_ready` 还要求问题适配、study-level 边界、来源层级及引文身份都通过。只有 `planning_ready=true` 才会写入 immutable commit；来源有争议或任务适配阻断时产物保留在 `needs_review`，不得作为已核实科学结论。每个问题、所需输出和必需输出 ID 均须覆盖，否则不提交成功。解释和案例须绑定快照中的精确原文引用。

每个 extraction unit 必须标记 `study_origin`：`self_paper` 用所选论文的快照身份与正文片段支持，不要求外部 bibliography handle；`review_reported_secondary` 表示综述转述的外部研究，具名研究/试验须将正文引文标记绑定到 REFERENCES；无法判断则为 `unknown` 并阻断 planning-ready。每个 unit 还保留 direct/contextual/excluded/unclear 适配、研究边界字段和 verifier 判断。同一研究内彼此相关的多项观察或终点可以共处一个 unit，但要分别保留对应条件；不同研究或实验条件不得混成一次实验。

`overview` 是可选的审计摘要，不参与 task-fit 判断或 planning-ready 内容。缺字段、空引用占位或不能绑定的摘要引用会被记录为 `overview_unverified`、`missing_fields` 和恢复问题；系统不会补写缺失的 summary/scope，也不会把此类概览送入 verifier。规划只应使用通过独立 verifier 的 task answers、extraction units 和 citation observations。

verifier 原始 JSON 始终单独原样保留。若某个必需的目标数组缺失、目标漏答或状态放错字段，解析器会为缺项生成 `unclear`/`insufficient`，把 `verification.status` 设为 `partial` 并阻断 `planning_ready`；遇到 `mixed_or_overclaimed` 被放在 answer-support 字段时，会保留原状态并按 `disputed` 处理。每个问题的 `answer_fit_reviews` 必须列出答案实际使用或提及的全部 unit，并提供 `unmapped_claims` 数组记录无法映射到 unit 的案例、研究名、作者或数值；缺任一字段会阻断规划，非空 `unmapped_claims` 也不能与 `direct` 并存。这样可安全重放已经缓存的 reader 与 verifier 原始响应，而不会把不完整核验提升成成功提交。

Verifier 按每个任务问题明确的资格条件、对象、关系、范围和终点判断材料是否直接相关。主题或术语相同本身不足以证明符合任务；任务要求报告某个额外字段，也不会使只研究该字段的其他工作自动合格。只有确实满足任务范围的 `direct` 单元可支持直接答案；相关但回答另一问题的材料可保留为 `contextual`，明确排除的材料为 `excluded`，二者不能拼入 direct answer。Verifier 还需检查答案中提到、但 reader 未对应任何 unit 的材料；本地门禁会把这种遗漏视为混合或过度推断风险。

`planning_usable_direct_units` exposes individual units that pass their own source support, reader/verifier direct-scope agreement, study boundary, and identity/reference checks. These validated units are usable writing material within their stated scope even if another unit or the whole answer is incomplete. `planning_materials.material_ready` and `eligible_unit_ids` describe that usable slice; `whole_answer_ready` independently records whole-answer completeness, and remaining question coverage must remain visible. Both `self_paper` and `review_reported_secondary` material follow the same task-fit and claim-quality checks; provenance records lineage and does not create an automatic weighting discount. A supported review-reported claim can be planned and written with the review citation without acquiring the cited original's full text, while accurately stating that the original full text was not independently read. A secondary-source unit with a blank study identifier may receive the derived structural label `bibliography_ref:<reference_handle>` only when its body citation is already bound to that exact frozen REFERENCES item; the original blank and derivation are retained in `study_identity_recovery`. This is a bibliography pointer, not an inferred trial name or scientific fact.

R→C citation observation 在 `citation_anchor_ready` 时，表示综述 passage 确实支持所述转述，且句内引用可绑定到有足够书目信息的 REFERENCES 条目；输出标为 `review_reported_secondary`，`direct_verified=false`。这是可用于成稿的二手来源引用身份，不表示系统读过或核实了原始 C。只有 bibliography 身份不足，或 claim 超出/无法由所绑定的综述 passage 支持时，才会要求 follow-up；不要求额外下载或阅读 C。

若模型给出了正确的 source handle，但 quote 含省略、大小写变化或多句改写，程序只在该 handle 对应的同一来源块内查找最长连续匹配，并要求匹配长度达到 `max(40 字符, 模型原 quote 字符数的 20%)`。程序锚定的是从快照原文切出的精确片段；输出审计同时保留模型原引文、选中的原文片段、修复方法、匹配长度和比例。低于阈值或 handle 无效仍拒绝。片段锚定只证明定位到文本，不证明 claim 获得支持，语义验证仍由独立 verifier 完成。

当前 reader prompt v8 要求每个 unit 的 `text` 非空，`conditions` 不可代替主要发现；缺失 text 会隔离该 unit，原始模型响应仍保存在 `RAW_RESPONSE.json`。提示要求提取服务当前任务的概念、机制、比较、结果和适用条件，并忠实保留作者自己的综合论述；不假装作者做过实验，也不因任务列出额外 reporting field 就把只涉及该字段的无关研究变成合格研究。资格条件和需报告的细节分别判断。`self_paper` 可用当前快照的论文身份；具名外部研究的引文须与 REFERENCES 绑定，bibliography 的 `r` handles 与正文证据的 `s` handles 分别处理。同一研究中彼此相关的多个观察或终点可留在一个 unit，但须分别附上条件和范围；不同研究或条件不能拼成一次实验。`task_answers` 基于身份充分、边界完整且符合任务范围的 direct units。scope/directness 由 reader 提出并由独立 verifier 对照问题重新判断。`self_paper` 与 `review_reported_secondary` 使用同一 task-fit 和主张质量标准；来源谱系用于准确标注，不自动折价。有支持的综述转述可用于规划和写作，无需先获取原始全文，但不得声称独立读过它。旧版本产物和原始响应保持原样，新 prompt 使用新版本与输出目录。

Prompt v8 使用可审计的结构化来源包：正文块仍保持逐个 `source_handle`，表格单元按原 row/column 重组并携带列标题、行标签、caption、footnote 的原始 handle；REFERENCES 的原文和元数据只在独立列表出现一次，重复的 bibliography source-block handle 不送入模型。`source_packet_audit` 记录正文 handle 清单、被 REFERENCES 列表替代的 reference-block handles、表/单元格计数和未覆盖 handle；任何正文 handle 未表示都会在调用前报错，不会静默丢弃。默认 reader 使用整篇正文。若已完成人工检查，可通过可选的 `--selected-source-packet` 将选择式包用于 reader；包必须与当前快照和选择审计完全一致，否则预检和付费调用都会在 task claim 前拒绝。Verifier 仍可用完整正文包和单独 REFERENCES 回查原始 handle，并且它不把未入选原文当作 reader 已读材料。

`selection-preflight` 是单独的离线第一阶段。非参考正文短段落完整显示；长段落会用最多六个、每个不超过 240 字符的等距窗口呈现开头到结尾，所有窗口仍绑定同一个原始正文 handle，并在本地审计中记录 offset。每个表格按原行构造目录，只提供共享一次的 caption/列头、行标签和每行至多 100 字符短预览；选择器目录不含整张表，也不把 REFERENCES 当正文候选。章节路径与表格信息只存一份，模型看到紧凑目录；`SOURCE_SELECTION_PROMPT.json` 的本地审计仍保存每个候选到所有原始正文 handle 的映射。提示会逐题列出证据类型、对象、干预/暴露、终点和排除条件，要求列出 `selected_candidate_ids` 与 `uncertain_candidate_ids`，并分别给纳入项理由和统一的遗漏规则。selected 项必须在 `selection_reasons` 中逐 ID 有理由。uncertain 项可在该字段给理由，或在 `uncertainties` 中提供含对应 candidate ID 的具体疑点；解析器会在结果中标出 `recovered_from_uncertainties`，且找不到相符疑点仍会拒绝。任务是从当前综述报告中寻找相关研究段落；不要求先取得综述所引 C 文献的全文。窗口只能显示部分细节时，模型应把可能相关项列为 `uncertain`，供下一阶段读取完整原文；只有目录完全没有潜在候选 passage/row 才能报 `inadequate`。本地解析器据完整目录确定所有 omitted IDs/handles 并记录该规则，避免模型重复数百个无关 ID。无候选或 `coverage_status=inadequate` 都会失败关闭。正文 preview 只用于路由，不能作为科学证据。

```powershell
python scripts/upgrade3/directed_reading.py selection-preflight `
  --request outputs/directed_reading/REQUEST.json `
  --paper outputs/directed_reading/PAPER.json `
  --snapshot outputs/materials/SNAPSHOT_ID `
  --output-dir outputs/directed_reading/source-selection-01
```

该命令不调用模型，不打开 admission store，也不读取或改变预算账本。它会不可覆盖地写出 `SOURCE_SELECTION_PROMPT.json`（完整目录、两条提示消息和尺寸/来源审计）以及 `SOURCE_SELECTION.json`（`awaiting_selection_response` 收据和候选 ID）。负责人可将提示交给允许的 selector 模型，人工检查其 JSON 后再用 `--selection-response PATH.json` 离线验证：程序会原样保存输入字节到 `SOURCE_SELECTION_RAW_RESPONSE.json`，写出 `SOURCE_SELECTION.json` 的选择决定与理由，并生成 `SELECTED_SOURCE_PACKET.json` 供人工审阅。该包只含所选正文、同章节相邻段落、选中表格的完整行与必要表头/caption/footnote，以及所选正文句内引用对应的 REFERENCES；原文 handle 不变，所有遗漏 handle 和其规则原因都进审计。`selection-run` 可以只请求选择结果；它不会自动接续 reader，也不替代人工查看目录和选择。收到选择提案不等同于审核批准，也不能直接作为 planning-ready 输出。

负责人批准 selector 路由测试后，可用 `selection-run` 单独调用 selector。该子命令要求一个已存在的共享预算账本、账本原有限额、API key 文件和新输出目录；会先按最多两个 key attempts 的 prompt、thinking 与可见输出上限估算最坏情况费用，并为每次尝试分别使用同一账本预留/结算。若首个 key 返回欠费等可换 key 错误，Qwen runtime 最多再尝试一个已配置 key；最多只有一个成功的付费响应，不使用代理。selector 不会调用 reader/verifier，不会打开 admission store、claim task 或占用 40 个核心论文名额。

```powershell
python scripts/upgrade3/directed_reading.py selection-run `
  --request outputs/directed_reading/REQUEST.json `
  --paper outputs/directed_reading/PAPER.json `
  --snapshot outputs/materials/SNAPSHOT_ID `
  --output-dir outputs/directed_reading/source-selection-02 `
  --key-file api_keys/qwen-api-key.txt `
  --budget-ledger outputs/directed_reading/qwen-ledger.sqlite `
  --budget-limit-cny 35
```

原始 provider JSON 会在解析前原子写入 `SOURCE_SELECTION_RAW_RESPONSE.json`；提示、响应元数据、解析选择与完整 handle/覆盖审计分别写入 `SOURCE_SELECTION_PROMPT.json`、`SOURCE_SELECTION_RESPONSE_META.json`、`SOURCE_SELECTION.json`、`SOURCE_SELECTION_AUDIT.json`。每个 key 的 provider 尝试另存于 `provider_raw_responses/`，`SOURCE_SELECTION_PROVIDER_ATTEMPTS.json` 汇总成功/失败、key index 和 raw hash。若模型 JSON 无效、候选 ID 未知、理由不全或覆盖不足，系统保留 RAW、写入 `rejected_fail_closed` 审计并拒绝生成 selected packet；任何失败都不会启动 reader。真实调用前仍应先用离线 preflight 确认 selector input 上界与要送模型的目录。

## 中断与争议恢复

普通调用若在 verifier 前中断，可对同一个 output directory 重跑；缓存的 `RAW_RESPONSE.json` 会被复用。若 verifier 已返回争议，原目录和 `COMMIT.json` 保持不变，task 进入 `needs_review`。人工决定继续重新核验时，指定一个新 output directory，并显式复用原 reader 响应：

```powershell
python scripts/upgrade3/directed_reading.py run `
  --request outputs/directed_reading/REQUEST.json `
  --paper outputs/directed_reading/PAPER.json `
  --snapshot outputs/materials/SNAPSHOT_ID `
  --store outputs/directed_reading/review.sqlite `
  --output-dir outputs/directed_reading/attempt-02 `
  --reuse-reader-response-from outputs/directed_reading/attempt-01 `
  --key-file api_keys/qwen-api-key.txt `
  --budget-ledger outputs/directed_reading/qwen-ledger.sqlite `
  --budget-limit-cny 35
```

这条路径只重跑 verifier，不会重新调用 reader；新的目录记录 `reader_response_source`，原尝试的原始响应和提交清单保持不变。若 verifier-only 重试再次中断，task 仍留在 `needs_review`，必须通过同一显式恢复选项继续。人工复核可以保留在外部审阅记录；当前 CLI 没有人工 override 来制造程序验证成功。

若决定更换 reader 提示词并重新读取，应显式使用 `--fresh-reader-retry` 并指定另一个从未创建的 output directory。该路径仅接受当前为 `needs_review` 的任务，仍会先重做材料/提示预检和全局预算准入，再 claim；新 reader 响应写入新目录，旧 attempt、原始响应和 COMMIT 均保留。它会重新调用 reader 和 verifier；不要同时使用 `--reuse-reader-response-from`。

运行结果写入 output directory：`INPUT.json` 和 `PROMPT.json` 固定本次身份与提示，`RAW_RESPONSE.json` 和 `VERIFIER_RAW_RESPONSE.json` 保存两个阶段的原始响应，`DIRECTED_READING.json` 与 Markdown 是结果视图，`COMMIT.json` 对成功结果及所有文件记录哈希。Store 的 attempt 事件采用 append-only 方式；再次调用已提交任务会校验 commit 并复用结果。2026-09-23 的 v8 round-two 检查结果和已知限制见 [quality checkpoint](directed_reading_quality_checkpoint_20260923.md)。
