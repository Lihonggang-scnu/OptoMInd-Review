"""Build high-density text paper cards from canonical fulltext chunks.

This pipeline complements the visual-asset path:
- visual assets use Qwen-VL to make figure/table cards;
- every fulltext paper, including papers with no recovered images, uses this
  text pipeline to make a structured paper card for downstream review writing.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.literature_resource_builder import normalize_doi, normalize_space, parse_json_like, safe_filename


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORE58_INDEX = (
    PROJECT_ROOT
    / "outputs"
    / "literature_resource_builder"
    / "web_jobs"
    / "20260701-152234-51de78"
    / "artifacts"
    / "core58_fulltext_index.json"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "paper_text_cards"
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Paper Text Card Builder.txt"
DEFAULT_VISUAL_SUMMARIES = [
    PROJECT_ROOT / "outputs" / "visual_asset_pipeline" / "core58-v31-20260701" / "run_summary.json",
    PROJECT_ROOT / "outputs" / "visual_asset_pipeline" / "zero13-rerun4-v31-20260702" / "run_summary.json",
    PROJECT_ROOT / "outputs" / "visual_asset_pipeline" / "zero5-rerun-v31-20260702" / "run_summary.json",
]


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_output_lock(output_dir: Path) -> Path:
    """Prevent two card builders from writing the same output directory."""

    lock_path = output_dir / ".paper_text_card_pipeline.lock"
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                old_pid = int(lock_path.read_text(encoding="ascii").strip())
            except Exception:
                old_pid = -1
            if _pid_is_alive(old_pid):
                raise RuntimeError(
                    f"Another paper-card pipeline is active for this output directory (pid={old_pid})."
                )
            lock_path.unlink(missing_ok=True)
    else:
        raise RuntimeError(f"Could not acquire output lock: {lock_path}")

    def release() -> None:
        try:
            if lock_path.exists() and lock_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(release)
    return lock_path


def read_text(path: str | Path) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def short_stem(item: dict[str, Any]) -> str:
    doi = normalize_doi(item.get("doi") or "")
    raw = doi or str(item.get("paper_id") or item.get("title") or "paper")
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    if doi:
        label = re.sub(r"[^A-Za-z0-9]+", "-", doi).strip("-")[:46]
    else:
        label = safe_filename(str(item.get("title") or "paper"))[:46]
    return f"{str(item.get('index') or 'x').zfill(3)}-{label}-{digest}"


def load_core_items(index_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("core_fulltexts"), list):
        return data, list(data["core_fulltexts"])
    if isinstance(data, list):
        return {"source": str(index_path), "core_fulltext_count": len(data)}, data
    raise ValueError(f"Unsupported index shape: {index_path}")


def load_chunks(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path) if path else Path()
    if not p.exists():
        return []
    chunks: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        text = normalize_space(str(obj.get("text") or ""))
        if not text:
            continue
        obj["text"] = text
        chunks.append(obj)
    return chunks


def load_visual_lookup(paths: list[Path]) -> dict[str, int]:
    by_doi: dict[str, int] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for row in data.get("paper_summaries", []):
            doi = normalize_doi(row.get("doi") or "")
            if not doi:
                continue
            by_doi[doi] = max(int(by_doi.get(doi, 0)), int(row.get("asset_count") or 0))
    return by_doi


BOILERPLATE_PATTERNS = [
    "download date",
    "take down policy",
    "publication record",
    "license:",
    "all rights reserved",
    "terms and conditions",
    "cookie",
    "advertisement",
    "sign in",
    "view article",
    "download pdf",
]


KEYWORD_GROUPS: dict[str, list[str]] = {
    "problem_context": [
        "abstract", "introduction", "background", "motivation", "challenge", "problem",
        "solar heating", "thermal management", "radiative cooling", "passive cooling",
        "greenhouse", "agricultural", "photosynthesis", "daytime",
    ],
    "method_design": [
        "method", "materials", "fabrication", "prepared", "synthesized", "coating",
        "film", "structure", "porous", "particle", "polymer", "cellulose", "multilayer",
        "simulation", "model", "machine learning", "inverse design", "optimization",
        "experiment", "measurement", "spectral", "emissivity", "reflectance",
        "transmittance", "scattering",
    ],
    "results_metrics": [
        "results", "performance", "cooling power", "temperature drop", "sub-ambient",
        "solar reflectance", "emissivity", "atmospheric window", "8-13", "thermal",
        "w/m", "degc", "°c", "reflectance", "transmittance", "absorption", "durability",
        "aging", "outdoor", "benchmark", "compared with",
    ],
    "limitations_outlook": [
        "limitation", "challenge", "future", "outlook", "conclusion", "discussion",
        "stability", "scalability", "cost", "durability", "humidity", "soiling",
        "weather", "application", "commercial",
    ],
    "review_taxonomy": [
        "review", "perspective", "classification", "taxonomy", "recent progress",
        "state-of-the-art", "mechanism", "summary", "bottleneck", "roadmap",
    ],
}


NUMBER_RE = re.compile(
    r"(?i)(?:[-+]?\d+(?:\.\d+)?\s*(?:%|w/m2|w m-2|w\s*m\s*[-−]\s*2|k|degc|°c|nm|um|µm|mm|m|pa|h|day|days|month|months|year|years)|8\s*[-–]\s*13\s*(?:um|µm))"
)


def is_boilerplate(text: str) -> bool:
    low = text.lower()
    hits = sum(1 for pat in BOILERPLATE_PATTERNS if pat in low)
    return hits >= 2 and len(text) < 2500


def keyword_score(text: str, keywords: list[str]) -> int:
    low = text.lower()
    return sum(low.count(k.lower()) for k in keywords)


def chunk_id(chunk: dict[str, Any]) -> str:
    return str(chunk.get("chunk_id") or f"chunk:{chunk.get('ordinal', '')}")


def trim_chunk(text: str, keywords: list[str], max_chars: int = 1800) -> str:
    text = normalize_space(text)
    if len(text) <= max_chars:
        return text
    low = text.lower()
    positions = [low.find(k.lower()) for k in keywords if low.find(k.lower()) >= 0]
    if positions:
        pos = min(positions)
        start = max(0, pos - max_chars // 3)
        end = min(len(text), start + max_chars)
        return text[start:end]
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + " ... " + tail


def rank_chunks_for_group(chunks: list[dict[str, Any]], group: str, limit: int) -> list[dict[str, Any]]:
    keywords = KEYWORD_GROUPS[group]
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, chunk in enumerate(chunks):
        text = str(chunk.get("text") or "")
        if len(text) < 120 or is_boilerplate(text):
            continue
        score = keyword_score(text, keywords)
        if group == "problem_context" and idx <= 8:
            score += 2
        if group == "limitations_outlook" and idx >= max(0, len(chunks) - 10):
            score += 2
        if group == "results_metrics":
            score += min(6, len(NUMBER_RE.findall(text)))
        if score <= 0:
            continue
        scored.append((score, -idx, chunk))
    scored.sort(reverse=True, key=lambda x: (x[0], x[1]))
    return [c for _, _, c in scored[:limit]]


def number_snippets(chunks: list[dict[str, Any]], limit: int = 16) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        if is_boilerplate(text):
            continue
        for match in NUMBER_RE.finditer(text):
            start = max(0, match.start() - 220)
            end = min(len(text), match.end() + 280)
            snippet = normalize_space(text[start:end])
            out.append({"source_chunk_id": chunk_id(chunk), "snippet": snippet})
            if len(out) >= limit:
                return out
    return out


def build_evidence_packet(
    item: dict[str, Any],
    chunks: list[dict[str, Any]],
    full_text_chars: int,
    visual_count: int,
    *,
    max_input_chars: int,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_chunks(group: str, rows: list[dict[str, Any]], per_chunk_chars: int = 1800) -> None:
        for chunk in rows:
            cid = chunk_id(chunk)
            if cid in seen:
                continue
            seen.add(cid)
            selected.append(
                {
                    "group": group,
                    "source_chunk_id": cid,
                    "ordinal": chunk.get("ordinal"),
                    "text": trim_chunk(str(chunk.get("text") or ""), KEYWORD_GROUPS.get(group, []), per_chunk_chars),
                }
            )

    non_boilerplate = [c for c in chunks if len(str(c.get("text") or "")) >= 120 and not is_boilerplate(str(c.get("text") or ""))]
    add_chunks("front_matter_or_abstract", non_boilerplate[:4], per_chunk_chars=1700)
    for group, limit in [
        ("problem_context", 4),
        ("method_design", 5),
        ("results_metrics", 7),
        ("limitations_outlook", 4),
        ("review_taxonomy", 4),
    ]:
        add_chunks(group, rank_chunks_for_group(chunks, group, limit), per_chunk_chars=1800)
    add_chunks("ending_or_conclusion", non_boilerplate[-3:], per_chunk_chars=1600)

    # Enforce a hard payload budget while preserving group diversity.
    budget = max(8000, int(max_input_chars))
    compact_selected: list[dict[str, Any]] = []
    used = 0
    for row in selected:
        text = str(row.get("text") or "")
        if used + len(text) > budget and compact_selected:
            continue
        compact_selected.append(row)
        used += len(text)

    return {
        "metadata": {
            "index": item.get("index"),
            "paper_id": item.get("paper_id"),
            "title": item.get("title"),
            "doi": item.get("doi"),
            "fulltext_type": item.get("fulltext_type"),
            "access_method": item.get("access_method"),
            "quality_tier": item.get("quality_tier"),
            "query_relevance": item.get("query_relevance"),
            "matched_features": item.get("matched_features"),
            "source_url": item.get("source_url"),
        },
        "source_status": {
            "full_text_chars": full_text_chars,
            "chunk_count": len(chunks),
            "visual_assets_count": visual_count,
            "visual_assets_status": "has_visual_assets" if visual_count > 0 else "none_recovered",
        },
        "selected_evidence_snippets": compact_selected,
        "number_snippets": number_snippets(chunks),
        "instructions": [
            "Use only supplied snippets.",
            "Cite source_chunk_ids for important claims and numbers.",
            "If snippets are insufficient, add extraction_warnings.",
        ],
    }


def parse_card(content: str) -> dict[str, Any]:
    parsed = parse_json_like(content, fallback={})
    return parsed if isinstance(parsed, dict) else {}


def canonicalize_chunk_references(card: dict[str, Any], valid_chunk_ids: set[str]) -> dict[str, Any]:
    """Map unambiguous short chunk aliases back to canonical per-paper IDs.

    Models sometimes copy ``c0007`` instead of the supplied
    ``doi:...:c0007``.  The alias is safe only when it resolves to exactly one
    candidate in the current paper.  Anything else remains unresolved and is
    rejected by validation rather than silently linked to the wrong text.
    """

    suffix_map: dict[str, list[str]] = {}
    for cid in valid_chunk_ids:
        suffix_map.setdefault(str(cid).rsplit(":", 1)[-1], []).append(str(cid))
    remapped: dict[str, str] = {}
    unresolved: set[str] = set()

    def canonical(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or raw in valid_chunk_ids:
            return raw
        matches = suffix_map.get(raw.rsplit(":", 1)[-1], [])
        if len(matches) == 1:
            remapped[raw] = matches[0]
            return matches[0]
        unresolved.add(raw)
        return raw

    for field_name in ("key_results", "important_numbers"):
        rows = card.get(field_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ids = row.get("source_chunk_ids")
            if isinstance(ids, list):
                row["source_chunk_ids"] = list(dict.fromkeys(canonical(cid) for cid in ids if cid))
    evidence_rows = card.get("evidence_map")
    if isinstance(evidence_rows, list):
        for row in evidence_rows:
            if isinstance(row, dict) and row.get("source_chunk_id"):
                row["source_chunk_id"] = canonical(row.get("source_chunk_id"))
    return {
        "remapped_count": len(remapped),
        "remapped": remapped,
        "unresolved": sorted(unresolved),
    }


def validate_card(card: dict[str, Any], item: dict[str, Any], valid_chunk_ids: set[str], full_text_chars: int) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if re.search(r"[\u3400-\u9fff]", json.dumps(card, ensure_ascii=False)):
        errors.append("non_english_cjk_output")
    if not isinstance(card, dict) or not card:
        return {"ok": False, "errors": ["empty_or_invalid_json"], "warnings": warnings}
    if card.get("schema_version") != "paper_text_card.v1":
        errors.append("wrong_or_missing_schema_version")
    if not normalize_space(str(card.get("one_sentence_contribution") or "")):
        errors.append("missing_one_sentence_contribution")
    summary = normalize_space(str(card.get("high_density_summary") or ""))
    if full_text_chars > 8000 and len(summary) < 120:
        errors.append("summary_too_short")
    key_results = card.get("key_results")
    if not isinstance(key_results, list) or not key_results:
        errors.append("missing_key_results")
    else:
        cited = 0
        for row in key_results:
            if isinstance(row, dict) and row.get("source_chunk_ids"):
                cited += 1
        if cited == 0:
            errors.append("key_results_have_no_chunk_citations")
    reusable = card.get("directly_reusable_sentences")
    if not isinstance(reusable, list) or not reusable:
        errors.append("missing_directly_reusable_sentences")
    identity = card.get("paper_identity") if isinstance(card.get("paper_identity"), dict) else {}
    expected_doi = normalize_doi(item.get("doi") or "")
    got_doi = normalize_doi(identity.get("doi") or "")
    if expected_doi and got_doi and expected_doi != got_doi:
        warnings.append(f"doi_mismatch:{got_doi}")
    for field in ("method_or_design", "limitations_and_open_questions", "useful_for_review_sections"):
        if field not in card:
            warnings.append(f"missing_field:{field}")
    referenced: set[str] = set()
    for container in [card.get("key_results"), card.get("important_numbers"), card.get("evidence_map")]:
        if not isinstance(container, list):
            continue
        for row in container:
            if not isinstance(row, dict):
                continue
            for cid in row.get("source_chunk_ids") or [row.get("source_chunk_id")]:
                if cid:
                    referenced.add(str(cid))
    unknown = sorted(cid for cid in referenced if cid not in valid_chunk_ids)
    if unknown:
        errors.append("unknown_chunk_ids:" + ",".join(unknown[:5]))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def normalize_card(card: dict[str, Any], item: dict[str, Any], visual_count: int) -> dict[str, Any]:
    card.setdefault("schema_version", "paper_text_card.v1")
    card.setdefault("card_type", "text_fulltext_card")
    identity = card.setdefault("paper_identity", {})
    if isinstance(identity, dict):
        # Bibliographic identity is deterministic upstream metadata, not an
        # LLM extraction target.  Overwrite model punctuation/encoding drift.
        identity["title"] = item.get("title") or identity.get("title") or ""
        identity["doi"] = item.get("doi") or identity.get("doi") or ""
        identity["paper_id"] = item.get("paper_id") or identity.get("paper_id") or ""
        identity["year"] = item.get("year") if item.get("year") is not None else identity.get("year")
        identity["venue"] = item.get("venue") or identity.get("venue") or ""
    source = card.setdefault("source_status", {})
    if isinstance(source, dict):
        source.setdefault("fulltext_type", item.get("fulltext_type") or "")
        source.setdefault("text_quality", "fulltext")
        source["visual_assets_count"] = int(visual_count)
        source["visual_assets_status"] = "has_visual_assets" if visual_count > 0 else "none_recovered"
    for key, default in [
        ("one_sentence_contribution", ""),
        ("high_density_summary", ""),
        ("research_problem_and_context", ""),
        ("core_question_or_gap", ""),
        ("mechanisms", []),
        ("key_results", []),
        ("important_numbers", []),
        ("comparison_or_benchmark", ""),
        ("limitations_and_open_questions", []),
        ("useful_for_review_sections", []),
        ("directly_reusable_sentences", []),
        ("evidence_map", []),
        ("extraction_warnings", []),
        ("confidence", "medium"),
    ]:
        card.setdefault(key, default)
    card.setdefault(
        "method_or_design",
        {
            "summary": "",
            "materials_or_system": [],
            "fabrication_or_implementation": [],
            "measurement_or_evaluation": [],
            "modeling_or_theory": [],
        },
    )
    return card


def call_card_builder(
    prompt: str,
    item: dict[str, Any],
    packet: dict[str, Any],
    *,
    model_tier: str,
    max_tokens: int,
    allow_model_fallback: bool,
    llm_max_retries: int,
    llm_timeout_seconds: float,
    llm_max_transport_key_candidates: int,
    retry_feedback: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    user_payload = {"paper_input": packet}
    if retry_feedback:
        user_payload["previous_output_validation_feedback"] = retry_feedback
        user_payload["retry_instruction"] = "Return a corrected complete JSON object following the schema."
    result = call_qwen_chat(
        "PaperTextCardAgent",
        [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        model_tier=model_tier,
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        allow_model_fallback=allow_model_fallback,
        max_retries=llm_max_retries,
        timeout_seconds=llm_timeout_seconds,
        max_transport_key_candidates=llm_max_transport_key_candidates,
        stream=True,
        accept_partial_stream=False,
    )
    content = str(result.get("content") or "")
    return parse_card(content), result.get("_llm_usage", {}), content


def build_one_card(
    item: dict[str, Any],
    *,
    prompt: str,
    output_dir: Path,
    visual_lookup: dict[str, int],
    model_tier: str,
    max_input_chars: int,
    max_tokens: int,
    retry_once: bool,
    allow_model_fallback: bool,
    llm_max_retries: int,
    llm_timeout_seconds: float,
    llm_max_transport_key_candidates: int,
    repair_model_tier: str,
) -> dict[str, Any]:
    start = time.time()
    text = read_text(item.get("parsed_text_path") or "")
    chunks = load_chunks(item.get("chunk_index_path") or "")
    doi = normalize_doi(item.get("doi") or "")
    visual_count = int(visual_lookup.get(doi, 0))
    stem = short_stem(item)
    card_dir = output_dir / "paper_cards"
    payload_dir = output_dir / "evidence_packets"
    card_dir.mkdir(parents=True, exist_ok=True)
    payload_dir.mkdir(parents=True, exist_ok=True)
    card_path = card_dir / f"{stem}.paper_text_card.json"
    packet_path = payload_dir / f"{stem}.evidence_packet.json"

    if not text or not chunks:
        result = {
            "index": item.get("index"),
            "doi": doi,
            "title": item.get("title"),
            "ok": False,
            "errors": ["missing_text_or_chunks"],
            "warnings": [],
            "card_path": str(card_path),
            "elapsed_seconds": round(time.time() - start, 2),
        }
        return result

    packet = build_evidence_packet(item, chunks, len(text), visual_count, max_input_chars=max_input_chars)
    packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    valid_chunk_ids = {chunk_id(c) for c in chunks}

    card, usage, raw_content = call_card_builder(
        prompt,
        item,
        packet,
        model_tier=model_tier,
        max_tokens=max_tokens,
        allow_model_fallback=allow_model_fallback,
        llm_max_retries=llm_max_retries,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_max_transport_key_candidates=llm_max_transport_key_candidates,
    )
    llm_attempts = [dict(usage)]
    card = normalize_card(card, item, visual_count)
    reference_canonicalization = canonicalize_chunk_references(card, valid_chunk_ids)
    validation = validate_card(card, item, valid_chunk_ids, len(text))
    retry_used = False
    raw_response_path = card_dir / f"{stem}.raw_response.txt"
    raw_response_path.write_text(raw_content, encoding="utf-8", errors="replace")

    if retry_once and not validation["ok"]:
        retry_used = True
        retry_card, retry_usage, retry_raw = call_card_builder(
            prompt,
            item,
            packet,
            model_tier=repair_model_tier,
            max_tokens=max_tokens,
            allow_model_fallback=allow_model_fallback,
            llm_max_retries=llm_max_retries,
            llm_timeout_seconds=llm_timeout_seconds,
            llm_max_transport_key_candidates=llm_max_transport_key_candidates,
            retry_feedback=validation,
        )
        llm_attempts.append(dict(retry_usage))
        retry_card = normalize_card(retry_card, item, visual_count)
        retry_reference_canonicalization = canonicalize_chunk_references(retry_card, valid_chunk_ids)
        retry_validation = validate_card(retry_card, item, valid_chunk_ids, len(text))
        if retry_validation["ok"] or len(retry_validation["errors"]) < len(validation["errors"]):
            card, usage, raw_content, validation = retry_card, retry_usage, retry_raw, retry_validation
            reference_canonicalization = retry_reference_canonicalization
            raw_response_path.write_text(raw_content, encoding="utf-8", errors="replace")

    card["_local_metadata"] = {
        "card_path": str(card_path),
        "evidence_packet_path": str(packet_path),
        "parsed_text_path": item.get("parsed_text_path") or "",
        "chunk_index_path": item.get("chunk_index_path") or "",
        "source_url": item.get("source_url") or "",
        "full_text_chars": len(text),
        "chunk_count": len(chunks),
        "selected_snippet_count": len(packet.get("selected_evidence_snippets") or []),
        "model_tier": model_tier,
        "llm_usage": usage,
        "llm_attempts": llm_attempts,
        "repair_model_tier": repair_model_tier if retry_used else "",
        "validation": validation,
        "reference_canonicalization": reference_canonicalization,
        "retry_used": retry_used,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "index": item.get("index"),
        "doi": doi,
        "title": item.get("title"),
        "ok": bool(validation["ok"]),
        "errors": validation["errors"],
        "warnings": validation["warnings"],
        "confidence": card.get("confidence"),
        "visual_assets_count": visual_count,
        "card_path": str(card_path),
        "evidence_packet_path": str(packet_path),
        "full_text_chars": len(text),
        "chunk_count": len(chunks),
        "retry_used": retry_used,
        "elapsed_seconds": round(time.time() - start, 2),
    }


def repair_existing_card_references(output_dir: Path, items: list[dict[str, Any]]) -> dict[str, int]:
    """Deterministically repair and revalidate already generated cards."""

    stats = {"cards_seen": 0, "cards_repaired": 0, "references_remapped": 0, "cards_still_invalid": 0}
    for item in items:
        card_path = output_dir / "paper_cards" / f"{short_stem(item)}.paper_text_card.json"
        if not card_path.exists():
            continue
        stats["cards_seen"] += 1
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
            chunks = load_chunks(item.get("chunk_index_path") or "")
            valid_chunk_ids = {chunk_id(chunk) for chunk in chunks}
            repair = canonicalize_chunk_references(card, valid_chunk_ids)
            full_text_chars = int((card.get("_local_metadata") or {}).get("full_text_chars") or 0)
            validation = validate_card(card, item, valid_chunk_ids, full_text_chars)
            local = card.setdefault("_local_metadata", {})
            local["reference_canonicalization"] = repair
            local["validation"] = validation
            if repair["remapped_count"]:
                stats["cards_repaired"] += 1
                stats["references_remapped"] += int(repair["remapped_count"])
            if not validation["ok"]:
                stats["cards_still_invalid"] += 1
            card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            stats["cards_still_invalid"] += 1
    return stats


def write_quality_report(output_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Paper text card quality report",
        "",
        f"- Processed: {summary['processed']}",
        f"- OK: {summary['ok']}",
        f"- Failed: {summary['failed']}",
        f"- Model tier: {summary['model_tier']}",
        "",
        "| # | DOI | OK | Visuals | Chars | Warnings/Errors | Card |",
        "|---:|---|---:|---:|---:|---|---|",
    ]
    for row in summary["results"]:
        notes = "; ".join((row.get("errors") or []) + (row.get("warnings") or []))[:260]
        lines.append(
            f"| {row.get('index')} | {row.get('doi')} | {row.get('ok')} | {row.get('visual_assets_count')} | {row.get('full_text_chars')} | {notes} | {row.get('card_path')} |"
        )
    (output_dir / "card_quality_report.md").write_text("\n".join(lines), encoding="utf-8")


def aggregate_existing_results(output_dir: Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a run-summary result table from all card files in output_dir."""
    card_dir = output_dir / "paper_cards"
    by_identity: dict[str, Path] = {}

    def identity_key(*, doi: Any = "", paper_id: Any = "", title: Any = "") -> str:
        norm_doi = normalize_doi(doi or "")
        if norm_doi:
            return f"doi:{norm_doi}"
        pid = normalize_space(str(paper_id or "")).casefold()
        if pid:
            return f"paper:{pid}"
        normalized_title = normalize_space(str(title or "")).casefold()
        return f"title:{normalized_title}" if normalized_title else ""

    if card_dir.exists():
        for path in card_dir.glob("*.paper_text_card.json"):
            try:
                card = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            identity = card.get("paper_identity") or {}
            key = identity_key(
                doi=identity.get("doi"),
                paper_id=identity.get("paper_id"),
                title=identity.get("title"),
            )
            if key:
                by_identity[key] = path
    rows: list[dict[str, Any]] = []
    for item in items:
        doi = normalize_doi(item.get("doi") or "")
        key = identity_key(doi=doi, paper_id=item.get("paper_id"), title=item.get("title"))
        path = by_identity.get(key)
        if not path and not doi:
            # LLM paper cards intentionally omit local paper_id.  Title is
            # therefore the stable fallback for DOI-less records.
            path = by_identity.get(identity_key(title=item.get("title")))
        if not path:
            # The deterministic filename is the authoritative local identity
            # when a model normalizes or mojibakes punctuation in a title.
            expected = card_dir / f"{short_stem(item)}.paper_text_card.json"
            if expected.exists():
                path = expected
        if not path:
            continue
        try:
            card = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            rows.append(
                {
                    "index": item.get("index"),
                    "doi": doi,
                    "title": item.get("title"),
                    "ok": False,
                    "errors": [f"card_read_failed:{type(exc).__name__}"],
                    "warnings": [],
                    "card_path": str(path),
                }
            )
            continue
        local = card.get("_local_metadata") if isinstance(card.get("_local_metadata"), dict) else {}
        validation = local.get("validation") if isinstance(local.get("validation"), dict) else {}
        source = card.get("source_status") if isinstance(card.get("source_status"), dict) else {}
        rows.append(
            {
                "index": item.get("index"),
                "doi": doi,
                "title": item.get("title"),
                "ok": bool(validation.get("ok", True)),
                "errors": validation.get("errors") or [],
                "warnings": validation.get("warnings") or [],
                "confidence": card.get("confidence"),
                "visual_assets_count": int(source.get("visual_assets_count") or 0),
                "card_path": str(path),
                "evidence_packet_path": local.get("evidence_packet_path") or "",
                "full_text_chars": local.get("full_text_chars"),
                "chunk_count": local.get("chunk_count"),
                "retry_used": bool(local.get("retry_used")),
                "elapsed_seconds": local.get("elapsed_seconds", ""),
            }
        )
    rows.sort(key=lambda r: int(r.get("index") or 0))
    return rows


