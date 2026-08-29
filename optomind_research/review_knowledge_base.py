"""ReviewKnowledgeBase builder and query utilities.

This module turns one upstream literature-resource run into a reusable,
traceable knowledge base for review planning and grounded writing.

Design boundary:
- It does not replace full text, paper cards, text slices, or visual chunks.
- It builds a durable index layer over those assets.
- It keeps raw JSON and local source pointers so every answer can trace back.

The v1 storage format intentionally has two faces:
- JSONL files for audit and handoff.
- SQLite tables + FTS5 indexes for retrieval and downstream agent use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from optomind_research.visual_argument_protocol import derive_visual_argument_fields


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CORE_FULLTEXT_INDEX = (
    PROJECT_ROOT
    / "outputs"
    / "literature_resource_builder"
    / "web_jobs"
    / "20260701-152234-51de78"
    / "artifacts"
    / "core58_fulltext_index.json"
)
DEFAULT_PAPER_CARDS_JSONL = (
    PROJECT_ROOT
    / "outputs"
    / "paper_text_cards_english"
    / "core58-en-v1-20260702"
    / "paper_text_cards.english.jsonl"
)
DEFAULT_TEXT_CHUNKS_JSONL = (
    PROJECT_ROOT
    / "outputs"
    / "structured_slices"
    / "profile_experiment_c2_v3_spread_20260702"
    / "all_slices.jsonl"
)
DEFAULT_VISUAL_ASSETS_JSONL = (
    PROJECT_ROOT
    / "outputs"
    / "visual_asset_pipeline"
    / "core58-v31-20260701"
    / "visual_assets.v1_1.all.jsonl"
)
DEFAULT_VISUAL_CHUNKS_JSONL = (
    PROJECT_ROOT
    / "outputs"
    / "visual_chunks"
    / "core58_visual_chunks_tagged_full_1pct_sanitized_20260703"
    / "visual_chunks.tagged.1pct_sanitized.jsonl"
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "review_knowledge_base" / "core58-rkb-v1-20260703"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", "", text)
    text = text.strip().strip(".;,")
    text = text.lower()
    # Some upstream sources occasionally emit a malformed DOI boundary such as
    # "10.62051//ijmsts.v1n1.01".  DOI identity should use a single slash
    # between the registered prefix and suffix, while preserving any slash that
    # legitimately appears inside the suffix.
    text = re.sub(r"^(10\.\d{4,9})/+", r"\1/", text)
    return text


def canonical_paper_id(*, paper_id: Any = "", doi: Any = "", title: Any = "") -> str:
    doi_norm = normalize_doi(doi)
    if doi_norm:
        return f"doi:{doi_norm}"
    raw = str(paper_id or "").strip()
    if raw:
        if raw.lower().startswith("doi:"):
            return f"doi:{normalize_doi(raw[4:])}"
        return raw
    slug = safe_slug(title, limit=80)
    return f"title:{slug}" if slug else "paper:unknown"


def safe_slug(value: Any, *, limit: int = 120) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return (text or "item")[:limit]


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()


def compact_text(value: Any, *, limit: int = 2000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def json_dump_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def flatten_short_strings(value: Any, *, max_items: int = 20, max_len: int = 120) -> list[str]:
    out: list[str] = []

    def walk(v: Any) -> None:
        if len(out) >= max_items:
            return
        if isinstance(v, str):
            s = compact_text(v, limit=max_len + 1)
            if 2 <= len(s) <= max_len:
                out.append(s)
            return
        if isinstance(v, (int, float)):
            return
        if isinstance(v, dict):
            # Prefer human label-like values; avoid recursively exploding raw JSON too much.
            for key in ["name", "label", "title", "role", "type", "section", "claim", "metric", "material", "method"]:
                if key in v:
                    walk(v.get(key))
            for key in ["items", "values", "terms", "tags"]:
                if key in v:
                    walk(v.get(key))
            return
        if isinstance(v, list):
            for item in v:
                walk(item)

    walk(value)
    seen: set[str] = set()
    deduped = []
    for item in out:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:max_items]


def cjk_present(value: Any) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", json.dumps(value, ensure_ascii=False)))


def contains_cjk_text(value: Any) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", str(value or "")))


def clean_matched_features(features: Any) -> list[dict[str, Any]]:
    """Preserve routing metadata while keeping this KB English-only.

    Older upstream experiments may contain Chinese feature names or audit
    reasons. The original source file remains listed in the manifest, but the
    reusable ReviewKnowledgeBase should not let those notes leak into retrieval
    or downstream English-only agent messages.
    """
    if not isinstance(features, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in features:
        if not isinstance(item, dict):
            continue
        rec: dict[str, Any] = {
            "feature_id": item.get("feature_id", ""),
            "score": item.get("score"),
        }
        feature_name = item.get("feature_name", "")
        reason = item.get("reason", "")
        omitted = False
        if feature_name:
            if contains_cjk_text(feature_name):
                omitted = True
            else:
                rec["feature_name"] = feature_name
        if reason:
            if contains_cjk_text(reason):
                omitted = True
            else:
                rec["reason"] = reason
        if omitted:
            rec["non_english_text_omitted"] = True
        cleaned.append(rec)
    return cleaned


def fts_query(value: str, *, max_terms: int = 24) -> str:
    # FTS5 query syntax is brittle with punctuation. Keep it conservative.
    terms = re.findall(r"[A-Za-z0-9µμ]+", value.lower())
    terms = [t for t in terms if len(t) >= 2]
    seen: set[str] = set()
    out = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            out.append(term)
        if len(out) >= max_terms:
            break
    return " OR ".join(out)


@dataclass
class ReviewKnowledgeBaseInputs:
    core_fulltext_index: Path = DEFAULT_CORE_FULLTEXT_INDEX
    paper_cards_jsonl: Path = DEFAULT_PAPER_CARDS_JSONL
    text_chunks_jsonl: Path = DEFAULT_TEXT_CHUNKS_JSONL
    visual_assets_jsonl: Path = DEFAULT_VISUAL_ASSETS_JSONL
    visual_chunks_jsonl: Path = DEFAULT_VISUAL_CHUNKS_JSONL


@dataclass
class BuildResult:
    output_dir: Path
    sqlite_path: Path
    manifest_path: Path
    audit_path: Path
    counts: dict[str, int]
    warnings: list[str]


class ReviewKnowledgeBaseBuilder:
    """Build a review knowledge base from existing structured assets."""

    def __init__(self, inputs: ReviewKnowledgeBaseInputs, output_dir: Path) -> None:
        self.inputs = inputs
        self.output_dir = output_dir
        self.records_dir = output_dir / "records"
        self.sqlite_path = output_dir / "review_knowledge_base.sqlite"
        self.warnings: list[str] = []
        self._paper_aliases: dict[str, str] = {}
        self._doi_aliases: dict[str, str] = {}
        self._title_aliases: dict[str, str] = {}
        self._identity_merges: list[dict[str, Any]] = []
        self.source_paths = {
            "core_fulltext_index": str(inputs.core_fulltext_index),
            "paper_cards_jsonl": str(inputs.paper_cards_jsonl),
            "text_chunks_jsonl": str(inputs.text_chunks_jsonl),
            "visual_assets_jsonl": str(inputs.visual_assets_jsonl),
            "visual_chunks_jsonl": str(inputs.visual_chunks_jsonl),
        }

    def build(self) -> BuildResult:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)

        loaded = self._load_assets()
        self._prepare_identity_resolver(loaded)
        papers = self._build_papers(loaded)
        text_chunks = self._build_text_chunks(loaded)
        visual_assets = self._build_visual_assets(loaded)
        visual_chunks = self._build_visual_chunks(loaded)
        links = self._build_links(papers, text_chunks, visual_assets, visual_chunks)
        concepts, concept_mentions = self._build_concepts(papers, text_chunks, visual_assets, visual_chunks, loaded)
        audit = self._audit(papers, text_chunks, visual_assets, visual_chunks, links, concepts, concept_mentions)

        paths = {
            "papers": self.records_dir / "papers.jsonl",
            "text_chunks": self.records_dir / "text_chunks.jsonl",
            "visual_assets": self.records_dir / "visual_assets.jsonl",
            "visual_chunks": self.records_dir / "visual_chunks.jsonl",
            "links": self.records_dir / "links.jsonl",
            "concepts": self.records_dir / "concepts.jsonl",
            "concept_mentions": self.records_dir / "concept_mentions.jsonl",
        }
        counts = {
            "papers": write_jsonl(paths["papers"], papers),
            "text_chunks": write_jsonl(paths["text_chunks"], text_chunks),
            "visual_assets": write_jsonl(paths["visual_assets"], visual_assets),
            "visual_chunks": write_jsonl(paths["visual_chunks"], visual_chunks),
            "links": write_jsonl(paths["links"], links),
            "concepts": write_jsonl(paths["concepts"], concepts),
            "concept_mentions": write_jsonl(paths["concept_mentions"], concept_mentions),
        }

        self._write_sqlite(papers, text_chunks, visual_assets, visual_chunks, links, concepts, concept_mentions)
        manifest = {
            "schema_version": "review_knowledge_base_manifest.v1",
            "created_at": utc_now(),
            "design_principle": "Index and link existing assets; do not replace full text, text chunks, visual chunks, or overview memory.",
            "source_paths": self.source_paths,
            "output_paths": {k: str(v) for k, v in paths.items()} | {"sqlite": str(self.sqlite_path)},
            "counts": counts,
            # Backend-fix ticket 2.4 (round 2): the detector computes these
            # under audit["quality_signals"] / audit["counts"].  Round 1 read
            # a nonexistent "quality" key, which reported missing=0 on an
            # all-dangling library -- the exact opposite of the truth.
            "quality_flags": {
                "visual_chunks_missing_image_count": int(
                    audit.get("quality_signals", {}).get(
                        "visual_chunks_missing_image_count", 0
                    )
                ),
                "visual_chunks_with_image_count": max(
                    0,
                    int(audit.get("counts", {}).get("visual_chunks", 0))
                    - int(
                        audit.get("quality_signals", {}).get(
                            "visual_chunks_missing_image_count", 0
                        )
                    ),
                ),
            },
            "warnings": self.warnings,
            "recommended_default_use": {
                "planning": "Use overview memory and paper cards.",
                "writing": "Retrieve text_chunks and visual_chunks through this KB, then bind claims to source records.",
                "evidence": "Use text chunks, captions, nearby text, and original fulltext/image paths, not compressed overview memory alone.",
            },
        }
        manifest_path = self.output_dir / "manifest.json"
        audit_path = self.output_dir / "quality_audit.json"
        write_json(manifest_path, manifest)
        write_json(audit_path, audit)
        self._write_markdown_report(audit, self.output_dir / "quality_report.md")

        return BuildResult(
            output_dir=self.output_dir,
            sqlite_path=self.sqlite_path,
            manifest_path=manifest_path,
            audit_path=audit_path,
            counts=counts,
            warnings=self.warnings,
        )

    def _load_assets(self) -> dict[str, Any]:
        for name, path in self.source_paths.items():
            if not Path(path).exists():
                self.warnings.append(f"missing_input:{name}:{path}")
        core = read_json(self.inputs.core_fulltext_index) if self.inputs.core_fulltext_index.exists() else {}
        return {
            "core": core,
            "paper_cards": read_jsonl(self.inputs.paper_cards_jsonl),
            "text_chunks": read_jsonl(self.inputs.text_chunks_jsonl),
            "visual_assets": read_jsonl(self.inputs.visual_assets_jsonl),
            "visual_chunks": read_jsonl(self.inputs.visual_chunks_jsonl),
        }

    @staticmethod
    def _title_identity_key(value: Any) -> str:
        text = str(value or "").casefold()
        text = text.replace("¡á", " x ").replace("×", " x ").replace("*", " x ")
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    def _prepare_identity_resolver(self, loaded: dict[str, Any]) -> None:
        """Use the accepted core index as the identity authority.

        DOI-less papers often acquire different local/openalex/title IDs in
        downstream assets.  Unique normalized titles may safely resolve those
        aliases; ambiguous titles (for example a preprint plus journal paper)
        are deliberately left unresolved unless DOI or an exact core ID is
        available.
        """
        title_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        core_items = loaded.get("core", {}).get("core_fulltexts") if isinstance(loaded.get("core"), dict) else []
        for item in core_items or []:
            pid = canonical_paper_id(paper_id=item.get("paper_id"), doi=item.get("doi"), title=item.get("title"))
            raw = str(item.get("paper_id") or "").strip()
            doi = normalize_doi(item.get("doi"))
            title_key = self._title_identity_key(item.get("title"))
            if title_key:
                title_groups[title_key].append({"pid": pid, "raw": raw, "doi": doi})
            else:
                if raw:
                    self._paper_aliases[raw.casefold()] = pid
                self._paper_aliases[pid.casefold()] = pid
                if doi:
                    self._doi_aliases[doi] = pid

        for title_key, members in title_groups.items():
            distinct_dois = {m["doi"] for m in members if m["doi"]}
            distinct_ids = {m["pid"] for m in members}
            # An exact normalized-title match is a strong duplicate signal.
            # Prefer the sole DOI identity when a DOI and local/title aliases
            # coexist.  Multiple different DOIs remain separate because they
            # may represent genuinely distinct articles with reused titles.
            canonical = ""
            if len(distinct_dois) == 1:
                canonical = f"doi:{next(iter(distinct_dois))}"
            elif not distinct_dois:
                canonical = sorted(
                    distinct_ids,
                    key=lambda value: (
                        0 if value.startswith("title:") else 1,
                        0 if value.startswith("openalex:") else 1,
                        value,
                    ),
                )[0]

            if canonical:
                self._title_aliases[title_key] = canonical
                for member in members:
                    if member["raw"]:
                        self._paper_aliases[member["raw"].casefold()] = canonical
                    self._paper_aliases[member["pid"].casefold()] = canonical
                    if member["doi"]:
                        self._doi_aliases[member["doi"]] = canonical
                if len(distinct_ids) > 1:
                    self._identity_merges.append(
                        {
                            "normalized_title": title_key,
                            "canonical_paper_id": canonical,
                            "merged_paper_ids": sorted(distinct_ids),
                            "reason": "exact_normalized_title_without_conflicting_dois",
                        }
                    )
            else:
                for member in members:
                    if member["raw"]:
                        self._paper_aliases[member["raw"].casefold()] = member["pid"]
                    self._paper_aliases[member["pid"].casefold()] = member["pid"]
                    if member["doi"]:
                        self._doi_aliases[member["doi"]] = member["pid"]

    def _resolve_paper_id(self, *, paper_id: Any = "", doi: Any = "", title: Any = "") -> str:
        doi_norm = normalize_doi(doi)
        if doi_norm and doi_norm in self._doi_aliases:
            return self._doi_aliases[doi_norm]
        raw = str(paper_id or "").strip().casefold()
        if raw and raw in self._paper_aliases:
            return self._paper_aliases[raw]
        title_key = self._title_identity_key(title)
        if title_key and title_key in self._title_aliases:
            return self._title_aliases[title_key]
        return canonical_paper_id(paper_id=paper_id, doi=doi, title=title)

    def _build_papers(self, loaded: dict[str, Any]) -> list[dict[str, Any]]:
        paper_map: dict[str, dict[str, Any]] = {}

        core_items = loaded.get("core", {}).get("core_fulltexts") if isinstance(loaded.get("core"), dict) else []
        for item in core_items or []:
            pid = self._resolve_paper_id(paper_id=item.get("paper_id"), doi=item.get("doi"), title=item.get("title"))
            paper_map.setdefault(pid, self._empty_paper(pid))
            rec = paper_map[pid]
            rec["doi"] = normalize_doi(item.get("doi")) or rec.get("doi", "")
            rec["title"] = item.get("title") or rec.get("title", "")
            quality_check = item.get("quality_check") if isinstance(item.get("quality_check"), dict) else {}
            current_topic_fit = item.get("current_topic_fit") if isinstance(item.get("current_topic_fit"), dict) else {}
            rec["fulltext"] = {
                "fulltext_type": item.get("fulltext_type", ""),
                "access_method": item.get("access_method", ""),
                "parsed_text_path": item.get("parsed_text_path", ""),
                "chunk_index_path": item.get("chunk_index_path", ""),
                "source_url": item.get("source_url", ""),
                "quality_tier": item.get("quality_tier") or quality_check.get("quality_tier", ""),
                "query_relevance": item.get("query_relevance") or quality_check.get("query_relevance", ""),
                "current_topic_fit": current_topic_fit,
                "topic_relevance_class": current_topic_fit.get("relevance_class", ""),
                "topic_directness_score": current_topic_fit.get("directness_score"),
                "downstream_use_policy": item.get("downstream_use_policy", ""),
                "core_acceptance_basis": item.get("core_acceptance_basis", ""),
            }
            rec["topic_relevance"] = current_topic_fit
            rec["downstream_use_policy"] = item.get("downstream_use_policy", "")
            rec["matched_features"] = clean_matched_features(item.get("matched_features"))
            rec["source_membership"]["core_fulltext_index"] = True
            rec["source_membership"]["core_index_order"] = item.get("index")

        for card in loaded.get("paper_cards", []):
            ident = card.get("paper_identity") if isinstance(card.get("paper_identity"), dict) else {}
            pid = self._resolve_paper_id(paper_id=ident.get("paper_id"), doi=ident.get("doi"), title=ident.get("title"))
            paper_map.setdefault(pid, self._empty_paper(pid))
            rec = paper_map[pid]
            rec["doi"] = normalize_doi(ident.get("doi")) or rec.get("doi", "")
            rec["title"] = ident.get("title") or rec.get("title", "")
            rec["year"] = ident.get("year") or rec.get("year")
            rec["venue"] = ident.get("venue") or rec.get("venue", "")
            rec["paper_card"] = {
                "schema_version": card.get("schema_version", ""),
                "card_type": card.get("card_type", ""),
                "source_status": card.get("source_status", {}),
                "one_sentence_contribution": card.get("one_sentence_contribution", ""),
                "high_density_summary": card.get("high_density_summary", ""),
                "research_problem_and_context": card.get("research_problem_and_context", ""),
                "core_question_or_gap": card.get("core_question_or_gap", ""),
                "method_or_design": card.get("method_or_design", {}),
                "mechanisms": card.get("mechanisms", []),
                "key_results": card.get("key_results", []),
                "important_numbers": card.get("important_numbers", []),
                "comparison_or_benchmark": card.get("comparison_or_benchmark", []),
                "limitations_and_open_questions": card.get("limitations_and_open_questions", []),
                "useful_for_review_sections": card.get("useful_for_review_sections", []),
                "directly_reusable_sentences": card.get("directly_reusable_sentences", []),
                "evidence_map": card.get("evidence_map", []),
                "confidence": card.get("confidence", ""),
                "_local_metadata": card.get("_local_metadata", {}),
            }
            rec["source_membership"]["paper_card"] = True
            rec["raw_card_sha1"] = sha1_text(json_dump_compact(card))

        # Enrich missing metadata from chunks and visuals.
        for chunk in loaded.get("text_chunks", []):
            pid = self._resolve_paper_id(paper_id=chunk.get("paper_id"), doi=chunk.get("doi"), title=chunk.get("title"))
            paper_map.setdefault(pid, self._empty_paper(pid))
            rec = paper_map[pid]
            rec["doi"] = normalize_doi(chunk.get("doi")) or rec.get("doi", "")
            rec["title"] = chunk.get("title") or rec.get("title", "")
            rec["source_membership"]["text_chunks"] = True
        for visual in loaded.get("visual_assets", []):
            paper = visual.get("paper") if isinstance(visual.get("paper"), dict) else {}
            pid = self._resolve_paper_id(paper_id=paper.get("paper_id"), doi=paper.get("doi"), title=paper.get("title"))
            paper_map.setdefault(pid, self._empty_paper(pid))
            rec = paper_map[pid]
            rec["doi"] = normalize_doi(paper.get("doi")) or rec.get("doi", "")
            rec["title"] = paper.get("title") or rec.get("title", "")
            rec["year"] = paper.get("year") or rec.get("year")
            rec["venue"] = paper.get("venue") or rec.get("venue", "")
            rec["source_membership"]["visual_assets"] = True
        for visual in loaded.get("visual_chunks", []):
            pid = self._resolve_paper_id(paper_id=visual.get("paper_id"), doi=visual.get("doi"), title=visual.get("paper_title"))
            paper_map.setdefault(pid, self._empty_paper(pid))
            rec = paper_map[pid]
            rec["doi"] = normalize_doi(visual.get("doi")) or rec.get("doi", "")
            rec["title"] = visual.get("paper_title") or rec.get("title", "")
            rec["year"] = visual.get("year") or rec.get("year")
            rec["venue"] = visual.get("venue") or rec.get("venue", "")
            rec["source_membership"]["visual_chunks"] = True

        out = []
        for pid, rec in sorted(paper_map.items()):
            rec["record_type"] = "paper"
            rec["schema_version"] = "review_kb.paper.v1"
            rec["search_text"] = self._paper_search_text(rec)
            out.append(rec)
        return out

    def _empty_paper(self, paper_id: str) -> dict[str, Any]:
        return {
            "paper_id": paper_id,
            "doi": normalize_doi(paper_id[4:]) if paper_id.startswith("doi:") else "",
            "title": "",
            "year": None,
            "venue": "",
            "fulltext": {},
            "matched_features": [],
            "paper_card": {},
            "source_membership": {},
            "raw_card_sha1": "",
            "topic_relevance": {},
            "downstream_use_policy": "",
        }

    def _paper_search_text(self, paper: dict[str, Any]) -> str:
        card = paper.get("paper_card") if isinstance(paper.get("paper_card"), dict) else {}
        parts = [
            paper.get("title", ""),
            paper.get("venue", ""),
            card.get("one_sentence_contribution", ""),
            card.get("high_density_summary", ""),
            card.get("research_problem_and_context", ""),
            card.get("core_question_or_gap", ""),
            json_dump_compact(card.get("method_or_design", {}))[:3000],
            json_dump_compact(card.get("mechanisms", []))[:3000],
            json_dump_compact(card.get("key_results", []))[:3000],
            json_dump_compact(card.get("limitations_and_open_questions", []))[:3000],
            json_dump_compact(paper.get("matched_features", []))[:3000],
            json_dump_compact(paper.get("topic_relevance", {}))[:1500],
            paper.get("downstream_use_policy", ""),
        ]
        return compact_text(" ".join(str(p) for p in parts if p), limit=24000)

    def _build_text_chunks(self, loaded: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for row in loaded.get("text_chunks", []):
            pid = self._resolve_paper_id(paper_id=row.get("paper_id"), doi=row.get("doi"), title=row.get("title"))
            section_path = row.get("section_path") if isinstance(row.get("section_path"), list) else []
            rec = {
                "schema_version": "review_kb.text_chunk.v1",
                "record_type": "text_chunk",
                "chunk_id": row.get("slice_id") or f"{pid}:text:{row.get('ordinal', len(out))}",
                "paper_id": pid,
                "doi": normalize_doi(row.get("doi")),
                "title": row.get("title", ""),
                "method": row.get("method", ""),
                "ordinal": row.get("ordinal"),
                "char_start": row.get("char_start"),
                "char_end": row.get("char_end"),
                "char_count": row.get("char_count"),
                "word_count_estimate": row.get("word_count_estimate"),
                "text_sha1": row.get("text_sha1") or sha1_text(str(row.get("text", ""))),
                "section_path": section_path,
                "section_path_text": " > ".join(str(x) for x in section_path),
                "starts_with_heading": bool(row.get("starts_with_heading", False)),
                "paragraph_indices": row.get("paragraph_indices") if isinstance(row.get("paragraph_indices"), list) else [],
                "boilerplate_score": row.get("boilerplate_score", 0),
                "base_method": row.get("base_method", ""),
                "hybrid_selected_method": row.get("hybrid_selected_method", ""),
                "text": row.get("text", ""),
                "source_text": row.get("source_text", ""),
                "source_text_sha1": row.get("source_text_sha1", ""),
                "source_language": row.get("source_language", "english"),
                "translation_status": row.get("translation_status", "original_english"),
                "translation_model": row.get("translation_model", ""),
                "translation_validation_errors": row.get("translation_validation_errors") if isinstance(row.get("translation_validation_errors"), list) else [],
                "source_pointer": {
                    "source_file": "structured_slices.all_slices.jsonl",
                    "char_start": row.get("char_start"),
                    "char_end": row.get("char_end"),
                },
                "raw_sha1": sha1_text(json_dump_compact(row)),
            }
            rec["search_text"] = compact_text(" ".join([rec["title"], rec["section_path_text"], rec["text"]]), limit=40000)
            out.append(rec)
        out.sort(key=lambda x: (str(x.get("paper_id")), int(x.get("ordinal") or 0), str(x.get("chunk_id"))))
        return out

    def _build_visual_assets(self, loaded: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for row in loaded.get("visual_assets", []):
            paper = row.get("paper") if isinstance(row.get("paper"), dict) else {}
            ident = row.get("asset_identity") if isinstance(row.get("asset_identity"), dict) else {}
            provenance = row.get("source_provenance") if isinstance(row.get("source_provenance"), dict) else {}
            resources = row.get("local_resources") if isinstance(row.get("local_resources"), dict) else {}
            context = row.get("document_context") if isinstance(row.get("document_context"), dict) else {}
            linkage = row.get("text_linkage") if isinstance(row.get("text_linkage"), dict) else {}
            hints = row.get("domain_hints") if isinstance(row.get("domain_hints"), dict) else {}
            quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
            pid = self._resolve_paper_id(paper_id=paper.get("paper_id"), doi=paper.get("doi"), title=paper.get("title"))
            asset_id = ident.get("asset_id") or f"{pid}:visual_asset:{len(out)}"
            rec = {
                "schema_version": "review_kb.visual_asset.v1",
                "record_type": "visual_asset",
                "asset_id": asset_id,
                "paper_id": pid,
                "doi": normalize_doi(paper.get("doi")),
                "title": paper.get("title", ""),
                "asset_type": ident.get("asset_type", ""),
                "label": ident.get("label", ""),
                "subpanel_labels": ident.get("subpanel_labels") if isinstance(ident.get("subpanel_labels"), list) else [],
                "caption": ident.get("caption_clean") or ident.get("caption_original") or "",
                "caption_confidence": ident.get("caption_confidence", ""),
                "source_provenance": provenance,
                "local_resources": resources,
                "document_context": context,
                "text_linkage": linkage,
                "domain_hints": hints,
                "quality": quality,
                "source_pointer": {
                    "source_file": provenance.get("source_file", ""),
                    "source_url": provenance.get("source_url", ""),
                    "page": provenance.get("page"),
                    "bbox": provenance.get("bbox", []),
                    "local_image_path": resources.get("local_image_path", ""),
                },
                "raw_sha1": sha1_text(json_dump_compact(row)),
            }
            # Persist the stable eight-role protocol inside the canonical
            # record. It is derived from the upstream multimodal profile, not
            # from a text-only guess or a claim-specific acceptance decision.
            rec.update(derive_visual_argument_fields(rec))
            rec["search_text"] = compact_text(
                " ".join(
                    [
                        rec["title"],
                        rec["label"],
                        rec["asset_type"],
                        rec["caption"],
                        context.get("nearby_text", ""),
                        context.get("caption_neighbor_text", ""),
                        json_dump_compact(hints),
                    ]
                ),
                limit=24000,
            )
            out.append(rec)
        out.sort(key=lambda x: (str(x.get("paper_id")), str(x.get("asset_id"))))
        return out

    def _build_visual_chunks(self, loaded: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for row in loaded.get("visual_chunks", []):
            profile = row.get("visual_profile") if isinstance(row.get("visual_profile"), dict) else {}
            intr = profile.get("intrinsic_visual_labels") if isinstance(profile.get("intrinsic_visual_labels"), dict) else {}
            task = profile.get("review_task_labels") if isinstance(profile.get("review_task_labels"), dict) else {}
            pid = self._resolve_paper_id(paper_id=row.get("paper_id"), doi=row.get("doi"), title=row.get("paper_title"))
            rec = {
                "schema_version": "review_kb.visual_chunk.v1",
                "record_type": "visual_chunk",
                "chunk_id": row.get("chunk_id"),
                "chunk_kind": row.get("chunk_kind", ""),
                "paper_id": pid,
                "doi": normalize_doi(row.get("doi")),
                "title": row.get("paper_title", ""),
                "parent_asset_id": row.get("parent_asset_id", ""),
                "parent_label": row.get("parent_label", ""),
                "subfigure_label": row.get("subfigure_label", ""),
                "local_image_path": row.get("local_image_path", ""),
                "parent_image_path": row.get("parent_image_path", ""),
                "overlay_path": row.get("overlay_path", ""),
                "bbox_px": row.get("bbox_px") if isinstance(row.get("bbox_px"), list) else [],
                "bbox_original_px": row.get("bbox_original_px") if isinstance(row.get("bbox_original_px"), list) else [],
                "bbox_padding_ratio": row.get("bbox_padding_ratio"),
                "caption": row.get("caption", ""),
                "subfigure_caption_focus": row.get("subfigure_caption_focus", ""),
                "nearby_text": row.get("nearby_text", ""),
                "caption_neighbor_text": row.get("caption_neighbor_text", ""),
                "body_callout_texts": row.get("body_callout_texts") if isinstance(row.get("body_callout_texts"), list) else [],
                "linked_text_chunk_ids": row.get("linked_text_chunk_ids") if isinstance(row.get("linked_text_chunk_ids"), list) else [],
                "domain_hints": row.get("domain_hints", {}),
                "quality": row.get("quality", {}),
                "needs_human_review": bool(row.get("needs_human_review", False)),
                "human_review_status": row.get("human_review_status", ""),
                "visual_profile": profile,
                "visual_crop_quality": row.get("visual_crop_quality", {}),
                "context_grounding_audit": row.get("context_grounding_audit", {}),
                "visual_role": intr.get("visual_role", ""),
                "visual_content_type": intr.get("visual_content_type", ""),
                "review_utility": task.get("review_utility", ""),
                "task_evidence_density": task.get("task_evidence_density", ""),
                "deferred_context_claim_count": len(profile.get("deferred_context_claims") or []) if isinstance(profile.get("deferred_context_claims"), list) else 0,
                "source_pointer": {
                    "local_image_path": row.get("local_image_path", ""),
                    "parent_image_path": row.get("parent_image_path", ""),
                    "parent_asset_id": row.get("parent_asset_id", ""),
                },
                "raw_sha1": sha1_text(json_dump_compact(row)),
            }
            rec["search_text"] = compact_text(
                " ".join(
                    [
                        rec["title"],
                        rec["chunk_kind"],
                        rec["parent_label"],
                        rec["subfigure_label"],
                        rec["caption"],
                        rec["subfigure_caption_focus"],
                        rec["nearby_text"],
                        " ".join(str(x) for x in rec["body_callout_texts"]),
                        json_dump_compact(intr),
                        json_dump_compact(task),
                    ]
                ),
                limit=30000,
            )
            out.append(rec)
        out.sort(key=lambda x: (str(x.get("paper_id")), str(x.get("chunk_id"))))
        return out

    def _build_links(
        self,
        papers: list[dict[str, Any]],
        text_chunks: list[dict[str, Any]],
        visual_assets: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        paper_ids = {p["paper_id"] for p in papers}
        text_by_id = {c["chunk_id"]: c for c in text_chunks}
        visual_asset_by_id = {v["asset_id"]: v for v in visual_assets}

        def add(source_type: str, source_id: str, target_type: str, target_id: str, relation: str, *, confidence: float = 1.0, evidence: Any = None) -> None:
            if not source_id or not target_id:
                return
            links.append(
                {
                    "schema_version": "review_kb.link.v1",
                    "link_id": f"l:{sha1_text('|'.join([source_type, source_id, target_type, target_id, relation]))[:16]}",
                    "source_type": source_type,
                    "source_id": source_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "relation": relation,
                    "confidence": round(float(confidence), 4),
                    "evidence": evidence or {},
                }
            )

        for paper in papers:
            pid = paper["paper_id"]
            add("paper", pid, "fulltext", pid, "has_fulltext_record", confidence=0.9, evidence=paper.get("fulltext", {}))
            if paper.get("paper_card"):
                add("paper", pid, "paper_card", pid, "has_paper_card", confidence=1.0)
        for chunk in text_chunks:
            add("paper", chunk["paper_id"], "text_chunk", chunk["chunk_id"], "has_text_chunk", confidence=1.0, evidence={"ordinal": chunk.get("ordinal")})
        for asset in visual_assets:
            add("paper", asset["paper_id"], "visual_asset", asset["asset_id"], "has_visual_asset", confidence=1.0, evidence={"label": asset.get("label"), "asset_type": asset.get("asset_type")})
            linkage = asset.get("text_linkage") if isinstance(asset.get("text_linkage"), dict) else {}
            linked_ids = set(linkage.get("linked_chunk_ids") if isinstance(linkage.get("linked_chunk_ids"), list) else [])
            for callout in linkage.get("body_callouts") if isinstance(linkage.get("body_callouts"), list) else []:
                if isinstance(callout, dict) and callout.get("chunk_id"):
                    linked_ids.add(callout.get("chunk_id"))
            for cid in linked_ids:
                if cid in text_by_id:
                    add("visual_asset", asset["asset_id"], "text_chunk", cid, "explained_by_text_chunk", confidence=0.85)
        for chunk in visual_chunks:
            add("paper", chunk["paper_id"], "visual_chunk", chunk["chunk_id"], "has_visual_chunk", confidence=1.0, evidence={"kind": chunk.get("chunk_kind")})
            parent_asset = chunk.get("parent_asset_id")
            if parent_asset in visual_asset_by_id:
                add("visual_chunk", chunk["chunk_id"], "visual_asset", parent_asset, "derived_from_visual_asset", confidence=1.0)
            for cid in chunk.get("linked_text_chunk_ids", []):
                if cid in text_by_id:
                    add("visual_chunk", chunk["chunk_id"], "text_chunk", cid, "explained_by_text_chunk", confidence=0.8)
        # Deduplicate
        dedup: dict[str, dict[str, Any]] = {}
        for link in links:
            dedup[link["link_id"]] = link
        return sorted(dedup.values(), key=lambda x: x["link_id"])

    def _build_concepts(
        self,
        papers: list[dict[str, Any]],
        text_chunks: list[dict[str, Any]],
        visual_assets: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
        loaded: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        concept_map: dict[str, dict[str, Any]] = {}
        mentions: list[dict[str, Any]] = []

        def concept_id(kind: str, label: str) -> str:
            return f"{kind}:{safe_slug(label.lower(), limit=120)}"

        def add(kind: str, label: Any, source_type: str, source_id: str, paper_id: str, relation: str, *, confidence: float = 0.7, evidence: Any = None) -> None:
            label_s = compact_text(label, limit=180)
            if not label_s:
                return
            cid = concept_id(kind, label_s)
            concept_map.setdefault(
                cid,
                {
                    "schema_version": "review_kb.concept.v1",
                    "concept_id": cid,
                    "kind": kind,
                    "label": label_s,
                    "description": "",
                    "source_count": 0,
                },
            )
            concept_map[cid]["source_count"] += 1
            mentions.append(
                {
                    "schema_version": "review_kb.concept_mention.v1",
                    "mention_id": f"cm:{sha1_text('|'.join([cid, source_type, source_id, relation]))[:16]}",
                    "concept_id": cid,
                    "concept_kind": kind,
                    "concept_label": label_s,
                    "source_type": source_type,
                    "source_id": source_id,
                    "paper_id": paper_id,
                    "relation": relation,
                    "confidence": round(float(confidence), 4),
                    "evidence": evidence or {},
                }
            )

        for paper in papers:
            pid = paper["paper_id"]
            for mf in paper.get("matched_features", []):
                if not isinstance(mf, dict):
                    continue
                fid = str(mf.get("feature_id") or "").strip()
                fname = str(mf.get("feature_name") or "").strip()
                label = f"{fid}: {fname}" if fid else fname
                score = mf.get("score", 3)
                try:
                    conf = max(0.1, min(1.0, float(score) / 5.0))
                except Exception:
                    conf = 0.6
                add("retrieval_feature", label, "paper", pid, pid, "matched_feature", confidence=conf, evidence={"score": score, "reason": mf.get("reason", "")})
            card = paper.get("paper_card") if isinstance(paper.get("paper_card"), dict) else {}
            for sec in flatten_short_strings(card.get("useful_for_review_sections"), max_items=20, max_len=120):
                add("review_section", sec, "paper", pid, pid, "useful_for_review_section", confidence=0.75)
            for mech in flatten_short_strings(card.get("mechanisms"), max_items=18, max_len=120):
                add("mechanism", mech, "paper", pid, pid, "paper_card_mechanism", confidence=0.65)
            for method in flatten_short_strings(card.get("method_or_design"), max_items=18, max_len=120):
                add("method_or_design", method, "paper", pid, pid, "paper_card_method", confidence=0.6)
            for lim in flatten_short_strings(card.get("limitations_and_open_questions"), max_items=12, max_len=120):
                add("limitation_or_gap", lim, "paper", pid, pid, "paper_card_limitation", confidence=0.6)

        for chunk in text_chunks:
            pid = chunk["paper_id"]
            for sec in chunk.get("section_path", []):
                add("section_path", sec, "text_chunk", chunk["chunk_id"], pid, "text_chunk_section_path", confidence=0.8)

        for asset in visual_assets:
            pid = asset["paper_id"]
            hints = asset.get("domain_hints") if isinstance(asset.get("domain_hints"), dict) else {}
            if hints.get("optical_asset_role"):
                add("optical_visual_role", hints.get("optical_asset_role"), "visual_asset", asset["asset_id"], pid, "visual_asset_domain_hint", confidence=0.75)
            for key, kind in [
                ("physical_quantities", "physical_quantity"),
                ("wavelength_ranges", "wavelength_range"),
                ("materials_or_stack", "material_or_stack"),
            ]:
                for item in flatten_short_strings(hints.get(key), max_items=12, max_len=100):
                    add(kind, item, "visual_asset", asset["asset_id"], pid, "visual_asset_domain_hint", confidence=0.7)

        for chunk in visual_chunks:
            pid = chunk["paper_id"]
            profile = chunk.get("visual_profile") if isinstance(chunk.get("visual_profile"), dict) else {}
            intr = profile.get("intrinsic_visual_labels") if isinstance(profile.get("intrinsic_visual_labels"), dict) else {}
            task = profile.get("review_task_labels") if isinstance(profile.get("review_task_labels"), dict) else {}
            if intr.get("visual_role"):
                add("visual_role", intr.get("visual_role"), "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_role", confidence=0.8)
            if intr.get("visual_content_type"):
                add("visual_content_type", intr.get("visual_content_type"), "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_content_type", confidence=0.75)
            for item in flatten_short_strings(intr.get("materials_or_structures"), max_items=10, max_len=100):
                add("material_or_structure", item, "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_label", confidence=0.7)
            for item in flatten_short_strings(intr.get("methods_or_instruments"), max_items=10, max_len=100):
                add("method_or_instrument", item, "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_label", confidence=0.7)
            for item in flatten_short_strings(intr.get("metrics_or_axes"), max_items=10, max_len=100):
                add("metric_or_axis", item, "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_label", confidence=0.7)
            for item in flatten_short_strings(intr.get("physical_quantities"), max_items=10, max_len=100):
                add("physical_quantity", item, "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_label", confidence=0.7)
            for item in flatten_short_strings(task.get("likely_review_sections"), max_items=10, max_len=120):
                add("review_section", item, "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_likely_section", confidence=0.65)
            for item in flatten_short_strings(task.get("framework_alignment_ids"), max_items=10, max_len=50):
                add("framework_alignment", item, "visual_chunk", chunk["chunk_id"], pid, "visual_chunk_framework_alignment", confidence=0.65)

        concept_rows = sorted(concept_map.values(), key=lambda x: (x["kind"], x["label"].lower()))
        mention_dedup = {m["mention_id"]: m for m in mentions}
        mention_rows = sorted(mention_dedup.values(), key=lambda x: (x["concept_id"], x["source_type"], x["source_id"]))
        return concept_rows, mention_rows

    def _audit(
        self,
        papers: list[dict[str, Any]],
        text_chunks: list[dict[str, Any]],
        visual_assets: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
        links: list[dict[str, Any]],
        concepts: list[dict[str, Any]],
        concept_mentions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        paper_ids = {p["paper_id"] for p in papers}
        text_papers = {c["paper_id"] for c in text_chunks}
        asset_papers = {v["paper_id"] for v in visual_assets}
        visual_chunk_papers = {v["paper_id"] for v in visual_chunks}
        visual_missing_image = [v["chunk_id"] for v in visual_chunks if v.get("local_image_path") and not Path(str(v.get("local_image_path"))).exists()]
        visual_deferred = [v["chunk_id"] for v in visual_chunks if int(v.get("deferred_context_claim_count") or 0) > 0]
        text_boilerplate_heavy = [c["chunk_id"] for c in text_chunks if int(c.get("boilerplate_score") or 0) >= 4]
        cjk_counts = {
            "papers": sum(1 for p in papers if cjk_present(p)),
            "text_chunks": sum(1 for c in text_chunks if cjk_present({k: v for k, v in c.items() if k not in {"text", "search_text"}})),
            "visual_chunks": sum(1 for v in visual_chunks if cjk_present(v)),
        }
        issues = []
        if len(papers) < 50:
            self.warnings.append(
                "corpus_scale_below_broad_review_target: use claim-level gap retrieval before final writing"
            )
        if len(text_chunks) < len(papers):
            issues.append("text_chunk_count_lower_than_paper_count")
        if visual_missing_image:
            issues.append("visual_chunks_missing_local_images")
        audit = {
            "schema_version": "review_knowledge_base_audit.v1",
            "created_at": utc_now(),
            "passed": not issues,
            "issues": issues,
            "warnings": self.warnings,
            "counts": {
                "papers": len(papers),
                "text_chunks": len(text_chunks),
                "visual_assets": len(visual_assets),
                "visual_chunks": len(visual_chunks),
                "links": len(links),
                "concepts": len(concepts),
                "concept_mentions": len(concept_mentions),
            },
            "coverage": {
                "papers_with_text_chunks": len(text_papers),
                "papers_with_visual_assets": len(asset_papers),
                "papers_with_visual_chunks": len(visual_chunk_papers),
                "papers_missing_text_chunks": sorted(paper_ids - text_papers),
                "papers_missing_visual_assets": sorted(paper_ids - asset_papers),
                "papers_missing_visual_chunks": sorted(paper_ids - visual_chunk_papers),
            },
            "quality_signals": {
                "corpus_scale_tier": (
                    "broad" if len(papers) >= 50 else "moderate" if len(papers) >= 20 else "thin"
                ),
                "broad_review_reference_target_reached": len(papers) >= 50,
                "visual_chunks_missing_image_count": len(visual_missing_image),
                "visual_chunks_missing_image_sample": visual_missing_image[:20],
                "visual_chunks_with_deferred_claims": len(visual_deferred),
                "visual_chunks_with_deferred_claims_sample": visual_deferred[:20],
                "text_chunks_boilerplate_heavy": len(text_boilerplate_heavy),
                "text_chunks_boilerplate_heavy_sample": text_boilerplate_heavy[:20],
                "cjk_counts_excluding_full_text": cjk_counts,
                "identity_merge_count": len(self._identity_merges),
                "identity_merge_sample": self._identity_merges[:20],
            },
            "concept_kind_counts": dict(Counter(c["kind"] for c in concepts)),
            "link_relation_counts": dict(Counter(l["relation"] for l in links)),
            "design_notes": [
                "Overview memory is not merged into this KB because it is a planning summary, not an evidence source.",
                "Full text remains at source paths; this KB indexes text chunks and source pointers.",
                "Visual labels are treated as retrieval metadata; final evidence should use caption, nearby text, and source images.",
            ],
        }
        return audit

    def _write_sqlite(
        self,
        papers: list[dict[str, Any]],
        text_chunks: list[dict[str, Any]],
        visual_assets: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
        links: list[dict[str, Any]],
        concepts: list[dict[str, Any]],
        concept_mentions: list[dict[str, Any]],
    ) -> None:
        if self.sqlite_path.exists():
            self.sqlite_path.unlink()
        conn = sqlite3.connect(str(self.sqlite_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        self._create_tables(conn)
        with conn:
            conn.executemany(
                "INSERT INTO papers(paper_id,doi,title,year,venue,quality_tier,query_relevance,topic_relevance_class,topic_directness_score,downstream_use_policy,search_text,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        p["paper_id"],
                        p.get("doi", ""),
                        p.get("title", ""),
                        p.get("year"),
                        p.get("venue", ""),
                        (p.get("fulltext") or {}).get("quality_tier", ""),
                        (p.get("fulltext") or {}).get("query_relevance", ""),
                        (p.get("topic_relevance") or {}).get("relevance_class", ""),
                        (p.get("topic_relevance") or {}).get("directness_score"),
                        p.get("downstream_use_policy", ""),
                        p.get("search_text", ""),
                        json_dump_compact(p),
                    )
                    for p in papers
                ],
            )
            conn.executemany(
                "INSERT INTO text_chunks(chunk_id,paper_id,doi,title,ordinal,section_path,char_start,char_end,char_count,boilerplate_score,text,search_text,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        c["chunk_id"],
                        c["paper_id"],
                        c.get("doi", ""),
                        c.get("title", ""),
                        c.get("ordinal"),
                        c.get("section_path_text", ""),
                        c.get("char_start"),
                        c.get("char_end"),
                        c.get("char_count"),
                        c.get("boilerplate_score", 0),
                        c.get("text", ""),
                        c.get("search_text", ""),
                        json_dump_compact(c),
                    )
                    for c in text_chunks
                ],
            )
            conn.executemany(
                "INSERT INTO visual_assets(asset_id,paper_id,doi,title,asset_type,label,caption,local_image_path,search_text,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        v["asset_id"],
                        v["paper_id"],
                        v.get("doi", ""),
                        v.get("title", ""),
                        v.get("asset_type", ""),
                        v.get("label", ""),
                        v.get("caption", ""),
                        (v.get("local_resources") or {}).get("local_image_path", ""),
                        v.get("search_text", ""),
                        json_dump_compact(v),
                    )
                    for v in visual_assets
                ],
            )
            conn.executemany(
                "INSERT INTO visual_chunks(chunk_id,paper_id,doi,title,chunk_kind,parent_asset_id,parent_label,subfigure_label,visual_role,review_utility,local_image_path,caption,search_text,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        v["chunk_id"],
                        v["paper_id"],
                        v.get("doi", ""),
                        v.get("title", ""),
                        v.get("chunk_kind", ""),
                        v.get("parent_asset_id", ""),
                        v.get("parent_label", ""),
                        v.get("subfigure_label", ""),
                        v.get("visual_role", ""),
                        v.get("review_utility", ""),
                        v.get("local_image_path", ""),
                        v.get("caption", ""),
                        v.get("search_text", ""),
                        json_dump_compact(v),
                    )
                    for v in visual_chunks
                ],
            )
            conn.executemany(
                "INSERT INTO links(link_id,source_type,source_id,target_type,target_id,relation,confidence,raw_json) VALUES(?,?,?,?,?,?,?,?)",
                [(l["link_id"], l["source_type"], l["source_id"], l["target_type"], l["target_id"], l["relation"], l.get("confidence", 0), json_dump_compact(l)) for l in links],
            )
            conn.executemany(
                "INSERT INTO concepts(concept_id,kind,label,description,source_count,raw_json) VALUES(?,?,?,?,?,?)",
                [(c["concept_id"], c["kind"], c["label"], c.get("description", ""), c.get("source_count", 0), json_dump_compact(c)) for c in concepts],
            )
            conn.executemany(
                "INSERT INTO concept_mentions(mention_id,concept_id,concept_kind,concept_label,source_type,source_id,paper_id,relation,confidence,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        m["mention_id"],
                        m["concept_id"],
                        m["concept_kind"],
                        m["concept_label"],
                        m["source_type"],
                        m["source_id"],
                        m["paper_id"],
                        m["relation"],
                        m.get("confidence", 0),
                        json_dump_compact(m),
                    )
                    for m in concept_mentions
                ],
            )
            self._populate_fts(conn, papers, text_chunks, visual_assets, visual_chunks)
        conn.close()

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE papers(
              paper_id TEXT PRIMARY KEY,
              doi TEXT,
              title TEXT,
              year INTEGER,
              venue TEXT,
              quality_tier TEXT,
              query_relevance TEXT,
              topic_relevance_class TEXT,
              topic_directness_score INTEGER,
              downstream_use_policy TEXT,
              search_text TEXT,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE text_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT NOT NULL,
              doi TEXT,
              title TEXT,
              ordinal INTEGER,
              section_path TEXT,
              char_start INTEGER,
              char_end INTEGER,
              char_count INTEGER,
              boilerplate_score INTEGER,
              text TEXT,
              search_text TEXT,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE visual_assets(
              asset_id TEXT PRIMARY KEY,
              paper_id TEXT NOT NULL,
              doi TEXT,
              title TEXT,
              asset_type TEXT,
              label TEXT,
              caption TEXT,
              local_image_path TEXT,
              search_text TEXT,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE visual_chunks(
              chunk_id TEXT PRIMARY KEY,
              paper_id TEXT NOT NULL,
              doi TEXT,
              title TEXT,
              chunk_kind TEXT,
              parent_asset_id TEXT,
              parent_label TEXT,
              subfigure_label TEXT,
              visual_role TEXT,
              review_utility TEXT,
              local_image_path TEXT,
              caption TEXT,
              search_text TEXT,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE links(
              link_id TEXT PRIMARY KEY,
              source_type TEXT,
              source_id TEXT,
              target_type TEXT,
              target_id TEXT,
              relation TEXT,
              confidence REAL,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE concepts(
              concept_id TEXT PRIMARY KEY,
              kind TEXT,
              label TEXT,
              description TEXT,
              source_count INTEGER,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE concept_mentions(
              mention_id TEXT PRIMARY KEY,
              concept_id TEXT,
              concept_kind TEXT,
              concept_label TEXT,
              source_type TEXT,
              source_id TEXT,
              paper_id TEXT,
              relation TEXT,
              confidence REAL,
              raw_json TEXT NOT NULL
            );
            CREATE INDEX idx_text_chunks_paper ON text_chunks(paper_id);
            CREATE INDEX idx_visual_assets_paper ON visual_assets(paper_id);
            CREATE INDEX idx_visual_chunks_paper ON visual_chunks(paper_id);
            CREATE INDEX idx_links_source ON links(source_type, source_id);
            CREATE INDEX idx_links_target ON links(target_type, target_id);
            CREATE TABLE paper_citations(
              citing_paper_id TEXT NOT NULL,
              cited_paper_id TEXT NOT NULL,
              PRIMARY KEY (citing_paper_id, cited_paper_id),
              FOREIGN KEY (citing_paper_id) REFERENCES papers(paper_id),
              FOREIGN KEY (cited_paper_id) REFERENCES papers(paper_id)
            );
            CREATE INDEX idx_mentions_concept ON concept_mentions(concept_id);
            CREATE INDEX idx_mentions_source ON concept_mentions(source_type, source_id);
            CREATE INDEX idx_mentions_paper ON concept_mentions(paper_id);
            CREATE INDEX idx_citations_citing ON paper_citations(citing_paper_id);
            CREATE INDEX idx_citations_cited ON paper_citations(cited_paper_id);
            """
        )
        try:
            conn.executescript(
                """
                CREATE VIRTUAL TABLE paper_fts USING fts5(paper_id UNINDEXED, title, search_text);
                CREATE VIRTUAL TABLE text_chunk_fts USING fts5(chunk_id UNINDEXED, paper_id UNINDEXED, title, section_path, text);
                CREATE VIRTUAL TABLE visual_asset_fts USING fts5(asset_id UNINDEXED, paper_id UNINDEXED, title, label, caption, search_text);
                CREATE VIRTUAL TABLE visual_chunk_fts USING fts5(chunk_id UNINDEXED, paper_id UNINDEXED, title, visual_role, caption, search_text);
                CREATE VIRTUAL TABLE concept_fts USING fts5(concept_id UNINDEXED, kind, label, description);
                """
            )
        except sqlite3.OperationalError as exc:
            self.warnings.append(f"fts5_unavailable:{exc}")

    def _populate_fts(
        self,
        conn: sqlite3.Connection,
        papers: list[dict[str, Any]],
        text_chunks: list[dict[str, Any]],
        visual_assets: list[dict[str, Any]],
        visual_chunks: list[dict[str, Any]],
    ) -> None:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "paper_fts" not in tables:
            return
        conn.executemany("INSERT INTO paper_fts(paper_id,title,search_text) VALUES(?,?,?)", [(p["paper_id"], p.get("title", ""), p.get("search_text", "")) for p in papers])
        conn.executemany(
            "INSERT INTO text_chunk_fts(chunk_id,paper_id,title,section_path,text) VALUES(?,?,?,?,?)",
            [(c["chunk_id"], c["paper_id"], c.get("title", ""), c.get("section_path_text", ""), c.get("text", "")) for c in text_chunks],
        )
        conn.executemany(
            "INSERT INTO visual_asset_fts(asset_id,paper_id,title,label,caption,search_text) VALUES(?,?,?,?,?,?)",
            [(v["asset_id"], v["paper_id"], v.get("title", ""), v.get("label", ""), v.get("caption", ""), v.get("search_text", "")) for v in visual_assets],
        )
        conn.executemany(
            "INSERT INTO visual_chunk_fts(chunk_id,paper_id,title,visual_role,caption,search_text) VALUES(?,?,?,?,?,?)",
            [(v["chunk_id"], v["paper_id"], v.get("title", ""), v.get("visual_role", ""), v.get("caption", ""), v.get("search_text", "")) for v in visual_chunks],
        )
        # Concepts are inserted after this function call in _write_sqlite's transaction order? No,
        # concepts table is already populated before this call only in Python list form, so insert here.
        # This FTS table is optional for query; populate from the concrete concepts table after commit if needed.
        for row in conn.execute("SELECT concept_id, kind, label, description FROM concepts"):
            conn.execute("INSERT INTO concept_fts(concept_id,kind,label,description) VALUES(?,?,?,?)", row)

    def _write_markdown_report(self, audit: dict[str, Any], path: Path) -> None:
        counts = audit.get("counts", {})
        coverage = audit.get("coverage", {})
        quality = audit.get("quality_signals", {})
        lines = [
            "# ReviewKnowledgeBase v1 质量报告",
            "",
            f"- 生成时间：{audit.get('created_at')}",
            f"- 审计通过：{audit.get('passed')}",
            "",
            "## 核心数量",
            "",
            f"- 论文：{counts.get('papers')}",
            f"- 文本切片：{counts.get('text_chunks')}",
            f"- 图表资源：{counts.get('visual_assets')}",
            f"- 统一图块：{counts.get('visual_chunks')}",
            f"- 连接关系：{counts.get('links')}",
            f"- 概念节点：{counts.get('concepts')}",
            f"- 概念挂载：{counts.get('concept_mentions')}",
            "",
            "## 覆盖情况",
            "",
            f"- 有文本切片的论文：{coverage.get('papers_with_text_chunks')}",
            f"- 有图表资源的论文：{coverage.get('papers_with_visual_assets')}",
            f"- 有统一图块的论文：{coverage.get('papers_with_visual_chunks')}",
            f"- 缺少图表资源的论文数：{len(coverage.get('papers_missing_visual_assets') or [])}",
            f"- 缺少统一图块的论文数：{len(coverage.get('papers_missing_visual_chunks') or [])}",
            "",
            "## 质量信号",
            "",
            f"- 本地图像路径缺失图块：{quality.get('visual_chunks_missing_image_count')}",
            f"- 含 deferred_context_claims 的图块：{quality.get('visual_chunks_with_deferred_claims')}",
            f"- boilerplate 较重文本切片：{quality.get('text_chunks_boilerplate_heavy')}",
            f"- 中间字段中文残留统计：{quality.get('cjk_counts_excluding_full_text')}",
            "",
            "## 设计边界",
            "",
            "- 该知识库只建立索引和连接，不替代原始全文、文本切片、图像文件或总览记忆。",
            "- 综述蓝图规划可使用总览记忆；具体写作和证据绑定必须回到本知识库中的 text chunk / visual chunk / source pointer。",
            "- visual chunk 的 Qwen-VL 标签只作为检索与组织元数据，关键事实仍应以 caption、nearby text、body callout 和原文为准。",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def query_kb(sqlite_path: Path, query: str, *, top_k: int = 8, include_raw: bool = False) -> dict[str, Any]:
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    q = fts_query(query)
    results: dict[str, Any] = {"query": query, "fts_query": q, "papers": [], "text_chunks": [], "visual_chunks": [], "visual_assets": [], "concepts": []}
    if not q:
        return results
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def run(kind: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            return [{"error": str(exc), "kind": kind}]
        out = []
        for row in rows:
            item = dict(row)
            if "raw_json" in item and item["raw_json"]:
                if include_raw:
                    try:
                        raw = json.loads(item["raw_json"])
                        item["raw"] = raw
                    except Exception:
                        pass
                item.pop("raw_json", None)
            out.append(item)
        return out

    if "paper_fts" in tables:
        results["papers"] = run(
            "paper",
            """
            SELECT p.paper_id,p.doi,p.title,p.year,p.venue,p.quality_tier,p.query_relevance,bm25(paper_fts) AS score,p.raw_json
            FROM paper_fts JOIN papers p ON paper_fts.paper_id=p.paper_id
            WHERE paper_fts MATCH ?
            ORDER BY score LIMIT ?
            """,
            (q, top_k),
        )
        results["text_chunks"] = run(
            "text_chunk",
            """
            SELECT c.chunk_id,c.paper_id,c.doi,c.title,c.ordinal,c.section_path,c.char_start,c.char_end,c.char_count,
                   substr(c.text, 1, 700) AS text_preview,
                   bm25(text_chunk_fts) AS score,c.raw_json
            FROM text_chunk_fts JOIN text_chunks c ON text_chunk_fts.chunk_id=c.chunk_id
            WHERE text_chunk_fts MATCH ?
            ORDER BY score LIMIT ?
            """,
            (q, top_k),
        )
        results["visual_chunks"] = run(
            "visual_chunk",
            """
            SELECT v.chunk_id,v.paper_id,v.doi,v.title,v.chunk_kind,v.parent_label,v.subfigure_label,v.visual_role,
                   v.review_utility,v.local_image_path,substr(v.caption, 1, 700) AS caption_preview,
                   bm25(visual_chunk_fts) AS score,v.raw_json
            FROM visual_chunk_fts JOIN visual_chunks v ON visual_chunk_fts.chunk_id=v.chunk_id
            WHERE visual_chunk_fts MATCH ?
            ORDER BY score LIMIT ?
            """,
            (q, top_k),
        )
        results["visual_assets"] = run(
            "visual_asset",
            """
            SELECT v.asset_id,v.paper_id,v.doi,v.title,v.asset_type,v.label,v.local_image_path,
                   substr(v.caption, 1, 700) AS caption_preview,
                   bm25(visual_asset_fts) AS score,v.raw_json
            FROM visual_asset_fts JOIN visual_assets v ON visual_asset_fts.asset_id=v.asset_id
            WHERE visual_asset_fts MATCH ?
            ORDER BY score LIMIT ?
            """,
            (q, top_k),
        )
        if "concept_fts" in tables:
            results["concepts"] = run(
                "concept",
                """
                SELECT c.concept_id,c.kind,c.label,c.source_count,bm25(concept_fts) AS score,c.raw_json
                FROM concept_fts JOIN concepts c ON concept_fts.concept_id=c.concept_id
                WHERE concept_fts MATCH ?
                ORDER BY score LIMIT ?
                """,
                (q, top_k),
            )
    conn.close()
    return results


def inspect_paper(sqlite_path: Path, paper_id_or_doi: str, *, include_raw: bool = False) -> dict[str, Any]:
    pid = canonical_paper_id(paper_id=paper_id_or_doi, doi=paper_id_or_doi)
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    paper = conn.execute("SELECT * FROM papers WHERE paper_id=? OR doi=?", (pid, normalize_doi(paper_id_or_doi))).fetchone()
    if not paper:
        return {"found": False, "paper_id": pid}
    paper_id = paper["paper_id"]
    counts = {
        "text_chunks": conn.execute("SELECT COUNT(*) FROM text_chunks WHERE paper_id=?", (paper_id,)).fetchone()[0],
        "visual_assets": conn.execute("SELECT COUNT(*) FROM visual_assets WHERE paper_id=?", (paper_id,)).fetchone()[0],
        "visual_chunks": conn.execute("SELECT COUNT(*) FROM visual_chunks WHERE paper_id=?", (paper_id,)).fetchone()[0],
        "concept_mentions": conn.execute("SELECT COUNT(*) FROM concept_mentions WHERE paper_id=?", (paper_id,)).fetchone()[0],
    }
    sample_text_chunks = [dict(r) for r in conn.execute("SELECT chunk_id,ordinal,section_path,char_count FROM text_chunks WHERE paper_id=? ORDER BY ordinal LIMIT 8", (paper_id,))]
    sample_visual_chunks = [dict(r) for r in conn.execute("SELECT chunk_id,chunk_kind,parent_label,subfigure_label,visual_role,review_utility,local_image_path FROM visual_chunks WHERE paper_id=? ORDER BY chunk_id LIMIT 8", (paper_id,))]
    concepts = [dict(r) for r in conn.execute("SELECT concept_kind,concept_label,COUNT(*) AS n FROM concept_mentions WHERE paper_id=? GROUP BY concept_kind, concept_label ORDER BY n DESC LIMIT 20", (paper_id,))]
    conn.close()
    paper_dict = dict(paper)
    if include_raw:
        if paper_dict.get("raw_json"):
            try:
                paper_dict["raw"] = json.loads(paper_dict["raw_json"])
            except Exception:
                pass
        paper_dict.pop("raw_json", None)
    else:
        paper_dict.pop("search_text", None)
        paper_dict.pop("raw_json", None)
    return {
        "found": True,
        "paper": paper_dict,
        "counts": counts,
        "sample_text_chunks": sample_text_chunks,
        "sample_visual_chunks": sample_visual_chunks,
        "top_concepts": concepts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and query OptoMind ReviewKnowledgeBase.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build a ReviewKnowledgeBase from structured assets.")
    build.add_argument("--core-fulltext-index", default=str(DEFAULT_CORE_FULLTEXT_INDEX))
    build.add_argument("--paper-cards-jsonl", default=str(DEFAULT_PAPER_CARDS_JSONL))
    build.add_argument("--text-chunks-jsonl", default=str(DEFAULT_TEXT_CHUNKS_JSONL))
    build.add_argument("--visual-assets-jsonl", default=str(DEFAULT_VISUAL_ASSETS_JSONL))
    build.add_argument("--visual-chunks-jsonl", default=str(DEFAULT_VISUAL_CHUNKS_JSONL))
    build.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))

    query = sub.add_parser("query", help="Query an existing ReviewKnowledgeBase.")
    query.add_argument("--kb-dir", default=str(DEFAULT_OUTPUT_DIR))
    query.add_argument("--sqlite-path", default="")
    query.add_argument("--query", required=True)
    query.add_argument("--top-k", type=int, default=8)
    query.add_argument("--output-json", default="")
    query.add_argument("--include-raw", action="store_true", help="Include full raw JSON records in query results.")

    inspect = sub.add_parser("inspect-paper", help="Inspect one paper and its linked assets.")
    inspect.add_argument("--kb-dir", default=str(DEFAULT_OUTPUT_DIR))
    inspect.add_argument("--sqlite-path", default="")
    inspect.add_argument("--paper", required=True)
    inspect.add_argument("--output-json", default="")
    inspect.add_argument("--include-raw", action="store_true", help="Include full paper raw JSON.")

    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build":
        inputs = ReviewKnowledgeBaseInputs(
            core_fulltext_index=Path(args.core_fulltext_index),
            paper_cards_jsonl=Path(args.paper_cards_jsonl),
            text_chunks_jsonl=Path(args.text_chunks_jsonl),
            visual_assets_jsonl=Path(args.visual_assets_jsonl),
            visual_chunks_jsonl=Path(args.visual_chunks_jsonl),
        )
        result = ReviewKnowledgeBaseBuilder(inputs, Path(args.output_dir)).build()
        print(
            json.dumps(
                {
                    "ok": True,
                    "output_dir": str(result.output_dir),
                    "sqlite_path": str(result.sqlite_path),
                    "manifest_path": str(result.manifest_path),
                    "audit_path": str(result.audit_path),
                    "counts": result.counts,
                    "warnings": result.warnings,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "query":
        sqlite_path = Path(args.sqlite_path) if args.sqlite_path else Path(args.kb_dir) / "review_knowledge_base.sqlite"
        result = query_kb(sqlite_path, args.query, top_k=args.top_k, include_raw=args.include_raw)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output_json:
            Path(args.output_json).write_text(text, encoding="utf-8")
        print(text[:12000])
        if len(text) > 12000:
            print(f"\n... truncated, full result chars={len(text)}")
        return 0
    if args.command == "inspect-paper":
        sqlite_path = Path(args.sqlite_path) if args.sqlite_path else Path(args.kb_dir) / "review_knowledge_base.sqlite"
        result = inspect_paper(sqlite_path, args.paper, include_raw=args.include_raw)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output_json:
            Path(args.output_json).write_text(text, encoding="utf-8")
        print(text[:12000])
        if len(text) > 12000:
            print(f"\n... truncated, full result chars={len(text)}")
        return 0
    return 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
