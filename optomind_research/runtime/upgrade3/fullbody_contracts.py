"""Lossless whole-BODY inputs and free-form manuscript output contracts.

The accepted chapter builder remains the material authority. This module adds
book-wide identities and read-only navigation, not planning, scientific edits,
source ranking, summarization, or a required paragraph-per-task output shape.
All public views are independent copies. Output completion describes a model's
structurally usable declarations; it never certifies the scientific content.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from . import review_unit_writer as _writer
from .chapter_arrangement import _escape_inner_json_quotes, normalize_doi
from .writer_candidates_contracts import (
    CandidateError, INPUT_SCHEMA as CHAPTER_INPUT_SCHEMA, _handles, _selected_ids,
    _signature, _string_prefix, _strip_json_fence, project_chapter, task_catalog,
)

INPUT_SCHEMA = "optomind.fullbody.input.v1"
PAYLOAD_SCHEMA = "optomind.fullbody.payload.v1"
RESULT_SCHEMA = "optomind.fullbody.result.v1"
FullbodyError = CandidateError


def _hash(value: Any) -> str:
    return hashlib.sha256(_signature(value).encode("utf-8")).hexdigest()


def _chapter_rows(book: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    chapters = book.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise CandidateError("fullbody_has_no_chapters")
    seen: set[str] = set()
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            raise CandidateError("fullbody_chapter_not_object")
        chapter_id = chapter.get("chapter_id")
        if not isinstance(chapter_id, str) or not chapter_id.strip():
            raise CandidateError("fullbody_chapter_id_missing_or_invalid")
        if chapter_id in seen:
            raise CandidateError("fullbody_chapter_id_duplicated:" + chapter_id)
        seen.add(chapter_id)
    return chapters


def fullbody_task_catalog(book: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Index full original tasks with injective, order-independent global IDs.

    URL escaping makes separators unambiguous even when accepted identifiers
    contain ``::``, percent signs, or chapter-like prefixes. Local identities
    from task_catalog, including deterministic legacy IDs, are not rewritten.
    """
    result: dict[str, dict[str, Any]] = {}
    for chapter in _chapter_rows(book):
        chapter_id = chapter["chapter_id"]
        for local_key, row in task_catalog(chapter).items():
            task_id = local_key[len(row["unit_id"]) + 2:]
            key = "::".join(quote(part, safe="") for part in (chapter_id, row["unit_id"], task_id))
            if key in result:
                raise CandidateError("fullbody_task_key_duplicated:" + key)
            result[key] = {"chapter_id": chapter_id, "local_task_id": local_key,
                           "original_task_id": task_id, **deepcopy(row)}
    return result



