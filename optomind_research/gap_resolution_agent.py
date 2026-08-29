"""M3 — Gap Resolution Agent for review blueprint claims.

Mock mode: deterministic, no API calls. Adds unused candidate chunks from same
section; calculates saturation from unique DOI prefixes in supporting chunk IDs.
Real mode: LLM generates 3 queries → round-robin KB FTS5 → relevance gate →
add chunks → rescore. Max 2 iterations; still below threshold → open_question.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GAP_QUERY_PROMPT = PROJECT_ROOT / "prompts" / "Gap Query Generator.txt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doi_from_chunk_id(chunk_id: str) -> str | None:
    m = re.match(r"^(doi-[^:]+)", chunk_id or "")
    return m.group(1) if m else None


def _rescore_saturation(chunk_ids: list[str]) -> float:
    """Saturation = number of unique DOI prefixes, capped at 3.0."""
    unique_dois = {_doi_from_chunk_id(c) for c in chunk_ids} - {None}
    return min(3.0, float(len(unique_dois)))


def _tokenize(text: str) -> set[str]:
    return {w for w in re.split(r"\W+", text.lower()) if len(w) >= 4}


def _relevance_gate(claim: dict, query: str, chunk: dict) -> tuple[bool, str]:
    """Deterministic keyword gate. Returns (accepted, reject_reason).

    Filters obviously irrelevant chunks — not a substitute for human review.
    Goal: drop empty text, boilerplate, and zero-keyword-overlap chunks.
    """
    text = (chunk.get("text_preview") or "").strip()
    if len(text) < 20:
        return False, "text_too_short"

    # Single-signal boilerplate: short block that looks like a reference list
    combined_lower = (
        (chunk.get("title") or "") + " " + text + " " + (chunk.get("section_path") or "")
    ).lower()
    boilerplate_signals = ("bibliography", "references\n", "© ", "copyright", "all rights reserved")
    if any(s in combined_lower for s in boilerplate_signals) and len(text) < 120:
        return False, "boilerplate"

    # Require at least one 4-char keyword from query OR claim to appear in chunk
    query_tokens = _tokenize(query)
    claim_tokens = _tokenize(claim.get("statement") or "")
    chunk_tokens = _tokenize(combined_lower)

    if (query_tokens | claim_tokens) & chunk_tokens:
        return True, ""
    return False, "no_keyword_overlap"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class GapResolutionResult:
    claim_id: str
    before_saturation: float
    after_saturation: float
    queries_generated: list[str] = field(default_factory=list)
    new_chunk_ids_added: list[str] = field(default_factory=list)
    iterations: int = 0
    status: str = "already_sufficient"
    gap_type: str = ""
    # Audit trail fields (new)
    accepted_chunks: list[dict] = field(default_factory=list)
    rejected_chunks: list[dict] = field(default_factory=list)
    query_to_candidate_counts: dict = field(default_factory=dict)
    gap_rationale: str = ""
    sqlite_path_used: str = ""

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "before_saturation": self.before_saturation,
            "after_saturation": self.after_saturation,
            "queries_generated": self.queries_generated,
            "new_chunk_ids_added": self.new_chunk_ids_added,
            "iterations": self.iterations,
            "status": self.status,
            "gap_type": self.gap_type,
            "accepted_chunks": self.accepted_chunks,
            "rejected_chunks": self.rejected_chunks,
            "query_to_candidate_counts": self.query_to_candidate_counts,
            "gap_rationale": self.gap_rationale,
            "sqlite_path_used": self.sqlite_path_used,
        }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class GapResolutionAgent:
    def __init__(
        self,
        real_llm: bool = False,
        model_tier: str = "standard_model",
        saturation_threshold: float = 1.5,
        max_iterations: int = 2,
        kb_path: Path | None = None,
        prompt_path: Path = DEFAULT_GAP_QUERY_PROMPT,
    ) -> None:
        self.real_llm = real_llm
        self.model_tier = model_tier
        self.saturation_threshold = saturation_threshold
        self.max_iterations = max_iterations
        self.kb_path = Path(kb_path) if kb_path else None
        self.prompt_path = Path(prompt_path)
        self._system_prompt: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_prompt(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        return self._system_prompt

    def _find_sqlite(self) -> Path | None:
        """Return a SQLite path from kb_path, whether kb_path is a file or dir."""
        if self.kb_path is None:
            return None
        p = self.kb_path
        # Direct file
        if p.suffix == ".sqlite" and p.is_file():
            return p
        # Directory: prefer well-known names, then any .sqlite
        if p.is_dir():
            for name in ("review_knowledge_base.sqlite", "knowledge_base.sqlite", "kb.sqlite"):
                c = p / name
                if c.exists():
                    return c
            for f in p.iterdir():
                if f.suffix == ".sqlite":
                    return f
        return None

    def _generate_queries_mock(self, claim: dict) -> tuple[list[str], str, str]:
        stmt = claim.get("statement", "")[:60]
        etype = claim.get("evidence_type", "mechanism")
        queries = [f"{stmt} {etype}", f"{stmt} evidence", f"{stmt} experimental study"]
        return queries, "direct_retrievable", ""

    def _generate_queries_llm(
        self, claim: dict, section: dict, supporting_previews: list[dict]
    ) -> tuple[list[str], str, str]:
        """Call LLM. Returns (queries, gap_type, rationale)."""
        from llm.qwen_chat_client import call_qwen_chat

        payload = json.dumps(
            {
                "claim_id": claim.get("claim_id", ""),
                "statement": claim.get("statement", ""),
                "evidence_type": claim.get("evidence_type", ""),
                "saturation_score": claim.get("saturation_score", 0.0),
                "current_supporting_chunk_previews": supporting_previews[:2],
                "section_title": section.get("title", ""),
                "section_argument_role": str(section.get("argument_role", ""))[:200],
            },
            ensure_ascii=False,
        )
        result = call_qwen_chat(
            "GapQueryGeneratorAgent",
            [
                {"role": "system", "content": self._load_prompt()},
                {"role": "user", "content": payload},
            ],
            model_tier=self.model_tier,
            temperature=0,
            max_tokens=400,
            response_format={"type": "json_object"},
            force_mock=False,
            max_retries=0,
            timeout_seconds=60,
            max_transport_key_candidates=1,
            allow_model_fallback=False,
        )
        text = str(result.get("content") or "")
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
                queries = [str(q) for q in (parsed.get("queries") or []) if q][:3]
                gap_type = str(parsed.get("gap_type", "direct_retrievable"))
                rationale = str(parsed.get("rationale", ""))
                if queries:
                    return queries, gap_type, rationale
            except Exception:
                pass
        queries, gap_type, _ = self._generate_queries_mock(claim)
        return queries, gap_type, ""

    def _search_kb_structured(
        self,
        queries: list[str],
        existing_ids: set[str],
        claim: dict,
        max_per_claim: int = 3,
    ) -> tuple[list[dict], list[dict], dict[str, dict]]:
        """Round-robin KB search with relevance gate.

        Returns:
            accepted:  list of chunk audit dicts added to the claim
            rejected:  list of chunk audit dicts rejected by gate
            q_stats:   {query: {candidates: N, accepted: N}}
        """
        sqlite_path = self._find_sqlite()
        if sqlite_path is None:
            return [], [], {}
        try:
            from optomind_research.review_knowledge_base import query_kb
        except ImportError:
            return [], [], {}

        # Collect per-query candidate lists (globally deduped, excluding known)
        seen_ids: set[str] = set()
        per_query: list[list[dict]] = []
        q_stats: dict[str, dict] = {}

        for q in queries:
            candidates: list[dict] = []
            try:
                raw = query_kb(sqlite_path, q, top_k=8)
                for chunk in raw.get("text_chunks") or []:
                    cid = str(chunk.get("chunk_id") or "")
                    if cid and cid not in existing_ids and cid not in seen_ids:
                        candidates.append({**chunk, "_query": q})
                        seen_ids.add(cid)
            except Exception:
                pass
            per_query.append(candidates)
            q_stats[q] = {"candidates": len(candidates), "accepted": 0}

        # Round-robin selection: 1 chunk per query per pass until max_per_claim reached
        accepted: list[dict] = []
        rejected: list[dict] = []
        pointers = [0] * len(queries)

        while len(accepted) < max_per_claim:
            progressed = False
            for qi, q in enumerate(queries):
                if len(accepted) >= max_per_claim:
                    break
                while pointers[qi] < len(per_query[qi]):
                    chunk = per_query[qi][pointers[qi]]
                    pointers[qi] += 1
                    cid = str(chunk.get("chunk_id", ""))
                    ok, reason = _relevance_gate(claim, q, chunk)
                    audit = {
                        "chunk_id": cid,
                        "query": q,
                        "title": str(chunk.get("title") or "")[:120],
                        "doi": _doi_from_chunk_id(cid),
                        "section_path": str(chunk.get("section_path") or ""),
                        "score": float(chunk.get("score") or 0.0),
                        "text_preview": str(chunk.get("text_preview") or "")[:200],
                    }
                    if ok:
                        accepted.append({**audit, "reason": "keyword_match"})
                        q_stats[q]["accepted"] = q_stats[q].get("accepted", 0) + 1
                        progressed = True
                        break
                    else:
                        rejected.append({**audit, "reject_reason": reason})
            if not progressed:
                break

        return accepted, rejected, q_stats

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, claim: dict, section: dict) -> GapResolutionResult:
        """Attempt gap resolution for one claim. Mutates claim dict in place."""
        before_sat = float(claim.get("saturation_score", 0.0))
        if before_sat >= self.saturation_threshold:
            return GapResolutionResult(
                claim_id=claim.get("claim_id", ""),
                before_saturation=before_sat,
                after_saturation=before_sat,
                status="already_sufficient",
            )
        if not self.real_llm:
            return self._resolve_mock(claim, section, before_sat)
        return self._resolve_real(claim, section, before_sat)

    def _resolve_mock(
        self, claim: dict, section: dict, before_sat: float
    ) -> GapResolutionResult:
        current_ids: set[str] = set(claim.get("supporting_text_chunk_ids") or [])
        candidate_ids: list[str] = list(section.get("candidate_text_chunk_ids") or [])
        unused = [c for c in candidate_ids if c not in current_ids]
        added = unused[:2]
        new_ids = list(current_ids) + added
        new_sat = _rescore_saturation(new_ids)
        after_sat = max(before_sat, new_sat)

        claim["supporting_text_chunk_ids"] = new_ids
        claim["saturation_score"] = after_sat
        claim["gap_resolution_status"] = (
            "resolved" if after_sat >= self.saturation_threshold else "open_question"
        )

        accepted_chunks = [
            {
                "chunk_id": cid,
                "query": "mock",
                "title": "",
                "doi": _doi_from_chunk_id(cid),
                "section_path": "",
                "score": 0.0,
                "text_preview": "",
                "reason": "mock_unused_candidate",
            }
            for cid in added
        ]

        return GapResolutionResult(
            claim_id=claim.get("claim_id", ""),
            before_saturation=before_sat,
            after_saturation=after_sat,
            queries_generated=[],
            new_chunk_ids_added=added,
            iterations=1,
            status=claim["gap_resolution_status"],
            gap_type="direct_retrievable",
            accepted_chunks=accepted_chunks,
            rejected_chunks=[],
            query_to_candidate_counts={},
            gap_rationale="",
            sqlite_path_used="",
        )

    def _resolve_real(
        self, claim: dict, section: dict, before_sat: float
    ) -> GapResolutionResult:
        current_ids: list[str] = list(claim.get("supporting_text_chunk_ids") or [])
        all_accepted: list[dict] = []
        all_rejected: list[dict] = []
        all_queries: list[str] = []
        q_counts: dict[str, dict] = {}
        gap_type = "direct_retrievable"
        gap_rationale = ""
        current_sat = before_sat
        actual_iterations = 0

        sqlite = self._find_sqlite()
        sqlite_path_used = str(sqlite) if sqlite else ""

        text_chunks = section.get("candidate_text_chunks") or []
        chunk_map = {
            c.get("chunk_id", ""): c.get("text_preview", "")
            for c in text_chunks if isinstance(c, dict)
        }
        supporting_previews = [
            {"chunk_id": cid, "text_preview": chunk_map.get(cid, "")[:300]}
            for cid in current_ids[:2]
        ]

        for iteration in range(1, self.max_iterations + 1):
            if current_sat >= self.saturation_threshold:
                break
            actual_iterations = iteration

            queries, gap_type, gap_rationale = self._generate_queries_llm(
                claim, section, supporting_previews
            )
            all_queries.extend(queries)

            existing = set(current_ids + [a["chunk_id"] for a in all_accepted])
            accepted, rejected, q_stats = self._search_kb_structured(
                queries, existing, claim
            )
            all_rejected.extend(rejected)
            q_counts.update(q_stats)

            if accepted:
                all_accepted.extend(accepted)
                combined = current_ids + [a["chunk_id"] for a in all_accepted]
                new_sat = _rescore_saturation(combined)
                current_sat = max(current_sat, new_sat)
            else:
                break

        final_ids = current_ids + [a["chunk_id"] for a in all_accepted]
        claim["supporting_text_chunk_ids"] = final_ids
        claim["saturation_score"] = current_sat
        status = "resolved" if current_sat >= self.saturation_threshold else "open_question"
        claim["gap_resolution_status"] = status

        return GapResolutionResult(
            claim_id=claim.get("claim_id", ""),
            before_saturation=before_sat,
            after_saturation=current_sat,
            queries_generated=all_queries,
            new_chunk_ids_added=[a["chunk_id"] for a in all_accepted],
            iterations=actual_iterations,
            status=status,
            gap_type=gap_type,
            accepted_chunks=all_accepted,
            rejected_chunks=all_rejected,
            query_to_candidate_counts=q_counts,
            gap_rationale=gap_rationale,
            sqlite_path_used=sqlite_path_used,
        )

    def resolve_blueprint(
        self,
        blueprint: dict[str, Any],
        *,
        target_claim_ids: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> tuple[dict[str, Any], list[GapResolutionResult]]:
        """Resolve all claims or an explicit target set (deepcopy, non-destructive)."""
        import copy
        blueprint = copy.deepcopy(blueprint)
        target_ids = None if target_claim_ids is None else {
            str(value) for value in target_claim_ids if str(value)
        }
        jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for section in blueprint.get("sections") or []:
            for claim in section.get("claims") or []:
                if target_ids is not None and str(claim.get("claim_id") or "") not in target_ids:
                    continue
                jobs.append((claim, section))
        if self.real_llm and len(jobs) > 1:
            # Claims are independent until the blueprint-level verifier and DAG
            # rebuild run.  Parallel query generation avoids serially paying one
            # network round trip per gap while preserving output order.
            with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
                results = list(pool.map(lambda pair: self.resolve(*pair), jobs))
        else:
            results = [self.resolve(claim, section) for claim, section in jobs]
        return blueprint, results
