# Cache / resume / material-persistence audit

审计日期：2026-10-02（Asia/Shanghai）

范围：当前 worktree 的 `progressive_review_plan.py`、`planning_retrieval_loop.py`、`chapter_arrangement.py`、`review_unit_writer.py` 及编排脚本的写作输入交接。此次只读审计没有改源码，没有联网或调用模型，也没有读写 writer PID 29080。下述复现均使用内存假 planner、零网络回调和临时目录；临时目录在复现后不作为生产输入。

## 已确认的问题

### P0：同一论文的旧精读会被当成新问题的答案

触发条件：同一个 `paper_id` 已存在于 `read_materials` 或 `prior_readings`，下一次 directed need 的 `questions`、`required_outputs`、`knowledge_gap` 或用途发生变化。

路径：

- `optomind_research/runtime/upgrade3/progressive_review_plan.py:6023-6049` 的 `make_retrieval_loop_runner.external_closure` 只按 `paper_id` 命中 `read_materials`，直接返回 `reused_prior_deep_read`，没有比较当前问题或必需输出。
- 同文件 `:6148-6175` 的 `make_directed_reading_runner.read_one` 也只按 `paper_id` 命中 `prior_by_id`，在问题归一化之前返回。

零网络复现：旧材料只回答 `OLD question`，新任务要求 `NEW question`，reader 被设置为一旦调用就失败；结果仍为 `status=answered`，`usable_content` 为旧答案，`still_missing` 为空，reader 调用次数为 0。

影响：新的证据缺口被错误清空，后续 chapter packet 和 writer 会把不回答当前问题的旧材料当成已完成的精读证据。

### P0/P1：章节级工具材料在编排输入中没有进入模型契约，特定交接下会在 writer 前丢失

路径：

- `optomind_research/runtime/upgrade3/chapter_arrangement.py:248-332` 的 `ChapterView.arrangement_payload()` 返回 sources、uses、open_questions 等摘要，但没有 `chapter_tool_materials`。
- `optomind_research/runtime/upgrade3/review_unit_writer.py:707-708` 的 `build_unit_view()` 只从 `arrangement.get("chapter_tool_materials")` 读取该字段。
- `scripts/upgrade3/chapter_arrangement.py:512` 和 `runtime/upgrade3/review_delivery.py:320` 的导出辅助会额外写入该字段，因此是否在当前生产 CLI 中必然丢失取决于最终使用的导出路径。

零网络临时 packet 复现：`build_chapter_view()` 保留 1 条多来源/未归属工具材料；`arrangement_payload()` 不含该字段；模拟 arrangement JSON 不含该字段时，`build_unit_view()` 传给 writer 的 `chapter_tool_materials` 数量为 0。故已确认模型侧无法看到这类实际材料；“最终 writer 一定丢失”是有条件的，只有未走上述导出补字段的交接路径才发生。

影响：多来源或尚未绑定单一 source handle 的检索材料不能参与编排决策；在缺字段的交接路径中，writer 完全看不到该材料，内容和用途同时丢失。

### P1：检索 journal 的 resume 只识别 need 形状，不识别底层材料/triage 输入变化

路径：`optomind_research/runtime/upgrade3/planning_retrieval_loop.py:134-136` 的 `_need_signature()` 只序列化 `InformationNeed`；`:224-237` 的 `RetrievalJournal.has()` 只按 `need_id`、round 和该 signature 命中。它不包含 material index 内容、local triage 结果、prompt/model、candidate pool 或 runner 配置。

零网络复现：同一 need 首次 local triage 返回 `OLD_INDEX` 并写 journal；第二次使用完全相同的 need、同一 journal，但 triage 返回 `NEW_INDEX`，resume 结果仍为 `OLD_INDEX`，journal entries 仍为 1，triage 没有再次执行。

影响：材料索引更新、A/B 内容补齐、triage prompt/model 更换后，resume 仍可能把旧的已回答结果当成当前答案；这是内容陈旧，不是单纯状态展示问题。

### P1：普通阶段缓存允许无输入签名的旧结果直接复用

