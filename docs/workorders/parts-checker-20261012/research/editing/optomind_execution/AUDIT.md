# OptoMind 局部编辑执行底座审计

审计基准：Lihonggang-scnu/OptoMInd-Review，固定提交 `2a28854cf86a98f5afb4a6e5fbe1a925694fd138`。读取时间：2026-10-12。只读 GitHub 连接器取源码，在独立目录检查，没有改生产代码，没有调用模型。

## 结论先行

1. 用户的两项具体判断属实：article 编辑器的 `target_block_id` 没参与定位或验证；`skipped.original_text` 截 120 字符，`applied.original_text/replacement_text` 也截 120 字符。[A-apply]
2. article 已有原始快照定位、非级联应用、输入顺序优先的重叠处理、逆序区间替换；它不是“改一处后再在变更稿中找下一处”。但这不等于完整幂等：`Alpha.` → `New Alpha.` 再执行会成为 `New New Alpha.`。[A-apply]
3. `post_body_revision_contracts` 确实已有可离线直接复用的块 ID、原文/块/全文哈希、原文区间、整批重叠拒绝、证据 ID 校验。其批次语义是全有或全无；旧补丁打到变更稿时报绑定失败，不是温和的 `already_applied`。[C-target][C-validate][C-apply]
4. `B0001` 和 scoped 的 `block_000001` 都是当前快照的顺序 ID。插段/删段后可能重排，不能当作跨版本持久身份。[C-blocks][S-index]
5. `scoped_body_revision` 已有“一个 issue 的多个块原子应用、坏组挂起、其他好组保留”的执行基础；这是另一条既有路径，并没有接入 article 的 `changes` 合同。[S-group][S-run]
6. `fullbody_writer.reader_revision` 是全文唯一 anchor 的多补丁路线，允许同一个 issue 改相关远处 anchor（必须说明 reason），不要求目标块 ID；它和 scoped 的显式可编辑块边界不能混称。[F-patch]
7. article/fullbody 使用 `str.count` 统计不重叠出现，会漏算 `aaa` 中两处相互重叠的 `aa`。contracts 的 lookahead 则正确拒绝这种歧义。[A-apply][F-patch][C-target]
8. 这些安全合同能证明“在指定原快照的哪个位置执行了什么”，不能证明科学语义正确。editorial 数字检查只挡新数字集合，引用检查只挡新增不支持的 handle；删除、重排、变符号、改变数值归属等不在该保证内。[C-validate]

## 一、article_text_editor：小型文本替换器及其独立缓存层

### 解析

`parse_edit_proposals` 接收 JSON 文本、含 content 的响应、或有 changes/unresolved_questions 的对象；支持简单代码围栏去除。要求两个数组中的元素是 Mapping。每个 change 的 operation、original_text 必须是非空字符串；operation 只能 replace/remove；replace 要求非空 replacement_text。一个坏 change 会使整次解析抛错，不是逐行收集错误。[A-parse]

未强制要求或校验 `target_block_id`、reason、问题 ID、请求 ID、快照 ID、位置区间、哈希；unresolved_questions 只检查每项为对象，没有校验 where/question。提示词要求 ID、不能增事实/数字/来源、通常不超过 10 处，这些不是运行时强校验。[A-parse][A-prompt]

复用时应先 parse 再 apply。`apply_text_edits` 自身默认输入已解析：非 replace 的未知 operation 会走空 replacement，不能把这个底层函数直接暴露给未验证数据。[A-apply]

### 定位、批内快照与重叠裁决

- 原文在整个 draft 上用 count/index 定位，完全不读取目标块目录。
- 所有区间基于同一个不可变 draft_text；先定位并收集，再按 start 递减应用，新增文本不能成为同批后续目标。
- 找不到或出现多次：记 `target_occurs_N_times` 并跳过。
- 遇到与已接受区间相交的提案：后来的跳过为 `overlapping_target`。这是提案列表顺序优先，不是最长匹配、置信度优先或两方都拒绝。
- applied 按实际逆序应用顺序返回；skipped 按扫描顺序返回。没有 proposal_index，不能以两个结果数组的位置直接对齐原提案。
- 只对“replacement 以 original 开头，且其新增尾部已经紧随这个实际目标”的追加式编辑报 already_applied；不会因为 replacement 出现在别处就把缺失目标误判为已应用。[A-apply]

### 重复执行：纯函数与 stage 缓存要分开

纯函数没有 edit_id 去重表、快照哈希或已执行凭据。常见的“替换后原 anchor 消失”和“目标尾部追加”两类会成为 no-op；保留原 anchor 的前置/包裹替换可重复执行。实测：原文 Alpha.，替换 New Alpha.，第二次得 New New Alpha.。因此文件开头关于 rerun idempotent 的注释只能按已覆盖情形理解。[A-apply]

