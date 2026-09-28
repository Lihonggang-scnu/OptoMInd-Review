"""M1.5 - ReviewMentorAgent.

The mentor is a writing-architecture advisor, not a fact source.  It reads the
M1 intellectual-moves library learned from top review papers and turns relevant
patterns into operational advice for blueprint planning, claim decomposition,
argument-DAG construction, and targeted gap retrieval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from llm.qwen_chat_client import call_qwen_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ACTIVE_LIBRARY = (
    PROJECT_ROOT
    / "outputs"
    / "review_example_memory"
    / "final_canonical"
    / "intellectual_moves_active_by_category.json"
)
from optomind_research.runtime.skill_guidance import (  # noqa: E402
    CommandKnowledgeBundle,
    SkillMetadataError,
    discover_command_bundles,
)
from optomind_research.runtime.skill_loader import DEFAULT_SKILLS_DIR  # noqa: E402
from optomind_research.move_index import DEFAULT_ENRICHED_LIBRARY  # noqa: E402

DEFAULT_MENTOR_PROMPT = PROJECT_ROOT / "prompts" / "Review Mentor Agent.txt"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "review_mentor"

M1_CATEGORIES = (
    "problem_reframing",
    "central_thesis",
    "taxonomy_design",
    "synthesis_moves",
    "section_progression",
    "paragraph_moves",
    "evidence_critique",
    "disagreement_handling",
    "gap_characterization",
    "figure_argument",
    "top_journal_publishability",
)

# S6/M1.5: versioned command-knowledge manuals loaded alongside the M1 library.
# Skills own workflow, decision gates, and editing discipline; M1 owns concrete
# case-derived moves. Neither is scientific evidence.
DEFAULT_COMMAND_SKILL_IDS = (
    "top-review-architecture",
    "section-review-authoring",
    "global-review-audit",
    "manuscript-integration",
)

# Domain-agnostic specificity signals for borrowed_pattern sanitizer
_UNIT_RE = re.compile(
    r'\b\d+\.?\d*\s*(?:nm|μm|mm|cm|GHz|MHz|THz|kHz|Hz|PHz|eV|meV|keV|mW|W|ns|ps|fs|K|°C)\b',
    re.I,
)
_PCT_RE = re.compile(r'\b\d+\.?\d*\s*%')
_ACRONYM3_RE = re.compile(r'\b[A-Z]{3,}\b')
_PRECISE_NUM_RE = re.compile(r'\b\d{4,}\b')


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def safe_json_parse(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except Exception:
                pass
    return {}


def tokenize(value: Any) -> set[str]:
    stop = {
        "about", "across", "after", "also", "among", "based", "because",
        "between", "claim", "claims", "could", "design", "evidence",
        "from", "into", "paper", "review", "section", "should", "study",
        "that", "their", "these", "this", "through", "what", "which",
        "with", "within", "would",
    }
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9\-]{2,}", str(value or "").lower())
        if token not in stop and len(token) >= 3
    }


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def move_to_text(move: Any) -> str:
    if isinstance(move, dict):
        parts = [
            move.get("transferable_rule") or move.get("reuse_for_our_review_system", ""),
            move.get("trigger_when", ""),
            move.get("move", ""),
            move.get("why_it_matters", ""),
            move.get("possible_overreach", ""),
        ]
        return " ".join(compact(x, 400) for x in parts if x)
    return compact(move, 1000)


@dataclass
class ReviewMentorAgent:
    active_library_path: Path = DEFAULT_ACTIVE_LIBRARY
    prompt_path: Path = DEFAULT_MENTOR_PROMPT
    model_tier: str = "advanced_model"
    real_llm: bool = False
    max_moves_per_category: int = 4
    max_total_moves: int = 28
    use_vector_index: bool = False
    use_three_party: bool = False
    domain_config_path: Path | None = None
    skills_dir: Path | None = None
    command_skill_ids: tuple[str, ...] = DEFAULT_COMMAND_SKILL_IDS
    command_knowledge_strict: bool = False
    _active_library: dict[str, list[Any]] = field(default_factory=dict, init=False)
    _system_prompt: str = field(default="", init=False)
    _move_index: Optional[Any] = field(default=None, init=False)
    _command_bundles: list[CommandKnowledgeBundle] = field(default_factory=list, init=False)
    _command_knowledge_errors: list[str] = field(default_factory=list, init=False)

    def load(self) -> None:
        enriched = Path(DEFAULT_ENRICHED_LIBRARY)
        requested = Path(self.active_library_path)
        # Respect an explicit per-run library.  The previous precedence always
        # selected the repository's enriched PDRC test library when it existed,
        # even if a caller supplied a different-domain M1 library.
        use_default_library = requested.resolve() == Path(DEFAULT_ACTIVE_LIBRARY).resolve()
        path = enriched if use_default_library and enriched.exists() else requested
        self._active_library = load_json(path)
        if Path(self.prompt_path).exists():
            self._system_prompt = Path(self.prompt_path).read_text(encoding="utf-8").strip()
        if self.use_vector_index and self._move_index is None:
            try:
                from optomind_research.move_index import MoveIndex
                idx = MoveIndex()
                if idx.is_built():
                    idx.load()
                    self._move_index = idx
                else:
                    # FAISS index not built yet; fall back to pure-stdlib BM25
                    from optomind_research.move_index import BM25MoveIndex
                    bm25 = BM25MoveIndex()
                    bm25.build(self._active_library)
                    self._move_index = bm25
            except Exception:
                self._move_index = None
        self.load_command_knowledge()

    def load_command_knowledge(self) -> None:
        """Load and validate the versioned command-knowledge skill bundles.

        Fail-closed when ``command_knowledge_strict`` is True (production
        callers that require the manuals).  Otherwise the failure is recorded
        in ``_command_knowledge_errors`` and surfaced explicitly in the advice
        payload; it is never silently replaced by M1 case moves.
        """

        self._command_bundles = []
        self._command_knowledge_errors = []
        skills_root = (
            Path(self.skills_dir)
            if self.skills_dir is not None
            else DEFAULT_SKILLS_DIR
        )
        requested: Optional[tuple[str, ...]] = None
        if self.command_skill_ids:
            ids = tuple(str(item) for item in self.command_skill_ids)
            if "*" not in ids:
                requested = ids
        try:
            bundles = discover_command_bundles(
                skills_root,
                names=requested,
                strict=True,
            )
            found = {bundle.name for bundle in bundles}
            if requested is not None:
                missing = sorted(set(requested) - found)
                if missing:
                    raise SkillMetadataError(
                        "command-knowledge skill(s) not found: "
                        + ", ".join(missing)
                    )
            self._command_bundles = list(bundles)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._command_knowledge_errors.append(message)
            if self.command_knowledge_strict:
                raise

    def _command_knowledge_prompt_block(self) -> str:
        """Compact prompt-injection text from validated command bundles."""

        if self._command_knowledge_errors:
            return ""
        return "\n\n".join(
            bundle.to_prompt_block() for bundle in self._command_bundles
        )

    def _command_knowledge_result(self) -> dict[str, Any]:
        """Structured command-knowledge payload with provenance and digests."""

        if self._command_knowledge_errors:
            return {
                "status": "error",
                "diagnosis": "; ".join(self._command_knowledge_errors),
                "fallback_used": False,
                "precedence": "unavailable",
                "evidence_prohibition": True,
                "skills": [],
                "prompt_block": "",
            }
        return {
            "status": "ok",
            "precedence": "process_and_judgment",
            "evidence_prohibition": True,
            "evidence_prohibition_text": (
                "Command knowledge is operational instruction for workflow, "
                "decision gates, and editing discipline. It is never scientific "
                "evidence and cannot resolve facts, measurements, mechanisms, or "
                "citations. Papers and material dossiers are the only basis for "
                "scientific claims and citations."
            ),
            "skills": [
                {
                    "name": bundle.name,
                    "version": bundle.version,
                    "role": bundle.role,
                    "digest": hashlib.sha256(
                        bundle.instructions.encode("utf-8")
                    ).hexdigest(),
                    "provenance": {
                        "schema_version": bundle.provenance.schema_version,
                        "skill_version": bundle.provenance.skill_version,
                        "adoption_mode": bundle.provenance.adoption_mode,
                        "applicability": bundle.provenance.applicability,
                        "evidence_prohibition": bundle.provenance.evidence_prohibition,
                        "source": bundle.provenance.source,
                        "influences": [
                            {
                                "project": item.get("project", ""),
                                "repository": item.get("repository", "unknown"),
                                "license": item.get("license", "unknown"),
                                "adoption_mode": item.get("adoption_mode", ""),
                            }
                            for item in bundle.provenance.influences
                        ],
                    },
                    "instructions": bundle.instructions,
                }
                for bundle in self._command_bundles
            ],
            "prompt_block": self._command_knowledge_prompt_block(),
        }

    @staticmethod
    def _workflow_precedence_policy() -> dict[str, Any]:
        """Workflow/judgment precedence among guidance layers only.

        This contract ranks command knowledge above M1 case moves for workflow,
        decision gates, and editing discipline. It is orthogonal to evidence
        authority and deliberately does not rank scientific evidence.
        """

        return {
            "contract": "workflow_and_judgment_precedence",
            "scope": "guidance_layers_only",
            "order": ["command_knowledge", "m1_case_moves"],
            "command_knowledge": (
                "Versioned skill manuals define workflow, decision gates, and "
                "editing discipline; they take process and judgment precedence "
                "over case-derived moves."
            ),
            "m1_case_moves": (
                "Concrete writing moves derived from top-review cases complement "
                "command knowledge and never override it for workflow decisions."
            ),
            "does_not_rank_evidence": True,
        }

    @staticmethod
    def _evidence_authority_policy() -> dict[str, Any]:
        """Exclusive scientific-evidence authority contract.

        Papers and material dossiers are the only admissible evidence source.
        When command knowledge or M1 moves conflict with the scientific record,
        evidence wins; without admissible support, the claim is refused.
        """

        return {
            "contract": "scientific_evidence_authority",
            "authority": "papers_and_material_dossiers",
            "exclusive": True,
            "conflict_policy": "evidence_wins_or_claim_refused",
            "cannot_resolve": ["facts", "measurements", "mechanisms", "citations"],
            "statement": (
                "Only papers and material dossiers may establish scientific facts, "
                "measurements, mechanisms, and citations. Command knowledge and M1 "
                "moves cannot resolve any of them; on conflict, evidence wins or the "
                "unsupported claim is refused."
            ),
        }

    @staticmethod
    def _command_knowledge_boundary() -> str:
        """Prompt-facing boundary: command knowledge cannot resolve science."""

        return (
            "Command knowledge defines workflow, decision gates, and editing "
            "discipline only. It cannot resolve scientific facts, measurements, "
            "mechanisms, or citations. Only papers and material dossiers have "
            "evidence authority; on conflict, evidence wins or the claim is "
            "refused."
        )

    @staticmethod
    def _m1_case_moves_result(
        selected: dict[str, list[dict[str, str]]],
        usable: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Structured M1 case-move payload, separate from command knowledge."""

        return {
            "status": "ok" if any(selected.values()) else "empty",
            "precedence": "concrete_case_moves",
            "evidence_prohibition": True,
            "note": (
                "Concrete case-derived writing moves from the M1 library; they "
                "complement command knowledge and are never scientific evidence."
            ),
            "selected_move_counts": {
                category: len(rows) for category, rows in selected.items()
            },
            "moves": selected,
            "usable_intellectual_moves": usable,
        }

    def build_advice(
        self,
        *,
        user_question: str,
        problem_understanding: str = "",
        scope_definition: str = "",
        planning_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.load()
        if self._command_knowledge_errors and self.real_llm:
            raise SkillMetadataError(
                "command-knowledge integration failed in production mode: "
                + "; ".join(self._command_knowledge_errors)
            )
        selected = self.select_moves(
            " ".join([user_question, problem_understanding, scope_definition, json.dumps(planning_evidence or {}, ensure_ascii=False)[:4000]])
        )
        # For LLM: only expose abstract fields (transferable_rule, trigger_when, bad_pattern_to_avoid)
        # to prevent domain-contamination of borrowed_pattern. Keep full 'selected' for deterministic fallback.
        filtered_for_llm = self._filter_moves_for_llm(selected)
        try:
            from optomind_research.domain_config_loader import (
                get_anti_patterns,
                get_key_tensions,
                get_review_style_context,
                load_domain_config,
            )
            if self.domain_config_path is not None:
                domain_cfg = load_domain_config(self.domain_config_path)
                domain_writing_context = {
                    "target_review_context": get_review_style_context(domain_cfg),
                    "domain_anti_patterns": get_anti_patterns(domain_cfg),
                    "domain_key_tensions": get_key_tensions(domain_cfg),
                }
            else:
                # The repository-level config is a worked example, not a
                # universal default.  Omitting it prevents a new optical topic
                # from inheriting PDRC-specific tensions by accident.
                domain_writing_context = {
                    "target_review_context": "top-tier scientific review",
                    "domain_anti_patterns": [],
                    "domain_key_tensions": [],
                }
        except Exception:
            domain_writing_context = {}
        payload = {
            "user_question": compact(user_question, 800),
            "problem_understanding": compact(problem_understanding, 800),
            "scope_definition": compact(scope_definition, 800),
            "selected_intellectual_moves": filtered_for_llm,
            "planning_evidence_summary": self._planning_evidence_summary(planning_evidence or {}),
            "domain_writing_context": domain_writing_context,
            "command_knowledge_prompt": self._command_knowledge_prompt_block(),
            "workflow_precedence": self._workflow_precedence_policy(),
            "evidence_authority": self._evidence_authority_policy(),
            "command_knowledge_boundary": self._command_knowledge_boundary(),
            "m1_case_moves": {
                "status": "ok" if any(selected.values()) else "empty",
                "precedence": "concrete_case_moves",
                "evidence_prohibition": True,
                "note": (
                    "Concrete case-derived writing moves; complement command "
                    "knowledge; never scientific evidence."
                ),
                "selected_move_counts": {
                    category: len(rows)
                    for category, rows in selected.items()
                },
                "selected_intellectual_moves": filtered_for_llm,
            },
        }
        if self.real_llm and self._system_prompt:
            attempt_notes: list[dict[str, Any]] = []
            for attempt in range(2):
                call_payload = dict(payload)
                if attempt:
                    call_payload["repair_instruction"] = (
                        "The previous response was invalid or incomplete. Return one complete JSON object matching every required field; keep lists concise."
                    )
                try:
                    result = call_qwen_chat(
                        "ReviewMentorAgent",
                        [
                            {"role": "system", "content": self._system_prompt},
                            {"role": "user", "content": json.dumps(call_payload, ensure_ascii=False)},
                        ],
                        model_tier=self.model_tier,
                        temperature=0.2,
                        max_tokens=2200,
                        response_format={"type": "json_object"},
                        force_mock=False,
                        max_retries=0,
                    )
                    raw = str(result.get("content") or "")
                    parsed = safe_json_parse(raw)
                    required = {
                        "mentor_summary",
                        "usable_intellectual_moves",
                        "m2a_claim_decomposition_advice",
                        "m2b_argument_dag_advice",
                        "m3_gap_resolution_advice",
                        "visual_argument_advice",
                        "quality_risks",
                    }
                    missing = sorted(required - set(parsed)) if parsed else sorted(required)
                    attempt_notes.append({
                        "attempt": attempt + 1,
                        "raw_chars": len(raw),
                        "parsed": bool(parsed),
                        "missing_fields": missing,
                    })
                    if parsed and not missing:
                        advice = self._normalize_advice(
                            parsed,
                            selected,
                            llm_usage=result.get("_llm_usage", {}),
                            mode="real_llm",
                        )
                        advice["_llm_attempts"] = attempt_notes
                        # T3: three-party convergence augmentation
                        if self.use_three_party:
                            convergence = ThreePartyMentorConvergence(model_tier=self.model_tier)
                            conv_out = convergence.run(payload=payload, selected_moves=selected)
                            advice["three_party_convergence"] = conv_out
                        return advice
                except Exception as exc:
                    attempt_notes.append({
                        "attempt": attempt + 1,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
            fallback = self._deterministic_advice(selected, payload, mode="deterministic_fallback")
            fallback["_llm_attempts"] = attempt_notes
            return fallback

        return self._deterministic_advice(selected, payload, mode="deterministic")

    @staticmethod
    def _move_to_row(move: Any, category: str, score: float) -> dict[str, Any]:
        d = move if isinstance(move, dict) else {}
        return {
            "category": category,
            "move": compact(d.get("move", "") if d else str(move), 700),
            "why_it_matters": compact(d.get("why_it_matters", ""), 500),
            "reuse_hint": compact(d.get("reuse_for_our_review_system", ""), 500),
            # T3 Teaching Card fields — domain-agnostic, abstract
            "transferable_rule": compact(d.get("transferable_rule", ""), 500),
            "trigger_when": compact(d.get("trigger_when") or d.get("trigger_condition", ""), 400),
            "input_tension": compact(d.get("input_tension", ""), 400),
            "reasoning_transition": compact(d.get("reasoning_transition", ""), 400),
            "argument_structure": compact(d.get("argument_structure", ""), 400),
            "success_signal": compact(d.get("success_signal") or d.get("why_it_matters", ""), 400),
            "failure_mode": compact(d.get("failure_mode") or d.get("bad_pattern_to_avoid", ""), 400),
            "bad_pattern_to_avoid": compact(d.get("bad_pattern_to_avoid", ""), 400),
            "downstream_hooks": list(d.get("downstream_hooks") or []),
            "example_transformation": d.get("example_transformation") if isinstance(d.get("example_transformation"), dict) else {},
            "source_paper_id": compact(d.get("source_paper_id", ""), 200),
            "source_spans": list(d.get("source_spans") or []),
            "retrieval_score": round(float(score), 4),
        }

    @staticmethod
    def _filter_moves_for_llm(selected: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
        """Extract only abstract, domain-agnostic fields for LLM input to prevent flattening."""
        filtered: dict[str, list[dict[str, str]]] = {}
        for cat, rows in selected.items():
            filtered[cat] = [
                {
                    "category": row.get("category", cat),
                    "transferable_rule": row.get("transferable_rule", ""),
                    "trigger_when": row.get("trigger_when", ""),
                    "bad_pattern_to_avoid": row.get("bad_pattern_to_avoid", ""),
                    "downstream_hooks": row.get("downstream_hooks", []),
                    "example_transformation": row.get("example_transformation", {}),
                    "retrieval_score": row.get("retrieval_score", ""),
                }
                for row in rows
            ]
        return filtered

    @staticmethod
    def _sanitize_borrowed_pattern(pattern: str, *, transferable_rule: str = "") -> tuple[str, bool]:
        """Domain-agnostic contamination detection via structural specificity signals.

        Uses regex patterns rather than a domain vocabulary list so detection works
        for any research field (optics, biology, CS, etc.).

        Signals (any ONE is sufficient to trigger replacement):
          - Physical-unit measurements: "1550 nm", "75 MHz"
          - Percentages: "10%"
          - Technical acronyms: "WGM", "TMDC", "TIR" (3+ caps, optionally plural)
          - 4+ digit precise values: "1550", "3000"

        Threshold = 1: a single specificity signal is enough — abstract transferable_rules
        never contain measurement values, domain acronyms, or precise numeric literals.

        Returns:
            (sanitized_pattern, was_contaminated)
        """
        has_signal = (
            bool(_UNIT_RE.search(pattern))
            or bool(_PCT_RE.search(pattern))
            or bool(_ACRONYM3_RE.search(pattern))
            or bool(_PRECISE_NUM_RE.search(pattern))
        )
        if has_signal and transferable_rule.strip():
            return (transferable_rule, True)
        return (pattern, False)

    def _vector_select_moves(self, context: str) -> dict[str, list[dict[str, str]]]:
        """Vector-based move selection using MoveIndex. Returns same structure as select_moves."""
        fetch_k = self.max_total_moves * 3
        hits = self._move_index.query(context, top_k=fetch_k)

        # Group by category, keep max_moves_per_category per category
        by_cat: dict[str, list[dict]] = {cat: [] for cat in M1_CATEGORIES}
        for hit in hits:
            cat = hit.get("category", "")
            if cat not in by_cat:
                continue
            if len(by_cat[cat]) < self.max_moves_per_category:
                by_cat[cat].append(hit)

        selected: dict[str, list[dict[str, str]]] = {cat: [] for cat in M1_CATEGORIES}
        total = 0
        # Round-robin prevents the fixed category order from starving later
        # writing moves such as gap_characterization and figure_argument.
        for depth in range(self.max_moves_per_category):
            for cat in M1_CATEGORIES:
                if total >= self.max_total_moves:
                    break
                if depth >= len(by_cat[cat]):
                    continue
                hit = by_cat[cat][depth]
                selected[cat].append(self._move_to_row(hit, cat, hit.get("retrieval_score", 0.0)))
                total += 1
            if total >= self.max_total_moves:
                break
        return selected

    def select_moves(self, context: str) -> dict[str, list[dict[str, str]]]:
        if self.use_vector_index and self._move_index is not None:
            return self._vector_select_moves(context)
        context_tokens = tokenize(context)
        ranked: dict[str, list[tuple[float, Any]]] = {}
        for category in M1_CATEGORIES:
            raw_moves = self._active_library.get(category) or []
            scored: list[tuple[float, int, Any]] = []
            for idx, move in enumerate(raw_moves):
                text = move_to_text(move)
                tokens = tokenize(text)
                overlap = len(context_tokens & tokens)
                score = overlap / max(1, len(context_tokens) ** 0.5) + min(0.3, len(tokens) / 400.0)
                if isinstance(move, dict) and move.get("reuse_for_our_review_system"):
                    score += 0.15
                if isinstance(move, dict) and move.get("trigger_when"):
                    trigger = tokenize(move.get("trigger_when", ""))
                    if trigger:
                        trigger_overlap = len(context_tokens & trigger)
                        score += 0.3 * trigger_overlap / max(1, len(trigger) ** 0.5)
                scored.append((score, idx, move))
            scored.sort(key=lambda x: x[0], reverse=True)
            ranked[category] = [(score, move) for score, _, move in scored[: self.max_moves_per_category]]
        selected: dict[str, list[dict[str, str]]] = {cat: [] for cat in M1_CATEGORIES}
        total = 0
        for depth in range(self.max_moves_per_category):
            for category in M1_CATEGORIES:
                if total >= self.max_total_moves:
                    break
                if depth >= len(ranked.get(category, [])):
                    continue
                score, move = ranked[category][depth]
                selected[category].append(self._move_to_row(move, category, score))
                total += 1
            if total >= self.max_total_moves:
                break
        return selected

    @staticmethod
    def _planning_evidence_summary(evidence: dict[str, Any]) -> dict[str, Any]:
        concepts = evidence.get("concept_nodes") or evidence.get("selected_concept_nodes") or []
        texts = evidence.get("text_anchors") or evidence.get("retrieved_text_chunks") or []
        visuals = evidence.get("visual_anchors") or evidence.get("retrieved_visual_chunks") or []
        clusters = evidence.get("clusters") or evidence.get("cluster_candidates") or []
        def label_of(value: Any) -> str:
            if isinstance(value, dict):
                return compact(value.get("label") or value.get("name") or value.get("title"), 120)
            return compact(value, 120)

        explicit_cluster_count = evidence.get("cluster_count")
        return {
            "concept_count": len(concepts),
            "text_anchor_count": len(texts),
            "visual_anchor_count": len(visuals),
            "cluster_count": int(explicit_cluster_count) if explicit_cluster_count is not None else len(clusters),
            "top_labels": [
                label_of(x)
                for x in concepts[:10]
                if label_of(x)
            ],
        }

    def _normalize_advice(
        self,
        parsed: dict[str, Any],
        selected: dict[str, list[dict[str, str]]],
        *,
        llm_usage: dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        def list_of_strings(key: str, limit: int = 8) -> list[str]:
            raw = parsed.get(key) if isinstance(parsed.get(key), list) else []
            return [compact(x, 500) for x in raw if compact(x, 500)][:limit]

        # Build lookup: category → list of transferable_rules for sanitizer
        transferable_rules_by_cat: dict[str, list[str]] = {}
        for cat, rows in selected.items():
            transferable_rules_by_cat[cat] = [row.get("transferable_rule", "") for row in rows]

        # Apply domain-contamination sanitizer to LLM-generated borrowed_patterns
        sanitization_stats = {"total": 0, "contaminated": 0}
        moves = parsed.get("usable_intellectual_moves") if isinstance(parsed.get("usable_intellectual_moves"), list) else []
        norm_moves = []
        cat_counters = {cat: 0 for cat in M1_CATEGORIES}

        for item in moves[:12]:
            if not isinstance(item, dict):
                continue
            cat = str(item.get("category") or "")
            if cat not in M1_CATEGORIES:
                continue

            borrowed_pattern = compact(item.get("borrowed_pattern"), 600)

            # Lookup transferable_rule from selected moves (by category index)
            idx = cat_counters[cat]
            transferable_rule = ""
            if cat in transferable_rules_by_cat and idx < len(transferable_rules_by_cat[cat]):
                transferable_rule = transferable_rules_by_cat[cat][idx]

            # Apply sanitizer
            sanitized_pattern, was_contaminated = self._sanitize_borrowed_pattern(
                borrowed_pattern, transferable_rule=transferable_rule
            )

            sanitization_stats["total"] += 1
            if was_contaminated:
                sanitization_stats["contaminated"] += 1

            norm_moves.append(
                {
                    "category": cat,
                    "borrowed_pattern": sanitized_pattern,
                    "adaptation_for_this_review": compact(item.get("adaptation_for_this_review"), 600),
                }
            )

            cat_counters[cat] += 1

        result = {
            "schema_version": "review_mentor_advice.v1",
            "created_at": utc_now(),
            "mode": mode or ("real_llm" if self.real_llm else "deterministic"),
            "mentor_summary": compact(parsed.get("mentor_summary"), 1200),
            "usable_intellectual_moves": norm_moves,
            "m2a_claim_decomposition_advice": list_of_strings("m2a_claim_decomposition_advice"),
            "m2b_argument_dag_advice": list_of_strings("m2b_argument_dag_advice"),
            "m3_gap_resolution_advice": list_of_strings("m3_gap_resolution_advice"),
            "visual_argument_advice": list_of_strings("visual_argument_advice"),
            "quality_risks": list_of_strings("quality_risks"),
            "recommended_gap_query_patterns": parsed.get("recommended_gap_query_patterns")
            if isinstance(parsed.get("recommended_gap_query_patterns"), list)
            else [],
            "selected_move_counts": {k: len(v) for k, v in selected.items()},
            "selected_moves_digest": selected,
            "_llm_usage": llm_usage or {},
            "_sanitization_stats": sanitization_stats,
        }
        result["command_knowledge"] = self._command_knowledge_result()
        result["m1_case_moves"] = self._m1_case_moves_result(selected, norm_moves)
        result["workflow_precedence"] = self._workflow_precedence_policy()
        result["evidence_authority"] = self._evidence_authority_policy()
        result["command_knowledge_boundary"] = self._command_knowledge_boundary()
        return result

    def _deterministic_advice(
        self,
        selected: dict[str, list[dict[str, str]]],
        payload: dict[str, Any],
        *,
        mode: str = "deterministic",
    ) -> dict[str, Any]:
        usable = []
        for cat in M1_CATEGORIES:
            for row in selected.get(cat, [])[:2]:
                usable.append(
                    {
                        "category": cat,
                        "borrowed_pattern": row.get("move", ""),
                        "adaptation_for_this_review": row.get("reuse_hint") or row.get("why_it_matters") or "Use this as a writing-organization pattern, not as factual evidence.",
                    }
                )
                if len(usable) >= 10:
                    break
            if len(usable) >= 10:
                break
        parsed = {
            "mentor_summary": (
                "Frame the review around a small number of load-bearing claims, make the opening problem reframing explicit, "
                "use taxonomy only when it exposes decision criteria, and reserve additional retrieval for gaps that would change a section's argument."
            ),
            "usable_intellectual_moves": usable,
            "m2a_claim_decomposition_advice": [
                "Decompose each section into claims that do distinct intellectual jobs: mechanism, measurement, comparison, and application should not be collapsed into one vague statement.",
                "Mark weakly supported but important claims as low-saturation rather than inventing stronger support.",
                "Prefer claims that can become paragraph-level load-bearing sentences in the final review.",
            ],
            "m2b_argument_dag_advice": [
                "Create edges only when one claim logically enables, constrains, motivates, or applies another; topic similarity is not enough.",
                "Use problem-reframing and taxonomy moves as high-level organizers, but do not let them become unsupported scientific claims.",
            ],
            "m3_gap_resolution_advice": [
                "Trigger targeted retrieval for low-saturation claims that are central to the review thesis or section transition.",
                "Stop retrieval when at least one directly relevant OA full-text route or a small set of credible metadata leads can support the claim's next revision.",
            ],
            "visual_argument_advice": [
                "Use figures to prove mechanisms, comparisons, trends, or limitations; decorative or merely representative figures should not carry key claims.",
                "If a figure is central to a claim, pair it with nearby text and caption evidence before using it in a section argument.",
            ],
            "quality_risks": [
                "A broad taxonomy can become encyclopedic unless every category supports the review thesis.",
                "Gap lists can become endless; prioritize gaps with high marginal value for the current review.",
            ],
            "recommended_gap_query_patterns": [
                {
                    "purpose": "Find direct mechanism support for a low-saturation claim.",
                    "query_pattern": "{core mechanism phrase} {material or system} experimental evidence review OR full text",
                },
                {
                    "purpose": "Trace a claim back to its root citation when a review only mentions it briefly.",
                    "query_pattern": "{exact claim phrase} cited by {seed paper title or DOI}",
                },
            ],
        }
        return self._normalize_advice(parsed, selected, mode=mode)


class ThreePartyMentorConvergence:
    """T3: Three-party structured deliberation for review planning.

    Parties:
      ReviewArchitect  — proposes the most intellectually valuable structure
      PerspectiveCritic — checks for missing perspectives, scope drift, marginal-value issues
      ReviewMentor     — validates that the plan has top-review intellectual moves

    Max 2 rounds. Produces adopted/rejected suggestion ledger so changes are auditable.
    Only runs when ReviewMentorAgent.use_three_party=True and real_llm=True.
    """

    ARCHITECT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "Review Architect Agent.txt"
    CRITIC_PROMPT_PATH = PROJECT_ROOT / "prompts" / "Perspective Critic Agent.txt"

    def __init__(self, model_tier: str = "advanced_model") -> None:
        self.model_tier = model_tier

    def run(
        self,
        *,
        payload: dict[str, Any],
        selected_moves: dict[str, list[dict[str, str]]],
        max_rounds: int = 2,
    ) -> dict[str, Any]:
        """Run three-party deliberation. Returns convergence metadata dict."""
        architect_payload = dict(payload)
        architect_payload["selected_intellectual_moves"] = selected_moves
        architect_proposal = self._call_architect(architect_payload)
        critic_feedback = self._call_critic(architect_payload, architect_proposal)

        high_severity = [
            i for i in (critic_feedback.get("issues") or [])
            if isinstance(i, dict) and str(i.get("severity") or "low") in ("high", "critical")
        ]

        rounds_done = 1
        if high_severity and max_rounds >= 2:
            architect_proposal = self._revise_architect(architect_payload, architect_proposal, critic_feedback)
            rounds_done = 2

        adopted: list[str] = []
        rejected: list[str] = []
        rejection_reasons: dict[str, str] = {}
        for sugg in (critic_feedback.get("suggestions") or []):
            if isinstance(sugg, str):
                sugg = {"suggestion": sugg, "severity": "high"}
            if not isinstance(sugg, dict):
                continue
            text = compact(sugg.get("suggestion") or sugg.get("text"), 200)
            if not text:
                continue
            severity = str(sugg.get("severity") or "low")
            if severity in ("high", "critical") and rounds_done >= 2:
                adopted.append(text)
            else:
                rejected.append(text)
                reason = compact(sugg.get("rejection_reason") or "Insufficient marginal value to alter the planned structure.", 300)
                rejection_reasons[text] = reason

        return {
            "architect_proposal_summary": compact(
                architect_proposal.get("architect_proposal_summary")
                or architect_proposal.get("structure_summary"), 500
            ),
            "section_proposals": list(architect_proposal.get("section_proposals") or [])[:8],
            "critic_perspective_gaps": [compact(g, 240) for g in (critic_feedback.get("perspective_gaps") or []) if g][:5],
            "adopted_suggestions": adopted[:6],
            "rejected_suggestions": rejected[:6],
            "rejection_reasons": rejection_reasons,
            "rounds_completed": rounds_done,
            "final_blueprint_changes": [
                compact(c, 240)
                for c in (
                    architect_proposal.get("blueprint_changes")
                    or architect_proposal.get("section_proposals")
                    or []
                ) if c
            ][:8],
        }

    def _call_architect(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.ARCHITECT_PROMPT_PATH.exists():
            return {"structure_summary": "ReviewArchitect prompt not configured.", "blueprint_changes": []}
        system_prompt = self.ARCHITECT_PROMPT_PATH.read_text(encoding="utf-8")
        try:
            result = call_qwen_chat(
                "ReviewArchitectAgent",
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:8000]},
                ],
                model_tier=self.model_tier,
                temperature=0.2,
                max_tokens=1500,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            raw = safe_json_parse(str(result.get("content") or "")) or {}
            return {
                **raw,
                "architect_proposal_summary": raw.get("architect_proposal_summary") or raw.get("structure_summary") or "",
                "section_proposals": list(raw.get("section_proposals") or []),
                "blueprint_changes": list(raw.get("blueprint_changes") or raw.get("section_proposals") or []),
            }
        except Exception:
            return {}

    def _call_critic(self, payload: dict[str, Any], architect_proposal: dict[str, Any]) -> dict[str, Any]:
        if not self.CRITIC_PROMPT_PATH.exists():
            return {"issues": [], "suggestions": [], "perspective_gaps": []}
        system_prompt = self.CRITIC_PROMPT_PATH.read_text(encoding="utf-8")
        critic_payload = {**payload, "architect_proposal": architect_proposal}
        try:
            result = call_qwen_chat(
                "PerspectiveCriticAgent",
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(critic_payload, ensure_ascii=False)[:8000]},
                ],
                model_tier=self.model_tier,
                temperature=0.2,
                max_tokens=1500,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            raw = safe_json_parse(str(result.get("content") or "")) or {}
            # Normalize field name variants across prompt versions:
            #   prompt v1: perspective_gaps / scope_drift_warnings / high_value_suggestions
            #   code expects: issues / suggestions / perspective_gaps
            issues = list(raw.get("issues") or [])
            for gap in raw.get("perspective_gaps") or []:
                issues.append({"issue": compact(gap, 240), "severity": "high"})
            for warning in raw.get("scope_drift_warnings") or []:
                if isinstance(warning, dict):
                    issues.append({**warning, "severity": warning.get("severity", "high")})
                elif warning:
                    issues.append({"issue": compact(warning, 240), "severity": "high"})
            suggestions = list(raw.get("suggestions") or [])
            for suggestion in raw.get("high_value_suggestions") or []:
                suggestions.append({"suggestion": compact(suggestion, 240), "severity": "high"})
            perspective_gaps = raw.get("perspective_gaps") or []
            return {"issues": issues, "suggestions": suggestions, "perspective_gaps": perspective_gaps}
        except Exception:
            return {"issues": [], "suggestions": [], "perspective_gaps": []}

    def _revise_architect(
        self, payload: dict[str, Any], original: dict[str, Any], critique: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.ARCHITECT_PROMPT_PATH.exists():
            return original
        system_prompt = self.ARCHITECT_PROMPT_PATH.read_text(encoding="utf-8")
        revision_payload = {
            **payload,
            "round": 2,
            "original_proposal": original,
            "critic_issues": critique.get("issues", []),
            "required_revisions": critique.get("suggestions", []),
        }
        try:
            result = call_qwen_chat(
                "ReviewArchitectAgent_round2",
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(revision_payload, ensure_ascii=False)[:8000]},
                ],
                model_tier=self.model_tier,
                temperature=0.2,
                max_tokens=1500,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            return safe_json_parse(str(result.get("content") or "")) or original
        except Exception:
            return original


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run M1.5 ReviewMentorAgent.")
    parser.add_argument("--user-question", required=True)
    parser.add_argument("--problem-understanding", default="")
    parser.add_argument("--scope-definition", default="")
    parser.add_argument("--active-library", type=Path, default=DEFAULT_ACTIVE_LIBRARY)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_MENTOR_PROMPT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--real-llm", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--model-tier", default="advanced_model")
    parser.add_argument("--domain-config", type=Path, default=None)
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--use-vector-index", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--use-three-party", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args(argv)

    agent = ReviewMentorAgent(
        active_library_path=args.active_library,
        prompt_path=args.prompt,
        model_tier=args.model_tier,
        real_llm=bool(args.real_llm),
        domain_config_path=args.domain_config,
        skills_dir=args.skills_dir,
        use_vector_index=bool(args.use_vector_index),
        use_three_party=bool(args.use_three_party),
    )
    advice = agent.build_advice(
        user_question=args.user_question,
        problem_understanding=args.problem_understanding,
        scope_definition=args.scope_definition,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "review_mentor_advice.json"
    out.write_text(json.dumps(advice, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(out), "mode": advice.get("mode")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
