"""Build a structure memory from top review-paper PDFs.

The memory is for blueprint planning only. It should not be used as scientific
evidence for final claims.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.common_io import clip


DEFAULT_REVIEW_EXAMPLE_DIR = PROJECT_ROOT / "review-example"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "review_example_memory" / "review-example-structure-v1-20260703"
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Review Example Structure Synthesizer.txt"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def compact_text(value: Any, limit: int = 300) -> str:
    return clip(re.sub(r"\s+", " ", str(value or "")).strip(), limit)


def safe_json_parse(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text or "", re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except Exception:
                pass
    try:
        from json_repair import repair_json  # type: ignore

        value = repair_json(str(text or ""), return_objects=True)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def cjk_present(value: Any) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", json.dumps(value, ensure_ascii=False)))


def clean_list(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = compact_text(item, 220)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def normalize_memory(parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "review_example_structure_memory.v1",
        "global_patterns": clean_list(parsed.get("global_patterns"), 10),
        "section_archetypes": [
            {
                "archetype": compact_text(x.get("archetype"), 120),
                "purpose": compact_text(x.get("purpose"), 220),
                "typical_position": compact_text(x.get("typical_position"), 80),
                "signals_in_headings": clean_list(x.get("signals_in_headings"), 8),
                "useful_for_blueprint": compact_text(x.get("useful_for_blueprint"), 240),
            }
            for x in (parsed.get("section_archetypes") if isinstance(parsed.get("section_archetypes"), list) else [])[:12]
            if isinstance(x, dict)
        ],
        "narrative_moves": [
            {
                "move": compact_text(x.get("move"), 120),
                "function": compact_text(x.get("function"), 220),
                "when_to_use": compact_text(x.get("when_to_use"), 180),
            }
            for x in (parsed.get("narrative_moves") if isinstance(parsed.get("narrative_moves"), list) else [])[:10]
            if isinstance(x, dict)
        ],
        "figure_table_patterns": [
            {
                "pattern": compact_text(x.get("pattern"), 120),
                "purpose": compact_text(x.get("purpose"), 220),
                "blueprint_implication": compact_text(x.get("blueprint_implication"), 220),
            }
            for x in (parsed.get("figure_table_patterns") if isinstance(parsed.get("figure_table_patterns"), list) else [])[:8]
            if isinstance(x, dict)
        ],
        "outline_templates": [
            {
                "template_name": compact_text(x.get("template_name"), 120),
                "best_for": compact_text(x.get("best_for"), 180),
                "section_sequence": clean_list(x.get("section_sequence"), 12),
            }
            for x in (parsed.get("outline_templates") if isinstance(parsed.get("outline_templates"), list) else [])[:5]
            if isinstance(x, dict)
        ],
        "critic_questions_for_blueprint": clean_list(parsed.get("critic_questions_for_blueprint"), 12),
        "anti_patterns": clean_list(parsed.get("anti_patterns"), 8),
        "memory_usage_rules": clean_list(parsed.get("memory_usage_rules"), 8),
    }


def fallback_memory(records: list[dict[str, Any]]) -> dict[str, Any]:
    heading_counter: Counter[str] = Counter()
    for rec in records:
        for h in rec.get("section_headings", [])[:40]:
            low = str(h.get("text") or "").lower()
            for key in [
                "introduction",
                "background",
                "mechanism",
                "materials",
                "fabrication",
                "applications",
                "challenges",
                "outlook",
                "future",
                "conclusion",
                "perspective",
            ]:
                if key in low:
                    heading_counter[key] += 1
    common = [k for k, _ in heading_counter.most_common(8)]
    return {
        "schema_version": "review_example_structure_memory.v1",
        "global_patterns": [
            "Strong reviews usually move from scope and motivation to mechanisms, routes, applications, bottlenecks, and outlook.",
            "High-quality outlines separate historical context from frontier opportunities.",
            "Figures and tables often serve as roadmap, taxonomy, mechanism explanation, comparison matrix, or future-direction summary.",
        ],
        "section_archetypes": [
            {"archetype": "Scope and motivation", "purpose": "Define the field boundary and why synthesis is needed.", "typical_position": "opening", "signals_in_headings": ["introduction", "background"], "useful_for_blueprint": "Prevents a topic list from replacing a review argument."},
            {"archetype": "Mechanism or principle", "purpose": "Explain the physics or conceptual basis.", "typical_position": "early", "signals_in_headings": ["mechanism", "principle", "fundamentals"], "useful_for_blueprint": "Gives later evidence a causal scaffold."},
            {"archetype": "Material or route taxonomy", "purpose": "Compare technical routes without listing papers one by one.", "typical_position": "middle", "signals_in_headings": ["materials", "platforms", "architectures"], "useful_for_blueprint": "Supports matrix-style synthesis."},
            {"archetype": "Applications and benchmarks", "purpose": "Map methods to use cases and comparison metrics.", "typical_position": "middle-late", "signals_in_headings": ["applications", "performance", "benchmark"], "useful_for_blueprint": "Connects literature to real design constraints."},
            {"archetype": "Challenges and outlook", "purpose": "Convert limitations into future research directions.", "typical_position": "late", "signals_in_headings": ["challenges", "outlook", "future"], "useful_for_blueprint": "Makes the review generative rather than merely descriptive."},
        ],
        "narrative_moves": [
            {"move": "From broad need to constrained design space", "function": "Turn a vague topic into a reviewable problem.", "when_to_use": "Opening section."},
            {"move": "From mechanism to route comparison", "function": "Link physical principles to material choices.", "when_to_use": "After fundamentals."},
            {"move": "From benchmark to bottleneck", "function": "Avoid overclaiming by comparing conditions and limits.", "when_to_use": "Late evidence sections."},
        ],
        "figure_table_patterns": [
            {"pattern": "Roadmap figure", "purpose": "Show field evolution and major branches.", "blueprint_implication": "Reserve one early review-level figure."},
            {"pattern": "Taxonomy or route matrix", "purpose": "Compare platforms and mechanisms.", "blueprint_implication": "Use visual chunks and text evidence to build a route map."},
            {"pattern": "Benchmark matrix", "purpose": "Clarify comparable and non-comparable metrics.", "blueprint_implication": "Pair performance plots with condition notes."},
        ],
        "outline_templates": [
            {"template_name": "Foundation-route-benchmark-outlook", "best_for": "Technology reviews with many material routes.", "section_sequence": ["Scope", "Fundamentals", "Route taxonomy", "Applications", "Benchmarks and bottlenecks", "Outlook"]},
            {"template_name": "Historical-development-frontier", "best_for": "Mature fields with clear milestones.", "section_sequence": ["Historical foundation", "Mechanism development", "Material platforms", "Frontier directions", "Open challenges"]},
        ],
        "critic_questions_for_blueprint": [
            "Does the outline make an argument rather than list topics?",
            "Does each section have a distinct role?",
            "Are figures planned as evidence-bearing objects?",
            "Are limitations connected to future directions?",
            "Does the blueprint avoid mixing incomparable metrics?",
        ],
        "anti_patterns": [
            "Paper-by-paper section order.",
            "A generic outlook that does not follow from bottlenecks.",
            "Figures added after writing rather than planned as argument carriers.",
        ],
        "memory_usage_rules": [
            "Use this memory only for structure and planning.",
            "Do not cite review-example papers as evidence unless separately selected as sources.",
            "Prefer adapting patterns to the user question rather than copying any outline.",
            f"Detected frequent heading signals: {', '.join(common)}",
        ],
    }


def figure_table_counts(text: str) -> dict[str, int]:
    return {
        "figure_mentions": len(re.findall(r"\b(?:fig\.?|figure)\s*\d+", text, re.I)),
        "table_mentions": len(re.findall(r"\btable\s*\d+", text, re.I)),
        "box_mentions": len(re.findall(r"\bbox\s*\d+", text, re.I)),
    }


def likely_heading(text: str) -> bool:
    text = compact_text(text, 180)
    if not text or len(text) < 4 or len(text) > 140:
        return False
    low = text.lower()
    reject = ["copyright", "all rights reserved", "downloaded", "doi:", "http", "www.", "received", "accepted", "references"]
    if any(x in low for x in reject):
        return False
    if re.match(r"^\d+(\.\d+)*\s+[A-Za-z][A-Za-z ,:/()-]{3,}$", text):
        return True
    keywords = [
        "introduction",
        "background",
        "fundamentals",
        "principles",
        "mechanism",
        "materials",
        "fabrication",
        "applications",
        "challenges",
        "outlook",
        "future",
        "conclusions",
        "perspective",
        "roadmap",
        "opportunities",
        "summary",
    ]
    if any(k in low for k in keywords) and len(text.split()) <= 12:
        return True
    return False


def extract_review_pdf(path: Path, max_pages: int = 80) -> dict[str, Any]:
    import fitz  # PyMuPDF

    doc = fitz.open(str(path))
    page_count = doc.page_count
    toc = doc.get_toc(simple=True)
    toc_rows = [{"level": int(x[0]), "title": compact_text(x[1], 180), "page": int(x[2])} for x in toc[:80]]
    pages_to_read = min(page_count, max_pages)
    first_page_text = doc[0].get_text("text") if page_count else ""
    title_candidate = ""
    for line in first_page_text.splitlines():
        line = compact_text(line, 180)
        if 8 <= len(line) <= 180 and not re.search(r"\b(journal|volume|copyright|doi)\b", line, re.I):
            title_candidate = line
            break

    all_text_parts: list[str] = []
    spans: list[tuple[str, float, int, bool]] = []
    for page_index in range(pages_to_read):
        page = doc[page_index]
        text = page.get_text("text")
        all_text_parts.append(text[:20000])
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_text = " ".join(span.get("text", "") for span in line.get("spans", []))
                line_text = compact_text(line_text, 180)
                if not line_text:
                    continue
                sizes = [float(span.get("size", 0)) for span in line.get("spans", []) if span.get("text")]
                fonts = " ".join(str(span.get("font", "")) for span in line.get("spans", []))
                size = max(sizes) if sizes else 0.0
                spans.append((line_text, size, page_index + 1, "bold" in fonts.lower()))
    text_joined = "\n".join(all_text_parts)
    sizes = [s[1] for s in spans if s[1] > 0]
    threshold = 12.0
    if sizes:
        try:
            threshold = max(12.0, statistics.quantiles(sizes, n=10)[-1])
        except Exception:
            threshold = max(12.0, sorted(sizes)[int(len(sizes) * 0.85)])
    headings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text, size, page, is_bold in spans:
        key = text.lower()
        if key in seen:
            continue
        if size >= threshold or is_bold or likely_heading(text):
            if likely_heading(text) or size >= threshold + 1 or is_bold:
                headings.append({"text": text, "page": page, "font_size": round(size, 2), "bold": is_bold})
                seen.add(key)
        if len(headings) >= 120:
            break
    counts = figure_table_counts(text_joined)
    doc.close()
    return {
        "schema_version": "review_example_record.v1",
        "file_name": path.name,
        "path": str(path),
        "file_size": path.stat().st_size,
        "page_count": page_count,
        "title_candidate": title_candidate,
        "toc": toc_rows,
        "section_headings": headings[:100],
        "figure_table_signals": counts,
        "abstract_or_opening_excerpt": compact_text(first_page_text, 1200),
        "parse_status": "ok",
    }


_INTRO_KEYWORDS = ["introduction", "background", "overview", "motivation"]
_CONCL_KEYWORDS = ["conclusion", "summary", "outlook", "perspective", "future", "challenges and future"]

DEFAULT_MOVES_PROMPT = PROJECT_ROOT / "prompts" / "Review Example Intellectual Moves Extractor.txt"

_MOVES_FALLBACK: dict[str, list] = {
    "problem_reframing": [],
    "central_thesis": [],
    "taxonomy_design": [],
    "synthesis_moves": [],
    "section_progression": [],
    "paragraph_moves": [],
    "evidence_critique": [],
    "disagreement_handling": [],
    "gap_characterization": [],
    "figure_argument": [],
    "top_journal_publishability": [],
}


def _extract_section_text(all_text: str, keywords: list[str], max_chars: int = 2800) -> str:
    """Find the first section whose heading matches any keyword and return its text."""
    lines = all_text.splitlines()
    start_idx = -1
    for i, line in enumerate(lines):
        low = line.strip().lower()
        if any(kw in low for kw in keywords) and len(low) < 80:
            start_idx = i
            break
    if start_idx == -1:
        return ""
    # Collect text until the next likely heading or max_chars
    collected: list[str] = []
    chars = 0
    for line in lines[start_idx + 1 :]:
        if chars >= max_chars:
            break
        if len(line.strip()) < 80 and any(
            kw in line.strip().lower()
            for kw in _INTRO_KEYWORDS + _CONCL_KEYWORDS + ["method", "result", "reference", "acknowledg"]
        ):
            if collected:  # non-empty → we hit the next section
                break
        collected.append(line)
        chars += len(line) + 1
    return "\n".join(collected)[:max_chars].strip()


def _normalize_moves(parsed: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key in _MOVES_FALLBACK:
        raw = parsed.get(key)
        if isinstance(raw, list):
            out[key] = [compact_text(x, 400) for x in raw if str(x).strip()][:3]
        else:
            out[key] = []
    return out


class IntellectualMovesExtractor:
    """Per-paper extractor: intro+conclusion -> intellectual move categories."""

    def __init__(
        self,
        prompt_path: Path = DEFAULT_MOVES_PROMPT,
        model_tier: str = "standard_model",
        real_llm: bool = True,
    ) -> None:
        self.prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
        self.model_tier = model_tier
        self.real_llm = real_llm

    def extract_one(self, record: dict[str, Any], all_text: str = "") -> dict[str, Any]:
        """Return intellectual_moves dict for one review paper."""
        intro = _extract_section_text(all_text, _INTRO_KEYWORDS) or compact_text(
            record.get("abstract_or_opening_excerpt", ""), 1200
        )
        conclusion = _extract_section_text(all_text, _CONCL_KEYWORDS)
        if not intro and not conclusion:
            return {**_MOVES_FALLBACK, "_extract_status": "no_text"}

        user_content = (
            f"INTRODUCTION EXCERPT:\n{intro}\n\nCONCLUSION/OUTLOOK EXCERPT:\n{conclusion}"
            if conclusion
            else f"INTRODUCTION EXCERPT:\n{intro}"
        )

        if not self.real_llm:
            return {**_MOVES_FALLBACK, "_extract_status": "mock"}

        try:
            result = call_qwen_chat(
                "IntellectualMovesExtractorAgent",
                [
                    {"role": "system", "content": self.prompt},
                    {"role": "user", "content": user_content},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=4000,
                response_format={"type": "json_object"},
                max_retries=1,
            )
            raw = str(result.get("content") or "")
            parsed = safe_json_parse(raw)
            moves = _normalize_moves(parsed) if parsed else dict(_MOVES_FALLBACK)
            non_empty = sum(1 for v in moves.values() if v)
            moves["_extract_status"] = "ok" if non_empty >= 5 else "low_yield"
            moves["_llm_usage"] = result.get("_llm_usage", {})
            return moves
        except Exception as exc:
            return {**_MOVES_FALLBACK, "_extract_status": "error", "_error": str(exc)}


def _aggregate_moves_library(moves_records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Flatten per-paper moves into a deduplicated library."""
    library: dict[str, list[str]] = {k: [] for k in _MOVES_FALLBACK}
    for rec in moves_records:
        for key in _MOVES_FALLBACK:
            seen: set[str] = {x.strip().lower() for x in library[key]}
            for item in rec.get(key, []):
                norm = item.strip().lower()
                if norm and norm not in seen:
                    seen.add(norm)
                    library[key].append(item)
    # Cap each category to 20 items
    return {k: v[:20] for k, v in library.items()}