live stage 另有较强的保存与恢复：完整源字节 hash、messages hash、profile、identity catalogs hash 固定输入；输出目录加锁；源快照校验；report 与结果文件 seal 校验；命中已封存结果返回 model_calls=0。这能避免相同 stage 再调服务，但不修复纯 applier 的幂等漏洞。[A-cache]

`_cached_call` 可从保存的 RAW 响应或唯一 SSE 文件免费恢复；请求 identity 改变会拒绝；有可重放内容就优先重放；只有明确 retry_failed、run、无可重放内容且保存了可重试错误时才另开 attempt。它不是自动付费重试。[U-cache]

### 部分成功、反馈与恢复边界

- fixture/replay 分支输出候选文件、完整计数、applied/skipped、unresolved；只按是否有 applied 选 edited/no_change。有 skips 也可能返回 edited，全部 skips 返回 no_change。[A-offline]
- live 分支仍保存好编辑生成的 CANDIDATE 与完整 EDIT_PROPOSALS；只要有 skipped 就保持 pending，增加 article_edit_targets_skipped，不宣布完成。[A-live]
- live 的 pending+skipped 也会写结果 seal；随后同目录调用即命中该保存结果，单传 retry_failed 不能自动修复这些 target。[A-cache][A-live]
- truncated/incomplete、finish_reason 非 stop、返回 model 不符：保留 RAW、报 pending，不应用。[A-live]
- skips 的 report 只保留 operation、original 前120字符、reason、target_block_id；没有候选位置、上下文、全文 hash、提案序号、匹配计数独立字段。live 的 EDIT_PROPOSALS/RAW 可另查完整原文；纯 apply 返回值本身不足以完整重建补丁。[A-apply][A-live]
- 没有把这些定位错误反馈给模型后自动修正的闭环。现有恢复主要是缓存/RAW 重放和显式失败调用重试。

### 换行保真与规模

入口用 Path.read_text 读取，CRLF 会归一为 LF。fixture/replay 总是写归一后的 edited_text，因此即便 no_change 也可改变换行。live 若 edited==draft_text 则写回 source_bytes，能保全 no-change 原始字节；只要真的编辑，就写 edited.encode，原稿其他 CRLF 也会变成 LF。不能把该文件 I/O 路径宣传成“未编辑字节完全保留”。[A-offline][A-live]

全文上限 400,000 字符；超限明确报 chapter_wise_reading_not_implemented，没有已实现的分章通读后跨章协调。[A-pass]

## 二、post_body_revision_contracts：可直接复用的严格快照纯函数

### 快照与定位

`normalize_case` 验证 draft、base_sha256 与外部传入 blocks，自动建立段落快照。空行分块、代码围栏内空行保留；块包含 Bnnnn、start/end、完整 text 与 sha256。偏移是 Python 字符串偏移，既不是 UTF-8 字节位置也不是归一化副本位置。Bnnnn 是枚举序号，不是跨版本身份。[C-blocks]

`load_case` 使用 read_bytes().decode('utf-8') 读取正文和材料，保留 CRLF。`resolve_target` 要求真实 target_block_id；在指定块内找原文，使用正则 lookahead 统计包括相互重叠的出现。跨块重复允许，同一块里不唯一拒绝。[C-load][C-target]

### 补丁合同

`validate_patch` 输出绑定：base_sha256、issue_id、target_block_id、operation、original_text、original_sha256、block_sha256、start/end。模型 proposal 可以只给 replacement_text，运行时用可信快照补齐绑定；若 proposal 自带任何绑定字段，必须与推导结果完全相等。[C-validate]

支持 replace/remove/insert_after；remove 不得带非空 replacement，其他操作 replacement 不得空白，完全相同 replace 作为 no_op_patch 拒绝。preserve 中每个字符串必须同时存在于原文与最终片段；它只是子串保留条件，不是结构或归属不变。[C-validate]

### 原子性、重放与重叠

`apply_patches` 先检查整批：要求绑定字段，重新 validate，每个 issue_id 只能出现一次；按原始 start/end 排序；任何相邻区间相交即抛 overlapping_patches，完全不返回部分成功。insert_after 也保护整个 anchor 区间，避免同一锚点插入顺序歧义与插入/删除冲突。全通过才逆序改字符串，返回完整 applied、空 skipped。[C-apply]

