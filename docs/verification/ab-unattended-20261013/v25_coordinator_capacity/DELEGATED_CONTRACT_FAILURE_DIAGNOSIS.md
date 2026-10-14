# V28 owner contract repair failure diagnosis

Gate: `cb099e5222d87a4e10ee9a01fd81704d1ca7f62e0cf99f1082c7670bf9f93fc7`.

## Result

一次合同修复机制已生效：实际调用为 qwen3.5-plus，原材料、原计划和首轮错误回答均进入请求；回答完整返回，原生分类器拒绝不合格结果，保留有效原计划。目标合同仍未满足，不能把“调用成功”写成“修复成功”。

实际请求同时携带两套结构：`chapter.substantive_threads` 为 5 项，`chapter_plan.units` 为 4 项；原单元字段为 `cases / ordered_development / paragraph_briefs / relations_limits / substantive_point / transition`，没有显式稳定 ID，`unit_identity_contract.existing_unit_ids=[]`。两者均处于原负责人输入顶层，没有明确的编辑对象与背景来源优先级。

V28 的实际 reasoning 明确写道：`Looking at the original plan, I can see it has 5 substantive_threads but no explicit unit_id values.` 随后决定为每个 thread 赋 ID。这直接支持“编辑对象锚错”的解释，超出仅凭返回字段猜测模型行为的程度。

实际回答 `updated_plan` 包含 `chapter_id / directed_reads / purpose / scope / source_handles / substantive_threads / supplement_requests / title / transition_logic` 等背景章字段，仍无 `units`；同时制造 `U01` 至 `U05` 的自映射。原输入没有这些旧 ID，映射无法验证。原生错误为 `structural_unit_change_old_units_missing_stable_ids` 和 `unit_id_remap_references_unknown_unit`。

## Input causes and boundaries

1. 主系统任务要求依据反馈修改完整章节计划，但没有把 `chapter_plan` 指定为唯一编辑对象，也没有声明 `chapter.substantive_threads` 是前序背景结构。两套表示的字段名与粒度不同，形成歧义。
2. 一次修复把首轮错误回答完整放在 `contract_repair.previous_response`，保留证据是正确行为，但错误章壳再次作为醒目的可复制结构出现。修复说明虽要求 `updated_plan.units` 和无旧 ID 时保留结构，却仅嵌套在巨大的 JSON payload 中；外层系统任务和末尾交付说明仍沿用原一般负责人任务，未明确提升本次唯一合同修复目标。
3. 泛化诊断 `structural_unit_change_requires_explicit_unit_ids_and_remap` 容易被解释为“补 ID 和 remap”，没有在诊断层直接说明“当前结果缺少 updated_plan.units，且你选用了非编辑背景对象”。V28 实际采取了补 ID/remap，而没有改变错误输出容器。
4. 这不是已观察到的容量截断：`finish_reason=stop`、`complete=true`、`near_limit=false`。输入约 223315 tokens，回答约 7729 completion tokens；此信息只排除本次显式截断，不能证明长输入不会降低指令遵循。

## Minimal generic follow-up proposal, pending root approval

仅对已失败的合同修复使用明确版本的新合同，不改普通 owner/coordinator 主提示词、主输入和成功缓存指纹。已有四章成功和协调器先走原缓存命中，避免重买。

修复专用消息把 `chapter_plan` 指定为唯一可编辑当前计划；把原 `chapter` 全量保存为背景，不删除其中知识或材料，不允许将其章壳替代当前 `units`。保留被拒回答作为带来源、无编辑权威的失败历史。

以实际当前计划字段生成结构说明，而非填写科学内容：输出必须是完整 `updated_plan={thesis,reader_objective,units:[...]}`，单元继承输入实际字段和知识职责。原稳定 ID 集合为空时禁止猜旧 ID、制造不可验证拆合或为格式减少知识；单元数量限制来自当前输入，不能写死本次数字。

