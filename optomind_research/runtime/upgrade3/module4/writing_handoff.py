"""Lossless writing handoff. Packaging never upgrades scientific confidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from .contracts import snapshot_payload, validate_input

VERSION = "optomind.module4.writing_handoff.lossless.v1"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _fields(value, prefix):
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in _fields(v, f"{prefix}.{k}")]
    if isinstance(value, list):
        return [s for i, v in enumerate(value) for s in _fields(v, f"{prefix}[{i}]")]
    return [] if value is None else [f"{prefix}: {value}"]


def build_writing_handoff(dossier, snapshot, *, output_dir):
    validation = validate_input(dossier.get("input", {}), snapshot)
    if not validation.get("valid"):
        raise ValueError("handoff_input_snapshot_invalid")
    out = Path(output_dir)
    if (out / "WRITING_HANDOFF.json").exists():
        raise ValueError("handoff_output_exists_use_new_directory")
    payload = snapshot_payload(snapshot)
    blocks = {b["block_id"]: b for b in payload["blocks"] if b.get("research_content", True)}
    anchors = {a["anchor_id"]: a for a in dossier.get("source_anchors", [])}
    units = deepcopy(dossier.get("content_units", []))
    ids = [u.get("unit_id") for u in units]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("handoff_unit_ids_invalid")
    cards, issues, used = [], [], set()
    for unit in units:
        primary = list(dict.fromkeys(list(unit.get("source_block_ids") or []) +
            [anchors[a]["block_id"] for a in unit.get("source_anchor_ids", []) if a in anchors]))
        missing = [bid for bid in primary if bid not in blocks]
        paths = {tuple(blocks[b].get("section_path") or []) for b in primary if b in blocks} - {()}
        companions = [bid for bid, b in blocks.items() if bid not in primary
            and tuple(b.get("section_path") or []) in paths
            and b.get("block_type") in {"figure_caption", "table_caption", "table", "table_cell"}]
        used.update(b for b in primary + companions if b in blocks)
        if not primary or missing:
            issues.append({"unit_id": unit["unit_id"], "code": "source_navigation_missing", "missing_block_ids": missing})
        review = unit.get("verification_ref") or {}
        text = [str(unit.get("statement") or ""), "Source attribution: " + str(unit.get("origin_type") or unit.get("evidence_role") or "unspecified")]
        text += _fields(unit.get("context") or {}, "context") + _fields(unit.get("quantities") or [], "quantities")
        text += _fields(review, "prior_model_review")
        cards.append({"card_id": unit["unit_id"], "text_with_conditions": "\n".join(text), "original_unit": unit,
            "source_block_ids": primary, "related_section_context_ids": companions,
            "context_is_not_claim_support": True,
            "requires_source_check": review.get("status") not in {"supported_as_report", "supported_as_inference"} or not primary or bool(missing)})
    analyses = {f.get("facet_id"): f for f in dossier.get("facet_analyses", [])}
    facets = []
    for f in dossier["input"].get("facets", []):
        fid, a = f["facet_id"], analyses.get(f["facet_id"], {})
        linked = list(a.get("relevant_unit_ids") or [])
        linked += [uid for answer in a.get("answer_blocks", []) for uid in answer.get("content_unit_ids", [])]
        linked += [u["unit_id"] for u in units if any(x.get("facet_id") == fid for x in u.get("facet_links", []))]
        facets.append({"facet_id": fid, "question": deepcopy(f), "card_ids": [i for i in dict.fromkeys(linked) if i in ids],
            "conditions_and_boundaries": deepcopy(a.get("conditions_and_boundaries", [])),
            "limitations_and_counterpoints": deepcopy(a.get("limitations_and_counterpoints", [])),
            "prior_coverage_label": a.get("answer_status"), "prior_coverage_label_is_not_a_filter": True})
    linked = {i for f in facets for i in f["card_ids"]}
    bank = [{"block_id": bid, "text": b.get("text_normalized") or b.get("text_raw") or "", "block_type": b.get("block_type"),
        "section_path": b.get("section_path", []), "locator": deepcopy(b.get("locator", {}))} for bid, b in blocks.items() if bid in used]
    result = {"schema_version": VERSION, "dossier_id": dossier.get("dossier_id"), "snapshot_id": payload.get("snapshot_id"),
        "material_scope": validation.get("material_scope"), "paper_identity": deepcopy(dossier.get("paper_identity_ref")),
        "upstream_delivery_status": deepcopy(dossier.get("status", {})), "scientific_certification": False,
        "consumer_contract": [
            "Read text_with_conditions together with original_unit; never use its statement alone.",
            "Retain applicable system/cohort, assay-specific uncertainty and attribution in prose.",
            "Prior model verdicts are fallible; check load-bearing claims against the source bank.",
            "Related section captions are navigation context, not automatically supporting citations.",
            "Attributed hypotheses and cited work may supply background despite a not-addressed coverage label.",
            "Abstract-only material is background; its silence cannot establish absence from the study.",
            "Cite actual supporting block IDs; do not upgrade upstream delivery status."],
        "facets": facets, "cards": cards, "unassigned_card_ids": [i for i in ids if i not in linked],
        "source_bank": bank, "issues": issues,
        "summary": {"input_units": len(units), "preserved_units": len(cards), "llm_calls": 0, "upstream_status_overridden": False},
        "input_units_sha256": hashlib.sha256(_json(units).encode()).hexdigest()}
    assert [c["original_unit"] for c in cards] == dossier.get("content_units", [])
    out.mkdir(parents=True, exist_ok=True)
    (out / "WRITING_HANDOFF.json").write_text(_json(result), encoding="utf-8")
    lines = ["# 写作素材交接包", "", "结论与限定一起读取。本包保留原有判断，不代表新的科学验收。", ""]
    for f in facets:
        lines += ["## " + f["facet_id"], "", "材料卡：" + ", ".join(f["card_ids"]), _json(f["conditions_and_boundaries"]), _json(f["limitations_and_counterpoints"]), ""]
    for c in cards:
        lines += ["## " + c["card_id"], "", c["text_with_conditions"], "", "原引用：" + ", ".join(c["source_block_ids"]),
            "同节图表上下文（需核对）：" + ", ".join(c["related_section_context_ids"]), ""]
    (out / "WRITING_HANDOFF.md").write_text("\n".join(lines), encoding="utf-8")
    return result