同一不可变输入执行两次得到相同输出；旧 patch 用于新的稿件快照则被 base/块/原文绑定拒绝。没有状态化的“已应用成功”记录，拒绝 stale 与幂等重放成功不是同一承诺。[C-apply]

### 证据与语义边界

- scientific/missing 必须选择已存在、正文非空的材料；summary 不能代替实际 text。
- proposal evidence_ids 只能是 issue 已选材料的无重复子集；scientific/missing 不能缩到空。
- 新 citation 必须由所选材料的 source_handles、材料 ID 或 exact source_identity_map 支持；不按标题/作者做模糊匹配。
- editorial 不允许新增 citation 或新增 `_NUMBER` 匹配值；该检查是集合差，不保留顺序、次数、符号、单位、条件或数值归属。[C-evidence][C-validate]

实测均被 editorial 接受：`+10→-10`；`10 and 20→20 and 10`；删掉数字；删掉已有 `[P001]`。引用集合没新增也不能证明“旧引用位置/归属没变”。remove 可删引用，除非 preserve 显式要求保留。模块开头本身也说明不证明科学蕴含。[C-validate][C-tests]

## 三、scoped_body_revision：issue 原子组，不是 article 的块定位增强开关

### 可复用能力

`index_blocks` 生成精确段落/表格/围栏块、start/end、sha256、content，以及由真实章标题或单章保存 segment 推导的章归属；边界分隔符留在编辑区间外。块序号同样只属于当前快照。这套切块方式还在 heading 开始处切开，与 contracts 的按空行切块并不相同；两者 ID/区间不能直接互换。[S-index]

reader payload 用完整有序 blocks+separator_before+trailing_separator 无损表示原 BODY。editor payload 保留全文只读导航、当前 issue 的明确 editable_blocks、邻块、块导航和材料 atoms；只有 target_block_ids 可写。[S-payload]

### 解析与原子组

reader_parser 对 issue_id、target_block_ids、related_task_ids、source_handles、reason、goal 校验；坏 issue 带 validation_errors 留在 pending，其他好 issue 不丢。[S-reader]

group_parser 支持 revision 或材料 reread，两者不能混在一个响应。每个 issue 最多一个 group 或 rejection。group 必须 replace_block、匹配 original_body_sha256，所有 replacement 的块必须属于该 issue 的可编辑集合，且原块 hash 与快照实际文本一致。组内任一 replacement 有错，整组拒绝；其他好组保留。[S-group]

同响应不同组抢同一个块，先被接受的组占用该块，后组 pending；重复 issue 的多个组通过 counts 使它们都不能部分应用。apply_groups 再检查原 BODY 哈希、原块区间/哈希，以及所有实际区间重叠，然后逆序替换。空 content 可删除块。[S-group][S-apply]

### 部分成功及恢复

运行层逐 issue 执行；所有候选始终从 original+已接受 groups 重新计算。它要求 transport 完整、无 call_error 且 protocol_complete 后，才允许消费包含 pending 的有效部分；截断响应的好组不会被当作安全部分。每组还进行全稿结构验证；失败只挂起该问题，已经接受的其他组保留。跨调用组重叠由 apply_groups 拦住，后来的组失败。[S-run]

结果区分 body_complete、revision_complete、reader_revision_status=complete/partial/pending，保留 accepted_groups、pending_issues、rejected_issues、block index、原稿、stage lineage、成本与 SHA。最后整体结构失败则回原稿并清空 groups。已有 complete 选定版本不被本次不完整版本覆盖。[S-result]

缓存/RAW 恢复复用 fullbody transport；失败组可在显式重试中重做。定位错误并没有自动追加成一轮“修补格式”反馈：当前循环只有材料 reread 的新请求；无效 group 记录后进入下一个 issue。[S-run][F-cache]

## 四、fullbody_writer.reader_revision：全文 anchor，按问题窗口做修订

### 解析、目标与范围

`_object` 支持含 content 的响应、JSON/围栏 JSON、strict=False 与特定内部引号恢复。`_reader_parser` 对缺失/重复 issue_id 赋确定性 ID；anchor/problem 无效或 anchor 全文不唯一就留 pending_reader_issues，其他问题继续。[F-object][F-reader]

`_patch_parser` 要求所有期望 issue 被 patches 或明确 rejection 覆盖。一个 issue 可有多处非重叠补丁；改到不同于 reader.anchor 的位置必须填 reason，但不会验证 reason 的语义相关性。replacement 可为空。该路线不需要 block ID 或每个 patch 自带 hash；原稿安全来自同一 original 上的精确 anchor 解析与上层 base 验证。[F-patch][F-base]

