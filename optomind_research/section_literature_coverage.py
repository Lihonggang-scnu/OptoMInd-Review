"""Chapter-level literature landscape planning and retrieval.

This layer answers a different question from claim-level evidence binding:
what literature must a review chapter *engage with* in order to explain its
history, mechanisms, methods, frontier, controversies, and applications?
It never promotes a retrieved paper into proof of a precise factual claim.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Section Literature Coverage Planner.txt"
SOURCE_AUDIT_PROMPT = PROJECT_ROOT / "prompts" / "Section Coverage Source Auditor.txt"

COVERAGE_ROLES = (
    "foundation",
    "mechanism",
    "method",
    "frontier",
    "controversy",
    "application",
)
VALID_PRIORITIES = {"required", "useful", "not_needed"}


def _compact(value: Any, limit: int = 700) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _tokens(text: Any) -> set[str]:
    stop = {
        "about", "after", "also", "among", "and", "are", "based", "between",
        "from", "have", "into", "more", "review", "section", "should", "study",
        "that", "their", "these", "this", "those", "through", "using", "what",
        "when", "where", "which", "with", "within", "would", "literature",
    }
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9-]{3,}", str(text or "").lower())
        if token not in stop
    }


def _default_plan(section: dict[str, Any]) -> dict[str, Any]:
    contract = section.get("section_contract") or {}
    title = _compact(
        section.get("title")
        or section.get("section_title")
        or contract.get("title")
        or contract.get("section_title"),
        240,
    )
    thesis = _compact(
        contract.get("central_thesis")
        or section.get("argument_role")
        or section.get("planned_thesis"),
        900,
    )
    context = f"{title} {thesis}".strip()
    lower = context.lower()
    required = {
        "mechanism" if any(term in lower for term in ("mechanism", "physics", "causal", "principle")) else "",
        "method" if any(term in lower for term in ("method", "design", "algorithm", "fabrication", "measurement")) else "",
        "application" if any(term in lower for term in ("application", "deployment", "device", "experimental", "validation")) else "",
    } - {""}
    role_hints = {
        "foundation": "seminal origin foundational theory early development review",
        "mechanism": "physical mechanism governing model causal explanation boundary condition",
        "method": "method algorithm experimental protocol fabrication characterization comparison",
        "frontier": "recent frontier emerging state of the art open challenge",
        "controversy": "controversy disagreement conflicting result limitation failure boundary",
        "application": "application deployment device engineering validation scale up",
    }
    rows = []
    for role in COVERAGE_ROLES:
        priority = "required" if role in required or role == "frontier" else "useful"
        rows.append({
            "role": role,
            "priority": priority,
            "coverage_question": f"What {role} literature is needed to develop the chapter argument?",
            "intended_synthesis": f"Use {role} literature to deepen the chapter argument without treating it as sentence-level proof.",
            "queries": [f"{context} {role_hints[role]}".strip()],
        })
    return {
        "section_id": str(section.get("section_id") or ""),
        "chapter_argument": thesis,
        "roles": rows,
        "planner_mode": "deterministic_fallback",
    }


class SectionLiteratureCoverageExpander:
    """Plan and retrieve a chapter-level source landscape from the local KB."""

    def __init__(
        self,
        *,
        kb_path: Path | str | None,
        real_llm: bool = True,
        model_tier: str = "premium_model",
        max_papers_per_role: int = 5,
        max_chunks_per_paper: int = 2,
    ) -> None:
        self.kb_path = Path(kb_path) if kb_path else None
        self.real_llm = bool(real_llm)
        self.model_tier = model_tier
        self.max_papers_per_role = max(1, int(max_papers_per_role))
        self.max_chunks_per_paper = max(1, int(max_chunks_per_paper))

    def plan(self, section: dict[str, Any]) -> dict[str, Any]:
        fallback = _default_plan(section)
        if not self.real_llm:
            return fallback
        contract = section.get("section_contract") or {}
        payload = {
            "section_id": section.get("section_id", ""),
            "section_title": (
                section.get("title")
                or section.get("section_title")
                or contract.get("title")
                or contract.get("section_title", "")
            ),
            "argument_role": section.get("argument_role", ""),
            "central_thesis": contract.get("central_thesis", ""),
            "argument_sequence": contract.get("argument_sequence") or [],
            "paragraph_functions": contract.get("paragraph_functions") or [],
            "key_questions": section.get("key_questions") or [],
            "scope_guardrails": section.get("scope_guardrails") or [],
        }
        parsed: dict[str, Any] = {}
        attempts: list[dict[str, Any]] = []
        for tier in dict.fromkeys([self.model_tier, "advanced_model"]):
            try:
                result = call_qwen_chat(
                    "SectionLiteratureCoveragePlanner",
                    [
                        {"role": "system", "content": DEFAULT_PROMPT.read_text(encoding="utf-8")},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model_tier=tier,
                    temperature=0.1,
                    max_tokens=2600,
                    response_format={"type": "json_object"},
                    timeout_seconds=180,
                    max_transport_key_candidates=2,
                    allow_model_fallback=False,
                    accept_partial_stream=False,
                    enable_thinking=False,
                    force_mock=False,
                    max_retries=0,
                )
                attempts.append({"model_tier": tier, "success": True})
                parsed = _safe_json(str(result.get("content") or ""))
                if parsed:
                    break
            except Exception as exc:
                attempts.append({
                    "model_tier": tier,
                    "success": False,
                    "error_type": type(exc).__name__,
                })
        normalized = self._normalize_plan(parsed, fallback)
        normalized["planner_attempts"] = attempts
        return normalized

    @staticmethod
    def _normalize_plan(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        raw_rows = raw.get("roles") if isinstance(raw.get("roles"), list) else []
        by_role = {
            str(row.get("role") or "").strip().lower(): row
            for row in raw_rows if isinstance(row, dict)
        }
        fallback_by_role = {row["role"]: row for row in fallback["roles"]}
        roles: list[dict[str, Any]] = []
        for role in COVERAGE_ROLES:
            source = by_role.get(role) or fallback_by_role[role]
            priority = str(source.get("priority") or "useful").lower()
            if priority not in VALID_PRIORITIES:
                priority = "useful"
            queries = []
            for value in source.get("queries") or []:
                query = _compact(value, 420)
                if query and query not in queries:
                    queries.append(query)
            if not queries and priority != "not_needed":
                queries = list(fallback_by_role[role]["queries"])
            roles.append({
                "role": role,
                "priority": priority,
                "coverage_question": _compact(
                    source.get("coverage_question") or fallback_by_role[role]["coverage_question"],
                    700,
                ),
                "intended_synthesis": _compact(
                    source.get("intended_synthesis") or fallback_by_role[role]["intended_synthesis"],
                    900,
                ),
                "queries": queries[:3],
            })
        return {
            "section_id": fallback["section_id"],
            "chapter_argument": _compact(
                raw.get("chapter_argument") or fallback["chapter_argument"], 1200
            ),
            "roles": roles,
            "planner_mode": "real_llm" if raw_rows else fallback["planner_mode"],
        }

    def expand(
        self,
        section: dict[str, Any],
        *,
        plan_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = (
            self._normalize_plan(plan_override, _default_plan(section))
            if isinstance(plan_override, dict) and plan_override
            else self.plan(section)
        )
        if self.kb_path is None or not self.kb_path.exists():
            return {
                "plan": plan,
                "sources": [],
                "coverage_gaps": [
                    {
                        "role": row["role"],
                        "priority": row["priority"],
                        "queries": row["queries"],
                        "reason": "knowledge_base_unavailable",
                    }
                    for row in plan["roles"] if row["priority"] != "not_needed"
                ],
                "summary": {"source_count": 0, "paper_count": 0},
            }

        from optomind_research.review_knowledge_base import query_kb

        by_paper: dict[str, dict[str, Any]] = {}
        role_audit: list[dict[str, Any]] = []
        for role_row in plan["roles"]:
            role = role_row["role"]
            if role_row["priority"] == "not_needed":
                role_audit.append({"role": role, "priority": "not_needed", "selected_papers": 0})
                continue
            role_candidates: dict[str, dict[str, Any]] = {}
            for query in role_row["queries"]:
                try:
                    result = query_kb(self.kb_path, query, top_k=16, include_raw=True)
                except Exception:
                    result = {}
                query_tokens = _tokens(query)
                for chunk in result.get("text_chunks") or []:
                    if not isinstance(chunk, dict) or not chunk.get("paper_id") or not chunk.get("chunk_id"):
                        continue
                    searchable = " ".join(
                        str(chunk.get(key) or "")
                        for key in ("title", "section_path", "text_preview")
                    )
                    matched = sorted(query_tokens & _tokens(searchable))
                    if not matched:
                        continue
                    paper_id = str(chunk["paper_id"])
                    candidate = role_candidates.setdefault(paper_id, {
                        "paper_id": paper_id,
                        "title": str(chunk.get("title") or ""),
                        "role": role,
                        "matched_terms": set(),
                        "chunks": [],
                        "score": 0.0,
                    })
                    candidate["matched_terms"].update(matched)
                    candidate["score"] = max(float(candidate["score"]), float(len(matched)))
                    candidate["chunks"].append({
                        "chunk_id": str(chunk["chunk_id"]),
                        "paper_id": paper_id,
                        "title": str(chunk.get("title") or ""),
                        "section_path": str(chunk.get("section_path") or ""),
                        "text_preview": _compact(chunk.get("text_preview"), 1200),
                        "coverage_role": role,
                        "retrieval_query": query,
                    })
            meta = self._paper_metadata(list(role_candidates))
            for row in role_candidates.values():
                row_meta = meta.get(str(row["paper_id"])) or {}
                row["role_rank_score"] = self._role_rank_score(
                    role,
                    lexical_score=float(row["score"]),
                    metadata=row_meta,
                )
            ranked = sorted(
                role_candidates.values(),
                key=lambda row: (
                    float(row.get("role_rank_score") or 0.0),
                    float(row["score"]),
                    len(row["matched_terms"]),
                ),
                reverse=True,
            )[: self.max_papers_per_role]
            for row in ranked:
                paper_id = str(row["paper_id"])
                existing = by_paper.setdefault(paper_id, {
                    "paper_id": paper_id,
                    "doi": "",
                    "title": row["title"],
                    "year": None,
                    "venue": "",
                    "quality_tier": "",
                    "citation_count": 0,
                    "source_genre": "",
                    "coverage_roles": [],
                    "role_uses": [],
                    "representative_chunks": [],
                    "citation_policy": "chapter_context_and_synthesis",
                    "precise_factual_support_requires_claim_verification": True,
                })
                existing.update({
                    key: value
                    for key, value in (meta.get(paper_id) or {}).items()
                    if value not in (None, "")
                })
                if role not in existing["coverage_roles"]:
                    existing["coverage_roles"].append(role)
                existing["role_uses"].append({
                    "role": role,
                    "priority": role_row["priority"],
                    "intended_synthesis": role_row["intended_synthesis"],
                    "matched_terms": sorted(row["matched_terms"])[:12],
                })
                existing_chunk_ids = {
                    str(chunk.get("chunk_id") or "")
                    for chunk in existing["representative_chunks"]
                }
                for chunk in row["chunks"]:
                    if chunk["chunk_id"] in existing_chunk_ids:
                        continue
                    existing["representative_chunks"].append(chunk)
                    existing_chunk_ids.add(chunk["chunk_id"])
                    if len(existing["representative_chunks"]) >= (
                        self.max_chunks_per_paper * max(1, len(existing["coverage_roles"]))
                    ):
                        break
            role_audit.append({
                "role": role,
                "priority": role_row["priority"],
                "query_count": len(role_row["queries"]),
                "candidate_papers": len(role_candidates),
                "selected_papers": len(ranked),
            })

        sources = list(by_paper.values())
        sources, source_scope_audit = self._audit_sources(section, plan, sources)
        selected_by_role = {
            role: sum(role in source.get("coverage_roles", []) for source in sources)
            for role in COVERAGE_ROLES
        }
        for row in role_audit:
            row["selected_after_scope_audit"] = selected_by_role.get(str(row.get("role")), 0)
        gaps = []
        for row in role_audit:
            priority = str(row.get("priority") or "")
            if priority == "not_needed":
                continue
            current = int(row.get("selected_after_scope_audit") or 0)
            target = 3 if priority == "required" else 1
            if current >= target:
                continue
            gaps.append({
                "role": row["role"],
                "priority": priority,
                "queries": next(
                    (plan_row["queries"] for plan_row in plan["roles"] if plan_row["role"] == row["role"]),
                    [],
                ),
                "coverage_question": next(
                    (plan_row["coverage_question"] for plan_row in plan["roles"] if plan_row["role"] == row["role"]),
                    "",
                ),
                "intended_synthesis": next(
                    (plan_row["intended_synthesis"] for plan_row in plan["roles"] if plan_row["role"] == row["role"]),
                    "",
                ),
                "current_papers": current,
                "target_papers": target,
                "missing_papers": target - current,
                "blocking": priority == "required",
                "reason": "insufficient_role_coverage",
            })
        return {
            "schema_version": "section_literature_coverage.v1",
            "plan": plan,
            "sources": sources,
            "coverage_gaps": gaps,
            "role_audit": role_audit,
            "source_scope_audit": source_scope_audit,
            "summary": {
                "source_count": len(sources),
                "paper_count": len({row["paper_id"] for row in sources}),
                "required_roles": [
                    row["role"] for row in plan["roles"] if row["priority"] == "required"
                ],
                "covered_roles": sorted({
                    role for source in sources for role in source["coverage_roles"]
                }),
                "uncovered_required_roles": [
                    row["role"] for row in gaps if row.get("priority") == "required"
                ],
                "undercovered_useful_roles": [
                    row["role"] for row in gaps if row.get("priority") == "useful"
                ],
            },
        }

    def _audit_sources(
        self,
        section: dict[str, Any],
        plan: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        guardrails = [
            _compact(value, 500)
            for value in (
                list(section.get("scope_guardrails") or [])
                + list((section.get("section_contract") or {}).get("scope_guardrails") or [])
            )
            if _compact(value, 500)
        ]
        # Deterministic backstop for explicit exclusions. The LLM performs the
        # nuanced scientific scope judgment; this parser merely ensures that a
        # source named by an "exclude" rule cannot slip through on API failure.
        excluded_terms: set[str] = set()
        for guardrail in guardrails:
            match = re.search(r"\bexclude\b(.+)", guardrail, re.I)
            if not match:
                continue
            tail = re.split(r"[.;]", match.group(1), 1)[0]
            for phrase in re.split(r"\s*(?:,|/|\band\b|\bor\b)\s*", tail, flags=re.I):
                normalized = re.sub(r"\b(?:approaches|methods|systems|structures)\b", "", phrase, flags=re.I)
                normalized = normalized.strip(" :-").lower()
                if len(normalized) >= 4:
                    excluded_terms.add(normalized.rstrip("s"))

        decisions: dict[str, dict[str, Any]] = {}
        for source in sources:
            # Mentioning an excluded platform in related-work prose does not
            # make the paper itself use that platform. The deterministic rule
            # therefore checks identity-bearing metadata only; the LLM sees
            # passages and performs the nuanced scope judgment.
            searchable = " ".join([
                str(source.get("title") or ""),
                str(source.get("venue") or ""),
            ]).lower()
            hits = sorted(term for term in excluded_terms if term in searchable)
            if hits:
                decisions[str(source.get("paper_id") or "")] = {
                    "keep": False,
                    "scope_fit": "out_of_scope",
                    "role_fit": [],
                    "reason": "Explicit section exclusion matched: " + ", ".join(hits),
                    "decision_mode": "deterministic_explicit_exclusion",
                }

        llm_attempt: dict[str, Any] = {"enabled": self.real_llm, "success": False}
        remaining = [
            source for source in sources
            if str(source.get("paper_id") or "") not in decisions
        ]
        if self.real_llm and remaining:
            payload = {
                "section_title": section.get("title") or section.get("section_title") or "",
                "chapter_argument": plan.get("chapter_argument") or "",
                "scope_guardrails": guardrails,
                "literature_role_plan": plan.get("roles") or [],
                "candidate_sources": [
                    {
                        "paper_id": source.get("paper_id"),
                        "title": source.get("title"),
                        "year": source.get("year"),
                        "venue": source.get("venue"),
                        "proposed_roles": source.get("coverage_roles") or [],
                        "representative_passages": [
                            _compact(chunk.get("text_preview"), 500)
                            for chunk in (source.get("representative_chunks") or [])[:2]
                        ],
                    }
                    for source in remaining
                ],
            }
            try:
                result = call_qwen_chat(
                    "SectionCoverageSourceAuditor",
                    [
                        {"role": "system", "content": SOURCE_AUDIT_PROMPT.read_text(encoding="utf-8")},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model_tier="advanced_model",
                    temperature=0,
                    max_tokens=3200,
                    response_format={"type": "json_object"},
                    timeout_seconds=150,
                    max_transport_key_candidates=1,
                    allow_model_fallback=False,
                    accept_partial_stream=False,
                    enable_thinking=False,
                    force_mock=False,
                    max_retries=0,
                )
                parsed = _safe_json(str(result.get("content") or ""))
                rows = parsed.get("source_decisions") if isinstance(parsed.get("source_decisions"), list) else []
                valid_ids = {str(source.get("paper_id") or "") for source in remaining}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    paper_id = str(row.get("paper_id") or "")
                    if paper_id not in valid_ids:
                        continue
                    scope_fit = str(row.get("scope_fit") or "adjacent").lower()
                    if scope_fit not in {"direct", "adjacent", "out_of_scope"}:
                        scope_fit = "adjacent"
                    role_fit = [
                        role for role in (row.get("role_fit") or [])
                        if role in COVERAGE_ROLES
                    ]
                    keep = bool(row.get("keep")) and scope_fit != "out_of_scope" and bool(role_fit)
                    decisions[paper_id] = {
                        "keep": keep,
                        "scope_fit": scope_fit,
                        "role_fit": role_fit,
                        "reason": _compact(row.get("reason"), 500),
                        "decision_mode": "llm_scope_and_role_audit",
                    }
                llm_attempt = {
                    "enabled": True,
                    "success": bool(rows),
                    "returned_decisions": len(rows),
                }
            except Exception as exc:
                llm_attempt = {
                    "enabled": True,
                    "success": False,
                    "error_type": type(exc).__name__,
                }

        kept: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []
        for source in sources:
            paper_id = str(source.get("paper_id") or "")
            decision = decisions.get(paper_id) or {
                "keep": True,
                "scope_fit": "unreviewed" if self.real_llm else "not_run",
                "role_fit": list(source.get("coverage_roles") or []),
                "reason": "No explicit exclusion was triggered.",
                "decision_mode": "deterministic_fallback",
            }
            audit_rows.append({
                "paper_id": paper_id,
                "doi": str(source.get("doi") or ""),
                "title": str(source.get("title") or ""),
                "year": source.get("year"),
                "venue": str(source.get("venue") or ""),
                **decision,
            })
            if not decision["keep"]:
                continue
            kept_source = dict(source)
            kept_source["scope_fit"] = decision["scope_fit"]
            kept_source["coverage_roles"] = list(
                decision.get("role_fit") or kept_source.get("coverage_roles") or []
            )
            kept_source["citation_policy"] = (
                "adjacent_context_only"
                if decision["scope_fit"] == "adjacent"
                else "chapter_context_and_synthesis"
            )
            kept.append(kept_source)
        return kept, {
            "guardrails": guardrails,
            "explicit_excluded_terms": sorted(excluded_terms),
            "input_sources": len(sources),
            "kept_sources": len(kept),
            "rejected_sources": len(sources) - len(kept),
            "llm_attempt": llm_attempt,
            "decisions": audit_rows,
            "summary": {
                "input_sources": len(sources),
                "kept_sources": len(kept),
                "rejected_sources": len(sources) - len(kept),
                "unreviewed_sources": sum(
                    str(row.get("scope_fit") or "") == "unreviewed"
                    for row in audit_rows
                ),
            },
        }

    def audit_candidate_chunks(
        self,
        section: dict[str, Any],
        plan: dict[str, Any],
        chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Audit every paper in the broad claim-candidate pool."""
        active_roles = [
            str(row.get("role") or "")
            for row in (plan.get("roles") or [])
            if isinstance(row, dict) and row.get("priority") != "not_needed"
        ]
        by_paper: dict[str, dict[str, Any]] = {}
        for chunk in chunks:
            if not isinstance(chunk, dict) or not chunk.get("paper_id"):
                continue
            paper_id = str(chunk.get("paper_id") or "")
            source = by_paper.setdefault(paper_id, {
                "paper_id": paper_id,
                "title": str(chunk.get("title") or ""),
                "coverage_roles": list(active_roles),
                "representative_chunks": [],
            })
            if len(source["representative_chunks"]) < 2:
                source["representative_chunks"].append({
                    "chunk_id": str(chunk.get("chunk_id") or ""),
                    "text_preview": _compact(chunk.get("text_preview"), 1200),
                })
        metadata = self._paper_metadata(list(by_paper))
        for paper_id, source in by_paper.items():
            source.update({
                key: value for key, value in (metadata.get(paper_id) or {}).items()
                if value not in (None, "")
            })
        _, audit = self._audit_sources(section, plan, list(by_paper.values()))
        audit["audit_target"] = "broad_claim_candidate_pool"
        return audit

    def audit_external_candidates(
        self,
        section: dict[str, Any],
        plan: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Audit metadata/abstract candidates before spending OA download quota."""
        role = str(
            next(
                (
                    row.get("coverage_role")
                    for row in candidates
                    if isinstance(row, dict) and row.get("coverage_role")
                ),
                "",
            )
        )
        active_roles = [role] if role in COVERAGE_ROLES else [
            str(row.get("role") or "")
            for row in (plan.get("roles") or [])
            if isinstance(row, dict) and row.get("priority") != "not_needed"
        ]
        sources: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            paper_id = str(candidate.get("candidate_id") or "")
            if not paper_id:
                continue
            sources.append({
                "paper_id": paper_id,
                "doi": str(candidate.get("doi") or ""),
                "title": str(candidate.get("title") or ""),
                "year": candidate.get("year"),
                "venue": str(candidate.get("venue") or ""),
                "coverage_roles": list(active_roles),
                "representative_chunks": [{
                    "chunk_id": f"candidate:{paper_id}",
                    "text_preview": _compact(candidate.get("abstract"), 1200),
                }],
            })
        _, audit = self._audit_sources(section, plan, sources)
        audit["audit_target"] = "external_metadata_before_download"
        return audit

    @staticmethod
    def _role_rank_score(
        role: str,
        *,
        lexical_score: float,
        metadata: dict[str, Any],
    ) -> float:
        """Prefer the kind of paper implied by a literature role.

        Lexical relevance remains dominant. Citation count, year, and genre
        only break ties; they never make an off-topic paper relevant.
        """
        try:
            year = int(metadata.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        try:
            citations = max(0, int(metadata.get("citation_count") or 0))
        except (TypeError, ValueError):
            citations = 0
        genre = str(metadata.get("source_genre") or "").lower()
        score = lexical_score
        if role == "foundation":
            score += min(1.5, math.log10(citations + 1) * 0.45)
            if year and year <= 2015:
                score += 0.45
        elif role == "frontier":
            if year:
                score += max(0.0, min(1.2, (year - 2018) * 0.15))
        elif role in {"controversy", "method"} and genre in {
            "review", "perspective", "roadmap", "meta_analysis",
        }:
            score += 0.55
        elif role == "application" and genre == "primary_article":
            score += 0.35
        return round(score, 4)

    def _paper_metadata(self, paper_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not paper_ids or self.kb_path is None:
            return {}
        connection = sqlite3.connect(str(self.kb_path))
        connection.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" for _ in paper_ids)
            rows = connection.execute(
                f"SELECT paper_id,doi,title,year,venue,quality_tier,raw_json "
                f"FROM papers WHERE paper_id IN ({placeholders})",
                paper_ids,
            ).fetchall()
            output: dict[str, dict[str, Any]] = {}
            for row in rows:
                record = dict(row)
                raw: dict[str, Any] = {}
                try:
                    raw = json.loads(str(record.pop("raw_json") or "{}"))
                except Exception:
                    record.pop("raw_json", None)
                record["citation_count"] = int(
                    raw.get("citation_count")
                    or raw.get("cited_by_count")
                    or raw.get("citationCount")
                    or 0
                )
                record["source_genre"] = str(
                    raw.get("source_genre")
                    or raw.get("publication_type")
                    or raw.get("paper_type")
                    or ""
                ).lower()
                output[str(row["paper_id"])] = record
            return output
        finally:
            connection.close()


def coverage_candidate_chunks(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten representative chunks for downstream claim and writing stages."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in coverage.get("sources") or []:
        for chunk in source.get("representative_chunks") or []:
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            result.append(dict(chunk))
    return result


def filter_candidate_chunks_by_coverage_scope(
    chunks: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply chapter scope decisions to the broader claim-candidate pool."""
    scope_audit = coverage.get("source_scope_audit") or {}
    rejected_paper_ids = {
        str(row.get("paper_id") or "")
        for row in (scope_audit.get("decisions") or [])
        if isinstance(row, dict) and not bool(row.get("keep"))
    }
    excluded_terms = {
        str(term).lower() for term in (scope_audit.get("explicit_excluded_terms") or [])
        if str(term).strip()
    }
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        paper_id = str(chunk.get("paper_id") or "")
        searchable = str(chunk.get("title") or "").lower()
        matched_terms = sorted(term for term in excluded_terms if term in searchable)
        if paper_id in rejected_paper_ids or matched_terms:
            rejected.append({
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "paper_id": paper_id,
                "reason": (
                    "paper_rejected_by_chapter_scope_audit"
                    if paper_id in rejected_paper_ids
                    else "explicit_excluded_term:" + ",".join(matched_terms)
                ),
            })
            continue
        kept.append(chunk)
    return kept, {
        "input_chunks": len(chunks),
        "kept_chunks": len(kept),
        "rejected_chunks": len(rejected),
        "rejected": rejected,
    }
