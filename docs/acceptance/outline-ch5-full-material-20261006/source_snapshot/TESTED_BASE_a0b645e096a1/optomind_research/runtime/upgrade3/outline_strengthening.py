"""Generic autonomous outline strengthening seam.

The seam deliberately stays between the existing planner response contract and
chapter arrangement.  It gives a model the original plan, complete supplied
material, and read-only neighbouring duties so the model can decide whether a
useful change exists.  It never accepts a hand-written issue list or a prior
candidate as corrective evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import progressive_review_plan as planning


STAGE = "affected_chapter_revision"
# The standalone entry point defaults to the quality-capacity profile.  The
# Plus profile remains available only when a caller names it explicitly.
ROLE = "strong_outline"

_AUTONOMOUS_SCOPE_CONTRACT = (
    "【自主范围与返回合同】本次输入的 chapter_plan 只包含并只允许修改 payload.modifiable_unit_ids。"
    "这里的 complete updated_plan 指对这组可编辑单元的完整计划；可以在这些单元内拆分或合并，但必须保留稳定新 unit_id 并提供有效 unit_id_remap。"
    "readonly_neighbor_unit_roles 只提供邻章职责参照，绝不能作为本轮返回任务或被改写。"
    "不得返回只读邻居单元、虚构来源或输入中没有实质材料的 source_handle。"
)

_FORBIDDEN_CONTROL_KEYS = frozenset({
    "review_targets", "expected_answers", "known_issues", "prior_candidate",
    "max_outline", "revised_outline", "edited_body", "problem_list",
})

_REVIEW_RESPONSE_CONTROL_KEYS = frozenset({
    "updated_plan", "chapter_updates", "body", "manuscript", "replacement_text",
    "edited_body", "source_materials", "candidate_materials",
})

_MATERIAL_CHANNELS = (
    "source_materials", "candidate_materials", "candidate_navigation", "tool_materials",
)
_MATERIAL_SUBTREE_KEYS = frozenset({
    "study_summary_A", "review_planning_B", "deep_read_material", "deep_read_materials",
    "supplement_material", "supplement_materials", "supplement_gap_material",
    "supplement_gap_materials", "local_passages", "local_passages_variants",
    "tool_supplement_materials", "tool_materials", "usable_content",
})
_MATERIAL_DEDUPE_MIN_CHARS = 256
_MATERIAL_REF_KEY = "$material_ref"


class OutlineStrengtheningError(ValueError):
    """Raised when a strengthening request cannot satisfy its input contract."""


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _material_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _fingerprint(left) == _fingerprint(right)


def _json_pointer(path: Sequence[Any]) -> str:
    return "#" + "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


def _pointer_parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("#/"):
        raise OutlineStrengtheningError("material_ref_pointer_invalid")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[2:].split("/")]


def _pointer_get(root: Any, pointer: str) -> Any:
    value = root
    for part in _pointer_parts(pointer):
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, Mapping):
            value = value[part]
        else:
            raise OutlineStrengtheningError("material_ref_pointer_target_missing")
    return value


def _material_subtree_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))


def model_material_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create a lossless model-only view with refs for repeated large material subtrees.

    Only named evidence/material channels are traversed.  All original fields,
    identities, handles, plans, bodies and duties remain in the returned full
    payload or can be restored by :func:`expand_material_projection`.
    """

    _verify_envelope(payload)
    original = _json_copy(payload)
    seen: dict[str, str] = {}
    ref_count = 0
    saved_chars = 0

    def walk(value: Any, path: list[Any], *, in_material_channel: bool = False) -> Any:
        nonlocal ref_count, saved_chars
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, child in value.items():
                child_path = [*path, key]
                if in_material_channel and str(key) in _MATERIAL_SUBTREE_KEYS:
                    fingerprint = _fingerprint(child)
                    size = _material_subtree_size(child)
                    if size >= _MATERIAL_DEDUPE_MIN_CHARS and fingerprint in seen:
                        output[str(key)] = {_MATERIAL_REF_KEY: seen[fingerprint]}
                        ref_count += 1
                        saved_chars += max(0, size - len(seen[fingerprint]) - 20)
                        continue
                    if size >= _MATERIAL_DEDUPE_MIN_CHARS:
                        seen[fingerprint] = _json_pointer(child_path)
                    output[str(key)] = walk(child, child_path, in_material_channel=True)
                else:
                    child_channel = in_material_channel or str(key) in _MATERIAL_CHANNELS
                    output[str(key)] = walk(child, child_path, in_material_channel=child_channel)
            return output
        if isinstance(value, list):
            return [walk(child, [*path, index], in_material_channel=in_material_channel)
                    for index, child in enumerate(value)]
        return value

    projected = walk(original, [])
    expanded = expand_material_projection(projected)
    if expanded != original:
        raise OutlineStrengtheningError("material_projection_not_lossless")
    return {
        "payload": projected,
        "original_payload_sha256": _fingerprint(original),
        "projected_payload_sha256": _fingerprint(projected),
        "material_ref_count": ref_count,
        "saved_chars_estimate": saved_chars,
        "dedupe_min_chars": _MATERIAL_DEDUPE_MIN_CHARS,
    }