一个补丁格式错、遗漏 issue 或同窗口 anchor 重叠，会让窗口解析失败，不像 scoped 那样解析后留下窗口内的好组。跨窗口则把已有 patches+新窗口 patches 一起套回 original，重叠会拒绝整个新窗口并保留此前窗口。[F-patch][F-apply][F-run]

### 证据扩展与部分成功

每个修订窗口仍携带完整 original BODY。窗口按完整 issue 的实际容量分批。实际 proposed target 涉及额外来源时，先扩展完整来源材料再让作者重新评估，受 max_rereads_per_window 限制。若扩展/后续阶段失败，已知不需要新增材料的 supported_fallback patches 可以保留，仍须全稿结构检查和无重叠。[F-evidence][F-run]

这不是自动目标定位修复循环。`_reject_outcome` 会将失败 status/required_action/issue 写回 RESULT，允许显式重试；没有把该定位错误自动加进下次模型消息。reader 的坏定位问题可以长期留 pending；reader stage 本身 complete 时，重跑仍会复用其缓存。[F-reject][F-run][F-tests-partial]

### 恢复与反馈

`_execute_stage` 的 cache signature 包含 route/stage/mode、fixture hash、messages、effective profile、wire request、code hash、dependencies。保存 RAW 在 parse 之前；RESULT 缺失时可本地由 RAW 重建；有旧 complete attempt 时优先复用，即使后面存在中断 attempt。没有成功结果且未显式 retry_failed 时复用失败或报 uncertain_attempt，不静默重买。[F-cache]

此缓存层不是 article 的结果 seal 机制：当前实现读已有 RESULT 的 complete 标志选择 attempt，并没有在这段恢复代码中对 RESULT/RAW 做 article 式逐文件 seal 比对。不能把两者完整性能力混称。[F-cache][A-cache]

最终保留 accepted_patches、pending_reader_issues、部分修订状态，失效旧 task anchor 被移入 original_location；保留原 segments 与新 body 段，已有完整选定版本不被不完整版本覆盖，明确 semantic_quality_unreviewed。[F-result]

## 五、复用建议：现成部分与需要新增的部分

### 已实现，可以直接调用/借用

- 最小 JSON 提案解析：article.parse_edit_proposals。输入边界清楚，但别把未执行的 prompt 要求视为安全合同。
- 严格快照目标、校验与原子应用：contracts.normalize_case → resolve_target/validate_patch → apply_patches。适合离线演示，也适合给新的接口薄适配；建议以这一套自身的 case/patch 格式贯通，保持错误抛出语义。
- issue 多块原子性与分项 pending：scoped.group_parser/apply_groups 的合同与报告结构。它有独立块格式与证据/transport 前提，不是把函数名替换即可无缝接入。
- 已付结果恢复、请求身份与显式重试：各路线现有 stage 层。应该保留对应路线的缓存约定，不要混用两个不同 cache/retry 合同。

### 建议新增/改进，当前没有统一实现

- 外部工具层明确 snapshot_id、proposal_id/issue_id、状态码、完整绑定、实际影响范围、失败明细；保持完整原文，预览截短只能用于显示。
- 如果要处理同一回复中的部分成功，明确原子单元是整个 batch 还是 issue group；不能把 article“先到先得”、contracts“整批拒绝”、scoped“整组拒绝”混成一个不透明策略。
- 将成功状态和内容变化分开，区分 no_op/already_applied/stale_target/ambiguous_target/conflict/invalid_proposal；支持 old patch 已应用的确认，需要可验证凭据，不应以“目标没了”猜测。
- 需要跨版本稳定块身份时，要新增持久 ID/版本迁移策略；当前顺序 ID 不满足此要求。
- 若需要全字节保真，入口和输出应全程保持同一个无归一化快照；当前 article 的文件入口不是该保证。
- 若要模型根据具体目标错误重提补丁，需要显式设计反馈闭环；当前自动新请求主要用于材料扩展，不是通用 target repair。
- 科学语义保真仍需专门审查。数字/引用集合或 preserve 子串不能取代数字—对象—条件—来源对应关系检查。

## 六、离线验证与交付物

- `source/`：20 份固定提交源码/提示词/测试副本。全部本地 Git blob SHA 与连接器返回值一致，清单见 source_manifest.json。
- `logs/original_contract_tests.log`：原仓库 test_post_body_revision_contracts.py 以 unittest 直接运行，12/12 通过。运行前检查了导入图，只有标准库与 portable_paths/citations_in；socket connect/connect_ex/create_connection 禁用，网络尝试 0。
- `logs/boundary_probes.log`：直接调用原 article/contracts 函数的最小反例，确认错误块 ID 仍生效、非普遍幂等、重叠出现漏算、数字/引用弱约束、顺序 ID 重排、fixture no_change CRLF 归一。
- 其余 article retry、scoped、fullbody integration 恢复测试只读源码，未执行。没有声称全仓测试通过；没有跑付费或真实 provider 流程；没有搭建新测试平台。

