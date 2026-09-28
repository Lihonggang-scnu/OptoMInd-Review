"""Retrieve validated S2 body snippets as first-class knowledge-base chunks."""

from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ftfy import fix_text

import time

from optomind_research.s2_cache import S2PersistentCache
from optomind_research.s2_citation_role_mapper import map_citation_roles
from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk
from optomind_research.runtime.review_quality_contract import (
    assess_structured_snippet,
    permission_for_content,
)


_BIBLIOGRAPHY_SECTIONS = {
    "references",
    "bibliography",
    "literature cited",
    "acknowledgements",
    "acknowledgments",
}

# Backend-fix ticket 1.1/1.2: a paper confirmed to have no snippet-index
# body text is skipped before the request is spent.  The confirmation is
# persisted in the shared S2 cache database so later runs do not repeat
# the same zero-result request.  After this window the paper becomes
# eligible again (S2 may have indexed new open full text).
_PRECISE_EMPTY_RETRY_SECONDS = 30 * 86400


@dataclass(slots=True)
class TextChunkRetrievalResult:
    accepted_chunks: list[UnifiedTextChunk]
    rejected_items: list[dict[str, Any]]
    query_runs: list[dict[str, Any]]
    paper_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_chunks": [chunk.to_dict() for chunk in self.accepted_chunks],
            "rejected_items": self.rejected_items,
            "query_runs": self.query_runs,
            "paper_ids": self.paper_ids,
        }


def merge_text_chunk_results(
    *results: TextChunkRetrievalResult,
) -> TextChunkRetrievalResult:
    """Merge retrieval waves without losing rejected-item or run audit."""

    accepted: list[UnifiedTextChunk] = []
    rejected: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    paper_ids: list[str] = []
    seen_chunks: set[str] = set()
    seen_text: set[str] = set()
    for result in results:
        rejected.extend(result.rejected_items)
        runs.extend(result.query_runs)
        paper_ids.extend(result.paper_ids)
        for chunk in result.accepted_chunks:
            text_key = hashlib.sha1(
                _normalize_text(chunk.text).casefold().encode("utf-8")
            ).hexdigest()
            if chunk.chunk_id in seen_chunks or text_key in seen_text:
                continue
            seen_chunks.add(chunk.chunk_id)
            seen_text.add(text_key)
            accepted.append(chunk)
    return TextChunkRetrievalResult(
        accepted_chunks=accepted,
        rejected_items=rejected,
        query_runs=runs,
        paper_ids=list(dict.fromkeys(paper_ids)),
    )


def _normalize_text(text: str) -> str:
    repaired = fix_text(text or "")
    return re.sub(r"\s+", " ", repaired).strip()