路径：`optomind_research/runtime/upgrade3/progressive_review_plan.py:2378-2397`。当 `cache_inputs is None` 时，`:2380-2383` 只要 stage JSON 存在就返回；只有调用者提供 `cache_inputs` 时才写入和比较 `RUN_STATE.stage_inputs`。当前 `run()` 中以下调用没有输入签名：`provisional_scope` (`:2927`)、`level1_outline` (`:2960`)、`chapter_proposals` (`:2988`，且其内部缓存也无签名)、`harmonized_scope` (`:2996`)、`finalize_chapter_scope` (`:3029`)；动态 legacy tool cycle (`:2867`) 也没有签名。resume 入口只校验 topic/planning mode（约 `:2905-2910`），不校验 pool、plan、内容、prompt、model 或配置。

零网络复现：同一临时输出目录先让 `provisional_scope` 返回 `OLD`，再让同名 stage 返回 `NEW` 并 resume；第二次 planner 调用次数为 0，结果仍为 `OLD`，该 stage 的 `stage_inputs` 为空。

影响：改了 source pool、原始 plan、prompt/model 或关键配置后，resume 会跳过本应重算的阶段，并把旧提纲、章节分工或工具结果传播到后续 packet。

### P1：source routing cache 不包含候选内容或共享提纲签名

路径：`progressive_review_plan.py:2521-2545` 组装包含 B material、补充材料和共享提纲的候选 batch；但 `:2679-2687` 保存的 batch cache 只有 handles、routes、telemetry，`:2668-2675` resume 只读取该文件和 route status，没有比较候选内容、shared outline、chapter ids、prompt 或 model。

零网络复现：第一次 shared outline 标题为 `OLD`，fake planner 返回 `route-OLD`；第二次同一 handles、标题为 `NEW` resume，planner 调用仍为 1，返回仍为 `route-OLD`。

影响：章节用途或 B 材料发生变化后，来源仍被旧章节路由；来源可能因此不进入正确的 chapter、case 或 material packet。

### P1：adaptive/legacy retrieval tool cache 可能永久保留 partial，且忽略 pool/plan/source map

路径：adaptive 分支 `progressive_review_plan.py:2764-2769` 的 `cache_inputs` 只有 supplement requests、directed requests、prior tool results，缺少 `pool_rows`、`plan`、`source_handle_map`、phase/config、prompt/model；legacy 分支的 `_tool_cycle`（约 `:2867`）没有 `cache_inputs`。

零网络复现一：fake retrieval runner 第一次看到 pool marker `OLD` 返回结果；第二次 marker 改为 `NEW`、plan 也改变并 resume，runner 调用次数为 1，结果仍带 `OLD`。

零网络复现二：第一次 runner 返回 `status=partial`，第二次若被调用本应返回 complete；由于 stage cache 命中，调用次数为 1，第二次仍为同一 partial 结果。

影响：新的池、章节计划或 source mapping 不会触发重新检索；provider 暂时失败或材料不完整的 partial 结果可能阻止后续重试，导致 chapter packet 缺材料。

### P1：正常 chapter detail packet 的 resume 检查漏掉章节和反馈内容

路径：`progressive_review_plan.py:4333-4348` 的 `build_one()` 只比较 `_adaptive_input_materials`、`source_materials`，planning revision 时再比较 `candidate_navigation` 和 `candidate_materials`。它没有比较 chapter title/scope/thesis、shared outline、research question、`relevant_tool_feedback`、citation rules、prompt/model 等；adaptive batch helper 的签名（约 `:1256-1275`）只覆盖走 batch 分支的调用。

零网络复现：第一次章节标题为 `OLD`，planner 产生 `generated-OLD`；第二次标题改为 `NEW` resume，planner 调用次数为 1，packet 标题和 thesis 仍是旧值。单独把 tool feedback gap 从 `old` 改为 `new` 也会复用旧 feedback，调用次数为 1。

影响：owner/工具反馈已经改变但 writer packet 仍使用旧 chapter plan；新材料可以在上游存在，却不改变最终写作输入。

### P1：final plan 默认把 partial tool/retrieval 结果标为 complete

路径：`progressive_review_plan.py:4805-4825` 直接设置 `final["status"] = "complete"`，之后 `:4842-4847` 只在 `owner_revision_unresolved` 存在时改为 partial。`level1_tools`、`level2_tools`、`chapter_tools` 的 partial/failed/unavailable 状态没有参与最终状态门。