def run(args: argparse.Namespace) -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    index_path = Path(args.index_path)
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / utc_stamp()
    output_dir.mkdir(parents=True, exist_ok=True)
    acquire_output_lock(output_dir)
    prompt = read_text(args.prompt_path)
    if not prompt:
        raise SystemExit(f"Prompt not found or empty: {args.prompt_path}")
    meta, items = load_core_items(index_path)
    selected = items[int(args.offset) :]
    if int(args.limit) > 0:
        selected = selected[: int(args.limit)]
    visual_paths = [Path(p) for p in str(args.visual_summaries or "").split(";") if p.strip()]
    if not visual_paths:
        visual_paths = DEFAULT_VISUAL_SUMMARIES
    visual_lookup = load_visual_lookup(visual_paths)

    if args.retry_invalid_existing:
        remaining = []
        for item in selected:
            card_path = output_dir / "paper_cards" / f"{short_stem(item)}.paper_text_card.json"
            if not card_path.exists():
                remaining.append(item)
                continue
            try:
                existing = json.loads(card_path.read_text(encoding="utf-8", errors="replace"))
                validation = ((existing.get("_local_metadata") or {}).get("validation") or {})
                if not bool(validation.get("ok")):
                    remaining.append(item)
            except Exception:
                remaining.append(item)
        selected = remaining
    elif args.skip_existing:
        remaining = []
        for item in selected:
            card_path = output_dir / "paper_cards" / f"{short_stem(item)}.paper_text_card.json"
            if not card_path.exists():
                remaining.append(item)
        selected = remaining

    start = time.time()
    results: list[dict[str, Any]] = []
    print(json.dumps({"event": "start", "index_path": str(index_path), "selected": len(selected), "output_dir": str(output_dir), "model_tier": args.model_tier}, ensure_ascii=False), flush=True)

    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for idx, item in enumerate(selected, 1):
            print(json.dumps({"event": "paper_start", "i": idx, "n": len(selected), "doi": item.get("doi"), "title": str(item.get("title") or "")[:100]}, ensure_ascii=False), flush=True)
            row = build_one_card(
                item,
                prompt=prompt,
                output_dir=output_dir,
                visual_lookup=visual_lookup,
                model_tier=args.model_tier,
                max_input_chars=args.max_input_chars,
                max_tokens=args.max_tokens,
                retry_once=bool(args.retry_once),
                allow_model_fallback=bool(args.allow_model_fallback),
                llm_max_retries=int(args.llm_max_retries),
                llm_timeout_seconds=float(args.llm_timeout_seconds),
                llm_max_transport_key_candidates=int(args.llm_max_transport_key_candidates),
                repair_model_tier=str(args.repair_model_tier),
            )
            results.append(row)
            print(json.dumps({"event": "paper_done", "doi": row.get("doi"), "ok": row.get("ok"), "errors": row.get("errors"), "elapsed_seconds": row.get("elapsed_seconds")}, ensure_ascii=False), flush=True)
            if float(args.sleep_seconds or 0) > 0:
                time.sleep(float(args.sleep_seconds))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {}
            for item in selected:
                future = pool.submit(
                    build_one_card,
                    item,
                    prompt=prompt,
                    output_dir=output_dir,
                    visual_lookup=visual_lookup,
                    model_tier=args.model_tier,
                    max_input_chars=args.max_input_chars,
                    max_tokens=args.max_tokens,
                    retry_once=bool(args.retry_once),
                    allow_model_fallback=bool(args.allow_model_fallback),
                    llm_max_retries=int(args.llm_max_retries),
                    llm_timeout_seconds=float(args.llm_timeout_seconds),
                    llm_max_transport_key_candidates=int(args.llm_max_transport_key_candidates),
                    repair_model_tier=str(args.repair_model_tier),
                )
                future_map[future] = item
            done = 0
            for future in as_completed(future_map):
                done += 1
                item = future_map[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = {
                        "index": item.get("index"),
                        "doi": normalize_doi(item.get("doi") or ""),
                        "title": item.get("title"),
                        "ok": False,
                        "errors": [f"{type(exc).__name__}:{exc}"],
                        "warnings": [],
                    }
                results.append(row)
                print(json.dumps({"event": "paper_done", "i": done, "n": len(selected), "doi": row.get("doi"), "ok": row.get("ok"), "errors": row.get("errors")}, ensure_ascii=False), flush=True)

    results.sort(key=lambda r: int(r.get("index") or 0))
    reference_repair = repair_existing_card_references(output_dir, items)
    aggregate_results = aggregate_existing_results(output_dir, items)
    summary_results = aggregate_results if aggregate_results else results
    jsonl_path = output_dir / "paper_text_cards.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in summary_results:
            path = row.get("card_path")
            if path and Path(path).exists():
                try:
                    card_obj = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
                    f.write(json.dumps(card_obj, ensure_ascii=False) + "\n")
                except Exception:
                    continue
    summary = {
        "schema_version": "paper_text_card_run.v1",
        "source_meta": meta,
        "index_path": str(index_path),
        "output_dir": str(output_dir),
        "prompt_path": str(args.prompt_path),
        "model_tier": args.model_tier,
        "workers": workers,
        "processed_this_run": len(results),
        "processed": len(summary_results),
        "ok": sum(1 for r in summary_results if r.get("ok")),
        "failed": sum(1 for r in summary_results if not r.get("ok")),
        "paper_text_cards_jsonl": str(jsonl_path),
        "elapsed_seconds": round(time.time() - start, 2),
        "results": summary_results,
        "reference_repair": reference_repair,
    }
    (output_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_quality_report(output_dir, summary)
    print(json.dumps({"event": "summary", "summary_path": str(output_dir / "run_summary.json"), "cards_jsonl": str(jsonl_path), "ok": summary["ok"], "failed": summary["failed"], "elapsed_seconds": summary["elapsed_seconds"]}, ensure_ascii=False), flush=True)
    return 0 if summary["failed"] == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build high-density text paper cards from fulltext chunks.")
    parser.add_argument("--index-path", default=str(DEFAULT_CORE58_INDEX))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT))
    parser.add_argument("--visual-summaries", default="")
    parser.add_argument("--model-tier", default="PaperTextCardAgent")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-input-chars", type=int, default=32000)
    parser.add_argument("--max-tokens", type=int, default=5200)
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=0,
        help="Retries per model before moving to the next model in the configured ladder.",
    )
    parser.add_argument("--llm-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--llm-max-transport-key-candidates", type=int, default=1)
    parser.add_argument(
        "--repair-model-tier",
        default="premium_model",
        help="Escalation tier used only when the batch model's card fails validation.",
    )
    parser.add_argument(
        "--allow-model-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow the configured tier to fall back to a cheaper model after errors.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=0.1)
    parser.add_argument("--retry-once", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument(
        "--retry-invalid-existing",
        action="store_true",
        help="Process only missing or validation-failed cards and preserve valid existing cards.",
    )
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