def expand_material_projection(projected_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expand all current-request material refs back to the original JSON tree."""

    root = _json_copy(projected_payload)
    resolving: set[str] = set()

    def expand(value: Any) -> Any:
        if isinstance(value, Mapping):
            if set(value) == {_MATERIAL_REF_KEY}:
                pointer = str(value[_MATERIAL_REF_KEY])
                if pointer in resolving:
                    raise OutlineStrengtheningError("material_ref_cycle")
                resolving.add(pointer)
                resolved = expand(_pointer_get(root, pointer))
                resolving.remove(pointer)
                return resolved
            return {str(key): expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value

    expanded = expand(root)
    return expanded


def _dedupe_exact_materials(
    rows: Sequence[Mapping[str, Any]],
    seen: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove only exact copies; unique fields and candidate identities survive."""

    output: list[dict[str, Any]] = []
    known = [dict(row) for row in seen if isinstance(row, Mapping)]
    removed = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        candidate = dict(row)
        if any(_material_equal(candidate, previous) for previous in [*known, *output]):
            removed += 1
            continue
        output.append(candidate)
    return output, removed


def _dedupe_navigation(
    navigation: Any,
    seen: Sequence[Mapping[str, Any]],
) -> tuple[Any, int]:
    if isinstance(navigation, Mapping):
        result = _json_copy(navigation)
        rows = result.get("candidate_materials")
        if isinstance(rows, list):
            result["candidate_materials"], removed = _dedupe_exact_materials(rows, seen)
            return result, removed
        return result, 0
    if isinstance(navigation, list):
        result, removed = _dedupe_exact_materials(navigation, seen)
        return result, removed
    return _json_copy(navigation), 0


def _assert_no_forbidden_controls(value: Any, *, path: str = "payload", recursive: bool = False) -> None:
    """Reject caller supplied answer controls without scanning source text.

    Real study material may legitimately contain fields such as
    ``known_issues``.  Only the request envelope is checked by default;
    structured review guidance is checked explicitly at its API boundary.
    """

    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold()
            if normalized in _FORBIDDEN_CONTROL_KEYS:
                raise OutlineStrengtheningError(f"forbidden_autonomous_control:{path}.{key}")
            if recursive:
                _assert_no_forbidden_controls(child, path=f"{path}.{key}", recursive=True)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if recursive:
            for index, child in enumerate(value):
                _assert_no_forbidden_controls(child, path=f"{path}[{index}]", recursive=True)


def _clean_ids(values: Sequence[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _input_integrity(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "chapter_plan_sha256": _fingerprint(payload.get("chapter_plan") or {}),
        "source_materials_sha256": _fingerprint(payload.get("source_materials") or []),
        "readonly_neighbor_unit_roles_sha256": _fingerprint(payload.get("readonly_neighbor_unit_roles") or []),
        "actual_local_body_sha256": hashlib.sha256(str(payload.get("actual_local_body") or "").encode("utf-8")).hexdigest(),
    }


def _verify_envelope(payload: Mapping[str, Any], *, allow_machine_review: bool = False) -> None:
    mode = payload.get("autonomous_outline_strengthening") if isinstance(payload, Mapping) else None
    if not isinstance(payload, Mapping) or not isinstance(mode, Mapping) or not mode.get("enabled"):
        raise OutlineStrengtheningError("autonomous_payload_required")
    _assert_no_forbidden_controls(payload)
    machine_review = bool(allow_machine_review and payload.get("review_feedback_origin") == "autonomous_reviewer")
    for key in ("chapter_feedback", "editorial_feedback_for_chapter", "case_suggestions"):
        if payload.get(key) and not machine_review:
            raise OutlineStrengtheningError(f"manual_feedback_not_allowed:{key}")
    expected = payload.get("input_integrity")
    if isinstance(expected, Mapping) and dict(expected) != _input_integrity(payload):
        raise OutlineStrengtheningError("autonomous_input_changed_after_prepare")
    modifiable = _clean_ids(payload.get("modifiable_unit_ids") or [])
    readonly = _clean_ids(payload.get("read_only_unit_ids") or [])
    if set(modifiable) & set(readonly):
        raise OutlineStrengtheningError("modifiable_readonly_unit_overlap")


def build_strengthening_payload(
    *,
    research_question: str,
    chapter_id: str,
    chapter_plan: Mapping[str, Any],
    source_materials: Sequence[Mapping[str, Any]],
    readonly_neighbor_unit_roles: Sequence[Mapping[str, Any]] = (),
    full_chapter_context: Mapping[str, Any] | None = None,
    chapter: Mapping[str, Any] | None = None,
    actual_local_body: str = "",
    shared_outline: Any = (),
    shared_scope: Mapping[str, Any] | None = None,
    review_argument: Any = "",
    review_argument_status: str = "",
    review_argument_source: str = "",
    source_identity_map: Mapping[str, Any] | None = None,
    candidate_materials: Sequence[Mapping[str, Any]] = (),
    candidate_navigation: Any = None,
    tool_materials: Sequence[Mapping[str, Any]] = (),
    modifiable_unit_ids: Sequence[str] | None = None,
    read_only_unit_ids: Sequence[str] | None = None,
    topic_id: str = "",
    call_id: str = "",
) -> dict[str, Any]:
    """Build an autonomous request from genuine current inputs.

    ``source_materials`` remains authoritative.  Candidate/navigation copies
    are dropped only when their complete records are byte-for-byte equivalent;
    records with additional material are kept.  Callers cannot supply manual
    findings through this function.
    """

    if not str(research_question).strip() or not str(chapter_id).strip():
        raise OutlineStrengtheningError("research_question_and_chapter_id_required")
    if not isinstance(chapter_plan, Mapping):
        raise OutlineStrengtheningError("chapter_plan_must_be_object")
    original_body = str(actual_local_body or "")
    base_sources = [dict(row) for row in source_materials if isinstance(row, Mapping)]
    candidate_rows, candidate_removed = _dedupe_exact_materials(candidate_materials, base_sources)
    navigation, navigation_removed = _dedupe_navigation(candidate_navigation, [*base_sources, *candidate_rows])
    payload: dict[str, Any] = {
        "topic_id": str(topic_id or ""),
        "research_question": str(research_question),
        "planning_revision_mode": True,
        "autonomous_outline_strengthening": {
            "enabled": True,
            "goal": "Inspect the supplied plan and evidence, then make only material-backed outline improvements worth carrying downstream.",
            "allow_no_change": True,
            "do_not_use_external_research": True,
            "do_not_add_evidence": True,
            "input_mode": "autonomous_from_current_evidence",
        },
        "call_id": str(call_id or f"autonomous-outline-{chapter_id}"),
        "chapter_id": str(chapter_id),
        "chapter": _json_copy(chapter or {"chapter_id": str(chapter_id)}),
        "chapter_plan": _json_copy(chapter_plan),
        "review_argument": _json_copy(review_argument),
        "review_argument_status": str(review_argument_status or ""),
        "review_argument_source": str(review_argument_source or ""),
        "shared_scope": _json_copy(shared_scope or {}),
        "shared_outline": _json_copy(shared_outline),
        # The autonomous path has no externally supplied issue list.
        "chapter_feedback": [],
        "editorial_feedback_for_chapter": [],
        "case_suggestions": [],
        "source_materials": base_sources,
        "source_identity_map": _json_copy(source_identity_map or {}),
        "candidate_materials": candidate_rows,
        "candidate_navigation": navigation if navigation is not None else {},
        "tool_materials": [dict(row) for row in tool_materials if isinstance(row, Mapping)],
        "readonly_neighbor_unit_roles": [dict(row) for row in readonly_neighbor_unit_roles if isinstance(row, Mapping)],
        "full_chapter_context": _json_copy(full_chapter_context or {}),
        "actual_local_body": original_body,
        "actual_local_body_sha256": hashlib.sha256(original_body.encode("utf-8")).hexdigest(),
        "unit_identity_contract": {
            "existing_unit_ids": [
                str(unit.get("unit_id") or unit.get("id") or "")
                for unit in chapter_plan.get("units") or ()
                if isinstance(unit, Mapping) and str(unit.get("unit_id") or unit.get("id") or "").strip()
            ],
            "new_unit_requires_explicit_unit_id": True,
            "split_or_merge_requires_unit_id_remap": True,
            "preserve_readonly_neighbor_units": True,
        },
        "required_behavior": {
            "return_complete_updated_plan_for_modifiable_units": True,
            "preserve_source_handles_conditions_argument_relations": True,
            "review_original_and_incremental_material_together": True,
            "do_not_add_evidence": True,
            "review_mediated_studies_identity_only_full_weight": True,
            "allow_no_change": True,
            "do_not_return_manuscript_body": True,
        },
        "material_transport": {
            "exact_candidate_copies_removed": candidate_removed + navigation_removed,
            "unique_candidate_materials_preserved": True,
            "source_materials_authoritative": True,
        },
    }
    _assert_no_forbidden_controls(review_argument, path="review_argument", recursive=True)
    inferred_modifiable = [
        str(unit.get("unit_id") or unit.get("id") or "")
        for unit in chapter_plan.get("units") or ()
        if isinstance(unit, Mapping) and str(unit.get("unit_id") or unit.get("id") or "").strip()
    ]
    inferred_readonly = [
        str(row.get("unit_id") or row.get("id") or "")
        for row in readonly_neighbor_unit_roles
        if isinstance(row, Mapping) and str(row.get("unit_id") or row.get("id") or "").strip()
    ]
    payload["modifiable_unit_ids"] = _clean_ids(modifiable_unit_ids or inferred_modifiable)
    payload["read_only_unit_ids"] = _clean_ids(read_only_unit_ids or inferred_readonly)
    payload["input_integrity"] = _input_integrity(payload)
    _verify_envelope(payload)
    return payload


def strengthening_messages(
    payload: Mapping[str, Any], *, allow_machine_review: bool = False,
    model_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Reuse the production owner contract with an autonomous task overlay."""

    _verify_envelope(payload, allow_machine_review=allow_machine_review)
    if model_payload is not None:
        if not isinstance(model_payload, Mapping):
            raise OutlineStrengtheningError("model_payload_must_be_object")
        messages = planning._messages_for(STAGE, model_payload)
    else:
        messages = planning._messages_for(STAGE, payload)
    autonomous = (
        "\n\n【自主细纲强化模式】"
        "本轮没有人工问题清单、预期答案或上一候选供你追随。请独立检查原始章节计划、完整材料、邻近单元职责、"
        "研究条件与论证关系，只有发现对组织、证据分工、条件边界或读者任务有实质收益的改动才修改；计划已经充分时返回 status=no_change。"
        "不得把本段指令当作事实，不得补充外部研究或正文；保持有效 source_handle、材料来源身份、研究转述身份、条件和 argument_relations。"
    )
    messages[0] = {**messages[0], "content": str(messages[0].get("content") or "") + autonomous + "\n\n" + _AUTONOMOUS_SCOPE_CONTRACT}
    messages[1] = {
        **messages[1],
        "content": str(messages[1].get("content") or "") + "\n\n" + _AUTONOMOUS_SCOPE_CONTRACT,
    }
    if model_payload is not None:
        transport_note = (
            "\n\n【材料传输说明】输入中的 $material_ref 是当前请求内同一材料通道中完整相同材料子树的 JSON pointer；"
            "它只节省重复传输，不表示材料缺失。请按 pointer 使用当前请求中的完整原内容，保持所有独有字段、来源身份、条件和限制。"
        )
        messages[0] = {**messages[0], "content": str(messages[0].get("content") or "") + transport_note}
        messages[1] = {**messages[1], "content": str(messages[1].get("content") or "") + transport_note}
    return messages


def reviewer_messages(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Build an independent, issue-only review request from the original packet."""

    _verify_envelope(payload)
    rendered_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    scope = (
        "本次审阅范围是 payload.modifiable_unit_ids 中的当前单元。"
        "readonly_neighbor_unit_roles 只用于理解邻章职责，不生成或修改邻章任务。"
        "每条意见必须绑定可编辑单元和本输入已有的实质材料句柄；保留来源身份、转述身份、条件、关系和证据限制。"
    )
    system = (
        "你是一个独立的章节计划审阅者。只审阅输入中已有计划与材料，不补充外部研究，不把自己的判断写成事实。"
        "检查论述组织、材料与判断的对应关系、研究条件和边界、对照与阴性结果、跨研究比较以及重复用途；"
        "只有对读者任务或证据安排有实质收益时才提出意见，计划充分时可以没有意见。"
        + scope
        + "只输出JSON对象，不输出正文、替换计划或解释性散文。"
    )
    user = (
        "请独立审阅下面的完整输入。返回且只返回："
        "{\"status\":\"reviewed\"或\"no_change\",\"issues\":["
        "{\"issue_id\":\"...\",\"affected_unit_ids\":[\"...\"],"
        "\"description\":\"具体可核查的问题\",\"evidence_handles\":[\"...\"],"
        "\"reason_to_change\":\"为什么值得改变\"}]}。"
        "issues 为空时使用 no_change；不得返回 updated_plan、chapter_updates、正文替换文本或预设答案。"
        + scope
        + "\n完整输入："
        + rendered_payload
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _review_material_handles(payload: Mapping[str, Any]) -> set[str]:
    """Return handles backed by supplied material, including tool-only identity."""

    handles: set[str] = set()

    def add(row: Any, *, tool: bool = False) -> None:
        if not isinstance(row, Mapping):
            return
        handle = str(row.get("source_handle") or "").strip()
        if not handle:
            return
        if tool or planning._owner_material_has_content(row):
            handles.add(handle)

    for row in payload.get("source_materials") or ():
        add(row)
    for row in payload.get("candidate_materials") or ():
        add(row)
    navigation = payload.get("candidate_navigation") or {}
    if isinstance(navigation, Mapping):
        for row in navigation.get("candidate_materials") or ():
            add(row)
    for row in payload.get("tool_materials") or ():
        if not isinstance(row, Mapping) or not str(row.get("usable_content") or "").strip():
            continue
        for source in row.get("sources") or ():
            add(source, tool=True)
    return handles


def _validate_review_response(
    payload: Mapping[str, Any], response: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate issue-only reviewer output before exposing it to the owner."""

    if not isinstance(response, Mapping):
        return [], ["review_response_not_object"]
    if any(str(key) in _REVIEW_RESPONSE_CONTROL_KEYS for key in response):
        return [], ["review_response_contains_plan_or_body"]
    raw_status = response.get("status") or response.get("review_status")
    if not str(raw_status or "").strip():
        return [], ["review_status_required"]
    status = str(raw_status).strip().casefold()
    if status in {"failed", "error", "partial", "unresolved", "incomplete"}:
        return [], ["review_response_status:" + status]
    if status not in {"reviewed", "no_change", "unchanged", "no-change", "reused"}:
        return [], ["review_response_status:" + status]
    if "issues" not in response and "review_issues" not in response:
        return [], ["review_issues_required"]
    raw_issues = response.get("issues")
    if raw_issues is None:
        raw_issues = response.get("review_issues")
    if not isinstance(raw_issues, list):
        return [], ["review_issues_not_list"]
    modifiable = set(_clean_ids(payload.get("modifiable_unit_ids") or []))
    readonly = set(_clean_ids(payload.get("read_only_unit_ids") or []))
    material_handles = _review_material_handles(payload)
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for index, raw in enumerate(raw_issues):
        if not isinstance(raw, Mapping):
            errors.append(f"review_issue_not_object:{index}")
            continue
        issue_id = str(raw.get("issue_id") or "").strip()
        affected = _clean_ids(raw.get("affected_unit_ids") or [])
        description = str(raw.get("description") or "").strip()
        evidence = _clean_ids(raw.get("evidence_handles") or [])
        reason = str(raw.get("reason_to_change") or "").strip()
        if not issue_id or issue_id in seen:
            errors.append(f"review_issue_id_invalid:{index}")
        if not affected or not set(affected).issubset(modifiable) or set(affected) & readonly:
            errors.append(f"review_issue_scope_invalid:{issue_id or index}")
        if not description or not reason:
            errors.append(f"review_issue_explanation_missing:{issue_id or index}")
        if not evidence or not set(evidence).issubset(material_handles):
            errors.append(f"review_issue_evidence_invalid:{issue_id or index}")
        if issue_id:
            seen.add(issue_id)
        issues.append({
            "issue_id": issue_id,
            "affected_unit_ids": affected,
            "description": description,
            "evidence_handles": evidence,
            "reason_to_change": reason,
        })
    if errors:
        return [], list(dict.fromkeys(errors))
    if status in {"no_change", "unchanged", "no-change", "reused"} and issues:
        return [], ["review_no_change_with_issues"]
    return issues, []


def estimate_strengthening_request(
    messages: Sequence[Mapping[str, Any]],
    *,
    profile: Mapping[str, Any],
    token_counter: Any = None,
) -> dict[str, Any]:
    """Return the same input/capacity estimate used by the production client."""

    if callable(token_counter):
        prompt_tokens = int(token_counter(b"", messages))
        tokenizer = "provided_token_counter"
    else:
        prompt_tokens = max(1, len(json.dumps(list(messages), ensure_ascii=False, default=str)) // 2)
        tokenizer = "conservative_utf8_approximation"
    reserved_input = int(prompt_tokens * planning.TOKEN_MARGIN_MULTIPLIER + 0.999999) + planning.TOKEN_FRAMING_MARGIN
    output_tokens = int(profile["max_output_tokens"])
    thinking_budget = int(profile["thinking_budget"])
    from .module4 import runtime
    return {
        "model": profile["model"],
        "prompt_tokens_estimate": prompt_tokens,
        "reserved_input_tokens": reserved_input,
        "output_tokens": output_tokens,
        "thinking_budget": thinking_budget,
        "max_completion_tokens": output_tokens + thinking_budget,
        "total_context_tokens": reserved_input + output_tokens + thinking_budget,
        "estimated_cost_cny": runtime.estimated_cost_cny(
            {"prompt_tokens": reserved_input, "completion_tokens": output_tokens + thinking_budget},
            model=str(profile["model"]), conservative=True,
        ),
        "tokenizer": tokenizer,
    }


def project_plan_for_arrangement(payload: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Project a validated candidate into the existing arrangement packet."""

    status = str(result.get("status") or "").casefold()
    if status not in {"updated", "no_change"}:
        raise OutlineStrengtheningError(f"outline_result_not_accepted:{status or 'missing'}")
    plan = result.get("updated_plan")
    if not isinstance(plan, Mapping):
        plan = payload.get("chapter_plan")
    if not isinstance(plan, Mapping):
        raise OutlineStrengtheningError("arrangement_projection_plan_missing")
    projected = _json_copy(payload)
    projected["chapter_plan"] = _json_copy(plan)
    accepted_materials = result.get("accepted_source_materials")
    if isinstance(accepted_materials, list):
        projected["source_materials"] = _json_copy(accepted_materials)
    if result.get("unit_id_remap"):
        projected["unit_id_remap"] = _json_copy(result["unit_id_remap"])
    projected["outline_strengthening_status"] = status
    return projected


def _run_owner_with_messages(
    payload: Mapping[str, Any], *, messages: Sequence[Mapping[str, Any]], client: Any,
    model: str, thinking_budget: int, max_output_tokens: int,
    call_id: str | None = None, allow_machine_review: bool = False,
) -> dict[str, Any]:
    """Invoke one owner request and apply the production response validation."""

    _verify_envelope(payload, allow_machine_review=allow_machine_review)
    from .module4 import runtime
    raw = runtime.invoke_client(
        client,
        messages,
        model=model,
        max_output_tokens=int(max_output_tokens),
        thinking=True,
        thinking_budget=int(thinking_budget),
        call_id=call_id or str(payload.get("call_id") or "autonomous-outline"),
    )
    parsed, telemetry = planning._parse_planner_response(raw)
    adopted_materials, adoption_resolution = planning._close_owner_response_materials(payload, parsed)
    returned_plan = planning._owner_response_plan(parsed)
    status, updated_plan, remap, structural_errors = planning._classify_owner_response(
        payload.get("chapter_plan") or {}, parsed, adopted_materials,
        expected_chapter_id=str(payload.get("chapter_id") or ""),
    )
    if isinstance(returned_plan, Mapping):
        returned_ids = _clean_ids([
            unit.get("unit_id") or unit.get("id")
            for unit in returned_plan.get("units") or ()
            if isinstance(unit, Mapping)
        ])
        modifiable_ids = _clean_ids(payload.get("modifiable_unit_ids") or [])
        readonly_ids = set(_clean_ids(payload.get("read_only_unit_ids") or []))
        if readonly_ids & set(returned_ids):
            status = "unresolved"
            structural_errors = [*structural_errors, "readonly_unit_returned_for_edit"]
        elif modifiable_ids and set(returned_ids) != set(modifiable_ids) and not remap:
            status = "unresolved"
            structural_errors = [*structural_errors, "modifiable_unit_ids_not_preserved"]
    if status == "no_change" and updated_plan is None:
        updated_plan = _json_copy(payload.get("chapter_plan") or {})
    result = {
        "status": status,
        "chapter_id": str(payload.get("chapter_id") or ""),
        "updated_plan": _json_copy(updated_plan) if isinstance(updated_plan, Mapping) else None,
        "unit_id_remap": remap,
        "structural_errors": structural_errors,
        "adoption_material_resolution": adoption_resolution,
        "accepted_source_materials": _json_copy(adopted_materials),
        "request_sha256": _fingerprint(messages),
        "parsed_response": parsed,
        "telemetry": telemetry,
        "raw_response": raw,
        "messages": messages,
    }
    return result


def run_strengthening(
    payload: Mapping[str, Any],
    *,
    client: Any,
    model: str,
    thinking_budget: int,
    max_output_tokens: int,
    call_id: str | None = None,
    model_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Call an injected production client and validate the existing owner contract."""

    messages = strengthening_messages(payload, model_payload=model_payload)
    return _run_owner_with_messages(
        payload, messages=messages, client=client, model=model,
        thinking_budget=thinking_budget, max_output_tokens=max_output_tokens,
        call_id=call_id, allow_machine_review=False,
    )


def run_reviewed_strengthening(
    payload: Mapping[str, Any],
    *,
    reviewer_client: Any,
    reviewer_model: str,
    reviewer_thinking_budget: int,
    reviewer_max_output_tokens: int,
    owner_client: Any,
    owner_model: str,
    owner_thinking_budget: int,
    owner_max_output_tokens: int,
    reviewer_call_id: str | None = None,
    owner_call_id: str | None = None,
) -> dict[str, Any]:
    """Run one independent issue-only review, then one owner revision.

    The second request is assembled only from the first model's validated issues
    and the original packet.  No caller-supplied review target or prior answer
    can enter this path.
    """

    _verify_envelope(payload)
    review_request = reviewer_messages(payload)
    from .module4 import runtime
    review_raw = runtime.invoke_client(
        reviewer_client, review_request, model=reviewer_model,
        max_output_tokens=int(reviewer_max_output_tokens), thinking=True,
        thinking_budget=int(reviewer_thinking_budget),
        call_id=reviewer_call_id or (str(payload.get("call_id") or "autonomous-outline") + ":reviewer"),
    )
    review_parsed, review_telemetry = planning._parse_planner_response(review_raw)
    issues, review_errors = _validate_review_response(payload, review_parsed)
    review_record = {
        "request_sha256": _fingerprint(review_request),
        "messages": review_request,
        "parsed_response": review_parsed,
        "raw_response": review_raw,
        "telemetry": review_telemetry,
        "issues": issues,
        "validation_errors": review_errors,
    }
    if review_errors:
        return {
            "status": "unresolved",
            "chapter_id": str(payload.get("chapter_id") or ""),
            "updated_plan": None,
            "unit_id_remap": {},
            "structural_errors": [*review_errors, "reviewer_output_not_accepted"],
            "adoption_material_resolution": {},
            "accepted_source_materials": _json_copy(payload.get("source_materials") or []),
            "request_sha256": _fingerprint(review_request),
            "parsed_response": review_parsed,
            "telemetry": review_telemetry,
            "raw_response": review_raw,
            "messages": review_request,
            "reviewed_strengthening": {
                "reviewer": review_record,
                "owner_not_called": True,
            },
        }
    if not issues:
        return {
            "status": "no_change",
            "chapter_id": str(payload.get("chapter_id") or ""),
            "updated_plan": _json_copy(payload.get("chapter_plan") or {}),
            "unit_id_remap": {},
            "structural_errors": [],
            "adoption_material_resolution": {},
            "accepted_source_materials": _json_copy(payload.get("source_materials") or []),
            "request_sha256": _fingerprint(review_request),
            "parsed_response": review_parsed,
            "telemetry": review_telemetry,
            "raw_response": review_raw,
            "messages": review_request,
            "reviewed_strengthening": {
                "reviewer": review_record,
                "owner_not_called": True,
                "review_issues": [],
            },
        }

    owner_payload = _json_copy(payload)
    owner_payload["chapter_feedback"] = _json_copy(issues)
    owner_payload["review_feedback_origin"] = "autonomous_reviewer"
    owner_payload["review_feedback_sha256"] = _fingerprint(issues)
    owner_payload["required_behavior"] = {
        **dict(owner_payload.get("required_behavior") or {}),
        "reviewer_findings_are_advisory": True,
        "owner_must_decide_each_review_issue": True,
    }
    owner_request = strengthening_messages(owner_payload, allow_machine_review=True)
    owner_overlay = (
        "\n\n【机器审阅结果的负责人复核】chapter_feedback 仅是本次独立审阅模型生成的建议。"
        "请结合原始章节计划和完整材料逐条独立采纳或拒绝，不把描述当作事实，不引入其未提供的证据。"
        "在返回结果中附带 review_decisions，每条包含 issue_id、decision（adopt/reject/partial）和 rationale；"
        "仍须返回完整当前可编辑章节计划或合理 no_change，不返回正文。"
    )
    owner_request[0] = {**owner_request[0], "content": str(owner_request[0].get("content") or "") + owner_overlay}
    owner_request[1] = {**owner_request[1], "content": str(owner_request[1].get("content") or "") + owner_overlay}
    owner_result = _run_owner_with_messages(
        owner_payload, messages=owner_request, client=owner_client, model=owner_model,
        thinking_budget=owner_thinking_budget, max_output_tokens=owner_max_output_tokens,
        call_id=owner_call_id or (str(payload.get("call_id") or "autonomous-outline") + ":owner"),
        allow_machine_review=True,
    )
    decisions = owner_result.get("parsed_response", {}).get("review_decisions")
    if not isinstance(decisions, list):
        decisions = owner_result.get("parsed_response", {}).get("issue_decisions")
    decisions = decisions if isinstance(decisions, list) else []
    decision_by_id = {
        str(item.get("issue_id") or "").strip(): dict(item)
        for item in decisions if isinstance(item, Mapping) and str(item.get("issue_id") or "").strip()
    }
    adoption_reasons = []
    for issue in issues:
        decision = decision_by_id.get(issue["issue_id"], {})
        adoption_reasons.append({
            "issue_id": issue["issue_id"],
            "decision": str(decision.get("decision") or "not_reported"),
            "rationale": str(decision.get("rationale") or ""),
            "review_reason_to_change": issue["reason_to_change"],
            "evidence_handles": list(issue["evidence_handles"]),
        })
    owner_result["reviewed_strengthening"] = {
        "reviewer": review_record,
        "owner_request_sha256": owner_result.get("request_sha256"),
        "owner_messages": owner_request,
        "review_issues": issues,
        "adoption_reasons": adoption_reasons,
        "owner_feedback_origin": "autonomous_reviewer",
    }
    return owner_result


def make_strengthening_client(
    *,
    role: str = ROLE,
    key_file: str | Path,
    budget_ledger: Any,
    raw_response_dir: str | Path,
    prompt_token_counter: Any = None,
    profile_path: str | Path | None = None,
) -> Any:
    """Construct the explicit autonomous-outline profile through the shared factory."""

    from .quality_capacity import make_quality_client
    return make_quality_client(
        role,
        key_file=key_file,
        budget_ledger=budget_ledger,
        raw_response_dir=raw_response_dir,
        prompt_token_counter=prompt_token_counter,
        profile_path=profile_path,
    )


__all__ = [
    "ROLE", "STAGE", "OutlineStrengtheningError", "build_strengthening_payload",
    "strengthening_messages", "reviewer_messages", "model_material_projection",
    "expand_material_projection", "estimate_strengthening_request",
    "project_plan_for_arrangement", "run_strengthening", "run_reviewed_strengthening",
    "make_strengthening_client",
]
