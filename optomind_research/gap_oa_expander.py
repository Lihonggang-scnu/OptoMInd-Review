"""M3-real — OA-only evidence expansion for weak review claims.

This module is intentionally narrower than the full literature-resource
builder.  It starts from one review-section claim, searches public scholarly
metadata sources, keeps only open-access candidates, and writes a traceable
"gap evidence package" that can later be handed to the full OA acquisition /
canonicalization pipeline.

Boundary:
- Uses Semantic Scholar + OpenAlex only.
- Does not use institutional browser access.
- Does not treat retrieved metadata as final evidence.
- Preserves provenance: every candidate is linked back to section_id,
  claim_id, query_id, and backend.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from optomind_research.metadata_index import (
    FulltextCandidate,
    MetadataIndex,
    infer_format,
    normalize_doi,
    normalize_space,
    oa_status,
    route_for_candidate,
)
from tools.academic_backends.openalex_backend import OpenAlexBackend
from tools.academic_backends.openalex_content import fetch_openalex_content, is_openalex_content_url
from tools.academic_backends.semantic_scholar_backend import SemanticScholarBackend
from tools.academic_backends.unpaywall_backend import UnpaywallBackend


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "gap_oa_expansion"
DEFAULT_RERANK_PROMPT = PROJECT_ROOT / "prompts" / "M3 OA Candidate Reranker.txt"
DEFAULT_QUERY_PROMPT = PROJECT_ROOT / "prompts" / "M3 OA Query Planner.txt"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_slug(value: str, *, limit: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return (text or "gap")[:limit]


def local_paper_id(*, doi: str = "", title: str = "") -> str:
    doi_norm = normalize_doi(doi)
    if doi_norm:
        return f"doi:{doi_norm}"
    return f"title:{safe_slug(title, limit=90).lower()}"


def tokenize(text: Any) -> set[str]:
    stop = {
        "this", "that", "with", "from", "into", "such", "their", "which",
        "review", "evidence", "study", "paper", "claim", "section", "using",
        "based", "between", "within", "across", "requires", "enables",
    }
    return {
        w
        for w in re.split(r"[^A-Za-z0-9]+", str(text or "").lower())
        if len(w) >= 4 and w not in stop and not re.fullmatch(r"\d+", w)
    }


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def compact(value: Any, limit: int = 500) -> str:
    return normalize_space(value)[:limit]


def normalize_scientific_text(value: Any) -> str:
    text = str(value or "")
    # Keep this map in UTF-8 source, but avoid relying on terminal rendering.
    # It helps phrase matching across TiO₂/Al₂O₃/μm variants.
    table = str.maketrans({
        "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
        "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
        "μ": "u", "µ": "u", "‐": "-", "‑": "-", "–": "-", "—": "-", "−": "-",
    })
    return text.translate(table)


def phrase_present(text: str, phrase: str) -> bool:
    t = normalize_scientific_text(text).lower()
    p = normalize_scientific_text(phrase).lower()
    return p in t


def is_supplementary_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return bool(
        re.search(r"\.s\d{3,}(?:$|[?#])", lowered)
        or "supporting-information" in lowered
        or "supplement" in lowered
        or "suppinfo" in lowered
    )


@dataclass
class GapOAQuery:
    query_id: str
    query: str
    purpose: str = ""


@dataclass
class GapOACandidate:
    candidate_id: str
    title: str
    doi: str = ""
    year: int | None = None
    venue: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    source_url: str = ""
    pdf_url: str = ""
    open_access_url: str = ""
    alternate_urls: list[str] = field(default_factory=list)
    is_oa: bool = False
    oa_status: str = ""
    citation_count: int = 0
    backends: list[str] = field(default_factory=list)
    query_ids: list[str] = field(default_factory=list)
    query_texts: list[str] = field(default_factory=list)
    semantic_scholar_id: str = ""
    openalex_id: str = ""
    relevance_score: float = 0.0
    selected_reason: str = ""
    llm_relevance_grade: str = "not_run"
    llm_relevance_score: float = 0.0
    llm_relevance_confidence: str = ""
    llm_support_status: str = ""
    llm_scope_fit: str = "not_run"
    llm_retrieval_role: str = "not_run"
    llm_supported_clause: str = ""
    llm_abstract_evidence_span: str = ""
    likely_contribution: str = ""
    raw_records: list[dict[str, Any]] = field(default_factory=list)
    local_download_path: str = ""
    download_status: str = "not_requested"
    download_error: str = ""
    download_attempted_urls: list[str] = field(default_factory=list)

    def best_url(self) -> str:
        urls = self.candidate_urls(pdf_first=True)
        nonsupp = [u for u in urls if not is_supplementary_url(u)]
        for url in (nonsupp or urls):
            clean = normalize_space(url)
            if clean:
                return clean
        return ""

    def candidate_urls(self, *, pdf_first: bool = True) -> list[str]:
        urls = [self.pdf_url, self.open_access_url, *self.alternate_urls, self.source_url]
        if pdf_first:
            urls = sorted(
                enumerate(urls),
                key=lambda item: (
                    0 if infer_format(str(item[1] or "")) == "pdf" else 1,
                    1 if is_supplementary_url(str(item[1] or "")) else 0,
                    item[0],
                ),
            )
            urls = [u for _, u in urls]
        out: list[str] = []
        seen: set[str] = set()
        for url in urls:
            clean = normalize_space(url)
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(clean)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "title": self.title,
            "doi": self.doi,
            "year": self.year,
            "venue": self.venue,
            "authors": self.authors[:12],
            "abstract": self.abstract,
            "source_url": self.source_url,
            "pdf_url": self.pdf_url,
            "open_access_url": self.open_access_url,
            "alternate_urls": self.alternate_urls,
            "best_url": self.best_url(),
            "is_oa": self.is_oa,
            "oa_status": self.oa_status,
            "citation_count": self.citation_count,
            "backends": self.backends,
            "query_ids": self.query_ids,
            "query_texts": self.query_texts,
            "semantic_scholar_id": self.semantic_scholar_id,
            "openalex_id": self.openalex_id,
            "relevance_score": self.relevance_score,
            "selected_reason": self.selected_reason,
            "llm_relevance_grade": self.llm_relevance_grade,
            "llm_relevance_score": self.llm_relevance_score,
            "llm_relevance_confidence": self.llm_relevance_confidence,
            "llm_support_status": self.llm_support_status,
            "llm_scope_fit": self.llm_scope_fit,
            "llm_retrieval_role": self.llm_retrieval_role,
            "llm_supported_clause": self.llm_supported_clause,
            "llm_abstract_evidence_span": self.llm_abstract_evidence_span,
            "likely_contribution": self.likely_contribution,
            "local_download_path": self.local_download_path,
            "download_status": self.download_status,
            "download_error": self.download_error,
            "download_attempted_urls": self.download_attempted_urls,
        }


class GapOAEvidenceExpander:
    """OA-only paper search and provenance packaging for one weak claim."""

    def __init__(
        self,
        *,
        max_queries: int = 3,
        results_per_backend: int = 8,
        use_openalex: bool = True,
        use_semantic_scholar: bool = True,
        use_unpaywall: bool = True,
        from_year: int | None = None,
        query_boost_terms: list[str] | None = None,
        real_llm_queries: bool = False,
        query_model_tier: str = "advanced_model",
        query_prompt_path: Path = DEFAULT_QUERY_PROMPT,
        real_llm_rerank: bool = True,
        rerank_model_tier: str = "premium_model",
        rerank_prompt_path: Path = DEFAULT_RERANK_PROMPT,
    ) -> None:
        self.max_queries = max(1, int(max_queries))
        self.results_per_backend = max(1, int(results_per_backend))
        self.use_openalex = use_openalex
        self.use_semantic_scholar = use_semantic_scholar
        self.use_unpaywall = use_unpaywall
        self.from_year = from_year
        self.query_boost_terms = [normalize_space(x) for x in (query_boost_terms or []) if normalize_space(x)]
        self.real_llm_queries = bool(real_llm_queries)
        self.query_model_tier = str(query_model_tier or "advanced_model")
        self.query_prompt_path = Path(query_prompt_path)
        self.last_query_audit: dict[str, Any] = {}
        self.real_llm_rerank = bool(real_llm_rerank)
        self.rerank_model_tier = rerank_model_tier
        self.rerank_prompt_path = Path(rerank_prompt_path)
        self.openalex = OpenAlexBackend()
        self.s2 = SemanticScholarBackend()
        # Allow slow public S2 access when no key is configured.
        self.s2.enabled = True
        self.unpaywall = UnpaywallBackend()

    # ------------------------------------------------------------------
    # Query generation
    # ------------------------------------------------------------------

    def build_queries(self, claim: dict[str, Any], section: dict[str, Any]) -> list[GapOAQuery]:
        planned_queries = [
            normalize_space(value)
            for value in (claim.get("planned_queries") or [])
            if normalize_space(value)
        ]
        if planned_queries:
            rows = [
                GapOAQuery(
                    f"Q{index}",
                    compact(query, 420),
                    "upstream chapter-role literature plan",
                )
                for index, query in enumerate(
                    list(dict.fromkeys(planned_queries))[: self.max_queries],
                    start=1,
                )
            ]
            self.last_query_audit = {
                "mode": "upstream_planned_queries",
                "fallback_used": False,
                "queries": [row.query for row in rows],
            }
            return rows
        statement = compact(normalize_scientific_text(claim.get("statement") or claim.get("claim_seed") or ""), 360)
        title = compact(normalize_scientific_text(section.get("title") or ""), 160)
        role = compact(normalize_scientific_text(section.get("argument_role") or ""), 220)
        topic_context = compact(normalize_scientific_text(section.get("_topic_context") or ""), 420)
        etype = str(claim.get("evidence_type") or "mechanism")
        lowered = " ".join([statement, title, role, topic_context]).lower()

        # Domain phrases come from domain_config.yaml.  This keeps the gap
        # retriever reusable when the upstream question moves to another
        # optical subfield.
        statement_lowered = statement.lower()
        matched_phrases = [term for term in self.query_boost_terms if term.lower() in lowered]
        # A phrase explicitly used by the claim is more useful than a generic
        # phrase found only in a long review-topic description.
        matched_phrases.sort(key=lambda term: term.lower() not in statement_lowered)
        phrases: list[str] = []
        seen_phrases: set[str] = set()
        for term in matched_phrases:
            words = re.findall(r"[A-Za-z0-9]+", term.lower())[:5]
            phrase = " ".join(words)
            if phrase and phrase not in seen_phrases:
                phrases.append(phrase)
                seen_phrases.add(phrase)
            if len(phrases) >= 2:
                break

        # Keep relation-shell verbs out of metadata queries.  They consume a
        # scarce word without identifying the scientific entity or phenomenon.
        stopwords = {
            "a", "an", "and", "are", "as", "at", "be", "been", "being", "by",
            "because", "due", "therefore", "thus",
            "can", "could", "for", "from", "has", "have", "having", "in", "into",
            "is", "it", "its", "may", "might", "of", "off", "on", "or", "our",
            "that", "the", "their", "these", "this", "those", "to", "via", "was",
            "were", "which", "while", "will", "with", "would", "within", "through",
            "using", "based", "between", "across", "claim", "evidence", "study",
            "paper", "review", "achieve", "achieved", "achieves", "achieving",
            "allow", "allowed", "allows", "allowing", "cause", "caused", "causes",
            "causing", "change", "changed", "changes", "changing", "create", "created",
            "creates", "creating", "demonstrate", "demonstrated", "demonstrates",
            "demonstrating", "drive", "driven", "drives", "driving", "enable",
            "enabled", "enables", "enabling", "exhibit", "exhibited", "exhibits",
            "exhibiting", "generate", "generated", "generates", "generating", "lead",
            "leading", "leads", "make", "makes", "making", "navigate", "navigated",
            "navigates", "navigating", "produce", "produced", "produces", "producing",
            "provide", "provided", "provides", "providing", "reduce", "reduced",
            "reduces", "reducing", "require", "required", "requires", "requiring",
            "result", "resulted", "resulting", "results", "show", "showed", "shown",
            "shows", "suggest", "suggested", "suggesting", "suggests", "yield", "yielded",
            "yielding", "yields", "strong",
            "arise", "arises", "arising", "remain", "remains", "remaining",
            "challenge", "challenges", "distinct", "different",
        }

        def ordered_stable_tokens(text: str, *, limit: int) -> list[str]:
            tokens = [
                w.lower()
                for w in re.split(r"[^A-Za-z0-9]+", text)
                if (
                    (len(w) >= 3 or (len(w) == 2 and w.isupper()))
                    and w.lower() not in stopwords
                    and not w.isdigit()
                )
            ]
            return list(dict.fromkeys(tokens))[:limit]

        phrase1_words = phrases[0].split() if phrases else []
        # Four topic words + a relationship window + two relaxed anchors can
        # collectively cover ten claim words while keeping every query short.
        relation_capacity = max(2, 7 - len(phrase1_words))
        # Keep enough room for late short acronyms (for example TE/TM) that
        # often follow the long-form object in scientific claim sentences.
        claim_limit = min(12, 6 + relation_capacity + 2)
        claim_tokens = ordered_stable_tokens(statement, limit=claim_limit)
        context_tokens = [
            token for token in ordered_stable_tokens(" ".join([title, role, topic_context]), limit=8)
            if token not in claim_tokens
        ]

        def fill_query(anchors: list[str], phrase: str = "", *, target_min: int = 3) -> str:
            phrase_words = phrase.split()
            available = max(1, 7 - len(phrase_words))
            words = [token for token in anchors if token not in phrase_words][:available]
            for token in context_tokens:
                if len(words) + len(phrase_words) >= target_min:
                    break
                if token not in words and token not in phrase_words:
                    words.append(token)
            if len(words) + len(phrase_words) < target_min:
                cue = etype.lower() if etype.lower() in {"mechanism", "measurement", "comparison", "application"} else "research"
                if cue not in words and cue not in phrase_words:
                    words.append(cue)
            return " ".join((words + phrase_words)[:7])

        topic_anchors = claim_tokens[:6]
        relation_start = min(4, max(0, len(claim_tokens) - relation_capacity))
        relation_anchors = claim_tokens[relation_start : relation_start + relation_capacity]
        if len(relation_anchors) < min(2, len(claim_tokens)):
            relation_anchors = claim_tokens[-min(2, len(claim_tokens)) :]
        relaxed_anchors = list(dict.fromkeys(
            claim_tokens[:2] + claim_tokens[-min(2, len(claim_tokens)) :]
        ))

        queries: list[GapOAQuery] = [
            GapOAQuery("Q1", fill_query(topic_anchors), "short claim entity or phenomenon query"),
            GapOAQuery(
                "Q2",
                fill_query(relation_anchors, phrases[0] if phrases else ""),
                "short claim relationship query with optional domain context",
            ),
            GapOAQuery(
                "Q3",
                fill_query(relaxed_anchors, phrases[1] if len(phrases) > 1 else ""),
                "deterministic relaxed background query",
            ),
        ]

        seen: set[str] = set()
        out: list[GapOAQuery] = []
        for q in queries:
            text = normalize_space(q.query)
            key = text.lower()
            if text and key not in seen:
                out.append(GapOAQuery(q.query_id, text, q.purpose))
                seen.add(key)
            if len(out) >= self.max_queries:
                break
        deterministic = out
        if not self.real_llm_queries or not self.query_prompt_path.exists():
            self.last_query_audit = {
                "mode": "deterministic",
                "fallback_used": False,
                "queries": [row.query for row in deterministic],
            }
            return deterministic

        # Targeted gap retrieval is low-volume and high-leverage. A B+ model
        # repairs vocabulary loss and creates complementary phenomenon,
        # mechanism, and foundation queries. Deterministic candidates remain
        # the validated fallback, so a model/API failure cannot stop M3.
        from llm.qwen_chat_client import call_qwen_chat

        llm_payload = {
            "claim_statement": statement,
            "evidence_type": etype,
            "section_title": title,
            "section_argument_role": role,
            "review_topic_context": topic_context,
            "domain_phrases": phrases,
            "deterministic_query_candidates": [row.query for row in deterministic],
        }
        try:
            response = call_qwen_chat(
                "M3OAQueryPlannerAgent",
                [
                    {"role": "system", "content": self.query_prompt_path.read_text(encoding="utf-8")},
                    {"role": "user", "content": json.dumps(llm_payload, ensure_ascii=False)},
                ],
                model_tier=self.query_model_tier,
                temperature=0,
                max_tokens=700,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
                timeout_seconds=60,
                max_transport_key_candidates=2,
                allow_model_fallback=True,
            )
            raw = str(response.get("content") or "")
            try:
                parsed = json.loads(raw)
            except Exception:
                match = re.search(r"\{.*\}", raw, re.S)
                parsed = json.loads(match.group(0)) if match else {}
            proposed = parsed.get("queries") if isinstance(parsed, dict) else []
            if not isinstance(proposed, list):
                proposed = []
            anchor_tokens = set(ordered_stable_tokens(statement, limit=30))
            llm_queries: list[GapOAQuery] = []
            seen_llm: set[str] = set()
            for index, value in enumerate(proposed, start=1):
                query = normalize_space(value.get("query") if isinstance(value, dict) else value)
                words = re.findall(r"[A-Za-z0-9]+", query)
                lowered_words = {word.lower() for word in words}
                if not (3 <= len(words) <= 10):
                    continue
                if not (anchor_tokens & lowered_words):
                    continue
                key = query.lower()
                if key in seen_llm:
                    continue
                seen_llm.add(key)
                llm_queries.append(GapOAQuery(
                    f"Q{index}", query, "B+ scientific query planning"
                ))
                if len(llm_queries) >= self.max_queries:
                    break
            for row in deterministic:
                if len(llm_queries) >= self.max_queries:
                    break
                if row.query.lower() not in seen_llm:
                    seen_llm.add(row.query.lower())
                    llm_queries.append(GapOAQuery(
                        f"Q{len(llm_queries) + 1}", row.query, row.purpose
                    ))
            if len(llm_queries) >= min(2, self.max_queries):
                self.last_query_audit = {
                    "mode": "llm_plus_deterministic_guard",
                    "fallback_used": False,
                    "queries": [row.query for row in llm_queries],
                    "usage": response.get("_llm_usage", {}),
                }
                return llm_queries
        except Exception as exc:
            self.last_query_audit = {
                "mode": "deterministic_fallback",
                "fallback_used": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.last_query_audit.update({
            "queries": [row.query for row in deterministic],
        })
        return deterministic

    # ------------------------------------------------------------------
    # Search / normalize / rank
    # ------------------------------------------------------------------

    def expand_claim(
        self,
        claim: dict[str, Any],
        section: dict[str, Any],
        *,
        top_k: int = 5,
        download_top_n: int = 0,
        download_dir: Path | None = None,
        metadata_db: Path | None = None,
        citation_chase_top_n: int = 0,
        references_per_seed: int = 10,
        exclude_dois: set[str] | None = None,
    ) -> dict[str, Any]:
        queries = self.build_queries(claim, section)
        raw_hits: list[tuple[GapOAQuery, dict[str, Any]]] = []
        backend_stats: dict[str, Any] = {}
        stage_started = time.monotonic()

        def search_openalex() -> tuple[str, list[tuple[GapOAQuery, dict[str, Any]]], dict[str, int]]:
            found: list[tuple[GapOAQuery, dict[str, Any]]] = []
            for q in queries:
                hits = self.openalex.search(q.query, max_results=self.results_per_backend, from_year=self.from_year)
                found.extend((q, h) for h in hits)
            return "openalex", found, {"queries": len(queries), "raw": len(found)}

        def search_semantic_scholar() -> tuple[str, list[tuple[GapOAQuery, dict[str, Any]]], dict[str, int]]:
            found: list[tuple[GapOAQuery, dict[str, Any]]] = []
            for q in queries:
                hits = self.s2.search(q.query, max_results=self.results_per_backend)
                found.extend((q, h) for h in hits)
            return "semantic_scholar", found, {"queries": len(queries), "raw": len(found)}

        search_jobs = []
        if self.use_openalex:
            search_jobs.append(search_openalex)
        if self.use_semantic_scholar:
            search_jobs.append(search_semantic_scholar)
        with ThreadPoolExecutor(max_workers=max(1, len(search_jobs))) as executor:
            for backend_name, found, stats in executor.map(lambda fn: fn(), search_jobs):
                backend_stats[backend_name] = stats
                raw_hits.extend(found)
        search_elapsed = time.monotonic() - stage_started

        review_context = str(section.get("_topic_context") or "")
        claim_statement = str(claim.get("statement") or "")
        section_title = str(section.get("title") or "")
        section_role = str(section.get("argument_role") or "")
        context = " ".join([
            review_context,
            claim_statement,
            section_title,
            section_role,
        ])
        # Candidate verification must be controlled by the precise gap rather
        # than by a broad review application.  Keeping the review scope in a
        # separately labelled, lower-priority field prevents papers from a
        # neighboring subtopic from becoming evidence merely because they fit
        # another part of the user's overall question.
        rerank_context = "\n".join([
            f"TARGET CLAIM (controlling): {compact(claim_statement, 900)}",
            f"EVIDENCE TYPE: {compact(claim.get('evidence_type'), 80)}",
            f"SECTION CONTEXT: {compact(' '.join([section_title, section_role]), 500)}",
            f"REVIEW SCOPE (disambiguation only): {compact(review_context, 500)}",
        ])

        candidates = self._dedupe_candidates(raw_hits)
        oa_candidates = [c for c in candidates if self._is_oa_candidate(c)]
        for c in oa_candidates:
            c.relevance_score, c.selected_reason = self._score_candidate(c, context, queries)

        citation_chase_report = {
            "enabled": bool(citation_chase_top_n),
            "seed_count": 0,
            "references_requested_per_seed": references_per_seed,
            "raw_reference_hits": 0,
            "usable_reference_hits": 0,
            "seed_statuses": [],
        }
        if citation_chase_top_n > 0 and self.use_semantic_scholar:
            seed_pool = sorted(oa_candidates, key=lambda c: c.relevance_score, reverse=True)
            chase_hits, citation_chase_report = self._citation_chase(
                seed_pool[:citation_chase_top_n],
                references_per_seed=references_per_seed,
            )
            if chase_hits:
                backend_stats.setdefault("semantic_scholar_references", {"queries": 0, "raw": 0})
                backend_stats["semantic_scholar_references"]["queries"] += citation_chase_report.get("seed_count", 0)
                backend_stats["semantic_scholar_references"]["raw"] += len(chase_hits)
                raw_hits.extend(chase_hits)
                candidates = self._dedupe_candidates(raw_hits)
                oa_candidates = [c for c in candidates if self._is_oa_candidate(c)]
                for c in oa_candidates:
                    c.relevance_score, c.selected_reason = self._score_candidate(c, context, queries)

        excluded = {normalize_doi(x) for x in (exclude_dois or set()) if normalize_doi(x)}
        rerank_started = time.monotonic()
        rerank_report = self._rerank_candidates(oa_candidates, rerank_context)
        rerank_elapsed = time.monotonic() - rerank_started
        ranked = sorted(oa_candidates, key=lambda c: c.relevance_score, reverse=True)
        if rerank_report.get("status") in {"ok", "partial"}:
            ranked = [
                c for c in ranked
                if c.llm_relevance_grade != "irrelevant"
                and c.llm_scope_fit != "off_domain"
                and c.llm_retrieval_role != "reject"
            ]
            grade_rank = {"direct": 0, "adjacent": 1, "background": 2, "not_run": 3}
            role_rank = {
                "evidence_candidate": 0,
                "method_transfer": 1,
                "background_only": 2,
                "not_run": 3,
            }
            scope_rank = {"in_domain": 0, "cross_domain_analogy": 1, "not_run": 2}
            ranked.sort(
                key=lambda c: (
                    role_rank.get(c.llm_retrieval_role, 3),
                    scope_rank.get(c.llm_scope_fit, 2),
                    grade_rank.get(c.llm_relevance_grade, 3),
                    -c.relevance_score,
                )
            )
        if excluded:
            ranked = [c for c in ranked if not c.doi or c.doi not in excluded]
        selected = ranked[: max(1, top_k)]
        # Metadata backends already established OA status. Unpaywall is used as
        # a route enricher only for finalists, avoiding dozens of serial network
        # calls for papers that will never be downloaded.
        if self.use_unpaywall:
            self._enrich_unpaywall(selected)

        download_summary = {
            "enabled": bool(download_top_n),
            "target_successes": max(0, int(download_top_n)),
            "attempted_candidates": 0,
            "attempted_urls": 0,
            "downloaded": 0,
            "failed_or_skipped": 0,
            "eligible_in_domain_candidates": 0,
            "skip_reason": "",
        }
        if download_top_n > 0:
            ddir = download_dir or (DEFAULT_OUTPUT_ROOT / "downloads")
            download_pool = [
                candidate for candidate in selected
                if candidate.llm_scope_fit == "in_domain"
                and candidate.llm_retrieval_role == "evidence_candidate"
            ]
            if download_pool:
                download_summary = self._download_until_success(
                    download_pool, ddir, target_successes=download_top_n
                )
                download_summary["eligible_in_domain_candidates"] = len(download_pool)
                download_summary["skip_reason"] = ""
            else:
                download_summary["eligible_in_domain_candidates"] = 0
                download_summary["skip_reason"] = "no_in_domain_evidence_candidates"

        metadata_updates = {"enabled": bool(metadata_db), "upserted_papers": 0, "upserted_fulltext_candidates": 0}
        if metadata_db:
            metadata_updates = self._upsert_metadata(selected, Path(metadata_db), claim, section)

        return {
            "schema_version": "m3_real_oa_expansion.v1",
            "created_at": utc_now(),
            "mode": "oa_only_semantic_scholar_openalex",
            "input": {
                "section_id": section.get("section_id", ""),
                "section_title": section.get("title", ""),
                "claim_id": claim.get("claim_id", ""),
                "claim_statement": claim.get("statement", ""),
                "evidence_type": claim.get("evidence_type", ""),
                "saturation_score": claim.get("saturation_score", None),
                "excluded_dois_count": len(excluded),
            },
            "queries": [q.__dict__ for q in queries],
            "query_generation": dict(self.last_query_audit),
            "backend_stats": backend_stats,
            "candidate_stats": {
                "raw_hits": len(raw_hits),
                "deduped_candidates": len(candidates),
                "oa_candidates": len(oa_candidates),
                "selected_candidates": len(selected),
                "selected_scope_fit": dict(Counter(c.llm_scope_fit for c in selected)),
                "selected_retrieval_roles": dict(
                    Counter(c.llm_retrieval_role for c in selected)
                ),
                "in_domain_evidence_candidates": sum(
                    c.llm_scope_fit == "in_domain"
                    and c.llm_retrieval_role == "evidence_candidate"
                    for c in selected
                ),
                "retrieval_shortfall": not any(
                    c.llm_scope_fit == "in_domain"
                    and c.llm_retrieval_role == "evidence_candidate"
                    for c in selected
                ),
            },
            "citation_chase": citation_chase_report,
            "candidate_rerank": rerank_report,
            "timings_seconds": {
                "initial_search": round(search_elapsed, 3),
                "candidate_rerank": round(rerank_elapsed, 3),
                "total_before_write": round(time.monotonic() - stage_started, 3),
            },
            "download_summary": download_summary,
            "gap_assignment": {
                "section_id": section.get("section_id", ""),
                "claim_id": claim.get("claim_id", ""),
                "selected_candidate_ids": [c.candidate_id for c in selected],
                "selected_dois": [c.doi for c in selected if c.doi],
                "note": "Metadata/OA candidates are assigned to this gap; they are not final textual evidence until full text is parsed into ReviewKnowledgeBase chunks.",
            },
            "selected_oa_candidates": [c.to_dict() for c in selected],
            "metadata_updates": metadata_updates,
            "backend_errors": {
                "openalex": getattr(self.openalex, "last_error", ""),
                "semantic_scholar_errors": getattr(self.s2, "stats", {}).get("errors", 0),
                "unpaywall_errors": getattr(self.unpaywall, "stats", {}).get("errors", 0),
            },
        }

    def _citation_chase(
        self,
        seeds: list[GapOACandidate],
        *,
        references_per_seed: int = 10,
    ) -> tuple[list[tuple[GapOAQuery, dict[str, Any]]], dict[str, Any]]:
        """Follow both references and forward citations from high-ranked seeds.

        This is intentionally conservative: it never fails the main expansion
        if Semantic Scholar is rate-limited or lacks a seed paper.
        """
        report: dict[str, Any] = {
            "enabled": True,
            "seed_count": len(seeds),
            "references_requested_per_seed": references_per_seed,
            "raw_reference_hits": 0,
            "raw_citation_hits": 0,
            "usable_reference_hits": 0,
            "backend_counts": {},
            "seed_statuses": [],
        }
        out: list[tuple[GapOAQuery, dict[str, Any]]] = []
        for idx, seed in enumerate(seeds, start=1):
            seed_id = seed.semantic_scholar_id or (f"DOI:{seed.doi}" if seed.doi else "")
            status = {
                "seed_candidate_id": seed.candidate_id,
                "seed_title": seed.title,
                "seed_lookup_id_type": "semantic_scholar_id" if seed.semantic_scholar_id else "doi" if seed.doi else "none",
                "references_returned": 0,
                "citations_returned": 0,
                "fallback_references_returned": 0,
                "error": "",
            }
            refs: list[dict[str, Any]] = []
            cites: list[dict[str, Any]] = []
            if seed_id:
                refs = self.s2.get_references(seed_id, max_results=max(1, references_per_seed))
                cites = self.s2.get_citations(seed_id, max_results=max(1, references_per_seed))
                report["backend_counts"]["semantic_scholar"] = int(report["backend_counts"].get("semantic_scholar", 0)) + len(refs)
                report["backend_counts"]["semantic_scholar_citations"] = int(report["backend_counts"].get("semantic_scholar_citations", 0)) + len(cites)
            else:
                status["error"] = "no semantic_scholar_id_or_doi"
            status["references_returned"] = len(refs)
            status["citations_returned"] = len(cites)
            if not refs and getattr(self.s2, "last_error", ""):
                status["error"] = str(getattr(self.s2, "last_error", ""))
            if not refs:
                openalex_seed = seed.openalex_id or seed.doi
                if openalex_seed:
                    oa_refs = self.openalex.get_references(openalex_seed, max_results=max(1, references_per_seed))
                    status["fallback_references_returned"] = len(oa_refs)
                    if oa_refs:
                        refs = oa_refs
                        status["seed_lookup_id_type"] += "+openalex_fallback"
                        report["backend_counts"]["openalex"] = int(report["backend_counts"].get("openalex", 0)) + len(oa_refs)
            q = GapOAQuery(
                f"R{idx}",
                f"reference-chase: {seed.title}",
                "citation-network roots from a high-ranked gap candidate",
            )
            for ref in refs:
                ref["backend"] = ref.get("backend") or "semantic_scholar_reference"
                ref["retrieval_method"] = "semantic_scholar_reference_chase"
                out.append((q, ref))
            cq = GapOAQuery(
                f"C{idx}",
                f"citation-chase: {seed.title}",
                "forward-citation frontier from a high-ranked gap candidate",
            )
            for cite in cites:
                cite["backend"] = cite.get("backend") or "semantic_scholar_citation"
                cite["retrieval_method"] = "semantic_scholar_citation_chase"
                out.append((cq, cite))
            report["seed_statuses"].append(status)
        report["raw_reference_hits"] = sum(int(x.get("references_returned", 0)) + int(x.get("fallback_references_returned", 0)) for x in report["seed_statuses"])
        report["raw_citation_hits"] = sum(int(x.get("citations_returned", 0)) for x in report["seed_statuses"])
        report["usable_reference_hits"] = len([1 for _, h in out if h.get("title")])
        return out, report

    def _dedupe_candidates(self, raw_hits: list[tuple[GapOAQuery, dict[str, Any]]]) -> list[GapOACandidate]:
        by_key: dict[str, GapOACandidate] = {}
        for q, h in raw_hits:
            c = self._candidate_from_hit(q, h)
            if not c.title:
                continue
            key = f"doi:{c.doi}" if c.doi else f"title:{title_key(c.title)}"
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = c
            else:
                existing.backends = sorted(set(existing.backends + c.backends))
                existing.query_ids = sorted(set(existing.query_ids + c.query_ids))
                existing.query_texts = sorted(set(existing.query_texts + c.query_texts))
                existing.citation_count = max(existing.citation_count, c.citation_count)
                existing.abstract = existing.abstract or c.abstract
                existing.pdf_url = existing.pdf_url or c.pdf_url
                existing.open_access_url = existing.open_access_url or c.open_access_url
                existing.source_url = existing.source_url or c.source_url
                existing.alternate_urls = self._dedupe_urls(existing.alternate_urls + c.alternate_urls)
                existing.is_oa = existing.is_oa or c.is_oa
                existing.oa_status = existing.oa_status or c.oa_status
                existing.semantic_scholar_id = existing.semantic_scholar_id or c.semantic_scholar_id
                existing.openalex_id = existing.openalex_id or c.openalex_id
                existing.raw_records.extend(c.raw_records)
        return list(by_key.values())

    def _candidate_from_hit(self, q: GapOAQuery, h: dict[str, Any]) -> GapOACandidate:
        backend = str(h.get("backend") or h.get("retrieval_method") or "unknown")
        raw = h.get("raw_metadata") if isinstance(h.get("raw_metadata"), dict) else {}
        s2_open_pdf = raw.get("open_access_pdf") if isinstance(raw.get("open_access_pdf"), dict) else {}
        pdf_url = str(h.get("pdf_url") or s2_open_pdf.get("url") or "")
        open_access_url = str(h.get("open_access_url") or "")
        alternate_urls = self._collect_candidate_urls(h, raw, pdf_url, open_access_url)
        is_oa = bool(h.get("is_oa") is True or oa_status(h.get("is_oa")) == "yes" or pdf_url or open_access_url)
        doi = normalize_doi(h.get("doi") or "")
        cid = f"doi:{doi}" if doi else f"title:{safe_slug(h.get('title',''), limit=60)}"
        return GapOACandidate(
            candidate_id=cid,
            title=str(h.get("title") or ""),
            doi=doi,
            year=h.get("year"),
            venue=str(h.get("journal_or_venue") or h.get("venue") or ""),
            authors=list(h.get("authors") or [])[:20],
            abstract=str(h.get("abstract_or_snippet") or ""),
            source_url=str(h.get("source_url") or h.get("url_or_doi") or ""),
            pdf_url=pdf_url,
            open_access_url=open_access_url,
            alternate_urls=alternate_urls,
            is_oa=is_oa,
            oa_status=str(h.get("oa_status") or ("yes" if is_oa else "unknown")),
            citation_count=int(h.get("citation_count") or h.get("cited_by_count") or raw.get("citation_count") or 0),
            backends=[backend],
            query_ids=[q.query_id],
            query_texts=[q.query],
            semantic_scholar_id=str(h.get("semantic_scholar_paper_id") or ""),
            openalex_id=str(h.get("openalex_id") or ""),
            raw_records=[h],
        )

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for url in urls:
            clean = normalize_space(url)
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(clean)
        return out

    def _collect_candidate_urls(
        self,
        h: dict[str, Any],
        raw: dict[str, Any],
        pdf_url: str = "",
        open_access_url: str = "",
    ) -> list[str]:
        urls: list[str] = []
        for key in ("pdf_url", "open_access_url", "source_url", "url_or_doi"):
            value = h.get(key)
            if isinstance(value, str):
                urls.append(value)
        for value in (pdf_url, open_access_url):
            if value:
                urls.append(value)

        content_urls = h.get("content_urls") or raw.get("content_urls") or {}
        if isinstance(content_urls, dict):
            for value in content_urls.values():
                if isinstance(value, str):
                    urls.append(value)
                elif isinstance(value, list):
                    urls.extend(str(x) for x in value if x)

        s2_open_pdf = raw.get("open_access_pdf") if isinstance(raw.get("open_access_pdf"), dict) else {}
        if isinstance(s2_open_pdf, dict):
            urls.append(str(s2_open_pdf.get("url") or ""))

        arxiv_id = str(h.get("arxiv_id") or "")
        if arxiv_id:
            urls.append(f"https://arxiv.org/pdf/{arxiv_id}")

        # Convert common arXiv abstract links to PDF links as an additional OA route.
        for url in list(urls):
            m = re.search(r"arxiv\.org/abs/([^?#]+)", str(url), flags=re.I)
            if m:
                urls.append(f"https://arxiv.org/pdf/{m.group(1)}")
        return self._dedupe_urls(urls)

    @staticmethod
    def _is_oa_candidate(c: GapOACandidate) -> bool:
        return bool(c.is_oa and (c.pdf_url or c.open_access_url or c.source_url))

    def _enrich_unpaywall(self, candidates: list[GapOACandidate]) -> None:
        for c in candidates:
            if not c.doi:
                continue
            info = self.unpaywall.lookup(c.doi)
            if not info:
                continue
            if info.get("is_oa"):
                c.is_oa = True
                c.oa_status = str(info.get("oa_status") or c.oa_status or "yes")
                best = str(info.get("best_oa_url") or "")
                extra_urls: list[str] = []
                if best:
                    if best.lower().split("?", 1)[0].endswith(".pdf") or "/pdf" in best.lower():
                        c.pdf_url = c.pdf_url or best
                    else:
                        c.open_access_url = c.open_access_url or best
                    extra_urls.append(best)
                for loc in info.get("oa_locations") or []:
                    if not isinstance(loc, dict):
                        continue
                    extra_urls.extend([
                        str(loc.get("url_for_pdf") or ""),
                        str(loc.get("url") or ""),
                    ])
                c.alternate_urls = self._dedupe_urls(c.alternate_urls + extra_urls)

    def _score_candidate(self, c: GapOACandidate, context: str, queries: list[GapOAQuery] | None = None) -> tuple[float, str]:
        context_norm = normalize_scientific_text(context)
        context_tokens = tokenize(context_norm)
        paper_tokens = tokenize(" ".join([c.title, c.abstract, c.venue]))
        overlap = context_tokens & paper_tokens
        lexical = len(overlap) / max(1, len(context_tokens))
        citation = min(1.0, math.log10(max(1, c.citation_count) + 1) / 4.0)
        current_year = datetime.now().year
        recency = 0.0
        if c.year:
            recency = max(0.0, min(1.0, 1.0 - ((current_year - int(c.year)) / 12.0)))
        url_bonus = 0.12 if c.pdf_url else 0.06 if c.open_access_url else 0.0
        abstract_bonus = 0.08 if len(c.abstract) >= 200 else 0.0
        paper_text = normalize_scientific_text(" ".join([c.title, c.abstract, c.venue]))
        phrase_boost = 0.0
        phrase_hits: list[str] = []
        contextual_boost_terms = [
            phrase for phrase in self.query_boost_terms
            if phrase_present(context_norm, phrase)
        ]
        for phrase in contextual_boost_terms:
            if phrase_present(context_norm, phrase) and phrase_present(paper_text, phrase):
                phrase_boost += 0.045
                phrase_hits.append(phrase)
        phrase_boost = min(0.28, phrase_boost)

        # Configured domain anchors prevent generic papers from outranking the
        # specific gap without baking one scientific topic into the code.
        penalty = 0.0
        if contextual_boost_terms and not phrase_hits:
            penalty += 0.18 if len(contextual_boost_terms) >= 2 else 0.10

        score = 0.54 * lexical + 0.12 * citation + 0.11 * recency + url_bonus + abstract_bonus + phrase_boost - penalty
        score = max(0.0, score)
        reason = (
            f"lexical_overlap={len(overlap)}; citation_count={c.citation_count}; "
            f"year={c.year}; has_pdf={bool(c.pdf_url)}; phrase_hits={', '.join(phrase_hits[:6])}; "
            f"penalty={round(penalty, 3)}; matched_terms={', '.join(sorted(overlap)[:8])}"
        )
        return round(score, 4), reason

    def _rerank_candidates(self, candidates: list[GapOACandidate], context: str) -> dict[str, Any]:
        """Parallel small-batch B-model rerank; lexical ranking remains fallback."""
        if not self.real_llm_rerank or not candidates or not self.rerank_prompt_path.exists():
            return {"enabled": self.real_llm_rerank, "status": "skipped", "evaluated": 0}
        from llm.qwen_chat_client import call_qwen_chat

        pool = sorted(candidates, key=lambda c: c.relevance_score, reverse=True)[:30]
        ref_map = {f"P{idx:02d}": cand for idx, cand in enumerate(pool, start=1)}
        prompt = self.rerank_prompt_path.read_text(encoding="utf-8")

        def call_batch(items: list[tuple[str, GapOACandidate]]) -> dict[str, Any]:
            payload = {
                "evidence_gap": compact(context, 1600),
                "candidates": [
                    {
                        "candidate_ref": ref,
                        "title": compact(c.title, 300),
                        "abstract": compact(c.abstract, 900),
                        "year": c.year,
                        "venue": compact(c.venue, 160),
                        "citation_count": c.citation_count,
                    }
                    for ref, c in items
                ],
            }
            try:
                result = call_qwen_chat(
                    "M3OACandidateRerankerAgent",
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model_tier=self.rerank_model_tier,
                    temperature=0,
                    max_tokens=1400,
                    response_format={"type": "json_object"},
                    force_mock=False,
                    max_retries=0,
                )
                raw = str(result.get("content") or "")
                try:
                    parsed = json.loads(raw)
                except Exception:
                    match = re.search(r"\{.*\}", raw, re.S)
                    parsed = json.loads(match.group(0)) if match else {}
                return {
                    "rankings": parsed.get("rankings") if isinstance(parsed.get("rankings"), list) else [],
                    "usage": result.get("_llm_usage", {}),
                    "error": "",
                }
            except Exception as exc:
                return {"rankings": [], "usage": {}, "error": f"{type(exc).__name__}: {exc}"}

        def run_batch(items: list[tuple[str, GapOACandidate]]) -> dict[str, Any]:
            """Retry malformed structured output with smaller independent batches."""
            first = call_batch(items)
            expected = {ref for ref, _ in items}
            returned = {
                str(row.get("candidate_ref") or "")
                for row in first.get("rankings", [])
                if isinstance(row, dict)
            }
            if not first.get("error") and expected <= returned:
                first["usages"] = [first.pop("usage", {})]
                return first
            if len(items) == 1:
                first["usages"] = [first.pop("usage", {})]
                return first
            midpoint = max(1, len(items) // 2)
            children = [run_batch(items[:midpoint]), run_batch(items[midpoint:])]
            rankings = [row for child in children for row in child.get("rankings", [])]
            errors = [child.get("error") for child in children if child.get("error")]
            usages = [u for child in children for u in child.get("usages", [])]
            return {
                "rankings": rankings,
                "usages": usages,
                "error": "; ".join(errors),
                "recovered_by_split": True,
            }

        items = list(ref_map.items())
        batches = [items[i : i + 5] for i in range(0, len(items), 5)]
        with ThreadPoolExecutor(max_workers=min(6, len(batches))) as executor:
            batch_results = list(executor.map(run_batch, batches))

        try:
            rankings = [row for result in batch_results for row in result.get("rankings", [])]
            grade_counts: dict[str, int] = {}
            scope_fit_counts: dict[str, int] = {}
            retrieval_role_counts: dict[str, int] = {}
            updated = 0
            for row in rankings:
                if not isinstance(row, dict):
                    continue
                cand = ref_map.get(str(row.get("candidate_ref") or ""))
                if not cand:
                    continue
                grade = str(row.get("grade") or "background").lower().strip()
                if grade not in {"direct", "adjacent", "background", "irrelevant"}:
                    grade = "background"
                try:
                    raw_score = max(0.0, min(100.0, float(row.get("score", 0))))
                    # Tolerate an otherwise valid 0-10 answer without silently
                    # converting 9/10 into 0.09/1.0.
                    if 0.0 < raw_score <= 10.0:
                        raw_score *= 10.0
                    llm_score = raw_score / 100.0
                except Exception:
                    llm_score = 0.0
                support_status = str(row.get("support_status") or "not_established").lower().strip()
                if support_status not in {"supports", "qualifies", "contradicts", "not_established"}:
                    support_status = "not_established"
                scope_fit = str(row.get("scope_fit") or "not_run").lower().strip()
                if scope_fit not in {"in_domain", "cross_domain_analogy", "off_domain"}:
                    scope_fit = "in_domain" if grade == "direct" else "not_run"
                retrieval_role = str(row.get("retrieval_role") or "not_run").lower().strip()
                if retrieval_role not in {
                    "evidence_candidate", "method_transfer", "background_only", "reject"
                }:
                    retrieval_role = (
                        "evidence_candidate" if grade == "direct"
                        else "background_only" if grade == "background"
                        else "not_run"
                    )
                supported_clause = compact(row.get("supported_clause"), 400)
                evidence_span = compact(row.get("abstract_evidence_span"), 500)

                # Cross-domain analogies may inspire methods, but they are not
                # evidence for an optical claim. Off-domain results are removed
                # even when generic words such as "simulation" or "imaging"
                # overlap with the query.
                if scope_fit == "off_domain":
                    grade = "irrelevant"
                    retrieval_role = "reject"
                    support_status = "not_established"
                    llm_score = min(llm_score, 0.24)
                elif scope_fit == "cross_domain_analogy":
                    if grade == "direct":
                        grade = "adjacent"
                    if retrieval_role == "evidence_candidate":
                        retrieval_role = "method_transfer"
                    support_status = "not_established"
                    llm_score = min(llm_score, 0.64)

                # Keep the discovery boundary coherent when the reranker
                # returns contradictory labels. An adjacent paper that names
                # no precise target-claim component may remain useful context,
                # but it must not consume the scarce full-text evidence budget.
                # Adjacent papers with a named component remain eligible for
                # full-text verification because an abstract can be incomplete.
                if grade == "irrelevant":
                    retrieval_role = "reject"
                    support_status = "not_established"
                elif grade == "background" and retrieval_role == "evidence_candidate":
                    retrieval_role = "background_only"
                elif (
                    grade == "adjacent"
                    and retrieval_role == "evidence_candidate"
                    and support_status == "not_established"
                    and not supported_clause
                ):
                    retrieval_role = "background_only"
                    row["reason"] = (
                        "Adjacent candidate names no precise target-claim component; "
                        f"kept as background only. {row.get('reason', '')}"
                    )

                # A direct label must be auditable against the abstract that the
                # model actually saw. Topic similarity cannot pass this gate.
                if grade == "direct":
                    span_tokens = tokenize(normalize_scientific_text(evidence_span))
                    abstract_tokens = tokenize(normalize_scientific_text(cand.abstract))
                    trace_ratio = len(span_tokens & abstract_tokens) / max(1, len(span_tokens))
                    direct_traceable = (
                        bool(cand.abstract)
                        and len(span_tokens) >= 4
                        and trace_ratio >= 0.75
                        and support_status in {"supports", "qualifies", "contradicts"}
                        and bool(supported_clause)
                    )
                    if not direct_traceable:
                        grade = "adjacent" if cand.abstract else "background"
                        llm_score = min(llm_score, 0.74 if cand.abstract else 0.49)
                        row["reason"] = (
                            f"Direct-evidence audit failed (trace_ratio={trace_ratio:.2f}); "
                            f"downgraded. {row.get('reason', '')}"
                        )
                grade_prior = {"direct": 1.0, "adjacent": 0.65, "background": 0.3, "irrelevant": 0.0}[grade]
                cand.relevance_score = round(0.35 * cand.relevance_score + 0.45 * llm_score + 0.20 * grade_prior, 4)
                cand.llm_relevance_grade = grade
                cand.llm_relevance_score = round(llm_score, 4)
                confidence = str(row.get("confidence") or "low").lower().strip()
                cand.llm_relevance_confidence = confidence if confidence in {"high", "medium", "low"} else "low"
                cand.llm_support_status = support_status
                cand.llm_scope_fit = scope_fit
                cand.llm_retrieval_role = retrieval_role
                cand.llm_supported_clause = supported_clause
                cand.llm_abstract_evidence_span = evidence_span
                cand.likely_contribution = compact(row.get("likely_contribution"), 500)
                cand.selected_reason = compact(
                    f"{cand.selected_reason}; llm_grade={grade}; llm_reason={row.get('reason', '')}",
                    1200,
                )
                grade_counts[grade] = grade_counts.get(grade, 0) + 1
                scope_fit_counts[scope_fit] = scope_fit_counts.get(scope_fit, 0) + 1
                retrieval_role_counts[retrieval_role] = retrieval_role_counts.get(retrieval_role, 0) + 1
                updated += 1
            coverage_rate = updated / max(1, len(pool))
            failed_refs = [
                ref for ref, cand in ref_map.items()
                if cand.llm_relevance_grade == "not_run"
            ]
            return {
                "enabled": True,
                "status": "ok" if coverage_rate >= 0.95 else "partial" if updated else "empty",
                "evaluated": updated,
                "pool_size": len(pool),
                "batch_count": len(batches),
                "batch_errors": [x.get("error") for x in batch_results if x.get("error")],
                "recovered_batches": sum(1 for x in batch_results if x.get("recovered_by_split")),
                "coverage_rate": round(coverage_rate, 4),
                "failed_candidate_refs": failed_refs,
                "grade_counts": grade_counts,
                "scope_fit_counts": scope_fit_counts,
                "retrieval_role_counts": retrieval_role_counts,
                "llm_usage": [u for x in batch_results for u in x.get("usages", [])],
            }
        except Exception as exc:
            return {
                "enabled": True,
                "status": "error",
                "evaluated": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _download_until_success(
        self,
        candidates: list[GapOACandidate],
        download_dir: Path,
        *,
        target_successes: int,
    ) -> dict[str, Any]:
        download_dir.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        attempted_candidates = 0
        attempted_urls = 0
        for c in candidates:
            if downloaded >= target_successes:
                c.download_status = "not_requested_after_target_met"
                continue
            attempted_candidates += 1
            urls = c.candidate_urls(pdf_first=True)
            if not urls:
                c.download_status = "skipped_no_url"
                c.download_error = "no OA URL available"
                continue
            errors: list[str] = []
            for url in urls:
                # M3-real v1.1 only saves verified PDF bytes. HTML/JATS entries
                # remain as fulltext candidates for later OA acquisition routes.
                if infer_format(url) != "pdf":
                    errors.append(f"non_pdf_route:{url[:120]}")
                    continue
                attempted_urls += 1
                c.download_attempted_urls.append(url)
                out = download_dir / (safe_slug(c.doi or c.title, limit=90) + ".pdf")
                ok, err = self._download_pdf_url(url, out)
                if ok:
                    c.local_download_path = str(out)
                    c.download_status = "downloaded"
                    c.download_error = ""
                    downloaded += 1
                    break
                errors.append(err)
            if c.download_status != "downloaded":
                c.download_status = "failed_all_pdf_routes" if c.download_attempted_urls else "skipped_no_pdf_route"
                c.download_error = " | ".join(errors[:5])
        failed = len([c for c in candidates if c.download_status not in {"downloaded", "not_requested_after_target_met", "not_requested"}])
        return {
            "enabled": True,
            "target_successes": target_successes,
            "attempted_candidates": attempted_candidates,
            "attempted_urls": attempted_urls,
            "downloaded": downloaded,
            "failed_or_skipped": failed,
        }

    @staticmethod
    def _download_pdf_url(url: str, out: Path) -> tuple[bool, str]:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9",
            }
            content_type = ""
            if is_openalex_content_url(url):
                data, error = fetch_openalex_content(url, timeout=60, headers=headers)
                if data is None:
                    return False, error
            else:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    content_type = str(resp.headers.get("Content-Type") or "").lower()
                    data = resp.read()
            if len(data) < 5000:
                return False, f"too_short:{len(data)} bytes"
            looks_pdf = data[:8].startswith(b"%PDF") or "application/pdf" in content_type
            if not looks_pdf:
                return False, f"non_pdf_content:{content_type or 'unknown'}:{len(data)} bytes"
            out.write_bytes(data)
            return True, ""
        except urllib.error.HTTPError as exc:
            return False, f"HTTPError:{exc.code}:{exc.reason}"
        except Exception as exc:
            return False, f"{type(exc).__name__}:{exc}"

    @staticmethod
    def _upsert_metadata(
        selected: list[GapOACandidate],
        db_path: Path,
        claim: dict[str, Any],
        section: dict[str, Any],
    ) -> dict[str, Any]:
        index = MetadataIndex(db_path)
        upserted = 0
        candidates = 0
        try:
            for c in selected:
                paper_id = local_paper_id(doi=c.doi, title=c.title)
                best = c.best_url()
                fmt = infer_format(best)
                route = route_for_candidate(best, is_oa="yes", source="m3_real_oa_expansion", fmt=fmt)
                sources = sorted(set(c.backends + ["m3_real_oa_expansion"]))
                data = {
                    "paper_id": paper_id,
                    "title": c.title,
                    "authors_json": json.dumps(c.authors, ensure_ascii=False),
                    "year": c.year,
                    "venue": c.venue,
                    "doi": c.doi,
                    "abstract": c.abstract,
                    "is_oa": "yes",
                    "best_fulltext_url": best,
                    "best_fulltext_format": fmt,
                    "best_fulltext_route": route,
                    "sources_json": json.dumps(sources, ensure_ascii=False),
                    "citation_count": c.citation_count,
                    "openalex_id": c.openalex_id,
                    "semantic_scholar_id": c.semantic_scholar_id,
                    "source_paper_id": f"m3_gap:{section.get('section_id','')}:{claim.get('claim_id','')}",
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                }
                data["metadata_completeness"], missing = index.completeness(data)
                data["missing_fields_json"] = json.dumps(missing, ensure_ascii=False)
                index.upsert_paper_metadata(data)
                for backend in c.backends:
                    index.upsert_paper_source(
                        paper_id=paper_id,
                        source=f"m3_real_oa_expansion:{backend}",
                        source_id=c.openalex_id or c.semantic_scholar_id or c.doi or c.candidate_id,
                        title=c.title,
                        doi=c.doi,
                        abstract_present=bool(c.abstract),
                        is_oa="yes",
                        url=best,
                        fmt=fmt,
                        raw={"gap_section_id": section.get("section_id"), "gap_claim_id": claim.get("claim_id"), "candidate": c.to_dict()},
                    )
                candidate_urls = c.candidate_urls(pdf_first=True)
                for url in candidate_urls[:6]:
                    url_fmt = infer_format(url)
                    url_route = route_for_candidate(url, is_oa="yes", source="m3_real_oa_expansion", fmt=url_fmt)
                    index.upsert_fulltext_candidate(
                        FulltextCandidate(
                            paper_id=paper_id,
                            url=url,
                            format=url_fmt,
                            route=url_route,
                            source="m3_real_oa_expansion",
                            is_oa="yes",
                            confidence=min(1.0, max(0.3, c.relevance_score)),
                            raw={"gap_section_id": section.get("section_id"), "gap_claim_id": claim.get("claim_id"), "candidate_id": c.candidate_id},
                        )
                    )
                    candidates += 1
                upserted += 1
            index.conn.commit()
        finally:
            index.close()
        return {"enabled": True, "db_path": str(db_path), "upserted_papers": upserted, "upserted_fulltext_candidates": candidates}


def load_claim_from_blueprint(path: Path, claim_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Accept both a bare blueprint and the stage envelopes emitted by S10-S12.
    # This keeps the standalone M3-real diagnostic/VIP entry point usable with
    # actual pipeline artifacts instead of forcing callers to create a one-off
    # extracted JSON file first.
    blueprint = payload.get("blueprint") if isinstance(payload, dict) else None
    if not isinstance(blueprint, dict):
        blueprint = payload
    if not isinstance(blueprint, dict):
        raise ValueError(f"Blueprint payload must be a JSON object: {path}")
    input_context = blueprint.get("input_context") if isinstance(blueprint.get("input_context"), dict) else {}
    topic_context = " ".join(
        compact(x, 500)
        for x in [
            input_context.get("user_question"),
            input_context.get("problem_understanding"),
            input_context.get("scope_definition"),
            blueprint.get("review_thesis"),
        ]
        if x
    )
    best: tuple[dict[str, Any], dict[str, Any]] | None = None
    best_sat = 999.0
    for section in blueprint.get("sections") or []:
        if isinstance(section, dict):
            section.setdefault("_topic_context", topic_context)
        for claim in section.get("claims") or []:
            if claim_id and str(claim.get("claim_id")) == claim_id:
                return section, claim
            try:
                sat = float(claim.get("saturation_score", 0.0))
            except Exception:
                sat = 0.0
            if sat < best_sat:
                best = (section, claim)
                best_sat = sat
    if best:
        return best
    raise ValueError(f"No claim found in blueprint: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3-real OA-only gap evidence expansion")
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--claim-id", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-queries", type=int, default=2)
    parser.add_argument("--results-per-backend", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--from-year", type=int, default=0)
    parser.add_argument(
        "--topic-context",
        default="",
        help="Optional global review topic/context; important when the blueprint file only contains sections/claims.",
    )
    parser.add_argument("--download-top-n", type=int, default=0)
    parser.add_argument("--citation-chase-top-n", type=int, default=0)
    parser.add_argument("--references-per-seed", type=int, default=10)
    parser.add_argument("--metadata-db", type=Path, default=None)
    parser.add_argument("--no-semantic-scholar", action="store_true")
    parser.add_argument("--no-openalex", action="store_true")
    parser.add_argument("--no-unpaywall", action="store_true")
    args = parser.parse_args(argv)

    section, claim = load_claim_from_blueprint(args.blueprint, args.claim_id or None)
    if args.topic_context:
        section["_topic_context"] = args.topic_context
    out_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_slug(str(claim.get('claim_id') or 'claim'))}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    expander = GapOAEvidenceExpander(
        max_queries=args.max_queries,
        results_per_backend=args.results_per_backend,
        use_openalex=not args.no_openalex,
        use_semantic_scholar=not args.no_semantic_scholar,
        use_unpaywall=not args.no_unpaywall,
        from_year=args.from_year or None,
    )
    result = expander.expand_claim(
        claim,
        section,
        top_k=args.top_k,
        download_top_n=args.download_top_n,
        download_dir=out_dir / "downloads",
        metadata_db=args.metadata_db,
        citation_chase_top_n=args.citation_chase_top_n,
        references_per_seed=args.references_per_seed,
    )
    (out_dir / "gap_oa_expansion_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rows = result.get("selected_oa_candidates") or []
    with (out_dir / "selected_oa_candidates.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps({
        "ok": True,
        "output_dir": str(out_dir),
        "claim_id": result["input"]["claim_id"],
        "queries": len(result["queries"]),
        "candidate_stats": result["candidate_stats"],
        "metadata_updates": result["metadata_updates"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