def _snippet_chunk_id(
    *, corpus_id: int | str | None, start: int | None, end: int | None, text: str
) -> str:
    digest = hashlib.sha1(
        f"{corpus_id}|{start}|{end}|{_normalize_text(text)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"s2chunk:{corpus_id or 'unknown'}:{start or 0}:{end or 0}:{digest}"


def materialize_abstract_claim(
    paper: S2PaperRecord,
) -> tuple[UnifiedTextChunk | None, str]:
    """Convert a verified abstract into one traceable, qualified claim chunk."""

    abstract = _normalize_text(paper.abstract)
    if not abstract:
        return None, "abstract_missing"
    alpha_ratio = sum(char.isalpha() for char in abstract) / max(1, len(abstract))
    if len(abstract) < 40 or alpha_ratio < 0.35:
        return None, "abstract_damaged_or_too_short"
    if not paper.paper_id or not paper.title.strip():
        return None, "paper_identity_incomplete"
    digest = hashlib.sha1(
        f"{paper.paper_id}|{abstract}".encode("utf-8")
    ).hexdigest()[:20]
    permission = permission_for_content(
        "abstract_claim",
        scope_fit="direct",
        context_complete=False,
    )
    abstract_provider = "semantic_scholar"
    for event in reversed(paper.route_events):
        if event.get("event") == "abstract_enriched_from_verified_public_metadata":
            abstract_provider = str(event.get("provider") or "verified_public_metadata")
            break
    materialization_route = (
        "s2_abstract_claim_after_body_and_oa_miss"
        if abstract_provider == "semantic_scholar"
        else "verified_public_metadata_abstract_claim_after_body_and_oa_miss"
    )
    chunk = UnifiedTextChunk(
        chunk_id=f"s2abstract:{digest}",
        paper_id=paper.paper_id,
        corpus_id=paper.corpus_id,
        doi=paper.doi,
        title=paper.title,
        text=abstract,
        section="Abstract",
        text_provenance="s2_abstract_snippet",
        source_locator={
            "provider": abstract_provider,
            "paper_id": paper.paper_id,
            "corpus_id": paper.corpus_id,
            "field": "abstract",
        },
        citation_roles=["partial_support", "background_context"],
        content_depth="abstract_claim",
        context_complete=False,
        scope_fit="direct",
        use_permission=str(permission["use_permission"]),
        allowed_claim_kinds=list(permission["allowed_claim_kinds"]),
        route_provenance={
            "discovery_route": "semantic_scholar_graph",
            "materialization_route": materialization_route,
            "abstract_provider": abstract_provider,
            "paper_id": paper.paper_id,
            "fallback_order": [
                "s2_structured_body_snippet",
                "public_oa_fulltext",
                "verified_abstract_claim",
            ],
            "claim_boundary": "paper_reported_abstract_claims_only",
        },
        context_limitations=[
            "abstract_only_no_full_method_or_results_context",
            "do_not_infer_unstated_numbers_mechanisms_or_causality",
            "use_only_for_claims_explicitly_reported_in_this_abstract",
        ],
        raw_metadata={
            "s2_abstract_materialization": True,
            "abstract_provider": abstract_provider,
            "paper": paper.to_dict(),
        },
    )
    return chunk, "materialized"


def _reject_reason(
    snippet: dict[str, Any], *, min_chars: int
) -> str:
    text = _normalize_text(str(snippet.get("text") or ""))
    kind = str(snippet.get("snippetKind") or "").casefold()
    section = str(snippet.get("section") or "").strip().casefold()
    if not text:
        return "empty_text"
    if kind != "body":
        return f"not_body:{kind or 'unknown'}"
    if len(text) < min_chars:
        return "too_short"
    if section in _BIBLIOGRAPHY_SECTIONS:
        return "bibliography_section"
    alpha = sum(char.isalpha() for char in text)
    if alpha / max(1, len(text)) < 0.45:
        return "parser_noise"
    return ""


class S2TextChunkRetriever:
    def __init__(
        self,
        gateway: S2IntelligenceGateway | None = None,
        *,
        min_chars: int = 500,
        max_workers: int | None = None,
    ) -> None:
        self.gateway = gateway or S2IntelligenceGateway()
        self.min_chars = max(100, int(min_chars))
        configured = max_workers
        if configured is None:
            configured = os.environ.get("OPTOMIND_S2_RETRIEVAL_WORKERS", "4")
        try:
            configured_int = int(configured)
        except (TypeError, ValueError):
            configured_int = 4
        self.max_workers = max(1, min(configured_int, 16))

    def retrieve(
        self,
        queries: list[str],
        *,
        paper_ids: list[str] | None = None,
        limit_per_query: int = 20,
        requested_roles: list[str] | None = None,
        scope_context: dict[str, Any] | None = None,
    ) -> TextChunkRetrievalResult:
        accepted: list[UnifiedTextChunk] = []
        rejected: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        resolved_paper_ids: list[str] = []

        paper_batches = [None]
        if paper_ids:
            unique_ids = list(dict.fromkeys(item for item in paper_ids if item))
            paper_batches = [
                unique_ids[index : index + 100]
                for index in range(0, len(unique_ids), 100)
            ] or [None]
        request_specs = [
            (query, paper_batch)
            for query in list(dict.fromkeys(queries))
            for paper_batch in paper_batches
        ]

        def execute(spec: tuple[str, list[str] | None]) -> tuple[str, list[str] | None, list[dict[str, Any]], Any]:
            query, paper_batch = spec
            items, response = self.gateway.search_snippets(
                query,
                limit=limit_per_query,
                paper_ids=paper_batch,
            )
            return query, paper_batch, items, response

        worker_count = min(self.max_workers, max(1, len(request_specs)))
        if worker_count == 1:
            responses = [execute(spec) for spec in request_specs]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="s2-snippet-search",
            ) as pool:
                # map() preserves request order, so duplicate suppression and
                # tie-breaking remain identical to the serial implementation.
                responses = list(pool.map(execute, request_specs))
        for request_index, (query, paper_batch, items, response) in enumerate(responses):
            runs.append(
                {
                    "query": query,
                    "paper_filter_count": len(paper_batch or []),
                    "paper_ids": list(paper_batch or []),
                    "status_code": response.status_code,
                    "status_category": response.status_category,
                    "cache_hit": response.cache_hit,
                    "result_count": len(items),
                    "wait_seconds": response.wait_seconds,
                    "request_index": request_index,
                    "request_concurrency": worker_count,
                    "concurrency_workers": worker_count,
                }
            )
            for item in items:
                self._accept_item(
                    item,
                    query=query,
                    response=response,
                    accepted=accepted,
                    rejected=rejected,
                    seen_hashes=seen_hashes,
                    resolved_paper_ids=resolved_paper_ids,
                    requested_roles=requested_roles,
                    scope_context=scope_context,
                )
        return TextChunkRetrievalResult(
            accepted_chunks=accepted,
            rejected_items=rejected,
            query_runs=runs,
            paper_ids=list(dict.fromkeys(resolved_paper_ids)),
        )

    def _known_empty_paper_keys(self) -> dict[str, float]:
        """Paper keys already confirmed empty in the shared S2 cache.

        Returns {} when the transport has no persistent cache (tests with
        fake gateways); the feature then degrades to plain retrieval.
        """

        try:
            cache = self.gateway.transport.cache
        except AttributeError:
            return {}
        if not isinstance(cache, S2PersistentCache):
            return {}
        cutoff = time.time() - _PRECISE_EMPTY_RETRY_SECONDS
        try:
            keys = cache.precise_empty_confirmed_since(cutoff)
        except Exception:
            return {}
        return {key: cutoff for key in keys}

    def _record_known_empty_paper(
        self,
        paper_key: str,
        title: str,
        known: dict[str, float],
    ) -> None:
        """Persist one freshly confirmed zero-result paper (best effort)."""

        known[paper_key] = time.time()
        try:
            cache = self.gateway.transport.cache
        except AttributeError:
            return
        if not isinstance(cache, S2PersistentCache):
            return
        try:
            cache.record_precise_empty(paper_key, title=title)
        except Exception:
            pass

    def retrieve_precise_missing_papers(
        self,
        papers: list[S2PaperRecord],
        *,
        existing_chunks: list[UnifiedTextChunk] | None = None,
        limit_per_paper: int = 100,
        max_papers: int = 300,
        requested_roles: list[str] | None = None,
        scope_context: dict[str, Any] | None = None,
    ) -> TextChunkRetrievalResult:
        """Search one exact paper at a time only when it has no body chunk.

        Candidates already confirmed to have no snippet-index body text
        (persisted in the shared S2 cache) and candidates that S2 itself
        marks as closed-access without any open PDF are skipped before a
        request is spent; both signals are available without querying.
        Newly confirmed empty papers are persisted for later runs.
        """

        present_aliases: set[str] = set()
        for chunk in existing_chunks or []:
            present_aliases.update(
                str(value or "").strip().casefold()
                for value in (chunk.paper_id, chunk.corpus_id, chunk.title)
                if str(value or "").strip()
            )
        known_empty = self._known_empty_paper_keys()
        skipped: list[dict[str, Any]] = []
        candidates: list[tuple[S2PaperRecord, set[str], list[str]]] = []
        for paper in papers:
            aliases = {
                str(value or "").strip().casefold()
                for value in (paper.paper_id, paper.corpus_id, paper.title)
                if str(value or "").strip()
            }
            if aliases & present_aliases:
                continue
            if len(candidates) >= max(0, int(max_papers)):
                break
            paper_filter = [paper.paper_id] if paper.paper_id else []
            if not paper_filter and paper.corpus_id is not None:
                paper_filter = [f"CorpusId:{paper.corpus_id}"]
            query = paper.title.strip()
            if not query or not paper_filter:
                continue
            # Backend-fix ticket 1.1: skip requests that are guaranteed to
            # return nothing.  Both checks use only request-time-local data.
            if paper_filter[0] in known_empty:
                skipped.append(
                    {
                        "query_category": "precise_missing_paper",
                        "query": query,
                        "target_paper_id": paper.paper_id,
                        "target_title": paper.title,
                        "status_category": "skipped_known_empty",
                        "result_count": 0,
                        "request_index": None,
                    }
                )
                continue
            if (
                paper.is_oa is False
                and not str(paper.s2_open_access_candidate_url or "").strip()
            ):
                skipped.append(
                    {
                        "query_category": "precise_missing_paper",
                        "query": query,
                        "target_paper_id": paper.paper_id,
                        "target_title": paper.title,
                        "status_category": "skipped_closed_no_open_pdf",
                        "result_count": 0,
                        "request_index": None,
                    }
                )
                continue
            candidates.append((paper, aliases, paper_filter))

        def retrieve_one(
            item: tuple[S2PaperRecord, set[str], list[str]],
        ) -> tuple[S2PaperRecord, set[str], TextChunkRetrievalResult]:
            paper, aliases, paper_filter = item
            result = self.retrieve(
                [paper.title.strip()],
                paper_ids=paper_filter,
                limit_per_query=limit_per_paper,
                requested_roles=requested_roles,
                scope_context=scope_context,
            )
            for run in result.query_runs:
                # Ticket 1.4: the generic concurrency_workers value written by
                # the inner layer is 1 when called per-paper and used to read
                # as if the whole precise wave were serial.  Override it with
                # the real outer parallelism and keep the layers explicit.
                run.update(
                    {
                        "query_category": "precise_missing_paper",
                        "target_paper_id": item[0].paper_id,
                        "target_title": item[0].title,
                        "request_concurrency": 1,
                        "paper_concurrency": worker_count,
                        "concurrency_workers": worker_count,
                    }
                )
            if result.query_runs and all(
                int(run.get("result_count") or 0) == 0
                and run.get("status_code") == 200
                and not run.get("cache_hit")
                for run in result.query_runs
            ):
                self._record_known_empty_paper(
                    paper_filter[0], paper.title, known_empty
                )
            return paper, aliases, result

        worker_count = min(self.max_workers, max(1, len(candidates)))
        if worker_count == 1:
            retrieved = [retrieve_one(item) for item in candidates]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="s2-precise-paper",
            ) as pool:
                retrieved = list(pool.map(retrieve_one, candidates))
        results: list[TextChunkRetrievalResult] = []
        for _paper, aliases, result in retrieved:
            results.append(result)
            if result.accepted_chunks:
                present_aliases.update(aliases)
        merged = (
            merge_text_chunk_results(*results)
            if results
            else TextChunkRetrievalResult([], [], [], [])
        )
        merged.query_runs.extend(skipped)
        return merged

    def _accept_item(
        self,
        item: dict[str, Any],
        *,
        query: str,
        response: Any,
        accepted: list[UnifiedTextChunk],
        rejected: list[dict[str, Any]],
        seen_hashes: set[str],
        resolved_paper_ids: list[str],
        requested_roles: list[str] | None,
        scope_context: dict[str, Any] | None,
    ) -> None:
                snippet = item.get("snippet") or {}
                paper = item.get("paper") or {}
                reason = _reject_reason(snippet, min_chars=self.min_chars)
                if reason:
                    rejected.append(
                        {
                            "query": query,
                            "paper_title": str(paper.get("title") or ""),
                            "reason": reason,
                        }
                    )
                    return
                text = _normalize_text(str(snippet.get("text") or ""))
                normalized_hash = hashlib.sha1(text.casefold().encode("utf-8")).hexdigest()
                if normalized_hash in seen_hashes:
                    rejected.append(
                        {
                            "query": query,
                            "paper_title": str(paper.get("title") or ""),
                            "reason": "duplicate_text",
                        }
                    )
                    return
                seen_hashes.add(normalized_hash)
                corpus_raw = paper.get("corpusId")
                try:
                    corpus_id = int(corpus_raw) if corpus_raw not in (None, "") else None
                except (TypeError, ValueError):
                    corpus_id = None
                offset = snippet.get("snippetOffset") or {}
                start = offset.get("start")
                end = offset.get("end")
                paper_id = str(paper.get("paperId") or "").strip()
                if not paper_id and corpus_id is not None:
                    paper_id = f"CorpusId:{corpus_id}"
                if not paper_id:
                    paper_id = f"s2-title:{hashlib.sha1(str(paper.get('title') or '').encode('utf-8')).hexdigest()[:12]}"
                resolved_paper_ids.append(paper_id)
                score = float(item.get("score") or 0.0)
                roles = map_citation_roles(
                    query_or_claim=query,
                    text=text,
                    section=str(snippet.get("section") or ""),
                    requested_roles=requested_roles or [],
                    direct_score=score,
                )
                limitations: list[str] = []
                if re.search(r"\b(?:Fig(?:ure)?|Table|Eq(?:uation)?)\.?\s*\d", text):
                    limitations.append("referenced_visual_or_equation_not_in_snippet")
                if text.endswith(("…", "...")):
                    limitations.append("possible_truncation")
                # Even without an explicit section context, assess the
                # snippet against its own retrieval query.  A body snippet is
                # a first-class chunk, but provider origin alone must never
                # grant direct/context-complete permission.
                scope_assessment = assess_structured_snippet(
                    text,
                    query=query,
                    section_context=str(
                        (scope_context or {}).get("section_context") or ""
                    ),
                    limitations=limitations,
                )
                limitations = list(
                    dict.fromkeys(
                        limitations
                        + list(scope_assessment.get("context_limitations") or [])
                    )
                )
                scope_fit = str(scope_assessment.get("scope_fit") or "unreviewed")
                context_complete = bool(scope_assessment.get("context_complete", False))
                permission = permission_for_content(
                    "structured_snippet",
                    scope_fit=scope_fit,
                    context_complete=context_complete,
                )
                chunk = UnifiedTextChunk(
                    chunk_id=_snippet_chunk_id(
                        corpus_id=corpus_id, start=start, end=end, text=text
                    ),
                    paper_id=paper_id,
                    corpus_id=corpus_id,
                    title=fix_text(str(paper.get("title") or "")).strip(),
                    text=text,
                    section=fix_text(str(snippet.get("section") or "")).strip(),
                    source_locator={
                        "provider": "semantic_scholar",
                        "corpus_id": corpus_id,
                        "offset_start": start,
                        "offset_end": end,
                        "retrieval_version": (
                            response.payload.get("retrievalVersion")
                            if isinstance(response.payload, dict)
                            else ""
                        ),
                    },
                    citation_roles=roles,
                    query_links=[query],
                    score=score,
                    # S2 body snippets are first-class structured chunks.  The
                    # later authoring layer may downgrade a particular use
                    # when scope audit says adjacent/contextual, but the
                    # provider is not treated as an abstract-only source.
                    scope_fit=scope_fit,
                    content_depth="structured_snippet",
                    context_complete=context_complete,
                    use_permission=str(permission["use_permission"]),
                    allowed_claim_kinds=list(permission["allowed_claim_kinds"]),
                    route_provenance={
                        "discovery_route": "semantic_scholar_snippet_search",
                        "materialization_route": "s2_structured_body_snippet",
                        "query": query,
                        "requested_roles": list(dict.fromkeys(
                            str(role).strip().casefold()
                            for role in (requested_roles or [])
                            if str(role).strip()
                        )),
                        "paper_id": paper_id,
                        "scope_assessment": scope_assessment,
                    },
                    context_limitations=limitations,
                    reference_mentions=list(
                        ((snippet.get("annotations") or {}).get("refMentions") or [])
                    ),
                    sentence_spans=list(
                        ((snippet.get("annotations") or {}).get("sentences") or [])
                    ),
                    raw_metadata={"s2_item": item},
                )
                accepted.append(chunk)