def _reconcile_source_identities(identities: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve explicitly connected, corroborated cross-chapter identities.

    An alias may be an older chapter's canonical handle. That is safe only
    when the two source authorities have the same nonempty DOI or stable paper
    identity. Similar titles and DOI equality without an explicit alias edge do
    not merge sources. Original scientific records remain untouched.
    """
    parents = {key: key for key in identities}

    def root(key: str) -> str:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def stable_values(identity: Mapping[str, Any], field: str) -> set[str]:
        values = [identity.get(field), *(identity.get("identity_metadata_variants", {}).get(field, []))]
        if field == "doi":
            return {normalize_doi(value) for value in values if normalize_doi(value)}
        return {value for value in values if isinstance(value, str) and value}

    def corroborated(left: str, right: str) -> bool:
        a, b = identities[left], identities[right]
        ad, bd = stable_values(a, "doi"), stable_values(b, "doi")
        if ad and bd:
            return ad == bd
        ac, bc = stable_values(a, "canonical_paper_id"), stable_values(b, "canonical_paper_id")
        if ac and bc:
            return bool(ac & bc)
        ap = ac or stable_values(a, "paper_id")
        bp = bc or stable_values(b, "paper_id")
        return bool(ap & bp)

    claims: dict[str, list[str]] = {}
    for owner, identity in identities.items():
        for alias in identity["aliases"]:
            if alias == owner:
                raise CandidateError("source_alias_ambiguous:" + alias)
            claims.setdefault(alias, []).append(owner)
    for alias, owners in claims.items():
        authorities = list(dict.fromkeys([*owners, *([alias] if alias in identities else [])]))
        for left_index, left in enumerate(authorities):
            for right in authorities[left_index + 1:]:
                if not corroborated(left, right):
                    raise CandidateError("source_alias_ambiguous:" + alias)
                parents[root(right)] = root(left)
    groups: dict[str, list[str]] = {}
    for handle in identities:
        groups.setdefault(root(handle), []).append(handle)
    grouped: dict[str, Any] = {}
    for members in groups.values():
        # An intermediate record with no DOI must not hide two contradictory
        # nonempty DOIs at opposite ends of an explicit alias chain.
        group_dois = set().union(*(stable_values(identities[member], "doi") for member in members))
        if len(group_dois) > 1:
            raise CandidateError("source_identity_conflict:" + ",".join(sorted(members)))
        claimed_members = {alias for owner in members for alias in identities[owner]["aliases"] if alias in members}
        roots = sorted(set(members) - claimed_members)
        canonical = roots[0] if roots else min(members)
        # Selecting a deterministic identity never selects a scientific record.
        merged = deepcopy(dict(identities[canonical]))
        merged["source_handle"] = canonical
        merged["source_id"] = "source::" + quote(canonical, safe="")
        merged["aliases"] = sorted({alias for member in members for alias in identities[member]["aliases"]
                                    if alias != canonical} | (set(members) - {canonical}))
        if len(members) > 1:
            merged["equivalent_source_handles"] = sorted(members)
            merged["identity_reconciliation"] = "explicit_alias_with_matching_stable_identity"
        for member in members:
            identity = identities[member]
            for field in ("record_ids", "chapter_ids"):
                merged[field] = list(dict.fromkeys([*merged[field], *identity[field]]))
            for field in ("title", "doi", "canonical_paper_id", "paper_id"):
                values = [identity.get(field), *(identity.get("identity_metadata_variants", {}).get(field, []))]
                for value in values:
                    if value is None or value == "":
                        continue
                    if field not in merged:
                        merged[field] = deepcopy(value)
                    elif _signature(value) != _signature(merged[field]):
                        variants = merged.setdefault("identity_metadata_variants", {}).setdefault(field, [])
                        if not any(_signature(prior) == _signature(value) for prior in variants):
                            variants.append(deepcopy(value))
        grouped[canonical] = merged
    try:
        aliases = _writer._explicit_source_aliases(grouped)
    except _writer.UnitWritingError as exc:
        raise CandidateError(str(exc)) from exc
    return grouped, aliases


def _source_records(chapters: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    """Deduplicate exact records only; validate every complementary identity.

    Equal DOIs can explain different historical paper IDs, exactly as in the
    existing chapter contract. Equal titles, citations or missing IDs cannot.
    Review-mediated records do not need their own A/B or full-text materials.
    """
    records: list[dict[str, Any]] = []
    record_ids: set[str] = set()
    identities: dict[str, Any] = {}
    comparisons: dict[str, list[Mapping[str, Any]]] = {}

    def check_identity(raw: Mapping[str, Any], expected: str | None = None) -> str:
        handle = raw.get("source_handle")
        if not isinstance(handle, str) or not handle.strip() or handle != handle.strip():
            raise CandidateError("source_identity_missing")
        if expected is not None and handle != expected:
            raise CandidateError("source_identity_conflict:" + expected + ":variant=" + handle)
        aliases = raw.get("aliases", [])
        if aliases is None:
            aliases = []
        if not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias or alias != alias.strip() for alias in aliases):
            raise CandidateError("source_alias_invalid:" + handle)
        for prior in comparisons.setdefault(handle, []):
            left, right = normalize_doi(prior.get("doi")), normalize_doi(raw.get("doi"))
            left_paper = prior.get("canonical_paper_id") or prior.get("paper_id")
            right_paper = raw.get("canonical_paper_id") or raw.get("paper_id")
            if ((left and right and left != right) or
                    (left_paper and right_paper and left_paper != right_paper and not (left and left == right))):
                raise CandidateError("source_identity_conflict:" + handle)
        comparisons[handle].append(raw)
        identity = identities.setdefault(handle, {"source_handle": handle,
            "source_id": "source::" + quote(handle, safe=""), "aliases": [], "record_ids": [], "chapter_ids": []})
        identity["aliases"] = list(dict.fromkeys([*identity["aliases"], *aliases]))
        # Navigators need the original bibliographic identity, not just opaque
        # handles. Preserve complete titles/IDs and any exact alternative forms;
        # never copy scientific A/B material into this inexpensive index.
        for field in ("title", "doi", "canonical_paper_id", "paper_id"):
            value = raw.get(field)
            if value is None or value == "":
                continue
            if field not in identity:
                identity[field] = deepcopy(value)
            elif _signature(identity[field]) != _signature(value):
                alternatives = identity.setdefault("identity_metadata_variants", {}).setdefault(field, [])
                if not any(_signature(prior) == _signature(value) for prior in alternatives):
                    alternatives.append(deepcopy(value))
        variants = raw.get("material_record_variants", [])
        if not isinstance(variants, list):
            raise CandidateError("source_material_record_variants_not_list:" + handle)
        for variant in variants:
            if not isinstance(variant, Mapping):
                raise CandidateError("source_material_record_variant_not_object:" + handle)
            check_identity(variant, handle)
        return handle

    for chapter in chapters:
        sources = chapter.get("sources")
        if not isinstance(sources, list):
            raise CandidateError("sources_not_list")
        for raw in sources:
            if not isinstance(raw, Mapping):
                raise CandidateError("source_identity_missing")
            handle = check_identity(raw)
            record_id = "record::" + _hash(raw)
            identity = identities[handle]
            if record_id not in identity["record_ids"]:
                identity["record_ids"].append(record_id)
            if chapter["chapter_id"] not in identity["chapter_ids"]:
                identity["chapter_ids"].append(chapter["chapter_id"])
            if record_id not in record_ids:
                records.append(deepcopy(dict(raw)))
                record_ids.add(record_id)
    identities, aliases = _reconcile_source_identities(identities)
    return records, identities, aliases


def _shared_requirement(chapters: Sequence[Mapping[str, Any]], field: str, supplied: Any) -> Any:
    if supplied is not None:
        return deepcopy(supplied)
    values: list[Any] = []
    for chapter in chapters:
        frame = chapter.get("chapter_frame") or {}
        value = frame.get(field) if isinstance(frame, Mapping) else None
        if value is not None and value != "" and not any(_signature(value) == _signature(prior) for prior in values):
            values.append(value)
    if len(values) > 1:
        raise CandidateError("fullbody_" + field + "_conflict:explicit_value_required")
    return deepcopy(values[0]) if values else ""


def build_fullbody_input(
    chapters: Sequence[Mapping[str, Any]], *, research_question: Any = None,
    review_argument: Any = None, language: str = "zh",
    user_request: Any = None, target_reader: Any = None, review_scope: Any = None,
    approved_plan: Any = None, original_user_request: Any = None, shared_scope: Any = None,
) -> dict[str, Any]:
    """Compose normalized build_chapter_input results in approved BODY order.

    Provenance is copied from the actual builder output. No old snapshot,
    packet root or acceptance hash is discovered, guessed, or synthesized.
    Hashes added here identify the normalized input, not upstream approval.
    """
    if isinstance(chapters, (str, bytes)) or not isinstance(chapters, Sequence):
        raise CandidateError("fullbody_chapters_not_sequence")
    if not isinstance(language, str) or not language.strip():
        raise CandidateError("fullbody_language_invalid")
    book: dict[str, Any] = {"schema_version": INPUT_SCHEMA, "language": language,
                           "chapters": deepcopy(list(chapters))}
    rows = _chapter_rows(book)
    for chapter in rows:
        if chapter.get("schema_version") != CHAPTER_INPUT_SCHEMA:
            raise CandidateError("fullbody_requires_normalized_chapter:" + chapter["chapter_id"])
        # Fail closed at the existing task/source handoff as well as globally.
        project_chapter(chapter)
    book["research_question"] = _shared_requirement(rows, "research_question", research_question)
    book["review_argument"] = _shared_requirement(rows, "review_argument", review_argument)
    if user_request is not None and original_user_request is not None and _signature(user_request) != _signature(original_user_request):
        raise CandidateError("fullbody_user_request_alias_conflict")
    if review_scope is not None and shared_scope is not None and _signature(review_scope) != _signature(shared_scope):
        raise CandidateError("fullbody_review_scope_alias_conflict")
    for field, supplied in {
        "user_request": user_request if user_request is not None else original_user_request,
        "target_reader": target_reader,
        "review_scope": review_scope if review_scope is not None else shared_scope,
        "approved_plan": approved_plan,
    }.items():
        book[field] = _shared_requirement(rows, field, supplied)
    book["missing_original_context_fields"] = [field for field in
        ("user_request", "target_reader", "review_scope", "research_question", "review_argument")
        if book[field] is None or book[field] == ""]
    book["task_catalog"] = fullbody_task_catalog(book)
    records, identities, aliases = _source_records(rows)
    book.update(sources=records, source_identities=identities, source_aliases=aliases)
    book["input_manifest"] = {
        "chapter_order": [chapter["chapter_id"] for chapter in rows],
        "chapters": [{"chapter_id": chapter["chapter_id"],
                      "normalized_chapter_sha256": _hash(chapter),
                      "provenance": deepcopy(chapter.get("provenance") or {})} for chapter in rows],
        "source_record_ids": ["record::" + _hash(source) for source in records],
        "task_ids": list(book["task_catalog"]),
        "material_builder": "writer_candidates_contracts.build_chapter_input",
        "scope": "complete_approved_BODY_chapter_sequence",
    }
    return seal_fullbody_input(book)


def seal_fullbody_input(book: Mapping[str, Any]) -> dict[str, Any]:
    """Hash the complete saved input after a caller adds accepted-file provenance."""
    sealed = deepcopy(dict(book))
    manifest = sealed.setdefault("input_manifest", {})
    if not isinstance(manifest, dict):
        raise CandidateError("fullbody_input_manifest_not_object")
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = _hash(manifest)
    sealed.pop("book_sha256", None)
    sealed["book_sha256"] = _hash(sealed)
    return sealed


def project_fullbody(book: Mapping[str, Any], task_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """Keep the full immutable intent/navigation and select complete evidence.

    Read-only means reference intent, not frozen Python objects: callers receive
    a JSON-serializable deep copy and cannot mutate the canonical book through
    it. No material text is clipped. Nonidentical records for a selected source
    all participate, including complementary records from another chapter.
    """
    chapters = _chapter_rows(book)
    catalog = fullbody_task_catalog(book)
    selected = _selected_ids(catalog, task_ids)
    selected_set = set(selected)
    records, identities, aliases = _source_records(chapters)
    projections: list[dict[str, Any]] = []
    needed: list[str] = []
    for chapter in chapters:
        pairs = {row["local_task_id"]: key for key, row in catalog.items()
                 if row["chapter_id"] == chapter["chapter_id"]}
        local_ids = [local for local, key in pairs.items() if key in selected_set]
        if not local_ids:
            continue
        projected = project_chapter(chapter, local_ids)
        projected["local_editable_task_ids"] = deepcopy(projected["editable_task_ids"])
        projected["editable_task_ids"] = [pairs[local] for local in local_ids]
        projected["fullbody_task_ids"] = deepcopy(projected["editable_task_ids"])
        needed.extend(source["source_handle"] for source in projected.pop("sources"))
        needed.extend(_handles(projected.get("chapter_frame", {})))
        for unit in projected["units"]:
            unit["local_editable_task_ids"] = deepcopy(unit["editable_task_ids"])
            unit["editable_task_ids"] = [pairs[local] for local in unit["editable_task_ids"]]
            # Keep local task_keys valid for the reused chapter contract; put
            # book-wide identities in an explicit parallel navigation field.
            unit["fullbody_task_keys"] = {kind: [pairs[local] for local in local_keys]
                                           for kind, local_keys in unit["task_keys"].items()}
            needed.extend(_handles({key: value for key, value in unit.items() if key != "source_handles"}))
        # All neighbor intent appears once in the full-body navigation below.
        projected.pop("shared_chapter_context", None)
        projected["source_pool_reference"] = "sources"
        projections.append(projected)
    # Source lineage in any complementary record participates in closure.
    selected_handles: set[str] = set()
    while needed:
        supplied = needed.pop(0)
        handle = aliases.get(supplied, supplied)
        if handle in selected_handles:
            continue
        if handle not in identities:
            raise CandidateError("task_source_missing:" + supplied)
        selected_handles.add(handle)
        for record in records:
            if aliases.get(record["source_handle"], record["source_handle"]) == handle:
                needed.extend(_handles(record))
    selected_sources = [deepcopy(record) for record in records
                        if aliases.get(record["source_handle"], record["source_handle"]) in selected_handles]
    # Keep every original task/context once, with exact references where the
    # current editable chapter already supplies it. No summaries are generated.
    outline: list[dict[str, Any]] = []
    neighbors: dict[str, Any] = {}
    for chapter in chapters:
        row = {key: deepcopy(value) for key, value in chapter.items()
               if key not in {"sources", "provenance", "warnings", "chapter_tool_materials", "other_chapters"}}
        chapter_selected = any(item["chapter_id"] == chapter["chapter_id"] for item in projections)
        if chapter_selected:
            row["chapter_frame"] = {"editable_chapter_reference": chapter["chapter_id"]}
        for unit in row["units"]:
            local_rows = [(key, item) for key, item in catalog.items()
                          if item["chapter_id"] == chapter["chapter_id"] and item["unit_id"] == unit["unit_id"]]
            editable = [key for key, item in local_rows if key in selected_set]
            if editable:
                unit_id = unit["unit_id"]
                # Selected unit metadata and owner context are already complete
                # in chapters. Only unselected task intent remains inline here.
                unit.clear()
                unit.update(unit_id=unit_id, editable_unit_reference={
                    "chapter_id": chapter["chapter_id"], "unit_id": unit_id})
                for kind, field in (("paragraph", "paragraph_tasks"), ("table", "table_tasks")):
                    unit[field] = [({"editable_task_reference": key} if key in selected_set
                                    else deepcopy(item["task"]))
                                   for key, item in local_rows if item["kind"] == kind]
            unit["fullbody_task_keys"] = {field: [key for key, item in local_rows if item["kind"] == kind]
                for kind, field in (("paragraph", "paragraph_tasks"), ("table", "table_tasks"))}
        chapter_neighbors = chapter.get("other_chapters", [])
        if not isinstance(chapter_neighbors, list):
            raise CandidateError("fullbody_other_chapters_not_list:" + chapter["chapter_id"])
        references = []
        for neighbor in chapter_neighbors:
            reference = "neighbor::" + _hash(neighbor)
            neighbors.setdefault(reference, deepcopy(neighbor))
            references.append(reference)
        row["other_chapter_references"] = references
        outline.append(row)
    roles = [{"task_id": key, "chapter_id": row["chapter_id"], "unit_id": row["unit_id"],
              "local_task_id": row["local_task_id"], "kind": row["kind"], "editable": key in selected_set}
             for key, row in catalog.items()]
    return {"schema_version": PAYLOAD_SCHEMA, "language": book.get("language", "zh"),
            "research_question": deepcopy(book.get("research_question", "")),
            "review_argument": deepcopy(book.get("review_argument", "")),
            **{field: deepcopy(book.get(field, "")) for field in
               ("user_request", "target_reader", "review_scope")},
            "approved_plan": {"canonical_book_reference": "approved_plan",
                              "sha256": _hash(book["approved_plan"]),
                              "normalized_outline_reference": "shared_fullbody_context"} if book.get("approved_plan") else "",
            "missing_original_context_fields": deepcopy(book.get("missing_original_context_fields", [])),
            "editable_task_ids": selected, "chapters": projections, "sources": selected_sources,
            "source_identities": {key: {field: deepcopy(value) for field, value in identities[key].items()
                                       if field not in {"record_ids", "chapter_ids", "source_handle"}}
                                  for key in identities if key in selected_handles},
            "source_aliases": {alias: handle for alias, handle in aliases.items() if handle in selected_handles},
            "shared_fullbody_context": {"read_only": True, "chapters": outline, "task_roles": roles, "other_chapters": neighbors,
                                        "intent_is_not_scientific_evidence": True},
            "input_manifest": {key: deepcopy(book.get("input_manifest", {}).get(key))
                               for key in ("manifest_sha256", "chapter_order")},
            "book_sha256": book.get("book_sha256", "")}


def _transport_status(response: Any) -> tuple[list[str], bool]:
    reasons: list[str] = []
    incomplete = False
    def visit(node: Any) -> None:
        nonlocal incomplete
        if not isinstance(node, Mapping):
            return
        reason = node.get("finish_reason") or node.get("stop_reason")
        if isinstance(reason, str) and reason not in reasons:
            reasons.append(reason)
        if (isinstance(reason, str) and reason in {"length", "max_tokens", "max_output_tokens", "content_filter", "error", "cancelled"}) or node.get("error"):
            incomplete = True
        # complete:false in an actual prose envelope is a manuscript declaration;
        # otherwise it is an explicit transport completion flag.
        if node.get("complete") is False and "body_markdown" not in node:
            incomplete = True
        for key in ("content", "response", "message"):
            visit(node.get(key))
        if isinstance(node.get("choices"), list):
            for choice in node["choices"]:
                visit(choice)
    visit(response)
    return reasons, incomplete


def _decode_response(response: Any) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    """Keep bare Markdown or useful truncated JSON, without invented coverage."""
    if isinstance(response, Mapping):
        if "body_markdown" in response:
            return deepcopy(dict(response)), [], True
        for key in ("content", "response", "message"):
            if isinstance(response.get(key), (Mapping, str)):
                return _decode_response(response[key])
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            return _decode_response(choices[0])
        return {}, [{"code": "fullbody_response_unreadable"}], False
    if not isinstance(response, str):
        return {}, [{"code": "fullbody_response_unreadable"}], False
    text = _strip_json_fence(response)
    raw_markdown = response
    try:
        decoded = json.loads(text, strict=False)
    except ValueError:
        decoded = None
        repaired = _escape_inner_json_quotes(text)
        if repaired != text:
            try:
                decoded = json.loads(repaired, strict=False)
            except ValueError:
                pass
            if isinstance(decoded, Mapping):
                envelope, problems, valid = _decode_response(decoded)
                return envelope, [{"code": "fullbody_json_inner_quote_repaired",
                    "method": "bounded_inner_json_quote_escape",
                    "original_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "normalized_sha256": hashlib.sha256(repaired.encode("utf-8")).hexdigest(),
                    "original_char_count": len(text), "normalized_char_count": len(repaired),
                    "inserted_escape_characters": len(repaired) - len(text),
                    "strict_json_parse": True}, *problems], valid
    if isinstance(decoded, Mapping):
        return _decode_response(decoded)
    if isinstance(decoded, str):
        return {"body_markdown": decoded}, [{"code": "fullbody_task_dispositions_missing"}], True
    if text.startswith(("{", "```json", "~~~json")) or re.match(r'^\[\s*(?:\{|\[|"|\]|$)', text):
        match = re.search(r'"body_markdown"\s*:\s*"', text)
        if match:
            body, ended = _string_prefix(text, match.end() - 1)
            envelope: dict[str, Any] = {"body_markdown": body, "partial": True}
            # Completed sidecars before the body remain useful on truncation;
            # unfinished sidecars are never repaired or treated as completion.
            decoder = json.JSONDecoder(strict=False)
            for field in ("task_dispositions", "completed_task_ids", "pending_task_ids"):
                sidecar = re.search(r'"' + field + r'"\s*:\s*', text[:match.start()])
                if sidecar:
                    try:
                        envelope[field] = decoder.raw_decode(text, sidecar.end())[0]
                    except ValueError:
                        pass
            return envelope, [{"code": "fullbody_json_incomplete_or_invalid", "body_string_finished": ended}], False
        return {}, [{"code": "fullbody_json_incomplete_or_invalid"}], False
    # A reserved trailing metadata fence permits natural Markdown transport.
    marker = re.search(r"\n(`{3,}|~{3,})fullbody_metadata[ \t]*\r?\n", raw_markdown)
    if marker:
        remainder = raw_markdown[marker.end():]
        closing = re.search(r"\r?\n" + re.escape(marker.group(1)) + r"[ \t]*\s*$", remainder)
        metadata_text = remainder[:closing.start()] if closing else remainder
        try:
            metadata = json.loads(metadata_text, strict=False)
        except ValueError:
            metadata = None
        if isinstance(metadata, Mapping):
            envelope = {**deepcopy(dict(metadata)), "body_markdown": raw_markdown[:marker.start()]}
            if closing:
                return envelope, [], True
            return envelope, [{"code": "fullbody_sidecar_incomplete"}], False
        # A cutoff in metadata must not pollute the recoverable manuscript with
        # JSON/fence fragments or manufacture declarations from partial fields.
        return {"body_markdown": raw_markdown[:marker.start()]}, [{
            "code": "fullbody_sidecar_invalid" if closing else "fullbody_sidecar_incomplete"}], False
    return {"body_markdown": raw_markdown}, [{"code": "fullbody_task_dispositions_missing"}], True


def parse_fullbody_response(
    response: Any, book: Mapping[str, Any], task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Parse prose with a many-to-many task disposition sidecar.

    ``completed_task_ids`` is accepted as a concise sidecar. Neither it nor
    ``complete: true`` proves that chapters or conditions were actually written.
    Bare prose stays recoverable but carries no inferred task completion.
    """
    catalog = fullbody_task_catalog(book)
    selected = _selected_ids(catalog, task_ids)
    allowed = set(selected)
    envelope, problems, valid = _decode_response(response)
    issues = deepcopy(envelope.get("issues", []))
    if not isinstance(issues, list):
        issues = [{"code": "fullbody_issues_not_list", "reported_issues": issues}]
        valid = False
    issues.extend(problems)
    if "complete" in envelope and not isinstance(envelope["complete"], bool):
        issues.append({"code": "fullbody_complete_flag_invalid", "reported_complete": deepcopy(envelope["complete"])})
        valid = False
    body = envelope.get("body_markdown", "")
    if not isinstance(body, str):
        issues.append({"code": "fullbody_body_not_string"})
        body, valid = "", False
    if not body.strip():
        issues.append({"code": "fullbody_body_missing"})
        valid = False
    dispositions = envelope.get("task_dispositions")
    if dispositions is None and "completed_task_ids" in envelope:
        completed_ids = envelope["completed_task_ids"]
        if isinstance(completed_ids, list) and all(isinstance(key, str) for key in completed_ids):
            dispositions = [{"task_id": key, "status": "completed"} for key in completed_ids]
        else:
            issues.append({"code": "fullbody_completed_task_ids_invalid", "reported_task_ids": deepcopy(completed_ids)})
            valid = False
    if dispositions is None:
        dispositions = []
        if not any(item.get("code") == "fullbody_task_dispositions_missing" for item in issues if isinstance(item, Mapping)):
            issues.append({"code": "fullbody_task_dispositions_missing"})
    if not isinstance(dispositions, list):
        issues.append({"code": "fullbody_task_dispositions_not_list", "reported_dispositions": deepcopy(dispositions)})
        dispositions, valid = [], False
    normalized: list[dict[str, Any]] = []
    declared: set[str] = set()
    seen: set[str] = set()
    for position, raw in enumerate(dispositions, 1):
        if not isinstance(raw, Mapping):
            issues.append({"code": "fullbody_task_disposition_not_object", "position": position})
            valid = False
            continue
        row = deepcopy(dict(raw))
        key, status = row.get("task_id"), row.get("status")
        if not isinstance(key, str) or key not in allowed:
            issues.append({"code": "fullbody_task_reference_invalid", "task_id": key,
                           "out_of_scope": isinstance(key, str) and key in catalog})
            valid = False
        elif key in seen:
            issues.append({"code": "fullbody_task_disposition_duplicated", "task_id": key})
            declared.discard(key)
            valid = False
        else:
            seen.add(key)
            if isinstance(status, str) and status in {"completed", "complete", "covered"}:
                row["status"] = "completed"
                if body.strip() and not envelope.get("partial"):
                    declared.add(key)
            elif not isinstance(status, str) or status not in {"pending", "deferred", "partial", "blocked", "not_started"}:
                issues.append({"code": "fullbody_task_disposition_status_invalid", "task_id": key,
                               "reported_status": status})
                valid = False
        normalized.append(row)
    explicit_pending = envelope.get("pending_task_ids", [])
    if not isinstance(explicit_pending, list) or any(not isinstance(key, str) for key in explicit_pending):
        issues.append({"code": "fullbody_pending_task_ids_invalid"})
        valid = False
    else:
        for key in explicit_pending:
            if key not in allowed:
                issues.append({"code": "fullbody_task_reference_invalid", "task_id": key,
                               "out_of_scope": key in catalog})
                valid = False
            elif key in declared:
                issues.append({"code": "fullbody_task_disposition_conflict", "task_id": key})
                declared.discard(key)
                valid = False
    table_ids = [key for key in selected if catalog[key]["kind"] == "table"]
    table_check = _writer._markdown_table_check(body) if table_ids else {"valid": None}
    if table_ids and not table_check["valid"]:
        for key in table_ids:
            if key in declared:
                declared.remove(key)
                issues.append({"code": "markdown_table_missing_or_invalid", "task_id": key})
    reasons, transport_incomplete = _transport_status(response)
    if transport_incomplete:
        issues.append({"code": "fullbody_transport_incomplete", "finish_reasons": reasons})
    completed = [key for key in selected if key in declared]
    pending = [key for key in selected if key not in declared]
    if pending:
        issues.append({"code": "fullbody_tasks_pending", "task_ids": pending})
    payload = project_fullbody(book, selected)
    # Citation identity is book-wide; a known source outside this window is a
    # context diagnostic, not an invented/unknown paper. Scientific support is
    # unverified whether the source was initially supplied or later reread.
    _, global_identities, global_aliases = _source_records(_chapter_rows(book))
    known = [*global_identities, *global_aliases]
    available = set(payload["source_identities"]) | set(payload["source_aliases"])
    tool_payload = {"sources": payload["sources"], "chapter_tool_materials": [
        tool for chapter in payload["chapters"] for tool in chapter.get("chapter_tool_materials", [])]}
    diagnostics = _writer._output_diagnostics(body, known, _writer._known_tool_identifiers(tool_payload))
    diagnostics["known_source_citations_outside_projection"] = [
        key for key in diagnostics["used_source_handles"] if key in known and key not in available]
    diagnostics.update({"coverage_kind": "model_declared_structural_only", "semantic_quality_unreviewed": True,
        "scientific_fidelity_verified": False, "declared_completed_task_ids": completed,
        "transport_incomplete": transport_incomplete, "finish_reasons": reasons,
        "valid_response_envelope": valid, "table_check": table_check,
        "table_task_mapping_verified": False, "numeric_citation_repairs": [],
        "body_complete_self_claim": envelope.get("complete"), "task_dispositions_are_not_proof": True,
        "format_repair": [deepcopy(issue) for issue in issues if isinstance(issue, Mapping)
                          and issue.get("code") == "fullbody_json_inner_quote_repaired"]})
    complete = bool(body.strip() and valid and not pending and not transport_incomplete
                    and not envelope.get("partial") and envelope.get("complete") is not False)
    result = {"schema_version": RESULT_SCHEMA, "body_markdown": body, "task_dispositions": normalized,
              "completed_task_ids": completed, "pending_task_ids": pending, "complete": complete,
              "issues": issues, "diagnostics": diagnostics, "semantic_quality_unreviewed": True,
              "finish_reason": reasons[-1] if reasons else None}
    if not complete:
        result["raw_response"] = deepcopy(response)
    return result