新 repair contract 应覆盖专用消息、结构要求、原合同和被拒响应，独立落盘并保持最多一次。旧 v1 claim、回答、失败和费用关联全部保留；不能重置旧 claim 冒充重试。是否真实调用由 root 决定。

首项免费验证：构造五项背景 threads 与四项当前 units、无旧 ID 的假客户端输入；检查真实发送消息将当前 units 指为唯一目标，所有原材料/背景逐值保留；预填其他成功 owner 与协调器缓存后只有失败章出现一个新合同调用；重启不重付，失败继续保留有效原计划。

## Evidence

- `supervision/calls/<gate>/REQUEST.json`: actual two messages, original structures, repair instructions and profile.
- `supervision/calls/<gate>/RESPONSE.json`: actual content, reasoning, remap and usage/capacity telemetry.
- `supervision/calls/<gate>/REQUEST_DECISION.json` and `RESPONSE_DECISION.json`: original payload preservation review and permission to reject the response, not adopt it.
- `preparation/ab_unattended_20261013/owner_contract_recovery_20261014/ROOT_V28_REPAIR_REQUEST_REVIEW.json` and `ROOT_V28_REPAIR_RESPONSE_REVIEW.json`: root review and native errors.
- `worktree/optomind_research/runtime/upgrade3/progressive_review_plan.py`: ordinary owner instructions, message rendering and existing repair payload construction.

No production source, gates, ledgers or runtime state changed during this diagnosis; no new model calls made. This document is the sole diagnosis artifact. Root subsequently approved implementation in the source worktree and targeted tests; no new paid request was dispatched by this agent.


## Approved V2 implementation and actual-input message preview

The following is rendered offline from the actual V27 input and V27/V28 returned answers. It is not evidence of a newly paid live model call. Ordinary owner messages and saved owner contract remain identical. V1 files were only read; V2 uses a separate compatibility hash and exclusive claim. Background and scientific material values are preserved exactly.

New IDs on unchanged valid units are accepted; only mappings to nonexistent old IDs are rejected. The existing `_chapter_details_record_error` checks minimum substantive content; exact type-schema echoes are rejected separately. This adds no scientific quality judge.

```json
{
  "preview_only_no_paid_call": true,
  "original_owner_messages_byte_identical": true,
  "original_owner_contract_matches_saved": true,
  "original_owner_contract": "7ed9bd6fc32c0ed9e29d022c3a8a9a7cab96751112e28de1f00e97598fb4921e",
  "v28_request_sha256": "d640437cebb1f8ff952f7417dcad5de95ad0ed727f0684720a6c5ace82da2e35",
  "v28_editable_ambiguity": {
    "chapter_substantive_threads": 5,
    "chapter_plan_units": 4
  },
  "v2_chapter_identity_only": {
    "chapter_id": "CH3"
  },
  "v2_chapter_background_full_equal": true,
  "v2_current_plan_full_equal": true,
  "v2_other_original_fields_full_equal": true,
  "v2_rejected_responses_full_equal": [
    true,
    true
  ],
  "v2_existing_unit_ids": [],
  "v2_current_unit_count": 4,
  "v2_plan_schema_fields": [
    "reader_objective",
    "thesis",
    "units"
  ],
  "v2_unit_schema_fields": [
    "cases",
    "ordered_development",
    "paragraph_briefs",
    "relations_limits",
    "substantive_point",
    "transition"
  ],
  "v2_repair_cache_contract": "fd553e33ea79de83fe07613272f99561250e7ec7fd733b6434b42c5b44c9a081",
  "v2_call_id": "owner-repair-v2-CH3-eb845c5397f4a8bf550e18b6393925921465a034",
  "settings_unchanged": {
    "model": "qwen3.5-plus",
    "output_tokens": 32768,
    "thinking_budget": 16384
  },
  "tests": "48 passed: owner_contract_repair + owner_recovery_contract"
}
```