### 已有恢复测试的具体证据（下列仅静态审阅）

- article 的快照非级联、后续重叠跳过、缺失目标不能凭别处 replacement 判已应用：[test_article_edit_live.py 43–68](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/test_article_edit_live.py#L43-L68)。
- article 的 SSE 免费恢复、截断响应不重新购买、输入身份变化拒绝：[同文件 84–117](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/test_article_edit_live.py#L84-L117)。
- article 的失败保存、普通 resume 不调 factory、显式 retry 新 attempt、完成缓存复用、删 report/seal 后免费 RAW 重放：[test_article_edit_retry_handoff.py 64–111](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_article_edit_retry_handoff.py#L64-L111)。测试工厂是受控本地返回，命名 live 不代表真实模型。
- article 的既有 rerun 测试只验证“原目标已消失”或“尾部追加已在目标处”；不能推广为任意 replace 幂等：[test_review_delivery_editing.py 137–154](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_review_delivery_editing.py#L137-L154)，[382–410](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_review_delivery_editing.py#L382-L410)。
- scoped 的坏组不污染好组、多块组哈希失败整体拒绝：[test_scoped_body_revision.py 107–131](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_scoped_body_revision.py#L107-L131)；部分成功、跨 issue 重叠后保留先前好组：[170–192](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_scoped_body_revision.py#L170-L192)；重复 group 不允许部分执行：[207–230](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_scoped_body_revision.py#L207-L230)。
- fullbody 的 RAW 重建优先于后来中断 attempt、失败须显式 retry：[test_fullbody_engine.py 216–236](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_fullbody_engine.py#L216-L236)；reader 缓存续用：[333–340](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_fullbody_engine.py#L333-L340)；坏 reader issue 仍 pending、有效编辑保留且重跑不再调用：[481–503](https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_fullbody_engine.py#L481-L503)。

### 根线程可用的离线导入

把 source/ 放入 PYTHONPATH 后，以下两组原模块可直接导入而不创建任何模型 client：

```python
from optomind_research.runtime.upgrade3.article_text_editor import parse_edit_proposals, apply_text_edits
from optomind_research.runtime.upgrade3.post_body_revision_contracts import normalize_case, resolve_target, validate_patch, apply_patches
```

纯函数正常路径不做磁盘写入或网络操作。`normalize_case` 返回新字典和新 blocks；`apply_patches` 输出新文本，不写 baseline 文件。

## 固定提交源码链接

[A-pass]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L35-L66
[A-parse]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L69-L113
[A-apply]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L116-L165
[A-offline]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L168-L263
[A-cache]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L266-L299
[A-live]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/article_text_editor.py#L300-L369
[A-prompt]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/prompts/article_text_editor.md#L1-L35
[U-cache]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/unit_realization.py#L351-L453
[C-blocks]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/post_body_revision_contracts.py#L39-L114
[C-load]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/post_body_revision_contracts.py#L117-L141
[C-target]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/post_body_revision_contracts.py#L144-L161
[C-evidence]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/post_body_revision_contracts.py#L164-L212
[C-validate]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/post_body_revision_contracts.py#L215-L267
[C-apply]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/post_body_revision_contracts.py#L270-L303
[C-tests]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_post_body_revision_contracts.py#L23-L167
[S-index]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/scoped_body_revision.py#L35-L97
[S-reader]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/scoped_body_revision.py#L100-L130
[S-group]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/scoped_body_revision.py#L133-L204
[S-apply]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/scoped_body_revision.py#L207-L225
[S-payload]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/scoped_body_revision.py#L240-L288
[S-run]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/scoped_body_revision.py#L298-L394
[S-result]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/scoped_body_revision.py#L395-L444
[F-object]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L84-L106
[F-reader]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L143-L182
[F-patch]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L185-L224
[F-base]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L615-L634
[F-apply]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L739-L752
[F-evidence]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L683-L736
[F-cache]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L250-L342
[F-reject]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L345-L354
[F-run]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L851-L945
[F-result]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/optomind_research/runtime/upgrade3/fullbody_writer.py#L1099-L1206
[F-tests-partial]: https://github.com/Lihonggang-scnu/OptoMInd-Review/blob/2a28854cf86a98f5afb4a6e5fbe1a925694fd138/tests/upgrade3/test_fullbody_engine.py#L481-L503