@dataclass
class ReviewExampleMemoryInputs:
    review_example_dir: Path = DEFAULT_REVIEW_EXAMPLE_DIR
    prompt_path: Path = DEFAULT_PROMPT
    moves_prompt_path: Path = DEFAULT_MOVES_PROMPT
    model_tier: str = "premium_model"
    moves_model_tier: str = "advanced_model"
    max_examples_for_llm: int = 80
    max_pages_per_pdf: int = 80
    real_llm: bool = True
    skip_intellectual_moves: bool = False
    max_papers: int = 0
    moves_workers: int = 1
    resume: bool = False


class ReviewExampleMemoryBuilder:
    def __init__(self, inputs: ReviewExampleMemoryInputs, output_dir: Path) -> None:
        self.inputs = inputs
        self.output_dir = output_dir
        self.records_dir = output_dir / "records"

    def build(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        pdfs = sorted(self.inputs.review_example_dir.glob("*.pdf"))
        if self.inputs.max_papers and self.inputs.max_papers > 0:
            pdfs = pdfs[: self.inputs.max_papers]
        records: list[dict[str, Any]] = []
        raw_texts: dict[str, str] = {}  # file_name → full text for moves extraction
        errors: list[dict[str, Any]] = []
        for pdf in pdfs:
            try:
                rec = extract_review_pdf(pdf, max_pages=self.inputs.max_pages_per_pdf)
                records.append(rec)
                # Store full text for intellectual moves extraction
                try:
                    import fitz
                    doc = fitz.open(str(pdf))
                    pages_to_read = min(doc.page_count, self.inputs.max_pages_per_pdf)
                    raw_texts[rec["file_name"]] = "\n".join(
                        doc[i].get_text("text")[:8000] for i in range(pages_to_read)
                    )
                    doc.close()
                except Exception:
                    raw_texts[rec["file_name"]] = rec.get("abstract_or_opening_excerpt", "")
            except Exception as exc:
                errors.append({"file_name": pdf.name, "error": type(exc).__name__, "message": str(exc)})
        records_path = self.records_dir / "review_examples.jsonl"
        write_jsonl(records_path, records)
        outline_text = self._outline_text(records[: self.inputs.max_examples_for_llm])
        (self.output_dir / "outline_compact_input.txt").write_text(outline_text, encoding="utf-8", newline="\n")
        memory = self._synthesize_memory(records, outline_text)

        # --- Intellectual moves extraction (M1) ---
        moves_library = dict(_MOVES_FALLBACK)
        moves_audit: dict[str, Any] = {"skipped": True}
        if not self.inputs.skip_intellectual_moves:
            existing_moves: list[dict[str, Any]] = []
            if self.inputs.resume:
                existing_path = self.output_dir / "intellectual_moves_per_paper.jsonl"
                if existing_path.exists():
                    with existing_path.open(encoding="utf-8") as _f:
                        existing_moves = [json.loads(l) for l in _f if l.strip()]
            moves_records, moves_audit = self._extract_intellectual_moves(
                records, raw_texts, existing_moves=existing_moves
            )
            moves_library = _aggregate_moves_library(moves_records)
            write_jsonl(
                self.output_dir / "intellectual_moves_per_paper.jsonl",
                moves_records,
            )
            write_json(self.output_dir / "intellectual_moves_library.json", moves_library)
        memory["intellectual_moves"] = moves_library
        memory["intellectual_moves_audit"] = moves_audit

        memory.update(
            {
                "source_review_example_dir": str(self.inputs.review_example_dir),
                "record_count": len(records),
                "error_count": len(errors),
                "errors": errors[:20],
                "records_jsonl": str(records_path),
                "created_at": utc_now(),
            }
        )
        write_json(self.output_dir / "review_example_structure_memory.json", memory)
        self._write_markdown(memory, self.output_dir / "review_example_structure_memory.md")
        audit = {
            "schema_version": "review_example_memory_audit.v1",
            "pdf_count": len(pdfs),
            "record_count": len(records),
            "error_count": len(errors),
            "cjk_in_memory": cjk_present(memory),
            "toc_available_count": sum(1 for r in records if r.get("toc")),
            "heading_available_count": sum(1 for r in records if r.get("section_headings")),
            "avg_pages": round(sum(int(r.get("page_count") or 0) for r in records) / len(records), 2) if records else 0,
        }
        write_json(self.output_dir / "quality_audit.json", audit)
        return {"memory": memory, "audit": audit, "records_path": str(records_path), "output_dir": str(self.output_dir)}

    def _extract_intellectual_moves(
        self,
        records: list[dict[str, Any]],
        raw_texts: dict[str, str],
        existing_moves: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        extractor = IntellectualMovesExtractor(
            prompt_path=self.inputs.moves_prompt_path,
            model_tier=self.inputs.moves_model_tier,
            real_llm=self.inputs.real_llm,
        )
        existing_moves = existing_moves or []
        already_done: dict[str, dict[str, Any]] = {
            m.get("file_name", m.get("_file_name", "")): m
            for m in existing_moves
            if m.get("file_name") or m.get("_file_name")
        }
        pending = [r for r in records if r.get("file_name", "") not in already_done]
        if already_done:
            print(f"[resume] Skipping {len(already_done)} already-done papers; processing {len(pending)} new.")
        moves_records: list[dict[str, Any]] = list(existing_moves)
        ok_count = low_yield_count = error_count = 0
        workers = max(1, int(self.inputs.moves_workers or 1))

        def one(rec: dict[str, Any]) -> dict[str, Any]:
            full_text = raw_texts.get(rec.get("file_name", ""), "")
            moves = extractor.extract_one(rec, all_text=full_text)
            moves["_file_name"] = rec.get("file_name", "")
            moves["_non_empty_category_count"] = sum(1 for k in _MOVES_FALLBACK if moves.get(k))
            return moves

        if workers == 1 or len(pending) <= 1:
            for rec in pending:
                moves_records.append(one(rec))
        else:
            ordered: list[dict[str, Any] | None] = [None] * len(pending)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(one, rec): idx for idx, rec in enumerate(pending)}
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        ordered[idx] = future.result()
                    except Exception as exc:
                        ordered[idx] = {
                            **_MOVES_FALLBACK,
                            "_file_name": pending[idx].get("file_name", ""),
                            "_extract_status": "error",
                            "_error": f"{type(exc).__name__}: {exc}",
                            "_non_empty_category_count": 0,
                        }
            moves_records.extend(x for x in ordered if isinstance(x, dict))

        for moves in moves_records:
            status = moves.get("_extract_status", "")
            if status == "ok":
                ok_count += 1
            elif status == "low_yield":
                low_yield_count += 1
            else:
                error_count += 1
        audit: dict[str, Any] = {
            "total": len(moves_records),
            "ok": ok_count,
            "low_yield": low_yield_count,
            "error_or_mock": error_count,
            "workers": workers,
            "avg_non_empty_categories": round(
                sum(int(x.get("_non_empty_category_count") or 0) for x in moves_records) / len(moves_records),
                2,
            )
            if moves_records
            else 0,
            "low_yield_or_error_files": [
                x.get("_file_name", "")
                for x in moves_records
                if x.get("_extract_status") != "ok"
            ][:20],
        }
        return moves_records, audit

    def _outline_text(self, records: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        for idx, rec in enumerate(records, 1):
            toc_titles = [f"{x.get('level')}. {x.get('title')}" for x in rec.get("toc", [])[:18]]
            heading_titles = [str(x.get("text") or "") for x in rec.get("section_headings", [])[:18]]
            chosen = toc_titles if toc_titles else heading_titles
            chunks.append(
                "\n".join(
                    [
                        f"Example {idx}: {rec.get('file_name')}",
                        f"Title candidate: {rec.get('title_candidate')}",
                        f"Pages: {rec.get('page_count')}; Figure/table signals: {rec.get('figure_table_signals')}",
                        "Outline/headings:",
                        *[f"- {x}" for x in chosen[:20]],
                    ]
                )
            )
        return "\n\n".join(chunks)[:90000]

    def _synthesize_memory(self, records: list[dict[str, Any]], outline_text: str) -> dict[str, Any]:
        if not self.inputs.real_llm:
            return fallback_memory(records)
        prompt = self.inputs.prompt_path.read_text(encoding="utf-8")
        try:
            result = call_qwen_chat(
                "ReviewExampleStructureSynthesizerAgent",
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": outline_text},
                ],
                model_tier=self.inputs.model_tier,
                temperature=0,
                max_tokens=5000,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            raw = str(result.get("content") or "")
            (self.output_dir / "raw_structure_synthesis.txt").write_text(raw, encoding="utf-8")
            parsed = safe_json_parse(raw)
            memory = normalize_memory(parsed) if parsed else fallback_memory(records)
            memory["_llm_usage"] = result.get("_llm_usage", {})
            memory["_synthesis_mode"] = "llm" if parsed else "fallback_after_parse_failure"
            return memory
        except Exception as exc:
            memory = fallback_memory(records)
            memory["_synthesis_mode"] = "fallback_after_exception"
            memory["_synthesis_error"] = {"error": type(exc).__name__, "message": str(exc)}
            return memory

    def _write_markdown(self, memory: dict[str, Any], path: Path) -> None:
        lines = [
            "# Review Example Structure Memory",
            "",
            f"- Records: {memory.get('record_count')}",
            f"- Source: `{memory.get('source_review_example_dir')}`",
            f"- Synthesis mode: `{memory.get('_synthesis_mode', '')}`",
            "",
            "## Global patterns",
            "",
        ]
        for item in memory.get("global_patterns", []):
            lines.append(f"- {item}")
        lines.extend(["", "## Section archetypes", ""])
        for item in memory.get("section_archetypes", []):
            lines.append(f"- `{item.get('archetype')}`: {item.get('purpose')}")
        lines.extend(["", "## Blueprint critic questions", ""])
        for item in memory.get("critic_questions_for_blueprint", []):
            lines.append(f"- {item}")
        # Intellectual moves library
        moves = memory.get("intellectual_moves", {})
        if any(moves.get(k) for k in _MOVES_FALLBACK):
            lines.extend(["", "## Intellectual moves library", ""])
            labels = {
                "problem_reframing": "Problem reframing",
                "central_thesis": "Central thesis",
                "taxonomy_design": "Taxonomy design",
                "synthesis_moves": "Cross-field synthesis",
                "section_progression": "Section progression",
                "paragraph_moves": "Paragraph moves",
                "evidence_critique": "Evidence critique",
                "disagreement_handling": "Disagreement handling",
                "gap_characterization": "Gap characterization",
                "figure_argument": "Figure / table argument",
                "top_journal_publishability": "Top-journal publishability",
            }
            for key, label in labels.items():
                items = moves.get(key, [])
                if items:
                    lines.append(f"### {label}")
                    for item in items:
                        lines.append(f"- {item}")
                    lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build review-example structure memory from PDF review papers.")
    parser.add_argument("--review-example-dir", default=str(DEFAULT_REVIEW_EXAMPLE_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT))
    parser.add_argument("--moves-prompt-path", default=str(DEFAULT_MOVES_PROMPT))
    parser.add_argument("--model-tier", default="premium_model")
    parser.add_argument("--moves-model-tier", default="standard_model")
    parser.add_argument("--max-examples-for-llm", type=int, default=80)
    parser.add_argument("--max-pages-per-pdf", type=int, default=80)
    parser.add_argument("--real-llm", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-intellectual-moves", action="store_true", default=False,
                        help="Skip intellectual moves extraction (faster, for structure-only runs)")
    parser.add_argument("--max-papers", type=int, default=0,
                        help="Limit number of review PDFs for smoke tests; 0 means all PDFs")
    parser.add_argument("--moves-workers", type=int, default=1,
                        help="Parallel workers for intellectual-moves extraction")
    parser.add_argument("--resume", action="store_true", default=False,
                        help="Skip papers already in output intellectual_moves_per_paper.jsonl")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = ReviewExampleMemoryInputs(
        review_example_dir=Path(args.review_example_dir),
        prompt_path=Path(args.prompt_path),
        moves_prompt_path=Path(args.moves_prompt_path),
        model_tier=args.model_tier,
        moves_model_tier=args.moves_model_tier,
        max_examples_for_llm=int(args.max_examples_for_llm),
        max_pages_per_pdf=int(args.max_pages_per_pdf),
        real_llm=bool(args.real_llm),
        skip_intellectual_moves=bool(args.skip_intellectual_moves),
        max_papers=int(args.max_papers),
        moves_workers=int(args.moves_workers),
        resume=bool(args.resume),
    )
    result = ReviewExampleMemoryBuilder(inputs, Path(args.output_dir)).build()
    print(json.dumps(result["audit"] | {"output_dir": result["output_dir"], "records_path": result["records_path"]}, ensure_ascii=False, indent=2))
    return 0 if not result["audit"].get("error_count") else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
