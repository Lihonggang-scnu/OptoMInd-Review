"""Normalize legacy paper text cards into English-only paper cards."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.literature_resource_builder import normalize_doi, parse_json_like, safe_filename
from optomind_research.common_io import contains_cjk, read_text, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "outputs" / "paper_text_cards" / "core58-v1-20260702" / "paper_text_cards.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "paper_text_cards_english"
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Paper Card English Normalizer.txt"


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def load_cards(path: Path) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            cards.append(obj)
    return cards


def identity(card: dict[str, Any]) -> dict[str, Any]:
    ident = card.get("paper_identity") if isinstance(card.get("paper_identity"), dict) else {}
    return {
        "title": str(ident.get("title") or ""),
        "doi": normalize_doi(ident.get("doi") or ""),
        "year": ident.get("year"),
        "venue": str(ident.get("venue") or ""),
    }


def stem_for(card: dict[str, Any], index: int) -> str:
    ident = identity(card)
    raw = ident["doi"] or ident["title"] or f"paper-{index}"
    return f"{index:03d}-{safe_filename(raw)[:80]}"


def minimal_english_card(card: dict[str, Any]) -> dict[str, Any]:
    ident = identity(card)
    source = card.get("source_status") if isinstance(card.get("source_status"), dict) else {}
    local = card.get("_local_metadata") if isinstance(card.get("_local_metadata"), dict) else {}
    return {
        "schema_version": "paper_text_card.v1",
        "card_type": "text_fulltext_card",
        "paper_identity": ident,
        "source_status": {
            "fulltext_type": str(source.get("fulltext_type") or ""),
            "text_quality": str(source.get("text_quality") or "uncertain"),
            "visual_assets_status": str(source.get("visual_assets_status") or "not_checked"),
            "visual_assets_count": int(source.get("visual_assets_count") or 0),
        },
        "one_sentence_contribution": "English normalization was not available; use the original full text and evidence packet for a complete card.",
        "high_density_summary": "This is a minimal English placeholder generated without model access. It preserves paper identity and local source pointers but does not preserve the full scientific content of the legacy non-English card.",
        "research_problem_and_context": "",
        "core_question_or_gap": "",
        "method_or_design": {
            "summary": "",
            "materials_or_system": [],
            "fabrication_or_implementation": [],
            "measurement_or_evaluation": [],
            "modeling_or_theory": [],
        },
        "mechanisms": [],
        "key_results": [],
        "important_numbers": [],
        "comparison_or_benchmark": "",
        "limitations_and_open_questions": [],
        "useful_for_review_sections": [],
        "directly_reusable_sentences": [
            "This placeholder should be replaced by a full English paper card before review writing."
        ],
        "evidence_map": [],
        "extraction_warnings": ["minimal_english_placeholder_used"],
        "confidence": "low",
        "_local_metadata": clean_local_metadata(local),
    }


def clean_local_metadata(local: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "card_path",
        "evidence_packet_path",
        "parsed_text_path",
        "chunk_index_path",
        "source_url",
        "full_text_chars",
        "chunk_count",
        "selected_snippet_count",
        "created_at",
    ]
    return {k: local.get(k) for k in keep if k in local and not contains_cjk(local.get(k))}


def parse_card(content: str) -> dict[str, Any]:
    parsed = parse_json_like(content, fallback={})
    return parsed if isinstance(parsed, dict) else {}


def normalize_shape(card: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    ident = identity(original)
    source = original.get("source_status") if isinstance(original.get("source_status"), dict) else {}
    local = original.get("_local_metadata") if isinstance(original.get("_local_metadata"), dict) else {}
    card.setdefault("schema_version", "paper_text_card.v1")
    card.setdefault("card_type", "text_fulltext_card")
    card.setdefault("paper_identity", {})
    if isinstance(card["paper_identity"], dict):
        card["paper_identity"].setdefault("title", ident["title"])
        card["paper_identity"].setdefault("doi", ident["doi"])
        card["paper_identity"].setdefault("year", ident["year"])
        card["paper_identity"].setdefault("venue", ident["venue"])
    card.setdefault("source_status", {})
    if isinstance(card["source_status"], dict):
        card["source_status"].setdefault("fulltext_type", source.get("fulltext_type") or "")
        card["source_status"].setdefault("text_quality", source.get("text_quality") or "uncertain")
        card["source_status"].setdefault("visual_assets_status", source.get("visual_assets_status") or "not_checked")
        card["source_status"].setdefault("visual_assets_count", int(source.get("visual_assets_count") or 0))
    defaults = {
        "one_sentence_contribution": "",
        "high_density_summary": "",
        "research_problem_and_context": "",
        "core_question_or_gap": "",
        "method_or_design": {
            "summary": "",
            "materials_or_system": [],
            "fabrication_or_implementation": [],
            "measurement_or_evaluation": [],
            "modeling_or_theory": [],
        },
        "mechanisms": [],
        "key_results": [],
        "important_numbers": [],
        "comparison_or_benchmark": "",
        "limitations_and_open_questions": [],
        "useful_for_review_sections": [],
        "directly_reusable_sentences": [],
        "evidence_map": [],
        "extraction_warnings": [],
        "confidence": "medium",
    }
    for key, value in defaults.items():
        card.setdefault(key, value)
    card["_local_metadata"] = clean_local_metadata(local)
    return trim_card(card)


def compact_for_translation(card: dict[str, Any]) -> dict[str, Any]:
    """Reduce legacy card payload before translation.

    We are not re-extracting from full text here. The goal is to translate the
    existing card into an English intermediate asset, while keeping model input
    and output bounded.
    """

    obj = {
        "schema_version": card.get("schema_version"),
        "card_type": card.get("card_type"),
        "paper_identity": card.get("paper_identity"),
        "source_status": card.get("source_status"),
        "one_sentence_contribution": clip_text(card.get("one_sentence_contribution"), 700),
        "high_density_summary": clip_text(card.get("high_density_summary"), 2200),
        "research_problem_and_context": clip_text(card.get("research_problem_and_context"), 900),
        "core_question_or_gap": clip_text(card.get("core_question_or_gap"), 700),
        "method_or_design": compact_method(card.get("method_or_design")),
        "mechanisms": trim_list(card.get("mechanisms"), 8, 260),
        "key_results": trim_list(card.get("key_results"), 6, 650),
        "important_numbers": trim_list(card.get("important_numbers"), 8, 420),
        "comparison_or_benchmark": clip_text(card.get("comparison_or_benchmark"), 900),
        "limitations_and_open_questions": trim_list(card.get("limitations_and_open_questions"), 6, 420),
        "useful_for_review_sections": trim_list(card.get("useful_for_review_sections"), 6, 220),
        "directly_reusable_sentences": trim_list(card.get("directly_reusable_sentences"), 4, 300),
        "evidence_map": trim_list(card.get("evidence_map"), 8, 360),
        "extraction_warnings": trim_list(card.get("extraction_warnings"), 4, 320),
        "confidence": card.get("confidence"),
    }
    return obj


def clip_text(value: Any, max_chars: int) -> str:
    s = str(value or "").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3].rstrip() + "..."


def trim_list(value: Any, max_items: int, item_chars: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    out: list[Any] = []
    for item in value[:max_items]:
        if isinstance(item, dict):
            out.append({k: clip_text(v, item_chars) if isinstance(v, str) else v for k, v in item.items()})
        else:
            out.append(clip_text(item, item_chars))
    return out


def compact_method(value: Any) -> dict[str, Any]:
    method = value if isinstance(value, dict) else {}
    return {
        "summary": clip_text(method.get("summary"), 900),
        "materials_or_system": trim_list(method.get("materials_or_system"), 8, 160),
        "fabrication_or_implementation": trim_list(method.get("fabrication_or_implementation"), 8, 180),
        "measurement_or_evaluation": trim_list(method.get("measurement_or_evaluation"), 8, 180),
        "modeling_or_theory": trim_list(method.get("modeling_or_theory"), 8, 180),
    }


def trim_card(card: dict[str, Any]) -> dict[str, Any]:
    card["one_sentence_contribution"] = clip_text(card.get("one_sentence_contribution"), 600)
    card["high_density_summary"] = clip_text(card.get("high_density_summary"), 2200)
    card["research_problem_and_context"] = clip_text(card.get("research_problem_and_context"), 900)
    card["core_question_or_gap"] = clip_text(card.get("core_question_or_gap"), 800)
    card["method_or_design"] = compact_method(card.get("method_or_design"))
    card["mechanisms"] = trim_list(card.get("mechanisms"), 8, 260)
    card["key_results"] = trim_list(card.get("key_results"), 6, 650)
    card["important_numbers"] = trim_list(card.get("important_numbers"), 8, 420)
    card["comparison_or_benchmark"] = clip_text(card.get("comparison_or_benchmark"), 900)
    card["limitations_and_open_questions"] = trim_list(card.get("limitations_and_open_questions"), 6, 420)
    card["useful_for_review_sections"] = trim_list(card.get("useful_for_review_sections"), 6, 220)
    card["directly_reusable_sentences"] = trim_list(card.get("directly_reusable_sentences"), 4, 300)
    card["evidence_map"] = trim_list(card.get("evidence_map"), 8, 360)
    card["extraction_warnings"] = trim_list(card.get("extraction_warnings"), 4, 320)
    return card


def validate_english(card: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if card.get("schema_version") != "paper_text_card.v1":
        errors.append("wrong_schema_version")
    if contains_cjk(json.dumps(card, ensure_ascii=False)):
        errors.append("contains_chinese")
    if not str(card.get("one_sentence_contribution") or "").strip():
        errors.append("missing_one_sentence_contribution")
    if not str(card.get("high_density_summary") or "").strip():
        errors.append("missing_high_density_summary")
    return errors


def normalize_one(
    index: int,
    card: dict[str, Any],
    *,
    prompt: str,
    output_dir: Path,
    real_llm: bool,
    model_tier: str,
    max_tokens: int,
    retry_once: bool,
) -> dict[str, Any]:
    start = time.time()
    stem = stem_for(card, index)
    out_dir = output_dir / "paper_cards"
    raw_dir = output_dir / "raw_responses"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    card_path = out_dir / f"{stem}.english.paper_text_card.json"
    raw_path = raw_dir / f"{stem}.raw.txt"
    if card_path.exists():
        try:
            existing = json.loads(card_path.read_text(encoding="utf-8", errors="replace"))
            errors = validate_english(existing)
            if not errors:
                return {
                    "index": index,
                    "doi": identity(card)["doi"],
                    "title": identity(card)["title"],
                    "ok": True,
                    "skipped_existing": True,
                    "errors": [],
                    "card_path": str(card_path),
                    "elapsed_seconds": round(time.time() - start, 2),
                }
        except Exception:
            pass
    if not contains_cjk(json.dumps(card, ensure_ascii=False)):
        english = normalize_shape(card, card)
        errors = validate_english(english)
        card_path.write_text(json.dumps(english, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "index": index,
            "doi": identity(card)["doi"],
            "title": identity(card)["title"],
            "ok": not errors,
            "errors": errors,
            "card_path": str(card_path),
            "elapsed_seconds": round(time.time() - start, 2),
        }
    if not real_llm:
        english = minimal_english_card(card)
        errors = validate_english(english)
        card_path.write_text(json.dumps(english, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "index": index,
            "doi": identity(card)["doi"],
            "title": identity(card)["title"],
            "ok": not errors,
            "errors": errors,
            "card_path": str(card_path),
            "elapsed_seconds": round(time.time() - start, 2),
        }

    payload = {
            "paper_card_to_rewrite": compact_for_translation(card)
    }
    result = call_qwen_chat(
        "PaperCardEnglishNormalizerAgent",
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model_tier=model_tier,
        temperature=0.05,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    raw = str(result.get("content") or "")
    raw_path.write_text(raw, encoding="utf-8", errors="replace")
    english = normalize_shape(parse_card(raw), card)
    errors = validate_english(english)
    retry_used = False
    if errors and retry_once:
        retry_used = True
        retry_payload = {
            "previous_errors": errors,
            "instruction": "Return a corrected English-only JSON object. Do not include Chinese characters.",
            "paper_card_to_rewrite": {
                k: v for k, v in compact_for_translation(card).items()
            },
        }
        retry = call_qwen_chat(
            "PaperCardEnglishNormalizerAgent",
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(retry_payload, ensure_ascii=False)},
            ],
            model_tier=model_tier,
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        raw = str(retry.get("content") or "")
        raw_path.write_text(raw, encoding="utf-8", errors="replace")
        english = normalize_shape(parse_card(raw), card)
        errors = validate_english(english)
    english["_local_metadata"]["english_normalizer"] = {
        "source_card_had_chinese": True,
        "model_tier": model_tier,
        "retry_used": retry_used,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    card_path.write_text(json.dumps(english, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "index": index,
        "doi": identity(card)["doi"],
        "title": identity(card)["title"],
        "ok": not errors,
        "errors": errors,
        "retry_used": retry_used,
        "card_path": str(card_path),
        "elapsed_seconds": round(time.time() - start, 2),
    }


def run(args: argparse.Namespace) -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    input_path = Path(args.input_jsonl)
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / utc_stamp()
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt = read_text(args.prompt_path)
    if not prompt:
        raise SystemExit(f"Missing prompt: {args.prompt_path}")
    cards = load_cards(input_path)
    selected = cards[int(args.offset) :]
    if int(args.limit) > 0:
        selected = selected[: int(args.limit)]
    print(json.dumps({"event": "start", "input": str(input_path), "selected": len(selected), "output_dir": str(output_dir), "real_llm": bool(args.real_llm)}, ensure_ascii=False), flush=True)
    results: list[dict[str, Any]] = []
    workers = max(1, int(args.workers))
    if workers == 1:
        for i, card in enumerate(selected, int(args.offset) + 1):
            row = normalize_one(
                i,
                card,
                prompt=prompt,
                output_dir=output_dir,
                real_llm=bool(args.real_llm),
                model_tier=args.model_tier,
                max_tokens=int(args.max_tokens),
                retry_once=bool(args.retry_once),
            )
            results.append(row)
            print(json.dumps({"event": "card_done", **row}, ensure_ascii=False), flush=True)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    normalize_one,
                    i,
                    card,
                    prompt=prompt,
                    output_dir=output_dir,
                    real_llm=bool(args.real_llm),
                    model_tier=args.model_tier,
                    max_tokens=int(args.max_tokens),
                    retry_once=bool(args.retry_once),
                ): i
                for i, card in enumerate(selected, int(args.offset) + 1)
            }
            for fut in as_completed(futures):
                try:
                    row = fut.result()
                except Exception as exc:
                    row = {"index": futures[fut], "ok": False, "errors": [f"{type(exc).__name__}:{exc}"]}
                results.append(row)
                print(json.dumps({"event": "card_done", **row}, ensure_ascii=False), flush=True)
    results.sort(key=lambda x: int(x.get("index") or 0))
    jsonl_path = output_dir / "paper_text_cards.english.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in results:
            p = Path(str(row.get("card_path") or ""))
            if p.exists():
                try:
                    f.write(json.dumps(json.loads(p.read_text(encoding="utf-8", errors="replace")), ensure_ascii=False) + "\n")
                except Exception:
                    pass
    summary = {
        "schema_version": "paper_card_english_normalizer_run.v1",
        "input_jsonl": str(input_path),
        "output_dir": str(output_dir),
        "cards_jsonl": str(jsonl_path),
        "processed": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results,
    }
    write_json(output_dir / "run_summary.json", summary)
    print(json.dumps({"event": "summary", "cards_jsonl": str(jsonl_path), "ok": summary["ok"], "failed": summary["failed"]}, ensure_ascii=False), flush=True)
    return 0 if summary["failed"] == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize legacy paper cards into English-only cards.")
    parser.add_argument("--input-jsonl", default=str(DEFAULT_INPUT))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT))
    parser.add_argument("--real-llm", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model-tier", default="advanced_model")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=6000)
    parser.add_argument("--retry-once", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