零网络直接调用 `_assemble_final()`，传入 level1 和 chapter tool `status=partial`、无 owner unresolved；返回 `final_status=complete`，同时 `planning_tool_results` 仍含 partial。

影响：缺检索或缺材料的计划会看起来已经完成，生产链可能继续把不完整 packet 交给 writer，且缺口不再由顶层状态显式提示。

### P1：formal case attach 只检查 source 对象存在，不检查对象是否含实际材料

路径：`progressive_review_plan.py:5384-5408`。direct case 分支接受 `all_sources` 中存在的 source，或由 candidate 构造出的 payload；条件是 `if not source`，没有调用 `_material_content()` 或检查 A/B/deep/supplement 内容。与函数 docstring `:5312-5314` 所说“no material 不 promotion”不一致。

零网络复现：`source_materials` 只有 `source_handle`/`paper_id` 的 identity-only row，case response 指向该 handle；`supporting_studies` 被接受，packet 中仍是 identity-only source。

影响：空材料来源可进入正式 case、编排和 writer。writer 目前在 `review_unit_writer.py:717-723` 只记录 `sources_without_any_material` warning，不阻止写作，因此可能把没有证据内容的案例当作可用案例。

### P2：route_result_missing 的 repair 只尝试一次，之后被标记 attempted

路径：`progressive_review_plan.py:2631-2648` 将 `repair_attempted_handles` 排除在 pending 之外；`:2650-2665` 把所有 pending handle 写入 attempted，即使 repair 仍返回 `route_result_missing`；`:2673-2675` 下次 resume 因此直接返回缓存。

零网络复现：fake planner 两次都漏掉 P0002；初次及一次 repair 后 P0002 仍是 `route_result_missing`，后续 resume planner 调用次数不再增加。

影响：未路由来源不会进入章节的 source route/case 路径，且后续 resume 不会再给它机会。相比上面的问题，这是较低优先级的内容遗漏，但仍会让池中的来源永久脱离下游。

## 已核对的 owner/case 行为

本轮没有复现“owner 失败后被当成成功采纳”的问题：

- `affected_chapter_revision` cache（`progressive_review_plan.py:3985-4015`）只有 `status=complete` 且 `owner_status` 为 `updated`/`no_change` 并且 `cache_inputs` 完全相等时才复用；失败或 unresolved 不命中。
- `:4041-4057` 只把成功 owner response 放入 `chapter_updates`，失败写入 `owner_revision_unresolved`；`:4064-4073` 只应用带 `_complete_chapter_revision=True` 的完整更新，未采纳的全局反馈不会直接覆盖原 chapter plan。
- `:4074-4078` 只对已完成 owner review 的章节归档 case suggestions；未完成 owner review 的 suggestions 留在 plan 中，下一次仍可处理。

但这些 cache 仍没有单独记录 prompt/model 版本；如果只替换 prompt/model 而 revision payload 不变，仍属于上面“输入签名不足”的未失效情形。

## 尚未用生产 run 全链确认的风险

以下项目已有 helper 级复现或代码路径证据，但本轮没有启动生产 run，也没有改写现有 stages/chapters：

1. 当前 run 的完整 CLI 是否始终经过会补写 `chapter_tool_materials` 的 `_export`/delivery 路径；因此 arrangement model 的字段缺失已确认，writer 的无条件丢失仍取决于实际导出路径。
2. 真实五章 packet 在 pool、source routing、retrieval journal 同时更新后，是否恰好命中上述旧 cache；本轮只用临时目录验证命中机制，未碰现有生产缓存。
3. 真实 provider 返回的 partial/failed 结构是否总是落入 `_tool_cycle` stage JSON；若是，P1 partial 复用和 final complete 两项会同时生效，需在生产 resume 前单独核对现有 JSON 状态。
4. `scripts/upgrade3/chapter_arrangement.py` 的正常导出是否覆盖所有 writer 入口（包括人工/临时 view→arrangement 流程）尚未做进程级确认。

本审计未产生源码改动、模型调用、网络调用或 writer 进程副作用。
