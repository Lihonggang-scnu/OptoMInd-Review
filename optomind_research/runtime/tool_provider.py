"""Tool provider extension point for ResearchWorker.

Allows Phase 2+ tool sets to be injected into ResearchWorker without
modifying its core. Default behavior (Phase 1) is unchanged when
tool_provider=None.
"""

from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .coverage_decision_contract import normalize_pipeline_structure


class ToolProvider(ABC):
    """Abstract base for any tool extension injected into ResearchWorker."""

    @abstractmethod
    def get_tools(self, work_dir: Path) -> list:
        """Return a list of FunctionTool instances bound to work_dir."""
        ...

    @abstractmethod
    def get_allowed_tool_names(self) -> List[str]:
        """Return the canonical tool names this provider exposes."""
        ...

    def try_auto_finalize(self) -> Optional[str]:
        """Return a trusted deterministic validation result when ready.

        Providers may override this hook when persisted artifacts are enough
        to decide completion without another model turn.  ``None`` means the
        task is not ready; only ``VALIDATION_PASSED`` is terminal.
        """

        return None


@dataclass
class SectionCoverageContext:
    """Immutable bindings for a single section-coverage task.

    The agent receives only opaque IDs (section_id, candidate_id, paper_id).
    All path resolution happens inside the tools — the model never touches
    a filesystem path directly.
    """

    section_id: str
    section_data: Dict[str, Any]          # raw section dict from blueprint
    kb_sqlite: Optional[Path]             # read-only knowledge base (may be None)
    temp_kb_sqlite: Path                  # writable staging KB for this run
    work_dir: Path                        # task working directory

    # Phase 3 -> Phase 2 bridge.  These are explicit, read-only declarations
    # of material already selected by the argument layer.  They are separate
    # from ``kb_sqlite`` because a section may draw from more than one shared
    # migrated KB and because the section overlay is not a global fact.
    shared_kb_sqlite_paths: List[Path] = field(default_factory=list)
    source_ledger_path: Optional[Path] = None
    section_overlay_path: Optional[Path] = None
    selected_paper_ids: List[str] = field(default_factory=list)
    selected_chunk_ids: List[str] = field(default_factory=list)
    selected_permissions: Dict[str, str] = field(default_factory=dict)
    selected_content_depths: Dict[str, str] = field(default_factory=dict)

    # Hard budget limits — set by caller for minimal smoke mode.
    # None / default values = no restriction (normal full-coverage mode).
    min_mode_allowed_role: Optional[str] = field(default=None)
    min_mode_max_queries: int = field(default=4)       # max queries per search_oa_candidates call
    min_mode_max_per_backend: int = field(default=10)  # max results per backend per query
    min_mode_max_total_papers: int = field(default=5)  # max papers materialized across all calls
    # Durable per-role search ceiling.  Unlike prompt-only guidance, this is
    # enforced by the tool and survives process restart/resume.
    max_search_rounds_per_role: int = field(default=3)
    # A single full-text acquisition can involve several HTTP routes, document
    # parsing, and figure extraction.  Limit one tool invocation before the
    # ReAct worker gets an opportunity to re-evaluate whether another paper is
    # genuinely needed.  This is deliberately a wall-clock bound rather than
    # an LLM-budget proxy.
    max_materialization_seconds_per_call: int = field(default=170)
    # Directly constructed legacy/test contexts keep the historical adapter.
    # The production orchestrator explicitly enables the S2-first route.
    s2_first_enabled: bool = field(default=False)
    # Serialized ReviewModeContract injected by the section orchestrator.
    # Keeping it inside section_data/context preserves compatibility with
    # callers that construct this context directly.
    review_quality_contract: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalize only string values at the boundary.  This repairs known
        # high-confidence mojibake in inherited blueprints while preserving
        # valid scientific Unicode such as μ, α, β, and em dashes.
        self.section_data = normalize_pipeline_structure(self.section_data)
    # Phase 3 may provide a focused request.  These fields are deterministic
    # tool constraints, not prompt suggestions.
    phase3_coverage_request: Dict[str, Any] = field(default_factory=dict)

    # Phase 2.1 short path state.  The flags are deterministic runtime state,
    # not model instructions.  They also make it possible to forbid citation
    # tracing until an audited materialization attempt has happened.
    short_path_mode: bool = field(default=False)
    resume_candidate_ledger_path: Optional[Path] = None
    # Optional state shared by bounded waves.  It is separate from the
    # candidate ledger so a new discovery candidate ID cannot reset
    # materialization/accounting history.
    cross_wave_state_path: Optional[Path] = None
    # Run-level cache of discovery and audit facts.  It is not an evidence
    # store and does not make a candidate eligible by itself.
    global_coverage_ledger_path: Optional[Path] = None
    _post_audit_transition_done: bool = field(default=False, repr=False)
    _post_audit_materialization_attempted: bool = field(default=False, repr=False)
    _post_audit_materialization_succeeded: bool = field(default=False, repr=False)
    _telemetry: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def targeted_queries(self) -> List[str]:
        raw = self.phase3_coverage_request.get("queries") or []
        result: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                value = item.get("query")
            else:
                value = item
            if str(value or "").strip():
                result.append(str(value).strip())
        return result

    @property
    def targeted_query_targets(self) -> List[Dict[str, Any]]:
        raw = self.phase3_coverage_request.get("query_targets") or []
        return [dict(item) for item in raw if isinstance(item, dict) and str(item.get("query") or "").strip()]

    @property
    def targeted_missing_claim_ids(self) -> List[str]:
        raw = self.phase3_coverage_request.get("missing_claim_ids") or []
        return [str(item).strip() for item in raw if str(item).strip()]

    @property
    def targeted_missing_roles(self) -> List[str]:
        raw = self.phase3_coverage_request.get("missing_roles") or []
        return [str(item).strip() for item in raw if str(item).strip()]

    @property
    def targeted_expected_new_papers(self) -> int:
        try:
            return max(0, int(self.phase3_coverage_request.get("expected_new_papers") or 0))
        except (TypeError, ValueError):
            return 0

    # In-memory candidate registry  {candidate_id: GapOACandidate-dict}
    # Populated by search_oa_candidates / trace_seed_references
    _candidate_store: Dict[str, Dict[str, Any]] = field(
        default_factory=dict, repr=False
    )
    _store_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )
    _papers_materialized_total: int = field(default=0, repr=False)

    def coverage_breadth_targets(self) -> Dict[str, int]:
        """Return article-quality source breadth targets for this section.

        Role coverage and literature breadth are different requirements.  A
        single paper may genuinely perform a required role, but one or two
        papers cannot support a publishable review chapter.  Callers may set
        explicit targets in ``section_data["literature_coverage_target"]``;
        otherwise targets scale with the planned section length.
        """

        raw = self.section_data.get("literature_coverage_target", {})
        raw = raw if isinstance(raw, dict) else {}
        contract_payload = (
            self.review_quality_contract
            if isinstance(self.review_quality_contract, dict)
            and self.review_quality_contract.get("mode")
            else self.section_data.get("review_quality_contract", {})
        )
        if isinstance(contract_payload, dict) and contract_payload.get("mode"):
            from .review_quality_contract import resolve_review_contract

            contract = resolve_review_contract(
                {},
                section={
                    **self.section_data,
                    "review_quality_contract": contract_payload,
                },
            )
            return contract.section_targets(
                section=self.section_data,
                section_count=int(
                    self.section_data.get("_review_section_count")
                    or contract.section_target_range[0]
                ),
            )
        word_range = self.section_data.get("target_word_range", {})
        word_range = word_range if isinstance(word_range, dict) else {}
        try:
            planned_words = int(
                word_range.get("min")
                or self.section_data.get("estimated_word_budget")
                or 900
            )
        except (TypeError, ValueError):
            planned_words = 900

        # Roughly one independently useful paper per 125 planned words, with
        # conservative bounds so short sections still synthesize a literature
        # base and long sections do not trigger unbounded acquisition.
        derived_unique = max(6, min(12, (planned_words + 124) // 125))
        try:
            minimum_unique = int(
                raw.get("minimum_unique_sources", derived_unique)
            )
        except (TypeError, ValueError):
            minimum_unique = derived_unique
        minimum_unique = max(3, min(20, minimum_unique))

        derived_direct = max(3, (minimum_unique * 3 + 4) // 5)
        try:
            minimum_direct = int(
                raw.get("minimum_direct_sources", derived_direct)
            )
        except (TypeError, ValueError):
            minimum_direct = derived_direct
        minimum_direct = max(2, min(minimum_unique, minimum_direct))
        return {
            "minimum_unique_sources": minimum_unique,
            "minimum_direct_sources": minimum_direct,
        }

    # ---------- candidate store helpers ----------

    def register_candidates(self, candidates: List[Dict[str, Any]]) -> List[str]:
        """Assign stable candidate_ids and store; return id list."""
        ids = []
        with self._store_lock:
            for cand in candidates:
                cid = cand.get("candidate_id") or _make_candidate_id()
                cand["candidate_id"] = cid
                self._candidate_store[cid] = cand
                ids.append(cid)
        return ids

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        with self._store_lock:
            return self._candidate_store.get(candidate_id)

    def get_candidates(self, candidate_ids: List[str]) -> List[Dict[str, Any]]:
        with self._store_lock:
            return [self._candidate_store[cid] for cid in candidate_ids
                    if cid in self._candidate_store]

    def all_candidate_ids(self) -> List[str]:
        with self._store_lock:
            return list(self._candidate_store.keys())

    @property
    def section_title(self) -> str:
        return self.section_data.get("title", self.section_id)

    @property
    def chapter_argument(self) -> str:
        return self.section_data.get("chapter_argument", "")

    @property
    def scope_guardrails(self) -> List[str]:
        return self.section_data.get("scope_guardrails", [])


def _make_candidate_id() -> str:
    return "cand_" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# Phase 3 — Section Review Authoring context
# ---------------------------------------------------------------------------

@dataclass
class SectionAuthoringContext:
    """Immutable bindings for a single section-authoring task.

    The agent receives only opaque IDs (section_id, chunk_id, paper_id).
    All path resolution happens inside the tools — the model never touches
    a filesystem path directly.
    """

    section_id: str
    section_data: Dict[str, Any]           # raw section dict from blueprint
    kb_sqlite: Optional[Path]              # Phase 2 coverage/staging KB (read-only)
    temp_kb_sqlite: Optional[Path]         # staging KB from coverage run (may be same as kb_sqlite)
    work_dir: Path                         # authoring task working directory
    additional_kb_sqlite_paths: List[Path] = field(default_factory=list)

    # Resolved paths to Phase 2 output artifacts; None if not available
    material_package_path: Optional[Path] = field(default=None)
    source_ledger_path: Optional[Path] = field(default=None)
    gap_report_path: Optional[Path] = field(default=None)
    synthesis_bundle_path: Optional[Path] = field(default=None)
    section_overlay_path: Optional[Path] = field(default=None)

    # Optional writing-context fields (Phase 3.1+) — all backward-compatible
    mentor_advice: Optional[Dict[str, Any]] = field(default=None)
    full_review_argument: str = field(default="")
    topic_identity: Dict[str, Any] = field(default_factory=dict)
    section_role: str = field(default="")
    preceding_section_conclusion: str = field(default="")
    following_section_role: str = field(default="")
    transition_contract: Dict[str, Any] = field(default_factory=dict)
    terminology_ledger: Dict[str, Any] = field(default_factory=dict)
    # Present only during editor-directed repair.  The worker must treat the
    # existing draft as the source of truth and change only the flagged scope.
    revision_instructions: Dict[str, Any] = field(default_factory=dict)
    existing_draft_text: str = field(default="")

    @property
    def section_title(self) -> str:
        return self.section_data.get("title", self.section_id)

    @property
    def chapter_argument(self) -> str:
        return self.section_data.get("chapter_argument", "")

    @property
    def scope_guardrails(self) -> List[str]:
        return self.section_data.get("scope_guardrails", [])
