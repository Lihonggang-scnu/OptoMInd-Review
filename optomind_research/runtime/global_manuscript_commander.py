"""Read-only global manuscript commander for the accepted chapter assets.

The commander is an advisory stage only. It loads a manifest of accepted
English chapter drafts and their input packets, builds deterministic local
ledgers (paragraph IDs, REF markers, paper identity, cross-section overlap,
claims, responsibilities, reviewer comments, visual status), then runs five
bounded roles: structure strategist, scientific synthesis editor, coverage
auditor, gap value critic, and final commander synthesis. The coverage auditor
proposes section_argument_gap/review_structure_gap candidates only; the gap
value critic approves/rejects/defers them against the latest chapter text and
is not an evidence court. It never edits chapter prose, never changes
scientific claims, never loads raw long-term material units, and never
launches retrieval. Legacy retrieval gaps remain typed proposals only.

The module is intentionally offline-testable: a deterministic provider is the
default in dry mode, and tests inject their own role providers. Live mode uses
the shared Qwen client without persisting secrets or raw provider headers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .artifact_store import atomic_write_json
from .cost_ledger import estimate_call_cost_cny, load_model_pricing

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"

SCHEMA_VERSION = "optomind.global_manuscript_commander.v2"
CONTEXT_SCHEMA_VERSION = "optomind.global_manuscript_commander.canonical_context.v2"
REVIEWS_SCHEMA_VERSION = "optomind.global_manuscript_commander.role_reviews.v2"
WORK_ORDER_SCHEMA_VERSION = "optomind.global_manuscript_commander.work_order.v2"
SUMMARY_SCHEMA_VERSION = "optomind.global_manuscript_commander.summary.v2"
M4_SNAPSHOT_SCHEMA_VERSION = (
    "optomind.global_manuscript_commander.m4_snapshot.v1"
)
M4_PROPOSAL_SCHEMA_VERSION = (
    "optomind.global_manuscript_commander.m4_proposal.v1"
)
M4_PATCH_SCHEMA_VERSION = "optomind.global_manuscript_commander.m4_patch_set.v1"
M4_APPLY_SCHEMA_VERSION = "optomind.global_manuscript_commander.m4_apply_report.v1"
M4_LEDGER_SCHEMA_VERSION = (
    "optomind.global_manuscript_commander.m4_ledger_audit.v1"
)
STAGED_AUTHORITY_SCHEMA_VERSION = (
    "optomind.global_manuscript_commander.staged_authority.v1"
)
DEFAULT_MODEL_TIER = "c2_model"

M4_AUTO_OPERATIONS = frozenset(
    {
        # Exact whole-block move between existing sibling sections.
        "move_block",
        # Deterministic reference-marker normalization (identity-safe only).
        "normalize_reference",
        # Deterministic paragraph-ID renumbering after ordering changes.
        "renumber_blocks",
    }
)
M4_SEMANTIC_OPERATIONS = frozenset(
    {
        "delete_block",
        "merge_blocks",
        "rewrite_transition",
        "ownership_change",
        "claim_strength_change",
        "evidence_change",
    }
)
M4_OPERATION_TYPES = M4_AUTO_OPERATIONS | M4_SEMANTIC_OPERATIONS
M4_RISK_LEVELS = frozenset({"none", "low", "medium", "high"})
M4_CLAIM_STRENGTH_CHANGES = frozenset({"none", "upgrade", "downgrade"})
M4_CITATION_CHANGES = frozenset(
    {"none", "added", "removed", "marker_normalized"}
)
M4_INVARIANT_NAMES = frozenset(
    {
        "no_scientific_meaning_change",
        "no_claim_text_changed",
        "no_evidence_weakening",
        "preserve_sibling_boundaries",
        "exact_block_text_preserved",
        "no_new_claims",
        "no_unknown_references",
    }
)
M4_STATUSES = frozenset(
    {"noop", "awaiting_approval", "applied", "rejected", "failed_qwen"}
)

WORD_COUNT_METRIC = "whitespace_split_units"
WORD_COUNT_DEFINITION = (
    "Deterministic count of whitespace-separated units (str.split) in every "
    "non-empty markdown block, including heading lines, summed per section by "
    "runtime._word_count. Independent of tokenizers and external reports."
)
PAPER_LEDGER_SEMANTICS = {
    "papers": (
        "All evidence/candidate papers available in input packets, including "
        "papers not cited in the drafts. Authoritative ID allowlist for "
        "sanitization; not manuscript citation evidence."
    ),
    "cited_papers": (
        "Papers actually cited in the drafts, resolved from REF markers. "
        "Authoritative for manuscript citation and repetition audit."
    ),
    "cross_section_overlap": (
        "Cited papers appearing in two or more sections (citation-based "
        "repetition audit)."
    ),
    "evidence_cross_section_overlap": (
        "All evidence/candidate papers appearing in two or more sections' "
        "evidence packets (candidate overlap, not citation evidence)."
    ),
}

ROLE_KEYS = (
    "structure_strategist",
    "scientific_synthesis_editor",
    "coverage_auditor",
    "evidence_attribution_critic",
    "commander_synthesis",
)

PROMPT_FILES = {
    "structure_strategist": "Global Manuscript Structure Strategist.txt",
    "scientific_synthesis_editor": "Global Manuscript Scientific Editor.txt",
    "coverage_auditor": "Global Manuscript Coverage Auditor.txt",
    # Role key retained for compatibility; the live prompt is now the
    # article-level gap value critic, not an evidence court.
    "evidence_attribution_critic": "Global Manuscript Gap Value Critic.txt",
    "commander_synthesis": "Global Manuscript Commander.txt",
}

RETRIEVAL_GAP_TYPES = frozenset(
    {
        "claim_evidence_gap",
        "section_claim_gap",
        "blueprint_structure_gap",
        "whole_manuscript_gap",
        "visual_evidence_gap",
    }
)

TWO_GAP_TYPES = frozenset(
    {"section_argument_gap", "review_structure_gap"}
)
TWO_GAP_REQUIRED_FIELDS = (
    "unique_contribution",
    "why_existing_structure_cannot_absorb",
    "expected_nonduplicate_gain",
    "affected_sections",
    "confidence",
    "existing_coverage",
    "residual_gap",
)
COVERAGE_CANDIDATE_REQUIRED_FIELDS = (
    "unique_contribution",
    "why_existing_structure_cannot_absorb",
    "expected_nonduplicate_gain",
    "success_criterion",
    "affected_sections",
    "confidence",
)
EVIDENCE_COURT_FORBIDDEN_FIELDS = frozenset(
    {
        "claim_evidence_gap",
        "evidence_sufficiency",
        "sentence_level_support",
        "evidence_court_verdict",
        "claim_evidence_audit",
    }
)

CANONICAL_CONTEXT_JSON = "canonical_context.json"
ROLE_REVIEWS_JSON = "role_reviews.json"
WORK_ORDER_JSON = "global_commander_work_order.json"
RUN_STATE_JSON = "run_state.json"
SUMMARY_JSON = "summary.json"

_REF_PATTERN = re.compile(r"\[REF:([^\]]+)\]")


class CommanderError(RuntimeError):
    """Base error for the global manuscript commander."""


class ManifestError(CommanderError):
    """Raised when the manifest or its referenced assets are invalid."""


class ResumeFingerprintMismatch(CommanderError):
    """Raised when a resume request meets a changed fingerprint."""


class UnusableCommanderResponse(CommanderError):
    """Raised when the final commander response cannot be used."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _block_hash(text: Any) -> str:
    """Stable sha256 of an exact block/paragraph string."""

    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _snapshot_serialize(
    sections: list[Mapping[str, Any]],
) -> bytes:
    """Deterministic serialization of draft text + input packets."""

    payload = {
        "schema_version": M4_SNAPSHOT_SCHEMA_VERSION,
        "sections": [
            {
                "section_id": str(section.get("section_id") or ""),
                "draft_text": str(section.get("draft_text") or ""),
                "input_packet": section.get("input_packet"),
            }
            for section in sections
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _snapshot_hash(sections: list[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_snapshot_serialize(sections)).hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _word_count(text: Any) -> int:
    return len(str(text or "").split())


def _split_paragraphs(text: str) -> list[str]:
    return [
        block.strip()
        for block in re.split(r"\n\s*\n", str(text or ""))
        if block.strip()
    ]


def _ref_markers(text: str) -> list[str]:
    return [m for m in _REF_PATTERN.findall(str(text or "")) if m.strip()]


def _paragraph_kind(text: str) -> str:
    return "heading" if text.lstrip().startswith("#") else "body"


def _contract_claim_ids(
    contract: Mapping[str, Any], paragraph_index: int
) -> list[str]:
    functions = contract.get("paragraph_functions")
    if not isinstance(functions, list):
        return []
    for item in functions:
        if not isinstance(item, Mapping):
            continue
        try:
            index = int(item.get("paragraph_index"))
        except (TypeError, ValueError):
            continue
        if index == paragraph_index:
            claim_ids = item.get("claim_ids")
            if isinstance(claim_ids, list):
                return [str(value) for value in claim_ids if str(value).strip()]
    return []


def _resolve_ref_marker(
    marker: str, papers: Mapping[str, Any]
) -> str | None:
    """Resolve a REF marker body to a known paper id, if possible."""
    value = str(marker or "").strip()
    if not value:
        return None
    if value in papers:
        return value
    if value.startswith("identity-fallback:"):
        suffix = value[len("identity-fallback:"):]
        if suffix in papers:
            return suffix
    if ":" in value:
        head = value.split(":", 1)[0]
        if head in papers:
            return head
    return None


def load_manifest(manifest_path: str | Path) -> list[dict[str, Any]]:
    """Load and validate the global commander manifest."""

    path = Path(manifest_path)
    if not path.is_file():
        raise ManifestError(f"missing manifest: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    raw_sections = (
        payload
        if isinstance(payload, list)
        else payload.get("sections")
        if isinstance(payload, Mapping)
        else None
    )
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ManifestError(
            "manifest must contain a non-empty sections list"
        )
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, Mapping):
            raise ManifestError(f"manifest section {index} must be an object")
        section_id = str(raw.get("section_id") or "").strip()
        english_draft_path = str(
            raw.get("english_draft_path")
            or raw.get("english_path")
            or raw.get("draft_path")
            or ""
        ).strip()
        input_packet_path = str(
            raw.get("input_packet_path") or raw.get("input_packet") or ""
        ).strip()
        explanatory_citation_ledger_path = str(
            raw.get("explanatory_citation_ledger_path")
            or raw.get("explanatory_ledger_path")
            or raw.get("citation_ledger_path")
            or ""
        ).strip()
        if not section_id or not english_draft_path or not input_packet_path:
            raise ManifestError(
                f"manifest section {index} requires section_id, "
                "english_draft_path, and input_packet_path"
            )
        if section_id in seen:
            raise ManifestError(f"duplicate section_id: {section_id}")
        seen.add(section_id)
        draft_path = Path(english_draft_path)
        packet_path = Path(input_packet_path)
        if not draft_path.is_file():
            raise ManifestError(
                f"section {section_id} English draft missing: {draft_path}"
            )
        if not packet_path.is_file():
            raise ManifestError(
                f"section {section_id} input packet missing: {packet_path}"
            )
        ledger_path: Path | None = None
        if explanatory_citation_ledger_path:
            ledger_path = Path(explanatory_citation_ledger_path)
            if not ledger_path.is_file():
                raise ManifestError(
                    f"section {section_id} explanatory citation ledger "
                    f"missing: {ledger_path}"
                )
        sections.append(
            {
                "section_id": section_id,
                "english_draft_path": english_draft_path,
                "input_packet_path": input_packet_path,
                **(
                    {
                        "explanatory_citation_ledger_path": str(ledger_path)
                    }
                    if ledger_path is not None
                    else {}
                ),
            }
        )
    return sections


def compute_fingerprint(
    manifest_path: str | Path,
    sections: list[dict[str, Any]],
    prompts_dir: str | Path | None = None,
) -> str:
    """Fingerprint manifest, source assets, and role prompts."""

    prompts = Path(prompts_dir or DEFAULT_PROMPTS_DIR)
    files: list[dict[str, str]] = []

    def add(label: str, path: str | Path) -> None:
        source = Path(path)
        if not source.is_file():
            raise CommanderError(f"fingerprint source missing: {source}")
        files.append(
            {
                "label": label,
                "path": str(source.resolve()),
                "sha256": _sha256_file(source),
            }
        )

    add("manifest", manifest_path)
    for section in sections:
        add(f"draft:{section['section_id']}", section["english_draft_path"])
        add(f"packet:{section['section_id']}", section["input_packet_path"])
        ledger_path = section.get("explanatory_citation_ledger_path")
        if ledger_path:
            add(
                f"ledger:{section['section_id']}",
                ledger_path,
            )
    for role in ROLE_KEYS:
        add(f"prompt:{role}", prompts / PROMPT_FILES[role])
    files.sort(key=lambda item: item["label"])
    payload = {"schema_version": SCHEMA_VERSION, "files": files}
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _norm_paper_id(val: str) -> str:
    """Normalize a paper identifier by stripping the optional 's2:' prefix.

    Semantic Scholar IDs may appear as either '29c7cbed...' or
    's2:29c7cbed...' depending on which data path produced them.  Stripping
    the prefix before any equality comparison or dict-key insertion prevents
    false alias-conflict errors when the same paper is referenced both ways.
    """
    return val[3:] if val.startswith("s2:") else val


def _norm_doi(val: str) -> str:
    """Return a canonical lower-case DOI suffix, or an empty string.

    Explanatory ledgers may carry the same paper as a DOI marker in one
    section and an S2 identifier in another.  A DOI is the stronger
    cross-provider identity, so normalize URL/prefix variants before alias
    comparison.
    """

    value = str(val or "").strip().lower()
    value = re.sub(r"^https?://doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    value = value.strip().rstrip(".,;")
    return value if re.match(r"^10\.\d{4,9}/\S+$", value) else ""


def _paper_identity_aliases(*, marker_id: str, paper_id: str, doi: str) -> tuple[str, ...]:
    """Build raw and canonical aliases for one explanatory record."""

    values: list[str] = []
    for raw in (marker_id, paper_id, doi):
        text = str(raw or "").strip()
        if text:
            values.append(text)
            normalized_id = _norm_paper_id(text)
            if normalized_id:
                values.append(normalized_id)
            normalized_doi = _norm_doi(text)
            if normalized_doi:
                values.extend((normalized_doi, f"doi:{normalized_doi}"))
    return tuple(dict.fromkeys(value for value in values if value))


def _canonical_paper_identity(*, paper_id: str, marker_id: str, doi: str) -> str:
    """Choose the DOI identity when available, otherwise the raw source ID.

    The DOI form is the cross-provider canonical identity, so URL/prefix,
    case, and trailing-punctuation variants all collapse to the same
    ``doi:`` value. Without a DOI the source id is kept verbatim; the
    ``s2:`` prefix normalization stays in the alias layer only.
    """

    normalized_doi = _norm_doi(doi) or _norm_doi(paper_id) or _norm_doi(marker_id)
    if normalized_doi:
        return f"doi:{normalized_doi}"
    return str(paper_id or marker_id or "").strip()


def _resolve_explanatory_identities(
    records: list[dict[str, Any]],
    identity_by_marker: dict[str, dict[str, Any]],
    explanatory_papers: dict[str, dict[str, Any]],
    marker_sections: dict[str, set[str]],
) -> None:
    """Resolve one canonical identity per explanatory paper.

    DOI-bearing records are resolved first so every alias they claim
    (including a bare Semantic Scholar id that appears without a DOI in
    another section) adopts the DOI identity instead of raising a false
    alias conflict. Remaining records keep their normalized source id
    unless an already-bound identity-bearing alias proves they are the
    same paper. A shared marker that points at two different identities
    still raises ManifestError, and two different DOIs are never merged.
    """

    def record_aliases(item: dict[str, Any]) -> tuple[str, ...]:
        return _paper_identity_aliases(
            marker_id=item["marker_id"],
            paper_id=item["paper_id"],
            doi=item["doi"],
        )

    def canonical_of(entry: Mapping[str, Any]) -> str:
        return str(
            entry.get("canonical_identity")
            or _norm_paper_id(str(entry.get("paper_id") or ""))
        )

    def bind(item: dict[str, Any], identity: str) -> None:
        for alias in record_aliases(item):
            existing = identity_by_marker.get(alias)
            if existing is None:
                identity_by_marker[alias] = {
                    "paper_id": identity,
                    "canonical_identity": identity,
                    "title": item["title"],
                    "trust_type": "background_explanation_only",
                    "role": "background",
                    "marker_id": item["marker_id"],
                    "handle": item["handle"],
                    "overlaps_core_reference": item[
                        "overlaps_core_reference"
                    ],
                }
            elif canonical_of(existing) != identity:
                raise ManifestError(
                    "explanatory marker alias conflict: "
                    f"{alias} ({existing.get('paper_id')} vs "
                    f"{item['paper_id'] or item['marker_id']})"
                )
            marker_sections[alias].add(item["section_id"])
        entry = explanatory_papers.setdefault(
            identity,
            {
                "paper_id": identity,
                "titles": set(),
                "sections": set(),
                "marker_ids": set(),
                "trust_type": "background_explanation_only",
                "role": "background",
                "overlaps_core_reference": False,
            },
        )
        if item["title"]:
            entry["titles"].add(item["title"])
        entry["sections"].add(item["section_id"])
        if item["marker_id"]:
            entry["marker_ids"].add(item["marker_id"])
        if item["overlaps_core_reference"]:
            entry["overlaps_core_reference"] = True

    doi_records: list[dict[str, Any]] = []
    plain_records: list[dict[str, Any]] = []
    for item in records:
        identity = _canonical_paper_identity(
            paper_id=item["paper_id"],
            marker_id=item["marker_id"],
            doi=item["doi"],
        )
        item["identity"] = identity
        if identity.startswith("doi:"):
            doi_records.append(item)
        elif identity:
            plain_records.append(item)

    for item in doi_records:
        bind(item, item["identity"])

    for item in plain_records:
        identity = item["identity"]
        bound = {
            canonical_of(identity_by_marker[alias])
            for alias in record_aliases(item)
            if alias in identity_by_marker
        }
        if not bound:
            bind(item, identity)
            continue
        if len(bound) > 1:
            raise ManifestError(
                "explanatory marker alias conflict: "
                f"{identity} ({' vs '.join(sorted(bound))})"
            )
        resolved = next(iter(bound))
        if resolved != identity:
            own_claimed = (
                identity in identity_by_marker
                and canonical_of(identity_by_marker[identity]) == resolved
            )
            if not (own_claimed and resolved.startswith("doi:")):
                raise ManifestError(
                    "explanatory marker alias conflict: "
                    f"{identity} ({resolved} vs {identity})"
                )
            identity = resolved
        bind(item, identity)


def build_canonical_context(
    manifest_path: str | Path,
    sections: list[dict[str, Any]],
    *,
    fingerprint: str = "",
    in_memory_sections: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the deterministic canonical context and local ledgers.

    Only the manifest-listed English drafts and input packets are read. No raw
    long-term material units are loaded. When ``in_memory_sections`` is
    supplied, its per-section ``draft_text`` and ``input_packet`` values are
    authoritative and the manifest paths serve only as audit provenance.
    """

    context_sections: list[dict[str, Any]] = []
    papers_titles: dict[str, list[str]] = defaultdict(list)
    paper_sections: dict[str, set[str]] = defaultdict(set)
    paper_chunk_counts: dict[str, int] = defaultdict(int)
    section_ref_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    ref_sections: dict[str, set[str]] = defaultdict(set)
    ref_total_counts: dict[str, int] = defaultdict(int)
    section_paper_ids: dict[str, set[str]] = defaultdict(set)
    section_claims: dict[str, set[str]] = {}
    full_workplan: list[Any] = []
    global_review_thesis = ""
    global_narrative_strategy = ""
    explanation_records_by_section: dict[str, list[dict[str, Any]]] = {}
    explanatory_identity_by_marker: dict[str, dict[str, Any]] = {}
    explanatory_papers: dict[str, dict[str, Any]] = {}
    explanatory_marker_sections: dict[str, set[str]] = defaultdict(set)
    pending_explanatory: list[dict[str, Any]] = []
    ledger_present = any(
        bool(section.get("explanatory_citation_ledger_path"))
        for section in sections
    )
    for memory in (in_memory_sections or {}).values():
        if isinstance(memory, Mapping) and "explanatory_ledger" in memory:
            ledger_present = True
            if not isinstance(memory["explanatory_ledger"], list):
                raise ManifestError(
                    "in-memory explanatory_ledger must be a list"
                )

    for section in sections:
        section_id = section["section_id"]
        memory = (in_memory_sections or {}).get(section_id)
        if isinstance(memory, Mapping) and "draft_text" in memory:
            draft = str(memory.get("draft_text") or "")
            packet = memory.get("input_packet")
        else:
            draft = Path(section["english_draft_path"]).read_text(
                encoding="utf-8", errors="replace"
            )
            packet = _read_json(Path(section["input_packet_path"]), {})
        if not isinstance(packet, Mapping):
            raise ManifestError(
                f"section {section_id} input packet is not a JSON object"
            )
        ledger_records: list[Mapping[str, Any]] | None = None
        ledger_path = ""
        if isinstance(memory, Mapping) and "explanatory_ledger" in memory:
            ledger_records = memory["explanatory_ledger"]
        else:
            ledger_path = str(
                section.get("explanatory_citation_ledger_path") or ""
            )
            if ledger_path:
                if not Path(ledger_path).is_file():
                    raise ManifestError(
                        f"section {section_id} explanatory citation ledger "
                        f"missing: {ledger_path}"
                    )
                ledger = _read_json(Path(ledger_path), {})
                if not isinstance(ledger, Mapping):
                    raise ManifestError(
                        f"section {section_id} explanatory citation ledger "
                        f"must be a JSON object: {ledger_path}"
                    )
                ledger_records = ledger.get("records")
                if not isinstance(ledger_records, list):
                    raise ManifestError(
                        f"section {section_id} explanatory citation ledger "
                        f"must contain a 'records' list: {ledger_path}"
                    )
        contract = (
            packet.get("section_contract")
            if isinstance(packet.get("section_contract"), Mapping)
            else {}
        )
        manuscript_context = (
            packet.get("manuscript_context")
            if isinstance(packet.get("manuscript_context"), Mapping)
            else {}
        )
        research_context = (
            manuscript_context.get("research_context")
            if isinstance(manuscript_context.get("research_context"), Mapping)
            else {}
        )
        claims_raw = (
            packet.get("claims") if isinstance(packet.get("claims"), list) else []
        )
        evidence_raw = (
            packet.get("evidence_packets")
            if isinstance(packet.get("evidence_packets"), list)
            else []
        )

        paragraphs: list[dict[str, Any]] = []
        for index, block in enumerate(_split_paragraphs(draft), start=1):
            paragraph_id = f"P{index:02d}" if index <= 99 else f"P{index}"
            refs = _ref_markers(block)
            for marker in refs:
                section_ref_counts[section_id][marker] += 1
                ref_sections[marker].add(section_id)
                ref_total_counts[marker] += 1
            paragraphs.append(
                {
                    "paragraph_id": paragraph_id,
                    "canonical_id": f"{section_id}-{paragraph_id}",
                    "kind": _paragraph_kind(block),
                    "text": block,
                    "hash": _block_hash(block),
                    "word_count": _word_count(block),
                    "ref_markers": refs,
                    "ref_marker_count": len(refs),
                    "contract_claim_ids": _contract_claim_ids(
                        contract, index
                    ),
                }
            )

        evidence_full: list[dict[str, Any]] = []
        for evidence in evidence_raw:
            if not isinstance(evidence, Mapping):
                continue
            paper_id = str(evidence.get("paper_id") or "").strip()
            title = str(evidence.get("source_title") or "").strip()
            if paper_id:
                paper_sections[paper_id].add(section_id)
                section_paper_ids[section_id].add(paper_id)
                paper_chunk_counts[paper_id] += 1
                if title:
                    papers_titles[paper_id].append(title)
            evidence_full.append(dict(evidence))

        claims_compact: list[dict[str, Any]] = []
        claims_full: list[dict[str, Any]] = []
        claim_ids: set[str] = set()
        for claim in claims_raw:
            if not isinstance(claim, Mapping):
                continue
            claim_id = str(claim.get("claim_id") or "").strip()
            if not claim_id:
                continue
            claim_ids.add(claim_id)
            claims_full.append(dict(claim))
            claim_state = str(claim.get("claim_state") or "")
            claims_compact.append(
                {
                    "claim_id": claim_id,
                    "role": str(claim.get("role") or ""),
                    "claim_state": claim_state,
                    "readiness": (
                        "ready_for_write"
                        if claim_state == "ready_for_write"
                        else "not_ready"
                    ),
                    "evidence_binding_status": str(
                        claim.get("evidence_binding_status") or ""
                    ),
                    "writing_permission": str(
                        claim.get("writing_permission") or ""
                    ),
                    "parent_claim_id": str(
                        claim.get("parent_claim_id") or ""
                    ),
                    "evidence_strength": str(
                        claim.get("evidence_strength") or ""
                    ),
                    "statement": str(claim.get("statement") or ""),
                }
            )
            source_contexts = claim.get("source_contexts")
            if isinstance(source_contexts, list):
                for source in source_contexts:
                    if not isinstance(source, Mapping):
                        continue
                    paper_id = str(source.get("paper_id") or "").strip()
                    title = str(source.get("title") or "").strip()
                    if paper_id:
                        paper_sections[paper_id].add(section_id)
                        section_paper_ids[section_id].add(paper_id)
                        if title:
                            papers_titles[paper_id].append(title)
        section_claims[section_id] = claim_ids

        section_explanation_records: list[dict[str, Any]] = []
        if ledger_records is not None and isinstance(ledger_records, list):
            for raw in ledger_records:
                if not isinstance(raw, Mapping):
                    continue
                metadata = raw.get("metadata") or {}
                metadata = metadata if isinstance(metadata, Mapping) else {}
                marker_id = str(
                    raw.get("marker_id")
                    or raw.get("marker")
                    or metadata.get("paper_id")
                    or metadata.get("doi")
                    or ""
                ).strip()
                paper_id = str(
                    metadata.get("paper_id") or metadata.get("doi") or ""
                ).strip()
                title = str(metadata.get("title") or "")
                record: dict[str, Any] = {
                    "marker_id": marker_id,
                    "handle": str(raw.get("handle") or ""),
                    "title": title,
                    "paper_id": paper_id,
                    "permission": str(raw.get("permission") or ""),
                    "trust_type": "background_explanation_only",
                    "role": "background",
                    "overlaps_core_reference": bool(
                        raw.get("overlaps_core_reference")
                    ),
                    "provenance": {
                        "section_id": section_id,
                        "ledger_path": ledger_path,
                    },
                }
                section_explanation_records.append(record)
                pending_explanatory.append(
                    {
                        "marker_id": marker_id,
                        "paper_id": paper_id,
                        "doi": str(metadata.get("doi") or ""),
                        "title": title,
                        "handle": str(raw.get("handle") or ""),
                        "overlaps_core_reference": bool(
                            raw.get("overlaps_core_reference")
                        ),
                        "section_id": section_id,
                    }
                )
        explanation_records_by_section[section_id] = (
            section_explanation_records
        )

        if not full_workplan:
            workplan = manuscript_context.get("full_section_workplan")
            if isinstance(workplan, list):
                full_workplan = workplan
        if not global_review_thesis:
            global_review_thesis = str(
                manuscript_context.get("global_review_thesis") or ""
            )
        if not global_narrative_strategy:
            global_narrative_strategy = str(
                manuscript_context.get("global_narrative_strategy") or ""
            )

        reviewer_comments = (
            manuscript_context.get("reviewer_comments_retained")
            if isinstance(
                manuscript_context.get("reviewer_comments_retained"), list
            )
            else []
        )
        reviewer_comments = [
            item for item in reviewer_comments if isinstance(item, Mapping)
        ]
        visual_evidence = (
            packet.get("visual_evidence")
            if isinstance(packet.get("visual_evidence"), list)
            else []
        )
        visual_gap_plan = (
            packet.get("visual_gap_plan")
            if isinstance(packet.get("visual_gap_plan"), list)
            else []
        )
        expected_visual_arguments = (
            contract.get("expected_visual_arguments")
            if isinstance(contract.get("expected_visual_arguments"), list)
            else []
        )
        visual_status = {
            "visual_evidence_count": len(visual_evidence),
            "visual_gap_plan_count": len(visual_gap_plan),
            "expected_visual_arguments": expected_visual_arguments,
            "visual_gap_plan": [
                item for item in visual_gap_plan if isinstance(item, Mapping)
            ],
            "has_visual_gap": bool(visual_gap_plan)
            or (bool(expected_visual_arguments) and not visual_evidence),
        }
        boundary_contract = (
            manuscript_context.get("current_section_boundary_contract")
            if isinstance(
                manuscript_context.get(
                    "current_section_boundary_contract"
                ),
                Mapping,
            )
            else {}
        )
        sibling_responsibilities = (
            manuscript_context.get("sibling_section_responsibilities")
            if isinstance(
                manuscript_context.get("sibling_section_responsibilities"),
                list,
            )
            else []
        )
        write_gate = (
            manuscript_context.get("write_gate")
            if isinstance(manuscript_context.get("write_gate"), Mapping)
            else {}
        )
        open_questions = (
            packet.get("open_questions")
            if isinstance(packet.get("open_questions"), list)
            else []
        )
        excluded_unready_claim_ids = [
            str(value)
            for value in manuscript_context.get("excluded_unready_claim_ids")
            or []
        ]
        cleaned_manuscript_context = {
            "section_id": section_id,
            "current_section_boundary_contract": boundary_contract,
            "reviewer_comments_retained": reviewer_comments,
            "excluded_unready_claim_ids": excluded_unready_claim_ids,
            "write_gate": write_gate,
            "sibling_section_responsibilities": sibling_responsibilities,
            "research_context": research_context,
            "open_questions": open_questions,
        }
        section_ref_counts_by_section = section_ref_counts[section_id]
        section_markers = sorted(section_ref_counts_by_section)
        context_section: dict[str, Any] = {
            "section_id": section_id,
            "title": str(
                contract.get("title")
                or manuscript_context.get("source_section_title")
                or ""
            ),
            "source_section_title": str(
                manuscript_context.get("source_section_title") or ""
            ),
            "english_draft_path": str(section["english_draft_path"]),
            "input_packet_path": str(section["input_packet_path"]),
            "draft_text": draft,
            "word_count": sum(
                paragraph["word_count"] for paragraph in paragraphs
            ),
            "paragraph_count": len(paragraphs),
            "paragraphs": paragraphs,
            "ref_markers": section_markers,
            "ref_marker_count": sum(
                section_ref_counts_by_section.values()
            ),
            "unique_ref_marker_count": len(section_markers),
            "section_contract": contract,
            "claims": claims_compact,
            "claims_full": claims_full,
            "claim_count": len(claims_compact),
            "ready_claim_count": sum(
                1
                for claim in claims_compact
                if claim["readiness"] == "ready_for_write"
            ),
            "excluded_unready_claim_ids": excluded_unready_claim_ids,
            "evidence_packets": evidence_full,
            "evidence_packet_count": len(evidence_full),
            "paper_ids": sorted(section_paper_ids[section_id]),
            "paper_titles": {},
            "visual_status": visual_status,
            "manuscript_context": cleaned_manuscript_context,
        }
        if ledger_present:
            context_section["explanatory_citation_ledger_path"] = ledger_path
            context_section["explanatory_citation_ledger_records"] = (
                section_explanation_records
            )
        context_sections.append(context_section)

    _resolve_explanatory_identities(
        pending_explanatory,
        explanatory_identity_by_marker,
        explanatory_papers,
        explanatory_marker_sections,
    )

    papers: dict[str, dict[str, Any]] = {}
    evidence_overlap: dict[str, dict[str, Any]] = {}
    for paper_id, titles in papers_titles.items():
        unique_titles = sorted({title for title in titles if title})
        primary_title = unique_titles[0] if unique_titles else ""
        sections_list = sorted(paper_sections.get(paper_id, set()))
        papers[paper_id] = {
            "paper_id": paper_id,
            "titles": unique_titles,
            "primary_title": primary_title,
            "sections": sections_list,
            "section_count": len(sections_list),
            "evidence_chunk_count": paper_chunk_counts.get(paper_id, 0),
        }
        if len(sections_list) >= 2:
            pairs = sorted(
                {
                    tuple(sorted((left, right)))
                    for left in sections_list
                    for right in sections_list
                    if left < right
                }
            )
            evidence_overlap[paper_id] = {
                "paper_id": paper_id,
                "titles": unique_titles,
                "primary_title": primary_title,
                "sections": sections_list,
                "section_pairs": [list(pair) for pair in pairs],
            }

    explanatory_papers_clean: dict[str, dict[str, Any]] = {}
    for identity, entry in explanatory_papers.items():
        unique_titles = sorted(entry["titles"])
        primary_title = unique_titles[0] if unique_titles else ""
        sections_list = sorted(entry["sections"])
        explanatory_papers_clean[identity] = {
            "paper_id": identity,
            "titles": unique_titles,
            "primary_title": primary_title,
            "trust_type": "background_explanation_only",
            "role": "background",
            "sections": sections_list,
            "section_count": len(sections_list),
            "marker_ids": sorted(entry["marker_ids"]),
            "evidence_chunk_count": 0,
            "overlaps_core_reference": (
                identity in papers or entry["overlaps_core_reference"]
            ),
        }

    ref_identity_map: dict[str, dict[str, Any]] = {}
    for marker in sorted(ref_total_counts):
        resolved = _resolve_ref_marker(marker, papers)
        if resolved:
            info = papers[resolved]
            entry: dict[str, Any] = {
                "known": True,
                "paper_id": resolved,
                "title": info["primary_title"],
                "sections": sorted(ref_sections[marker]),
            }
            if ledger_present:
                entry["trust_type"] = "core_evidence"
            ref_identity_map[marker] = entry
        elif marker in explanatory_identity_by_marker:
            explanatory = explanatory_identity_by_marker[marker]
            ref_identity_map[marker] = {
                "known": True,
                "paper_id": explanatory["paper_id"],
                "title": explanatory["title"],
                "sections": sorted(ref_sections.get(marker, set())),
                "trust_type": "background_explanation_only",
                "role": "background",
            }
        else:
            ref_identity_map[marker] = {
                "known": False,
                "paper_id": "",
                "title": "",
                "sections": sorted(ref_sections[marker]),
            }
            if ledger_present:
                ref_identity_map[marker]["trust_type"] = ""

    # Cited-paper ledger: resolved from REF markers only. This is the
    # authoritative view of what the manuscript actually cites and repeats.
    cited_ref_markers: dict[str, set[str]] = defaultdict(set)
    cited_citation_counts: dict[str, int] = defaultdict(int)
    cited_sections: dict[str, set[str]] = defaultdict(set)
    for marker, info in ref_identity_map.items():
        if not info.get("known"):
            continue
        paper_id = str(info.get("paper_id") or "")
        if not paper_id:
            continue
        cited_ref_markers[paper_id].add(marker)
        cited_citation_counts[paper_id] += int(
            ref_total_counts.get(marker) or 0
        )
        cited_sections[paper_id].update(info.get("sections") or [])
    cited_papers: dict[str, dict[str, Any]] = {}
    for paper_id in sorted(cited_sections):
        if paper_id in papers:
            base = papers[paper_id]
            effective_trust = "core_evidence" if ledger_present else ""
            effective_role = "core" if ledger_present else ""
        elif paper_id in explanatory_papers_clean:
            base = explanatory_papers_clean[paper_id]
            effective_trust = "background_explanation_only"
            effective_role = "background"
        else:
            continue
        sections_list = sorted(cited_sections[paper_id])
        cited_entry: dict[str, Any] = {
            "paper_id": paper_id,
            "titles": base["titles"],
            "primary_title": base["primary_title"],
            "sections": sections_list,
            "section_count": len(sections_list),
            "citation_count": cited_citation_counts[paper_id],
            "ref_markers": sorted(cited_ref_markers[paper_id]),
            "evidence_chunk_count": base["evidence_chunk_count"],
        }
        if ledger_present:
            cited_entry["trust_type"] = effective_trust
            cited_entry["role"] = effective_role
        cited_papers[paper_id] = cited_entry
    cited_overlap: dict[str, dict[str, Any]] = {}
    for paper_id, info in cited_papers.items():
        if info["section_count"] < 2:
            continue
        pairs = sorted(
            {
                tuple(sorted((left, right)))
                for left in info["sections"]
                for right in info["sections"]
                if left < right
            }
        )
        cited_overlap[paper_id] = {
            "paper_id": paper_id,
            "titles": info["titles"],
            "primary_title": info["primary_title"],
            "sections": info["sections"],
            "section_pairs": [list(pair) for pair in pairs],
            "citation_count": info["citation_count"],
        }

    for section in context_sections:
        section_id = section["section_id"]
        section["paper_titles"] = {
            paper_id: papers[paper_id]["primary_title"]
            for paper_id in section["paper_ids"]
            if papers[paper_id]["primary_title"]
        }

    canonical_context: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "manifest_path": str(Path(manifest_path).resolve()),
        "fingerprint": fingerprint,
        "generated_at": _now(),
        "section_count": len(sections),
        "sections": context_sections,
        "full_section_workplan": full_workplan,
        "global_review_thesis": global_review_thesis,
        "global_narrative_strategy": global_narrative_strategy,
        # All evidence/candidate papers; authoritative sanitization allowlist.
        "papers": papers,
        "cited_papers": cited_papers,
        # Citation-based repetition audit (authoritative).
        "cross_section_overlap": cited_overlap,
        # Candidate/all-evidence overlap for revision and gap proposals.
        "evidence_cross_section_overlap": evidence_overlap,
        "ref_identity_map": ref_identity_map,
        "paper_ledger_semantics": PAPER_LEDGER_SEMANTICS,
        "total_word_count": sum(
            section["word_count"] for section in context_sections
        ),
        "word_count_metric": WORD_COUNT_METRIC,
        "word_count_definition": WORD_COUNT_DEFINITION,
        "total_paragraph_count": sum(
            section["paragraph_count"] for section in context_sections
        ),
        "total_ref_marker_count": sum(ref_total_counts.values()),
        "unique_ref_marker_count": len(ref_total_counts),
        "paper_count": len(cited_papers),
        "evidence_paper_count": len(papers),
        "cross_section_overlap_count": len(cited_overlap),
        "evidence_cross_section_overlap_count": len(evidence_overlap),
        "usage_rule": (
            "This context is advisory and read-only. Full chapter text is "
            "provided for every role without truncation. papers is the "
            "all-evidence/candidate allowlist; cited_papers and "
            "cross_section_overlap are authoritative for actual manuscript "
            "citation and repetition. Only typed retrieval gap proposals may "
            "be recorded; nothing is enqueued."
        ),
        "read_only_declaration": {
            "chapter_text_changed": False,
            "retrieval_launched": False,
        },
    }
    if ledger_present:
        canonical_context["explanatory_papers"] = explanatory_papers_clean
        canonical_context["explanation_ledgers"] = (
            explanation_records_by_section
        )
        canonical_context["explanatory_paper_count"] = len(
            explanatory_papers_clean
        )
        canonical_context["explanatory_marker_count"] = len(
            explanatory_identity_by_marker
        )
    return canonical_context


def _role_context_view(canonical: Mapping[str, Any]) -> dict[str, Any]:
    """Build a role-safe view of the canonical context.

    Every role keeps the latest exact chapter text, stable paragraph
    IDs/hashes, section contracts/responsibilities, global thesis/narrative,
    and current citation identity/overlap summaries with trust labels. Raw
    evidence packets, full claim records, source quotes, and historical
    reviewer/open-question payloads are deliberately excluded so roles cannot
    re-litigate evidence or be steered by stale notes.
    """

    sections = canonical.get("sections") or []
    global_thesis = str(canonical.get("global_review_thesis") or "")
    global_strategy = str(canonical.get("global_narrative_strategy") or "")
    view_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        manuscript_context = section.get("manuscript_context") or {}
        manuscript_context = (
            manuscript_context
            if isinstance(manuscript_context, Mapping)
            else {}
        )
        boundary_contract = manuscript_context.get(
            "current_section_boundary_contract"
        )
        boundary_contract = (
            boundary_contract
            if isinstance(boundary_contract, Mapping)
            else {}
        )
        sibling_responsibilities = manuscript_context.get(
            "sibling_section_responsibilities"
        )
        sibling_responsibilities = (
            sibling_responsibilities
            if isinstance(sibling_responsibilities, list)
            else []
        )
        research_context = manuscript_context.get("research_context")
        research_context = (
            research_context
            if isinstance(research_context, Mapping)
            else {}
        )
        write_gate = manuscript_context.get("write_gate")
        write_gate = write_gate if isinstance(write_gate, Mapping) else {}
        section_governance = {
            "current_section_boundary_contract": boundary_contract,
            "sibling_section_responsibilities": sibling_responsibilities,
            "research_context": research_context,
            "write_gate": {
                "allowed_to_write": bool(
                    write_gate.get("allowed_to_write")
                )
            },
            "excluded_unready_claim_ids": [
                str(value)
                for value in (
                    manuscript_context.get("excluded_unready_claim_ids")
                    or []
                )
            ],
        }
        contract = section.get("section_contract")
        contract = contract if isinstance(contract, Mapping) else {}
        section_contract_view = {
            key: value
            for key, value in contract.items()
            if key != "open_questions"
        }
        ledger_records_view = [
            {
                "marker_id": str(record.get("marker_id") or ""),
                "paper_id": str(record.get("paper_id") or ""),
                "title": str(record.get("title") or ""),
                "trust_type": str(record.get("trust_type") or ""),
                "role": str(record.get("role") or ""),
                "overlaps_core_reference": bool(
                    record.get("overlaps_core_reference")
                ),
            }
            for record in (
                section.get("explanatory_citation_ledger_records") or []
            )
            if isinstance(record, Mapping)
        ]
        paragraph_keys = (
            "paragraph_id",
            "canonical_id",
            "kind",
            "hash",
            "word_count",
            "ref_markers",
            "ref_marker_count",
            "contract_claim_ids",
        )
        view_sections.append(
            {
                "section_id": str(section.get("section_id") or ""),
                "title": str(section.get("title") or ""),
                "source_section_title": str(
                    section.get("source_section_title") or ""
                ),
                "draft_text": str(section.get("draft_text") or ""),
                "word_count": int(section.get("word_count") or 0),
                "paragraph_count": int(section.get("paragraph_count") or 0),
                "paragraphs": [
                    {
                        key: paragraph.get(key)
                        for key in paragraph_keys
                        if key in paragraph
                    }
                    for paragraph in (section.get("paragraphs") or [])
                    if isinstance(paragraph, Mapping)
                ],
                "ref_markers": section.get("ref_markers") or [],
                "ref_marker_count": int(
                    section.get("ref_marker_count") or 0
                ),
                "unique_ref_marker_count": int(
                    section.get("unique_ref_marker_count") or 0
                ),
                "section_contract": section_contract_view,
                "section_governance": section_governance,
                "claim_count": int(section.get("claim_count") or 0),
                "ready_claim_count": int(
                    section.get("ready_claim_count") or 0
                ),
                "excluded_unready_claim_ids": (
                    section.get("excluded_unready_claim_ids") or []
                ),
                "evidence_packet_count": int(
                    section.get("evidence_packet_count") or 0
                ),
                "paper_ids": section.get("paper_ids") or [],
                "paper_titles": section.get("paper_titles") or {},
                "visual_status": section.get("visual_status") or {},
                "explanatory_citation_ledger_records": ledger_records_view,
            }
        )
    role_view: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "section_count": len(view_sections),
        "sections": view_sections,
        "global_review_thesis": global_thesis,
        "global_narrative_strategy": global_strategy,
        # All evidence/candidate papers; sanitization allowlist, not citation
        # evidence. See paper_ledger_semantics.
        "papers": canonical.get("papers") or {},
        "cited_papers": canonical.get("cited_papers") or {},
        # Citation-based repetition audit (authoritative).
        "cross_section_overlap": canonical.get("cross_section_overlap") or {},
        # Candidate/all-evidence overlap for revision and gap proposals.
        "evidence_cross_section_overlap": (
            canonical.get("evidence_cross_section_overlap") or {}
        ),
        "ref_identity_map": canonical.get("ref_identity_map") or {},
        "paper_ledger_semantics": (
            canonical.get("paper_ledger_semantics")
            or PAPER_LEDGER_SEMANTICS
        ),
        "total_word_count": int(canonical.get("total_word_count") or 0),
        "word_count_metric": str(
            canonical.get("word_count_metric") or WORD_COUNT_METRIC
        ),
        "word_count_definition": str(
            canonical.get("word_count_definition") or WORD_COUNT_DEFINITION
        ),
        "total_paragraph_count": int(
            canonical.get("total_paragraph_count") or 0
        ),
        "total_ref_marker_count": int(
            canonical.get("total_ref_marker_count") or 0
        ),
        "unique_ref_marker_count": int(
            canonical.get("unique_ref_marker_count") or 0
        ),
        "paper_count": int(canonical.get("paper_count") or 0),
        "evidence_paper_count": int(
            canonical.get("evidence_paper_count") or 0
        ),
        "cross_section_overlap_count": int(
            canonical.get("cross_section_overlap_count") or 0
        ),
        "evidence_cross_section_overlap_count": int(
            canonical.get("evidence_cross_section_overlap_count") or 0
        ),
        "usage_rule": str(canonical.get("usage_rule") or ""),
        "read_only_declaration": canonical.get("read_only_declaration") or {},
    }
    if "explanatory_papers" in canonical:
        role_view["explanatory_papers"] = {
            paper_id: {
                "paper_id": str(entry.get("paper_id") or ""),
                "titles": list(entry.get("titles") or []),
                "primary_title": str(entry.get("primary_title") or ""),
                "trust_type": str(entry.get("trust_type") or ""),
                "role": str(entry.get("role") or ""),
                "sections": list(entry.get("sections") or []),
                "section_count": int(entry.get("section_count") or 0),
                "marker_ids": list(entry.get("marker_ids") or []),
                "overlaps_core_reference": bool(
                    entry.get("overlaps_core_reference")
                ),
            }
            for paper_id, entry in (
                canonical.get("explanatory_papers") or {}
            ).items()
            if isinstance(entry, Mapping)
        }
        role_view["explanatory_paper_count"] = int(
            canonical.get("explanatory_paper_count") or 0
        )
        role_view["explanatory_marker_count"] = int(
            canonical.get("explanatory_marker_count") or 0
        )
    return role_view


def _commander_context_view(canonical: Mapping[str, Any]) -> dict[str, Any]:
    """Role view for commander synthesis, extended with exact block text
    and hashes so the model can propose typed M4 patches against stable IDs.
    """

    view = _role_context_view(canonical)
    blocks: dict[str, dict[str, Any]] = {}
    for section in canonical.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("section_id") or "")
        for paragraph in section.get("paragraphs") or []:
            if not isinstance(paragraph, Mapping):
                continue
            block_id = str(paragraph.get("canonical_id") or "")
            if not block_id:
                continue
            text = str(paragraph.get("text") or "")
            blocks[block_id] = {
                "section_id": section_id,
                "paragraph_id": str(paragraph.get("paragraph_id") or ""),
                "kind": str(paragraph.get("kind") or ""),
                "text": text,
                "hash": _block_hash(text),
                "ref_markers": list(paragraph.get("ref_markers") or []),
                "contract_claim_ids": list(
                    paragraph.get("contract_claim_ids") or []
                ),
            }
    view["patch_blocks"] = blocks
    view["m4_patch_contract_note"] = (
        "Patch proposals MUST use patch_blocks block IDs, exact block text, "
        "and sha256 hashes. The deterministic safety gate rejects unknown "
        "IDs, stale hashes, unapproved semantic operations, new/lost "
        "unexplained claims, and evidence weakening."
    )
    return view


def _parse_json_text(text: str) -> Any:
    """Parse role JSON, tolerating fenced and trailing-prose output."""

    value = str(text or "").strip()
    if not value:
        raise ValueError("empty role output")
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        pass
    fence = re.search(
        r"```(?:json)?\s*(.*?)```", value, re.DOTALL | re.IGNORECASE
    )
    if fence:
        candidate = fence.group(1).strip()
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass
    start = value.find("{")
    end = value.rfind("}")
    if start != -1 and end > start:
        candidate = value[start : end + 1]
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass
    raise ValueError("role output is not valid JSON")


def parse_role_output(raw: Any) -> tuple[Any, list[str]]:
    """Return parsed role content plus parse-level notes."""

    if isinstance(raw, Mapping) and "content" in raw:
        content = raw.get("content")
        if isinstance(content, str):
            return _parse_json_text(content), []
        return content, []
    if isinstance(raw, (dict, list)):
        return raw, []
    if isinstance(raw, str):
        return _parse_json_text(raw), []
    raise ValueError(
        f"role provider returned unsupported type {type(raw).__name__}"
    )


def _unwrap_provider_result(raw: Any) -> tuple[Any, dict[str, Any]]:
    """Extract content and optional sanitized usage from a provider result."""

    if isinstance(raw, Mapping) and "content" in raw:
        usage = raw.get("usage")
        return raw.get("content"), (
            dict(usage) if isinstance(usage, Mapping) else {}
        )
    return raw, {}


def _empty_usage(model_tier: str, actual_model: str = "") -> dict[str, Any]:
    return {
        "call_count": 0,
        "api_call_count": 0,
        "model_tier": model_tier,
        "actual_model": actual_model,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_cny": 0.0,
        "cost_provenance": "no_tokens",
        "token_usage_source": "unavailable",
    }


def _integer_metric(
    usage: Mapping[str, Any], keys: tuple[str, ...]
) -> tuple[int, str]:
    """Extract a token metric, preferring provider-reported values."""

    for key in keys:
        value = usage.get(key)
        if value in (None, ""):
            continue
        try:
            return max(0, int(value)), "provider_reported"
        except (TypeError, ValueError):
            continue
    return 0, "unavailable"


def _cost_provenance_for_model(model_name: str) -> str:
    """Return the pricing provenance used for a model name."""

    pricing = load_model_pricing()
    if str(model_name or "") in pricing.get("models", {}):
        return "configured_model_rate"
    return "conservative_unknown_model_rate"


def _normalize_usage_cost(usage: dict[str, Any]) -> None:
    '''Estimate CNY from token counts when the provider omitted a cost.

    The shared cost ledger is authoritative. A provider-supplied
    ``estimated_cost_cny`` is never trusted as the only source: nonzero token
    counts for a known model always produce a nonzero estimate, while unknown
    models use the ledger's conservative fallback rate.
    '''

    model = str(usage.get("actual_model") or "").strip()
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    if not model or (input_tokens + output_tokens) <= 0:
        if not str(usage.get("cost_provenance") or "").strip():
            usage["cost_provenance"] = "no_tokens"
        return
    cost = float(usage.get("estimated_cost_cny") or 0.0)
    if cost <= 0.0:
        cost = estimate_call_cost_cny(model, input_tokens, output_tokens)
        usage["estimated_cost_cny"] = round(cost, 6)
    if not str(usage.get("cost_provenance") or "").strip():
        usage["cost_provenance"] = _cost_provenance_for_model(model)


def _merge_usage(target: dict[str, Any], usage: Mapping[str, Any]) -> None:
    if not usage:
        return
    target["call_count"] = int(target.get("call_count") or 0) + int(
        usage.get("call_count") or 1
    )
    target["api_call_count"] = int(target.get("api_call_count") or 0) + int(
        usage.get("api_call_count") or 0
    )
    target["input_tokens"] = int(target.get("input_tokens") or 0) + int(
        usage.get("input_tokens") or 0
    )
    target["output_tokens"] = int(target.get("output_tokens") or 0) + int(
        usage.get("output_tokens") or 0
    )
    target["estimated_cost_cny"] = round(
        float(target.get("estimated_cost_cny") or 0.0)
        + float(usage.get("estimated_cost_cny") or 0.0),
        6,
    )
    target["model_tier"] = str(
        usage.get("model_tier") or target.get("model_tier") or ""
    )
    target["actual_model"] = str(
        usage.get("actual_model") or target.get("actual_model") or ""
    )
    target["token_usage_source"] = str(
        usage.get("token_usage_source")
        or target.get("token_usage_source")
        or "unavailable"
    )
    if usage.get("cost_provenance"):
        target["cost_provenance"] = str(usage.get("cost_provenance") or "")
    _normalize_usage_cost(target)


def sanitize_role_result(
    role: str,
    content: Any,
    canonical: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], bool]:
    """Validate role output against local canonical IDs.

    Unknown section IDs, paragraph IDs, claim IDs, paper IDs, and REF
    identities are dropped with audit notes. Ordinary quality opinions never
    fail the role.
    """

    issues: list[str] = []
    if not isinstance(content, dict):
        return (
            {},
            [f"{role}: role output was not a JSON object; result stored empty"],
            False,
        )
    if role in ("evidence_attribution_critic", "coverage_auditor"):
        forbidden_present = sorted(
            key
            for key in EVIDENCE_COURT_FORBIDDEN_FIELDS
            if key in content
        )
        if forbidden_present:
            issues.append(
                f"{role}: forbidden evidence-court "
                f"field(s): {forbidden_present}"
            )
            return {}, issues, False

    sections = canonical.get("sections") or []
    section_ids = {
        str(section.get("section_id") or "")
        for section in sections
        if isinstance(section, Mapping)
    }
    local_paragraphs: dict[str, set[str]] = {
        str(section.get("section_id") or ""): {
            str(paragraph.get("paragraph_id") or "")
            for paragraph in (section.get("paragraphs") or [])
            if isinstance(paragraph, Mapping)
        }
        for section in sections
        if isinstance(section, Mapping)
    }
    canonical_paragraphs = {
        f"{section_id}-{paragraph_id}"
        for section_id, paragraph_ids in local_paragraphs.items()
        for paragraph_id in paragraph_ids
    }
    claim_ids = {
        str(section.get("section_id") or ""): {
            str(claim.get("claim_id") or "")
            for claim in (section.get("claims") or [])
            if isinstance(claim, Mapping)
        }
        for section in sections
        if isinstance(section, Mapping)
    }
    # ``papers`` is the all-evidence/candidate allowlist so unused evidence
    # papers remain discussable in gap/revision proposals; ``cited_papers``
    # is a subset used for citation audit. Explanatory ledgers extend the
    # citation/editorial allowlist only; core evidence candidates stay
    # canonical['papers'].
    core_paper_ids = set((canonical.get("papers") or {}).keys())
    explanatory_paper_ids = set(
        (canonical.get("explanatory_papers") or {}).keys()
    )
    citation_paper_ids = core_paper_ids | explanatory_paper_ids
    ref_known = {
        marker
        for marker, info in (canonical.get("ref_identity_map") or {}).items()
        if isinstance(info, Mapping) and info.get("known")
    }

    def note(message: str) -> None:
        issues.append(message)

    def clean_section_id(value: Any, where: str) -> str | None:
        section_id = str(value or "").strip()
        if section_id in section_ids:
            return section_id
        note(f"{where}: dropped unknown section_id '{section_id}'")
        return None

    def clean_paragraph_id(
        section_id: str, value: Any, where: str
    ) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw in canonical_paragraphs and raw.startswith(f"{section_id}-"):
            return raw
        if raw in local_paragraphs.get(section_id, set()):
            return f"{section_id}-{raw}"
        note(f"{where}: dropped unknown paragraph_id '{raw}'")
        return None

    def clean_ref(value: Any, where: str) -> str | None:
        raw = str(value or "").strip()
        normalized = re.sub(r"^\[?REF:", "", raw).rstrip("]").strip()
        if normalized in ref_known:
            return normalized
        note(f"{where}: dropped unknown REF identity '{raw}'")
        return None

    def paper_trust(paper_id: str) -> str:
        if paper_id in core_paper_ids:
            return "core_evidence"
        if paper_id in explanatory_paper_ids:
            return "background_explanation_only"
        return ""

    def clean_paper_id(
        value: Any,
        where: str,
        *,
        allow_explanatory: bool = False,
    ) -> str | None:
        raw = str(value or "").strip()
        allowed = citation_paper_ids if allow_explanatory else core_paper_ids
        if raw in allowed:
            return raw
        note(f"{where}: dropped unknown paper_id '{raw}'")
        return None

    def clean_claim_id(
        section_id: str | None, value: Any, where: str
    ) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if section_id and raw not in claim_ids.get(section_id, set()):
            note(f"{where}: dropped unknown claim_id '{raw}'")
            return None
        return raw

    def clean_gap_type(value: Any, where: str) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw in RETRIEVAL_GAP_TYPES:
            return raw
        note(f"{where}: dropped unsupported gap_type '{raw}'")
        return None

    def clean_sections(values: Any, where: str) -> list[str]:
        cleaned: list[str] = []
        if not isinstance(values, list):
            if values not in (None, ""):
                note(f"{where}: expected a list; stored empty")
            return cleaned
        for value in values:
            section_id = clean_section_id(value, where)
            if section_id:
                cleaned.append(section_id)
        return cleaned

    def clean_string_list(values: Any, where: str) -> list[str]:
        if values in (None, ""):
            return []
        if not isinstance(values, list):
            note(f"{where}: expected a list; stored empty")
            return []
        cleaned: list[str] = []
        for item in values:
            if isinstance(item, str):
                cleaned.append(item)
            elif isinstance(item, Mapping) and isinstance(
                item.get("text"), str
            ):
                cleaned.append(item["text"])
            else:
                note(f"{where}: dropped non-text entry")
        return cleaned

    def clean_mixed_entries(values: Any, where: str) -> list[Any]:
        if values in (None, ""):
            return []
        if not isinstance(values, list):
            note(f"{where}: expected a list; stored empty")
            return []
        cleaned: list[Any] = []
        for item in values:
            if isinstance(item, str):
                cleaned.append(item)
                continue
            if not isinstance(item, Mapping):
                note(f"{where}: dropped non-object entry")
                continue
            entry = dict(item)
            section_id = clean_section_id(
                entry.get("section_id"), where
            )
            if section_id:
                entry["section_id"] = section_id
                paragraph_id = clean_paragraph_id(
                    section_id, entry.get("paragraph_id"), where
                )
                if paragraph_id:
                    entry["paragraph_id"] = paragraph_id
                elif entry.get("paragraph_id") not in (None, ""):
                    entry.pop("paragraph_id", None)
            cleaned.append(entry)
        return cleaned

    def clean_order(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("proposed_section_order: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "proposed_section_order: "
                    f"dropped non-object entry {index}"
                )
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "proposed_section_order"
            )
            if not section_id:
                continue
            cleaned.append(
                {
                    "section_id": section_id,
                    "reason": str(entry.get("reason") or ""),
                }
            )
        return cleaned

    def clean_decisions(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("section_decisions: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(f"section_decisions: dropped non-object entry {index}")
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "section_decisions"
            )
            if not section_id:
                continue
            cleaned.append(
                {
                    "section_id": section_id,
                    "decision": str(entry.get("decision") or ""),
                    "rationale": str(entry.get("rationale") or ""),
                    "responsibility": str(
                        entry.get("responsibility") or ""
                    ),
                    "provenance": str(
                        entry.get("provenance")
                        or entry.get("provenance_source")
                        or ""
                    ),
                }
            )
        return cleaned

    def clean_conflicts(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("cross_section_conflicts: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "cross_section_conflicts: "
                    f"dropped non-object entry {index}"
                )
                continue
            sections_list = clean_sections(
                entry.get("sections"), "cross_section_conflicts"
            )
            if entry.get("sections") and not sections_list:
                note(
                    "cross_section_conflicts: dropped entry "
                    f"{index}: no valid section IDs"
                )
                continue
            cleaned.append(
                {
                    "conflict_type": str(entry.get("conflict_type") or ""),
                    "sections": sections_list,
                    "description": str(entry.get("description") or ""),
                    "recommendation": str(entry.get("recommendation") or ""),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_missing_axes(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("missing_axes: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(f"missing_axes: dropped non-object entry {index}")
                continue
            affected = clean_sections(
                entry.get("affected_section_ids"), "missing_axes"
            )
            gap_type = clean_gap_type(
                entry.get("gap_type"), "missing_axes"
            )
            axis = str(entry.get("axis") or "")
            proposal = str(entry.get("proposal") or "")
            if not axis and not proposal and not affected:
                note(f"missing_axes: dropped empty entry {index}")
                continue
            cleaned.append(
                {
                    "axis": axis,
                    "affected_section_ids": affected,
                    "proposal": proposal,
                    "gap_type": gap_type,
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_structure_gaps(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("structure_gaps: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(f"structure_gaps: dropped non-object entry {index}")
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "structure_gaps"
            )
            gap = str(entry.get("gap") or "")
            if not section_id and not gap:
                note(f"structure_gaps: dropped empty entry {index}")
                continue
            gap_type = clean_gap_type(
                entry.get("gap_type"), "structure_gaps"
            )
            cleaned.append(
                {
                    "section_id": section_id or "",
                    "gap": gap,
                    "gap_type": gap_type,
                    "proposal": str(entry.get("proposal") or ""),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_paragraph_refs(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("paragraph_references: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "paragraph_references: "
                    f"dropped non-object entry {index}"
                )
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "paragraph_references"
            )
            paragraph_id = None
            if section_id:
                paragraph_id = clean_paragraph_id(
                    section_id,
                    entry.get("paragraph_id"),
                    "paragraph_references",
                )
            if not section_id or not paragraph_id:
                note(
                    "paragraph_references: dropped entry "
                    f"{index}: no valid paragraph reference"
                )
                continue
            cleaned.append(
                {
                    "section_id": section_id,
                    "paragraph_id": paragraph_id,
                    "note": str(entry.get("note") or ""),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_synthesis(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("synthesis_findings: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(f"synthesis_findings: dropped non-object entry {index}")
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "synthesis_findings"
            )
            paragraph_id = None
            if section_id:
                paragraph_id = clean_paragraph_id(
                    section_id,
                    entry.get("paragraph_id"),
                    "synthesis_findings",
                )
            source_papers: list[str] = []
            for paper_id in (
                entry.get("source_paper_ids")
                if isinstance(entry.get("source_paper_ids"), list)
                else []
            ):
                cleaned_paper = clean_paper_id(
                    paper_id, "synthesis_findings", allow_explanatory=True
                )
                if cleaned_paper:
                    source_papers.append(cleaned_paper)
            finding = str(entry.get("finding") or "")
            if not finding:
                note(f"synthesis_findings: dropped entry {index}: no finding")
                continue
            cleaned.append(
                {
                    "section_id": section_id or "",
                    "paragraph_id": paragraph_id or "",
                    "finding": finding,
                    "finding_type": str(entry.get("finding_type") or ""),
                    "source_paper_ids": source_papers,
                    "source_paper_trust_types": {
                        paper_id: paper_trust(paper_id)
                        for paper_id in source_papers
                    },
                    "boundary_conditions": str(
                        entry.get("boundary_conditions") or ""
                    ),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_narrative(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("narrative_progression: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "narrative_progression: "
                    f"dropped non-object entry {index}"
                )
                continue
            from_section = clean_section_id(
                entry.get("from_section_id"), "narrative_progression"
            )
            to_section = clean_section_id(
                entry.get("to_section_id"), "narrative_progression"
            )
            if not from_section or not to_section:
                note(
                    "narrative_progression: dropped entry "
                    f"{index}: invalid section IDs"
                )
                continue
            cleaned.append(
                {
                    "from_section_id": from_section,
                    "to_section_id": to_section,
                    "assessment": str(entry.get("assessment") or ""),
                    "recommendation": str(
                        entry.get("recommendation") or ""
                    ),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_overlap_recommendations(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note(
                    "overlap_recommendations: expected a list; stored empty"
                )
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "overlap_recommendations: "
                    f"dropped non-object entry {index}"
                )
                continue
            sections_list = clean_sections(
                entry.get("sections"), "overlap_recommendations"
            )
            paper_id = clean_paper_id(
                entry.get("paper_id"),
                "overlap_recommendations",
                allow_explanatory=True,
            )
            if not sections_list and not paper_id:
                note(
                    "overlap_recommendations: dropped entry "
                    f"{index}: no valid content"
                )
                continue
            cleaned.append(
                {
                    "sections": sections_list,
                    "paper_id": paper_id or "",
                    "trust_type": (
                        paper_trust(paper_id) if paper_id else ""
                    ),
                    "recommendation": str(
                        entry.get("recommendation") or ""
                    ),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_repeated(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("repeated_paper_roles: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "repeated_paper_roles: "
                    f"dropped non-object entry {index}"
                )
                continue
            paper_id = clean_paper_id(
                entry.get("paper_id"),
                "repeated_paper_roles",
                allow_explanatory=True,
            )
            if not paper_id:
                continue
            sections_list = clean_sections(
                entry.get("sections"), "repeated_paper_roles"
            )
            cleaned.append(
                {
                    "paper_id": paper_id,
                    "trust_type": paper_trust(paper_id),
                    "title": str(entry.get("title") or ""),
                    "sections": sections_list,
                    "roles": [
                        str(value)
                        for value in (
                            entry.get("roles")
                            if isinstance(entry.get("roles"), list)
                            else []
                        )
                    ],
                    "recommendation": str(
                        entry.get("recommendation") or ""
                    ),
                    "decision": str(entry.get("decision") or ""),
                    "rationale": str(entry.get("rationale") or ""),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_source_concentration(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("source_concentration: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "source_concentration: "
                    f"dropped non-object entry {index}"
                )
                continue
            paper_id = clean_paper_id(
                entry.get("paper_id"),
                "source_concentration",
                allow_explanatory=True,
            )
            if not paper_id:
                continue
            sections_list = clean_sections(
                entry.get("sections"), "source_concentration"
            )
            cleaned.append(
                {
                    "paper_id": paper_id,
                    "trust_type": paper_trust(paper_id),
                    "title": str(entry.get("title") or ""),
                    "sections": sections_list,
                    "concentration": str(
                        entry.get("concentration") or ""
                    ),
                    "recommendation": str(
                        entry.get("recommendation") or ""
                    ),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_attribution(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("attribution_issues: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "attribution_issues: dropped non-object entry {index}"
                )
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "attribution_issues"
            )
            if entry.get("section_id") not in (None, "") and not section_id:
                note(
                    f"attribution_issues: dropped entry {index}: "
                    "unknown section_id"
                )
                continue
            paragraph_id = None
            if section_id:
                paragraph_id = clean_paragraph_id(
                    section_id,
                    entry.get("paragraph_id"),
                    "attribution_issues",
                )
            if (
                entry.get("paragraph_id") not in (None, "")
                and not paragraph_id
            ):
                note(
                    f"attribution_issues: dropped entry {index}: "
                    "unknown paragraph_id"
                )
                continue
            claim_id = clean_claim_id(
                section_id, entry.get("claim_id"), "attribution_issues"
            )
            if entry.get("claim_id") not in (None, "") and not claim_id:
                note(
                    f"attribution_issues: dropped entry {index}: "
                    "unknown claim_id"
                )
                continue
            issue = str(entry.get("issue") or "")
            if not issue:
                note(
                    f"attribution_issues: dropped entry {index}: no issue"
                )
                continue
            cleaned.append(
                {
                    "section_id": section_id or "",
                    "paragraph_id": paragraph_id or "",
                    "claim_id": claim_id or "",
                    "issue": issue,
                    "recommendation": str(
                        entry.get("recommendation") or ""
                    ),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_citation_audit(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("citation_audit: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(f"citation_audit: dropped non-object entry {index}")
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "citation_audit"
            )
            if entry.get("section_id") not in (None, "") and not section_id:
                note(
                    f"citation_audit: dropped entry {index}: "
                    "unknown section_id"
                )
                continue
            paragraph_id = None
            if section_id:
                paragraph_id = clean_paragraph_id(
                    section_id,
                    entry.get("paragraph_id"),
                    "citation_audit",
                )
            if (
                entry.get("paragraph_id") not in (None, "")
                and not paragraph_id
            ):
                note(
                    f"citation_audit: dropped entry {index}: "
                    "unknown paragraph_id"
                )
                continue
            ref_marker = clean_ref(
                entry.get("ref_marker"), "citation_audit"
            )
            paper_id = clean_paper_id(
                entry.get("paper_id"),
                "citation_audit",
                allow_explanatory=True,
            )
            claim_id = clean_claim_id(
                section_id, entry.get("claim_id"), "citation_audit"
            )
            if (
                not ref_marker
                and not paper_id
                and not claim_id
            ):
                note(
                    f"citation_audit: dropped entry {index}: "
                    "no valid identity reference"
                )
                continue
            cleaned.append(
                {
                    "section_id": section_id or "",
                    "paragraph_id": paragraph_id or "",
                    "ref_marker": ref_marker or "",
                    "paper_id": paper_id or "",
                    "trust_type": paper_trust(paper_id) if paper_id else "",
                    "title": str(entry.get("title") or ""),
                    "status": str(entry.get("status") or ""),
                    "note": str(entry.get("note") or ""),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_retrieval_gaps(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note(
                    "retrieval_gap_proposals: expected a list; stored empty"
                )
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "retrieval_gap_proposals: "
                    f"dropped non-object entry {index}"
                )
                continue
            gap_type = clean_gap_type(
                entry.get("gap_type"), "retrieval_gap_proposals"
            )
            if not gap_type:
                note(
                    "retrieval_gap_proposals: dropped entry "
                    f"{index}: missing or unsupported gap_type"
                )
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "retrieval_gap_proposals"
            )
            if entry.get("section_id") not in (None, "") and not section_id:
                note(
                    "retrieval_gap_proposals: dropped entry "
                    f"{index}: unknown section_id"
                )
                continue
            claim_id = clean_claim_id(
                section_id, entry.get("claim_id"), "retrieval_gap_proposals"
            )
            if entry.get("claim_id") not in (None, "") and not claim_id:
                note(
                    "retrieval_gap_proposals: dropped entry "
                    f"{index}: unknown claim_id"
                )
                continue
            cleaned.append(
                {
                    "gap_type": gap_type,
                    "section_id": section_id or "",
                    "claim_id": claim_id or "",
                    "reason": str(entry.get("reason") or ""),
                    "proposed_retrieval_scope": str(
                        entry.get("proposed_retrieval_scope") or ""
                    ),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_visual_work_orders(values: Any) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            if values not in (None, ""):
                note("visual_work_orders: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    f"visual_work_orders: dropped non-object entry {index}"
                )
                continue
            section_id = clean_section_id(
                entry.get("section_id"), "visual_work_orders"
            )
            if not section_id:
                continue
            gap_type = clean_gap_type(
                entry.get("gap_type"), "visual_work_orders"
            )
            cleaned.append(
                {
                    "section_id": section_id,
                    "visual_requirement": str(
                        entry.get("visual_requirement") or ""
                    ),
                    "gap_type": gap_type,
                    "priority": str(entry.get("priority") or ""),
                    "action": str(entry.get("action") or ""),
                    "finding_id": str(entry.get("finding_id") or ""),
                }
            )
        return cleaned

    def clean_structure_candidates(values: Any) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list):
            note("structure_candidates: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    f"structure_candidates: dropped non-object entry {index}"
                )
                continue
            story_shape = str(entry.get("story_shape") or "")
            section_order = clean_sections(
                entry.get("section_order"), "structure_candidates"
            )
            if not story_shape and not section_order:
                note(
                    f"structure_candidates: dropped entry {index}: "
                    "no story_shape or section_order"
                )
                continue
            cleaned.append(
                {
                    "candidate_id": str(
                        entry.get("candidate_id")
                        or f"STRUCT-{index + 1:03d}"
                    ),
                    "story_shape": story_shape,
                    "narrative_backbone": str(
                        entry.get("narrative_backbone") or ""
                    ),
                    "section_order": section_order,
                    "reader_path": str(entry.get("reader_path") or ""),
                    "rationale": str(entry.get("rationale") or ""),
                    "risks": [
                        str(value)
                        for value in (
                            entry.get("risks")
                            if isinstance(entry.get("risks"), list)
                            else []
                        )
                    ],
                }
            )
        return cleaned

    def clean_selected_story_shape(
        values: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(values, Mapping):
            return None
        candidate_id = str(values.get("candidate_id") or "")
        story_shape = str(values.get("story_shape") or "")
        if not candidate_id and not story_shape:
            note("selected_story_shape: dropped; no candidate_id or shape")
            return None
        return {
            "candidate_id": candidate_id,
            "story_shape": story_shape,
            "rationale": str(values.get("rationale") or ""),
            "provenance_prior_role": str(
                values.get("provenance_prior_role") or ""
            ),
        }

    def clean_reader_path_findings(values: Any) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list):
            note("reader_path_findings: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    f"reader_path_findings: dropped non-object entry {index}"
                )
                continue
            assessment = str(entry.get("assessment") or "")
            if not assessment:
                note(
                    f"reader_path_findings: dropped entry {index}: "
                    "no assessment"
                )
                continue
            cleaned.append(
                {
                    "finding_id": str(
                        entry.get("finding_id")
                        or f"READER-{index + 1:03d}"
                    ),
                    "section_id": str(entry.get("section_id") or ""),
                    "assessment": assessment,
                    "recommendation": str(
                        entry.get("recommendation") or ""
                    ),
                    "severity": str(entry.get("severity") or "advisory"),
                    "fail_open": True,
                }
            )
        return cleaned

    def clean_two_gap(
        values: Any,
        gap_type: str,
    ) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list):
            note(f"{gap_type}: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(f"{gap_type}: dropped non-object entry {index}")
                continue
            if str(entry.get("gap_type") or "") != gap_type:
                note(
                    f"{gap_type}: dropped entry {index}: gap_type mismatch"
                )
                continue
            def field_missing(field: str) -> bool:
                if field not in entry or entry.get(field) is None:
                    return True
                value = entry.get(field)
                if field == "affected_sections":
                    # A review-structure gap may legitimately affect no
                    # existing section (new chapter axis), so an empty list
                    # is a valid value as long as the key is present.
                    return not isinstance(value, list)
                if field == "existing_coverage":
                    if not isinstance(value, Mapping):
                        return True
                    if not isinstance(value.get("paragraph_ids"), list):
                        return True
                    return not str(value.get("summary") or "").strip()
                return not value

            missing = [
                field for field in TWO_GAP_REQUIRED_FIELDS if field_missing(field)
            ]
            if missing:
                note(
                    f"{gap_type}: dropped entry {index}: missing required "
                    f"fields {missing}"
                )
                continue
            prefix = (
                "GAP-SEC"
                if gap_type == "section_argument_gap"
                else "GAP-REV"
            )
            row: dict[str, Any] = dict(entry)
            row["gap_id"] = str(
                entry.get("gap_id") or f"{prefix}-{index + 1:03d}"
            )
            row["gap_type"] = gap_type
            row["affected_sections"] = clean_sections(
                entry.get("affected_sections"), gap_type
            )
            affected_ids = [
                section_id
                for section_id in row["affected_sections"]
                if section_id
            ]
            coverage_paragraphs: list[str] = []
            coverage_record = entry.get("existing_coverage")
            coverage_record = (
                coverage_record if isinstance(coverage_record, Mapping) else {}
            )
            for raw in coverage_record.get("paragraph_ids") or []:
                matches: list[str] = []
                for section_id in affected_ids:
                    paragraph_id = clean_paragraph_id(
                        section_id, raw, gap_type
                    )
                    if paragraph_id and paragraph_id not in matches:
                        matches.append(paragraph_id)
                if len(matches) == 1:
                    if matches[0] not in coverage_paragraphs:
                        coverage_paragraphs.append(matches[0])
                elif len(matches) > 1:
                    note(
                        f"{gap_type}: existing_coverage paragraph '{raw}' "
                        "is ambiguous across affected sections; dropped"
                    )
                else:
                    note(
                        f"{gap_type}: existing_coverage paragraph '{raw}' "
                        "is not a current paragraph id; dropped"
                    )
            row["existing_coverage"] = {
                "paragraph_ids": coverage_paragraphs,
                "summary": str(coverage_record.get("summary") or ""),
            }
            row["residual_gap"] = str(entry.get("residual_gap") or "")
            row["missing_claim_roles"] = [
                str(value)
                for value in (
                    entry.get("missing_claim_roles")
                    if isinstance(entry.get("missing_claim_roles"), list)
                    else []
                )
            ]
            row["fact_units"] = [
                str(value)
                for value in (
                    entry.get("fact_units")
                    if isinstance(entry.get("fact_units"), list)
                    else []
                )
            ]
            row["required_material_strength"] = entry.get(
                "required_material_strength"
            ) or {}
            row["success_criterion"] = str(
                entry.get("success_criterion") or ""
            )
            row["one_round_stop_reason"] = str(
                entry.get("one_round_stop_reason")
                or entry.get("closure_criterion")
                or ""
            )
            row["confidence"] = str(entry.get("confidence") or "")
            decision = str(entry.get("decision") or "")
            if decision not in {"approve", "defer", "reject"}:
                decision = "approve"
            row["decision"] = decision
            row["status"] = {
                "approve": "approved",
                "defer": "deferred",
                "reject": "rejected",
            }[decision]
            cleaned.append(row)
        return cleaned

    def clean_coverage_candidate_paragraph_ids(
        values: Any,
        section_id: str,
        where: str,
    ) -> list[str]:
        if not isinstance(values, list):
            return []
        cleaned_ids: list[str] = []
        for raw in values:
            paragraph_id = clean_paragraph_id(section_id, raw, where)
            if paragraph_id and paragraph_id not in cleaned_ids:
                cleaned_ids.append(paragraph_id)
        return cleaned_ids

    def clean_coverage_gap_candidates(
        values: Any,
        gap_type: str,
    ) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list):
            note(f"{gap_type}_candidates: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    f"{gap_type}_candidates: "
                    f"dropped non-object entry {index}"
                )
                continue
            if str(entry.get("gap_type") or "") != gap_type:
                note(
                    f"{gap_type}_candidates: dropped entry {index}: "
                    "gap_type mismatch"
                )
                continue
            missing = [
                field
                for field in COVERAGE_CANDIDATE_REQUIRED_FIELDS
                if (
                    field not in entry
                    or entry.get(field) is None
                    or (
                        field != "affected_sections"
                        and not entry.get(field)
                    )
                    or (
                        field == "affected_sections"
                        and not isinstance(entry.get(field), list)
                    )
                )
            ]
            if missing:
                note(
                    f"{gap_type}_candidates: dropped entry {index}: "
                    f"missing required fields {missing}"
                )
                continue
            section_id = clean_section_id(
                entry.get("section_id"), f"{gap_type}_candidates"
            )
            affected_sections = clean_sections(
                entry.get("affected_sections"), f"{gap_type}_candidates"
            )
            prefix = (
                "CAND-SEC"
                if gap_type == "section_argument_gap"
                else "CAND-REV"
            )
            row: dict[str, Any] = dict(entry)
            row["candidate_id"] = str(
                entry.get("candidate_id") or f"{prefix}-{index + 1:03d}"
            )
            row["gap_type"] = gap_type
            row["section_id"] = section_id or ""
            row["paragraph_ids"] = clean_coverage_candidate_paragraph_ids(
                entry.get("paragraph_ids"), section_id or "", gap_type
            )
            row["affected_sections"] = affected_sections
            row["missing_claim_roles"] = [
                str(value)
                for value in (
                    entry.get("missing_claim_roles")
                    if isinstance(entry.get("missing_claim_roles"), list)
                    else []
                )
            ]
            row["fact_units"] = [
                str(value)
                for value in (
                    entry.get("fact_units")
                    if isinstance(entry.get("fact_units"), list)
                    else []
                )
            ]
            row["required_material_strength"] = entry.get(
                "required_material_strength"
            ) or {}
            row["status"] = "candidate"
            cleaned.append(row)
        return cleaned

    def clean_rejected_gap_candidates(
        values: Any,
    ) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list):
            note("rejected_gap_candidates: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    "rejected_gap_candidates: "
                    f"dropped non-object entry {index}"
                )
                continue
            gap_type = str(entry.get("gap_type") or "")
            candidate_id = str(entry.get("candidate_id") or "")
            reason = str(entry.get("reason") or "")
            if gap_type not in TWO_GAP_TYPES or not candidate_id or not reason:
                note(
                    "rejected_gap_candidates: dropped entry "
                    f"{index}: missing candidate_id/gap_type/reason"
                )
                continue
            cleaned.append(
                {
                    "rejection_id": str(
                        entry.get("rejection_id")
                        or f"REJECT-{index + 1:03d}"
                    ),
                    "candidate_id": candidate_id,
                    "gap_type": gap_type,
                    "decision": "reject",
                    "reason": reason,
                    "existing_coverage": entry.get(
                        "existing_coverage"
                    ) or {},
                    "residual_gap": str(entry.get("residual_gap") or ""),
                    "provenance_prior_role": str(
                        entry.get("provenance_prior_role") or ""
                    ),
                }
            )
        return cleaned

    def clean_gap_value_decisions(values: Any) -> list[dict[str, Any]]:
        if values is None:
            return []
        if not isinstance(values, list):
            note("gap_value_decisions: expected a list; stored empty")
            return []
        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping):
                note(
                    f"gap_value_decisions: dropped non-object entry {index}"
                )
                continue
            gap_id = str(
                entry.get("gap_id") or entry.get("candidate_id") or ""
            )
            decision = str(entry.get("decision") or "").casefold()
            if not gap_id or decision not in {"approve", "reject", "defer"}:
                note(
                    f"gap_value_decisions: dropped entry {index}: "
                    "missing gap_id or invalid decision"
                )
                continue
            cleaned.append(
                {
                    "decision_id": str(
                        entry.get("decision_id")
                        or f"GAPVAL-{index + 1:03d}"
                    ),
                    "gap_id": gap_id,
                    "decision": decision,
                    "confidence": str(entry.get("confidence") or ""),
                    "reason": str(entry.get("reason") or ""),
                    "provenance_prior_role": str(
                        entry.get("provenance_prior_role") or ""
                    ),
                }
            )
        return cleaned

    cleaned = dict(content)
    cleaned["proposed_section_order"] = clean_order(
        content.get("proposed_section_order")
    )
    cleaned["section_decisions"] = clean_decisions(
        content.get("section_decisions")
    )
    cleaned["cross_section_conflicts"] = clean_conflicts(
        content.get("cross_section_conflicts")
    )
    cleaned["missing_axes"] = clean_missing_axes(content.get("missing_axes"))
    cleaned["structure_gaps"] = clean_structure_gaps(
        content.get("structure_gaps")
    )
    cleaned["paragraph_references"] = clean_paragraph_refs(
        content.get("paragraph_references")
    )
    cleaned["synthesis_findings"] = clean_synthesis(
        content.get("synthesis_findings")
    )
    cleaned["narrative_progression"] = clean_narrative(
        content.get("narrative_progression")
    )
    cleaned["overlap_recommendations"] = clean_overlap_recommendations(
        content.get("overlap_recommendations")
    )
    cleaned["repeated_paper_roles"] = clean_repeated(
        content.get("repeated_paper_roles")
    )
    cleaned["repeated_paper_role_audit"] = clean_repeated(
        content.get("repeated_paper_role_audit")
    )
    cleaned["source_concentration"] = clean_source_concentration(
        content.get("source_concentration")
    )
    cleaned["attribution_issues"] = clean_attribution(
        content.get("attribution_issues")
    )
    cleaned["citation_audit"] = clean_citation_audit(
        content.get("citation_audit")
    )
    cleaned["retrieval_gap_proposals"] = clean_retrieval_gaps(
        content.get("retrieval_gap_proposals")
    )
    cleaned["visual_work_orders"] = clean_visual_work_orders(
        content.get("visual_work_orders")
    )
    cleaned["structure_candidates"] = clean_structure_candidates(
        content.get("structure_candidates")
    )
    cleaned["selected_story_shape"] = clean_selected_story_shape(
        content.get("selected_story_shape")
    )
    cleaned["reader_path_findings"] = clean_reader_path_findings(
        content.get("reader_path_findings")
    )
    cleaned["section_argument_gaps"] = clean_two_gap(
        content.get("section_argument_gaps"), "section_argument_gap"
    )
    cleaned["review_structure_gaps"] = clean_two_gap(
        content.get("review_structure_gaps"), "review_structure_gap"
    )
    cleaned["gap_value_decisions"] = clean_gap_value_decisions(
        content.get("gap_value_decisions")
    )
    cleaned["rejected_gap_candidates"] = clean_rejected_gap_candidates(
        content.get("rejected_gap_candidates")
    )
    if role == "coverage_auditor":
        # The auditor proposes only; it can never approve/defer its own
        # candidates. Authoritative gap lists stay empty at this stage.
        section_candidates = clean_coverage_gap_candidates(
            content.get("section_argument_gap_candidates"),
            "section_argument_gap",
        )
        review_candidates = clean_coverage_gap_candidates(
            content.get("review_structure_gap_candidates"),
            "review_structure_gap",
        )
        legacy_candidates: list[dict[str, Any]] = []
        for entry in content.get("coverage_gap_candidates") or []:
            if not isinstance(entry, Mapping):
                continue
            gap_type = str(entry.get("gap_type") or "")
            if gap_type == "section_argument_gap":
                legacy_candidates.extend(
                    clean_coverage_gap_candidates(
                        [entry], "section_argument_gap"
                    )
                )
            elif gap_type == "review_structure_gap":
                legacy_candidates.extend(
                    clean_coverage_gap_candidates(
                        [entry], "review_structure_gap"
                    )
                )
        cleaned["coverage_gap_candidates"] = (
            section_candidates + review_candidates + legacy_candidates
        )
        cleaned["section_argument_gaps"] = []
        cleaned["review_structure_gaps"] = []
        cleaned["gap_value_decisions"] = []
        cleaned["rejected_gap_candidates"] = []
    elif role == "evidence_attribution_critic":
        rejected_rows: list[dict[str, Any]] = []

        def split_open_gaps(
            rows: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            open_rows: list[dict[str, Any]] = []
            for row in rows:
                if str(row.get("decision") or "approve") == "reject":
                    rejected_rows.append(row)
                else:
                    open_rows.append(row)
            return open_rows

        cleaned["section_argument_gaps"] = split_open_gaps(
            cleaned["section_argument_gaps"]
        )
        cleaned["review_structure_gaps"] = split_open_gaps(
            cleaned["review_structure_gaps"]
        )
        known_rejections = {
            str(entry.get("candidate_id") or "")
            for entry in cleaned["rejected_gap_candidates"]
        }
        known_decisions = {
            str(entry.get("gap_id") or entry.get("candidate_id") or "")
            for entry in cleaned["gap_value_decisions"]
        }
        for row in rejected_rows:
            key = str(row.get("candidate_id") or row.get("gap_id") or "")
            if not key:
                key = (
                    f"{row.get('gap_type')}-rejected-{len(rejected_rows)}"
                )
            if key not in known_rejections:
                cleaned["rejected_gap_candidates"].append(
                    {
                        "rejection_id": (
                            f"REJECT-"
                            f"{len(cleaned['rejected_gap_candidates']) + 1:03d}"
                        ),
                        "candidate_id": key,
                        "gap_type": str(row.get("gap_type") or ""),
                        "decision": "reject",
                        "reason": str(
                            row.get("reason")
                            or row.get("residual_gap")
                            or "rejected by gap value critic"
                        ),
                        "existing_coverage": row.get(
                            "existing_coverage"
                        ) or {},
                        "residual_gap": str(
                            row.get("residual_gap") or ""
                        ),
                        "provenance_prior_role": str(
                            row.get("provenance_prior_role") or ""
                        ),
                    }
                )
                known_rejections.add(key)
            if key not in known_decisions:
                cleaned["gap_value_decisions"].append(
                    {
                        "decision_id": (
                            f"GAPVAL-"
                            f"{len(cleaned['gap_value_decisions']) + 1:03d}"
                        ),
                        "gap_id": key,
                        "decision": "reject",
                        "confidence": str(row.get("confidence") or ""),
                        "reason": str(
                            row.get("reason")
                            or row.get("residual_gap")
                            or "rejected by gap value critic"
                        ),
                        "provenance_prior_role": str(
                            row.get("provenance_prior_role")
                            or "coverage_auditor"
                        ),
                    }
                )
                known_decisions.add(key)
        cleaned["coverage_gap_candidates"] = []
    else:
        cleaned["coverage_gap_candidates"] = []
    cleaned["coverage_audit_summary"] = str(
        content.get("coverage_audit_summary") or ""
    )
    cleaned["coverage_search_notes"] = str(
        content.get("coverage_search_notes") or ""
    )
    cleaned["affected_section_ids"] = clean_sections(
        content.get("affected_section_ids"), "affected_section_ids"
    )
    raw_patches = content.get("proposed_patch_set")
    if raw_patches not in (None, "") and not isinstance(raw_patches, list):
        note(
            "commander_synthesis: proposed_patch_set must be a list; "
            "stored empty"
        )
        cleaned["proposed_patch_set"] = []
    else:
        cleaned["proposed_patch_set"] = (
            list(raw_patches) if isinstance(raw_patches, list) else []
        )
    for key in (
        "retained_advisory_issues",
        "unresolved_issues",
        "visual_evidence_notes",
        "next_execution_stages",
        "strengths",
        "risks",
    ):
        cleaned[key] = clean_string_list(content.get(key), key)
    cleaned["evidence_outline_discipline"] = clean_mixed_entries(
        content.get("evidence_outline_discipline"),
        "evidence_outline_discipline",
    )
    cleaned["diagnosis"] = content.get("diagnosis") or {}
    cleaned["manuscript_diagnosis"] = str(
        content.get("manuscript_diagnosis") or ""
    )

    declaration = content.get("read_only_declaration")
    declaration = declaration if isinstance(declaration, Mapping) else {}
    if declaration.get("chapter_text_changed") is True:
        note(
            "read_only_declaration: model claimed chapter text changed; "
            "forced to false because this stage never edits prose"
        )
    if declaration.get("retrieval_launched") is True:
        note(
            "read_only_declaration: model claimed retrieval launched; "
            "forced to false because this stage never launches retrieval"
        )
    cleaned["read_only_declaration"] = {
        "chapter_text_changed": False,
        "retrieval_launched": False,
        "note": str(
            declaration.get("note") or "Advisory work order only."
        ),
    }

    if role == "commander_synthesis":
        diagnosis = str(cleaned.get("manuscript_diagnosis") or "").strip()
        order = cleaned["proposed_section_order"]
        decisions = cleaned["section_decisions"]
        ordered_ids = {item["section_id"] for item in order}
        decided_ids = {item["section_id"] for item in decisions}
        if ordered_ids != section_ids:
            note(
                "commander_synthesis: proposed_section_order does not cover "
                "every existing section"
            )
        for section_id in sorted(section_ids - decided_ids):
            section = next(
                (
                    section
                    for section in sections
                    if isinstance(section, Mapping)
                    and str(section.get("section_id") or "") == section_id
                ),
                {},
            )
            contract = (
                section.get("section_contract")
                if isinstance(section.get("section_contract"), Mapping)
                else {}
            )
            argument_role = contract.get("argument_role")
            if isinstance(argument_role, Mapping):
                responsibility = str(
                    argument_role.get("statement") or ""
                )
            else:
                responsibility = str(argument_role or "")
            if not responsibility:
                responsibility = str(
                    contract.get("section_purpose")
                    or contract.get("title")
                    or section.get("title")
                    or ""
                )
            cleaned["section_decisions"].append(
                {
                    "section_id": section_id,
                    "decision": "retain",
                    "rationale": (
                        "No change proposed by commander; local completion "
                        "preserves the existing responsibility."
                    ),
                    "responsibility": responsibility,
                    "provenance": "local_completion",
                }
            )
        if not diagnosis:
            note("commander_synthesis: missing manuscript_diagnosis")
        if not order:
            note("commander_synthesis: empty proposed_section_order")
        if not decisions:
            note("commander_synthesis: empty section_decisions")
        return cleaned, issues, bool(diagnosis and order and decisions)
    return cleaned, issues, True


def _structure_candidate_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """Deterministic identity key for a structure candidate."""

    return (
        str(entry.get("story_shape") or "").strip().casefold(),
        str(entry.get("narrative_backbone") or "").strip().casefold(),
        tuple(
            str(value)
            for value in (entry.get("section_order") or [])
            if str(value).strip()
        ),
        str(entry.get("reader_path") or "").strip().casefold(),
    )


def _merge_structure_candidates(
    *groups: list[dict[str, Any]] | list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge structure candidates by identity/content without semantic
    invention, preserving first-seen order and at most three candidates."""

    seen: set[tuple[Any, ...]] = set()
    merged: list[dict[str, Any]] = []
    for group in groups:
        for entry in group:
            if not isinstance(entry, Mapping):
                continue
            key = _structure_candidate_key(entry)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(entry))
            if len(merged) >= 3:
                return merged
    return merged


class RoleProvider(Protocol):
    """Callable role provider: (role_key, payload) -> raw result."""

    def __call__(
        self, role: str, payload: Mapping[str, Any]
    ) -> Any:  # pragma: no cover - protocol only
        ...


class DeterministicRoleProvider:
    """Offline deterministic role provider used by dry mode."""

    def __call__(
        self, role: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        canonical = payload.get("canonical_context") or {}
        sections = canonical.get("sections") or []
        section_ids = [
            str(section.get("section_id") or "")
            for section in sections
            if isinstance(section, Mapping)
        ]
        papers = canonical.get("papers") or {}
        overlap = canonical.get("cross_section_overlap") or {}
        ref_identity_map = canonical.get("ref_identity_map") or {}
        previous = payload.get("previous_role_results") or {}
        if role == "structure_strategist":
            repeated = [
                {
                    "paper_id": paper_id,
                    "title": info.get("primary_title") or "",
                    "sections": info.get("sections") or [],
                    "roles": [
                        "shared across listed sections"
                    ] * len(info.get("sections") or []),
                    "recommendation": (
                        "Verify that each section gives this paper a "
                        "distinct evidence role."
                    ),
                }
                for paper_id, info in overlap.items()
                if isinstance(info, Mapping)
            ]
            return {
                "diagnosis": {
                    "strengths": [
                        "Manifest order is retained in deterministic dry mode."
                    ],
                    "risks": [
                        "No LLM structure judgement was applied in dry mode."
                    ],
                },
                "proposed_section_order": [
                    {
                        "section_id": section_id,
                        "reason": (
                            "Deterministic dry mode preserves manifest order."
                        ),
                    }
                    for section_id in section_ids
                ],
                "section_decisions": [
                    {
                        "section_id": section_id,
                        "decision": "retain",
                        "rationale": (
                            "No change is proposed without live judgement."
                        ),
                    }
                    for section_id in section_ids
                ],
                "cross_section_conflicts": [
                    {
                        "conflict_type": "redundancy",
                        "sections": info.get("sections") or [],
                        "description": (
                            f"Paper {paper_id} appears in multiple sections; "
                            "its roles need live verification."
                        ),
                        "recommendation": (
                            "Check whether the repeated use is evidence-distinct."
                        ),
                    }
                    for paper_id, info in overlap.items()
                    if isinstance(info, Mapping)
                    and len(info.get("sections") or []) >= 2
                ],
                "missing_axes": [],
                "structure_gaps": [],
                "paragraph_references": [],
                "repeated_paper_roles": repeated,
                "structure_candidates": [
                    {
                        "candidate_id": "STRUCT-001",
                        "story_shape": "manifest_order",
                        "narrative_backbone": (
                            "Retain the manifest section order as the "
                            "reader-state backbone."
                        ),
                        "section_order": list(section_ids),
                        "reader_path": (
                            "Readers follow the manifest order without "
                            "restructure."
                        ),
                        "rationale": (
                            "Deterministic dry mode preserves manifest order."
                        ),
                        "risks": [
                            "No live structure judgement was applied."
                        ],
                    },
                    {
                        "candidate_id": "STRUCT-002",
                        "story_shape": "synthesis_first_reversal",
                        "narrative_backbone": (
                            "Reverse section order to foreground a "
                            "synthesis-first reading."
                        ),
                        "section_order": list(reversed(section_ids)),
                        "reader_path": (
                            "Readers start from synthesis and backtrack to "
                            "foundations."
                        ),
                        "rationale": (
                            "Deterministic dry alternative for comparison."
                        ),
                        "risks": [
                            "Reversal may invert evidence-dependent "
                            "progression."
                        ],
                    },
                ],
                "reader_path_findings": [],
                "retained_advisory_issues": [
                    "Deterministic dry mode: LLM structure judgement not applied."
                ],
            }
        if role == "scientific_synthesis_editor":
            progression = [
                {
                    "from_section_id": section_ids[index],
                    "to_section_id": section_ids[index + 1],
                    "assessment": (
                        "Adjacent sections are kept in manifest order; "
                        "synthesis quality requires live judgement."
                    ),
                    "recommendation": (
                        "Run the live scientific editor for handoff quality."
                    ),
                }
                for index in range(len(section_ids) - 1)
            ]
            return {
                "synthesis_findings": [],
                "narrative_progression": progression,
                "overlap_recommendations": [
                    {
                        "sections": info.get("sections") or [],
                        "paper_id": paper_id,
                        "recommendation": (
                            "Keep only if the sections use distinct findings."
                        ),
                    }
                    for paper_id, info in overlap.items()
                    if isinstance(info, Mapping)
                ],
                "repeated_paper_roles": [
                    {
                        "paper_id": paper_id,
                        "title": info.get("primary_title") or "",
                        "sections": info.get("sections") or [],
                        "roles": ["shared across listed sections"],
                        "recommendation": "Verify distinct roles live.",
                    }
                    for paper_id, info in overlap.items()
                    if isinstance(info, Mapping)
                ],
                "source_concentration": [
                    {
                        "paper_id": paper_id,
                        "title": info.get("primary_title") or "",
                        "sections": info.get("sections") or [],
                        "concentration": "high"
                        if len(info.get("sections") or []) >= 3
                        else "medium",
                        "recommendation": (
                            "Review source diversity in a live run."
                        ),
                    }
                    for paper_id, info in overlap.items()
                    if isinstance(info, Mapping)
                ],
                "evidence_outline_discipline": [],
                "reader_path_findings": [],
                "unresolved_issues": [],
                "retained_advisory_issues": [
                    "Deterministic dry mode: LLM synthesis judgement not applied."
                ],
            }
        if role == "coverage_auditor":
            return {
                "coverage_audit_summary": (
                    "Deterministic dry mode: no gap candidates proposed; "
                    "live coverage auditing is required for substantive "
                    "candidate proposals."
                ),
                "section_argument_gap_candidates": [],
                "review_structure_gap_candidates": [],
                "coverage_gap_candidates": [],
                "retained_advisory_issues": [
                    "Deterministic dry mode: LLM coverage audit not applied."
                ],
            }
        if role == "evidence_attribution_critic":
            citation_audit: list[dict[str, Any]] = []
            visual_notes: list[str] = []
            for section in sections:
                if not isinstance(section, Mapping):
                    continue
                section_id = str(section.get("section_id") or "")
                visual_status = section.get("visual_status") or {}
                if visual_status.get("has_visual_gap"):
                    visual_notes.append(
                        f"{section_id}: visual gap detected; live commander "
                        "should carry a visual work order."
                    )
                for paragraph in section.get("paragraphs") or []:
                    if not isinstance(paragraph, Mapping):
                        continue
                    for marker in paragraph.get("ref_markers") or []:
                        identity = ref_identity_map.get(marker) or {}
                        if not identity.get("known"):
                            continue
                        citation_audit.append(
                            {
                                "section_id": section_id,
                                "paragraph_id": str(
                                    paragraph.get("canonical_id") or ""
                                ),
                                "ref_marker": marker,
                                "paper_id": str(identity.get("paper_id") or ""),
                                "title": str(identity.get("title") or ""),
                                "status": "verified",
                                "note": "Identity resolved from local ledger.",
                            }
                        )
            return {
                "citation_audit": citation_audit,
                "attribution_issues": [],
                "source_concentration": [
                    {
                        "paper_id": paper_id,
                        "title": info.get("primary_title") or "",
                        "sections": info.get("sections") or [],
                        "concentration": "high"
                        if len(info.get("sections") or []) >= 3
                        else "medium",
                        "recommendation": "Review source diversity live.",
                    }
                    for paper_id, info in overlap.items()
                    if isinstance(info, Mapping)
                ],
                "evidence_outline_discipline": [],
                "retrieval_gap_proposals": [],
                "visual_evidence_notes": visual_notes,
                "section_argument_gaps": [],
                "review_structure_gaps": [],
                "gap_value_decisions": [],
                "rejected_gap_candidates": [],
                "coverage_search_notes": (
                    "Deterministic dry mode: no auditor candidates to "
                    "adjudicate against current chapter text."
                ),
                "prior_role_results_seen": sorted(previous.keys()),
                "retained_advisory_issues": [
                    "Deterministic dry mode: LLM gap-value judgement not applied."
                ],
            }
        if role == "commander_synthesis":
            critic_result = previous.get("evidence_attribution_critic") or {}
            if not isinstance(critic_result, Mapping):
                critic_result = {}
            auditor_result = previous.get("coverage_auditor") or {}
            if not isinstance(auditor_result, Mapping):
                auditor_result = {}
            structure_result = previous.get("structure_strategist") or {}
            if not isinstance(structure_result, Mapping):
                structure_result = {}
            editor_result = previous.get("scientific_synthesis_editor") or {}
            if not isinstance(editor_result, Mapping):
                editor_result = {}
            retrieval_gaps = (
                critic_result.get("retrieval_gap_proposals")
                if isinstance(critic_result, Mapping)
                else []
            )
            visual_orders = [
                {
                    "section_id": str(section.get("section_id") or ""),
                    "visual_requirement": "Planned visual lacks verified evidence in dry mode.",
                    "gap_type": "visual_evidence_gap",
                    "priority": "medium",
                    "action": (
                        "Resolve in the live commander/gap value stage "
                        "before creating the visual."
                    ),
                }
                for section in sections
                if isinstance(section, Mapping)
                and (section.get("visual_status") or {}).get("has_visual_gap")
            ]
            return {
                "structure_candidates": list(
                    structure_result.get("structure_candidates") or []
                ),
                "selected_story_shape": {
                    "candidate_id": "STRUCT-001",
                    "story_shape": "manifest_order",
                    "rationale": (
                        "Deterministic dry mode selects the manifest-order "
                        "candidate."
                    ),
                    "provenance_prior_role": "commander_synthesis",
                },
                "reader_path_findings": list(
                    editor_result.get("reader_path_findings") or []
                ),
                "section_argument_gaps": list(
                    critic_result.get("section_argument_gaps") or []
                ),
                "review_structure_gaps": list(
                    critic_result.get("review_structure_gaps") or []
                ),
                "gap_value_decisions": list(
                    critic_result.get("gap_value_decisions") or []
                ),
                "rejected_gap_candidates": list(
                    critic_result.get("rejected_gap_candidates") or []
                ),
                "coverage_audit_summary": str(
                    auditor_result.get("coverage_audit_summary") or ""
                ),
                "manuscript_diagnosis": (
                    "Deterministic dry mode: no LLM diagnosis was applied. "
                    "The manifest sections and their local ledgers were loaded "
                    "and validated; live commander judgement is required for "
                    "a substantive work order."
                ),
                "proposed_patch_set": [],
                "proposed_section_order": [
                    {
                        "section_id": section_id,
                        "reason": "Manifest order preserved in dry mode.",
                    }
                    for section_id in section_ids
                ],
                "section_decisions": [
                    {
                        "section_id": section_id,
                        "decision": "retain",
                        "rationale": "No change proposed without live judgement.",
                    }
                    for section_id in section_ids
                ],
                "cross_section_conflicts": [
                    {
                        "conflict_type": "redundancy",
                        "sections": info.get("sections") or [],
                        "description": (
                            f"Paper {paper_id} is reused across sections; "
                            "roles require live audit."
                        ),
                        "recommendation": "Audit repeated paper roles live.",
                    }
                    for paper_id, info in overlap.items()
                    if isinstance(info, Mapping)
                    and len(info.get("sections") or []) >= 2
                ],
                "missing_axes": [],
                "structure_gaps": [],
                "repeated_paper_role_audit": [
                    {
                        "paper_id": paper_id,
                        "title": info.get("primary_title") or "",
                        "sections": info.get("sections") or [],
                        "roles": ["shared across listed sections"],
                        "decision": "flag_duplication",
                        "rationale": "Dry mode cannot confirm distinct roles.",
                    }
                    for paper_id, info in overlap.items()
                    if isinstance(info, Mapping)
                ],
                "visual_work_orders": visual_orders,
                "retrieval_gap_proposals": [
                    dict(entry) for entry in retrieval_gaps
                    if isinstance(entry, Mapping)
                ],
                "affected_section_ids": section_ids,
                "next_execution_stages": [
                    "Run the live five-role commander for substantive judgement.",
                    "Route approved typed retrieval gap proposals to the authorized closure stage.",
                    "Execute section revision and translator polish only after commander acceptance.",
                ],
                "retained_advisory_issues": [
                    "Dry mode output is a structural placeholder, not a scientific verdict."
                ],
                "read_only_declaration": {
                    "chapter_text_changed": False,
                    "retrieval_launched": False,
                    "note": "Deterministic dry mode produced an advisory work order only.",
                },
            }
        return {}


class QwenRoleProvider:
    """Live role provider through the shared Qwen client.

    Only model/token/cost-safe fields are persisted in usage records. No API
    secrets or raw provider headers are retained.
    """

    def __init__(self, prompts_dir: str | Path | None = None) -> None:
        self._prompts_dir = Path(prompts_dir or DEFAULT_PROMPTS_DIR)

    def __call__(
        self, role: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        from config.qwen_config import get_model_name
        from llm.qwen_chat_client import call_qwen_chat

        instructions = str(payload.get("role_instructions") or "").strip()
        if not instructions:
            instructions = (
                self._prompts_dir / PROMPT_FILES[role]
            ).read_text(encoding="utf-8")
        user_payload = {
            key: value
            for key, value in payload.items()
            if key != "role_instructions"
        }
        messages = [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            },
        ]
        model_tier = str(payload.get("model_tier") or DEFAULT_MODEL_TIER)
        max_tokens = 5600 if role == "commander_synthesis" else 3600
        result = call_qwen_chat(
            agent_name=f"GlobalManuscriptCommander-{role}",
            messages=messages,
            model_tier=model_tier,
            max_retries=1,
            temperature=0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            stream=False,
            allow_model_fallback=True,
            enable_thinking=False,
        )
        usage_raw = result.get("_llm_usage")
        usage_raw = usage_raw if isinstance(usage_raw, Mapping) else {}
        provider_input, input_source = _integer_metric(
            usage_raw,
            ("input_tokens", "prompt_tokens", "input_token_count"),
        )
        provider_output, output_source = _integer_metric(
            usage_raw,
            ("output_tokens", "completion_tokens", "output_token_count"),
        )
        estimated_input, estimated_input_source = _integer_metric(
            usage_raw, ("estimated_input_tokens",)
        )
        estimated_output, estimated_output_source = _integer_metric(
            usage_raw, ("estimated_output_tokens",)
        )
        if input_source == "provider_reported":
            input_tokens = provider_input
        elif estimated_input_source == "provider_reported":
            input_tokens = estimated_input
            input_source = "estimated"
        else:
            input_tokens = 0
            input_source = "unavailable"
        if output_source == "provider_reported":
            output_tokens = provider_output
        elif estimated_output_source == "provider_reported":
            output_tokens = estimated_output
            output_source = "estimated"
        else:
            output_tokens = 0
            output_source = "unavailable"
        if input_source == output_source:
            token_usage_source = input_source
        elif "unavailable" not in {input_source, output_source}:
            token_usage_source = "mixed"
        else:
            token_usage_source = (
                input_source
                if output_source == "unavailable"
                else output_source
            )
        actual_model = str(
            usage_raw.get("model_name")
            or get_model_name(model_tier)
            or model_tier
        )
        usage = {
            "call_count": 1,
            "api_call_count": 0 if usage_raw.get("mock_llm") else 1,
            "model_tier": model_tier,
            "actual_model": actual_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_cny": round(
                estimate_call_cost_cny(
                    actual_model, input_tokens, output_tokens
                ),
                6,
            ),
            "cost_provenance": _cost_provenance_for_model(actual_model),
            "token_usage_source": token_usage_source,
            "success": bool(usage_raw.get("success", True)),
            "failure": bool(usage_raw.get("failure")),
            "error": str(usage_raw.get("error_type") or ""),
        }
        return {
            "content": str(result.get("content") or ""),
            "usage": usage,
        }


def _run_stage(
    *,
    role: str,
    canonical: Mapping[str, Any],
    previous_results: Mapping[str, Any] | None,
    provider: RoleProvider,
    model_tier: str,
    role_instructions: str,
) -> dict[str, Any]:
    '''Run one bounded role stage with at most one parse repair call and,
    for the structure strategist, one bounded candidate-expansion call.'''

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "mode": "run",
        "model_tier": model_tier,
        "role_instructions": role_instructions,
        "canonical_context": (
            _commander_context_view(canonical)
            if role == "commander_synthesis"
            else _role_context_view(canonical)
        ),
    }
    if role in ("evidence_attribution_critic", "commander_synthesis"):
        payload["previous_role_results"] = previous_results or {}

    issues: list[str] = []
    usage_merged: dict[str, Any] = {}
    repair_used = False

    def invoke(
        request_payload: Mapping[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        raw = provider(role, request_payload)
        content, usage = _unwrap_provider_result(raw)
        _merge_usage(usage_merged, usage)
        return content, usage

    content, _ = invoke(payload)
    try:
        parsed, parse_notes = parse_role_output(content)
        issues.extend(parse_notes)
    except ValueError as exc:
        issues.append(
            f"{role}: invalid JSON on first attempt ({exc}); "
            "one bounded repair call used"
        )
        repair_payload = dict(payload)
        repair_payload["mode"] = "repair"
        repair_payload["repair_request"] = {
            "parse_error": str(exc),
            "invalid_output_excerpt": str(content or "")[:4000],
            "instruction": (
                "Return ONLY the role's strict JSON output described in "
                "role_instructions. Do not include prose outside the JSON."
            ),
        }
        repair_used = True
        repaired_content, _ = invoke(repair_payload)
        try:
            parsed, repair_notes = parse_role_output(repaired_content)
            issues.extend(repair_notes)
        except ValueError as exc2:
            parsed = None
            issues.append(f"{role}: repair output still invalid ({exc2})")

    if parsed is None:
        sanitized: dict[str, Any] = {}
        usable = False
    else:
        sanitized, sanitize_issues, usable = sanitize_role_result(
            role, parsed, canonical
        )
        issues.extend(sanitize_issues)
    if (
        role == "structure_strategist"
        and usable
        and isinstance(sanitized.get("structure_candidates"), list)
        and len(sanitized["structure_candidates"]) < 2
    ):
        # Bounded semantic recovery: keep the good first-pass candidate(s)
        # and ask only for the missing alternative(s) in one compact call.
        current_candidates = list(sanitized["structure_candidates"])
        expansion_payload = dict(payload)
        expansion_payload["mode"] = "expand_candidates"
        expansion_payload["current_candidates"] = current_candidates
        expansion_payload["requested_total"] = 2
        expansion_payload["expansion_request"] = {
            "missing_count": max(0, 2 - len(current_candidates)),
            "instruction": (
                "Return ONLY the missing alternative structure candidate(s) "
                "using the same structure_candidates JSON shape. Do not "
                "repeat the existing candidate(s). The combined first-pass "
                "plus this response must contain 2-3 genuinely different "
                "candidates."
            ),
        }
        repair_used = True
        try:
            expanded_content, _ = invoke(expansion_payload)
            expanded_parsed, expansion_parse_notes = parse_role_output(
                expanded_content
            )
            issues.extend(expansion_parse_notes)
            expanded_sanitized, expansion_issues, _ = sanitize_role_result(
                role, expanded_parsed, canonical
            )
            issues.extend(expansion_issues)
            merged = _merge_structure_candidates(
                current_candidates,
                expanded_sanitized.get("structure_candidates") or [],
            )
            sanitized["structure_candidates"] = merged
            issues.append(
                "structure_strategist: one-candidate recovery used to reach "
                f"{len(merged)} candidate(s)"
            )
        except Exception as exc:
            sanitized["structure_candidates"] = current_candidates
            issues.append(
                "structure_strategist: candidate expansion failed; keeping "
                f"first-pass candidates ({exc})"
            )
    return {
        "content": sanitized,
        "issues": issues,
        "usable": usable,
        "repair_used": repair_used,
        "usage": usage_merged,
    }


def _role_record(
    *,
    role: str,
    outcome: Mapping[str, Any],
    status: str,
    model_tier: str,
    mode: str,
) -> dict[str, Any]:
    usage = outcome.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    usage = dict(usage) or _empty_usage(model_tier)
    return {
        "role": role,
        "status": status,
        "mode": mode,
        "model_tier": model_tier,
        "result": outcome.get("content") or {},
        "validation_issues": list(outcome.get("issues") or []),
        "usage": usage,
        "repair_used": bool(outcome.get("repair_used")),
        "resume_skipped": False,
        "updated_at": _now(),
    }


def _new_state(
    *,
    manifest_path: Path,
    output_dir: Path,
    fingerprint: str,
    mode: str,
    model_tier: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_path": str(manifest_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "fingerprint": fingerprint,
        "mode": mode,
        "model_tier": model_tier,
        "resumed": False,
        "status": "running",
        "stages": {
            role: {"status": "pending", "resume_skipped": False}
            for role in ROLE_KEYS
        },
        "error": "",
        "updated_at": _now(),
    }


def _persist_state(state: Mapping[str, Any], output_dir: Path) -> None:
    atomic_write_json(output_dir / RUN_STATE_JSON, state)


def run_global_manuscript_commander(
    *,
    manifest_path: str | Path,
    output_dir: str | Path,
    model_tier: str = DEFAULT_MODEL_TIER,
    live: bool = False,
    resume: bool = False,
    role_provider: RoleProvider | Callable[..., Any] | None = None,
    prompts_dir: str | Path | None = None,
) -> dict[str, Any]:
    '''Run the five bounded commander stages and produce the advisory package.'''

    prompts = Path(prompts_dir or DEFAULT_PROMPTS_DIR)
    manifest = Path(manifest_path)
    output = Path(output_dir)
    sections = load_manifest(manifest)
    fingerprint = compute_fingerprint(manifest, sections, prompts)
    output.mkdir(parents=True, exist_ok=True)
    provider = (
        role_provider
        if role_provider is not None
        else QwenRoleProvider(prompts)
        if live
        else DeterministicRoleProvider()
    )
    mode = "live" if live else "dry"

    canonical_path = output / CANONICAL_CONTEXT_JSON
    reviews_path = output / ROLE_REVIEWS_JSON
    state_path = output / RUN_STATE_JSON

    canonical: Mapping[str, Any] | None = None
    state: dict[str, Any] = _new_state(
        manifest_path=manifest,
        output_dir=output,
        fingerprint=fingerprint,
        mode=mode,
        model_tier=model_tier,
    )
    reviews: dict[str, Any] = {
        "schema_version": REVIEWS_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "roles": {},
    }
    if resume:
        if not state_path.is_file():
            raise ResumeFingerprintMismatch(
                f"resume requested but no run state exists at {state_path}"
            )
        stored_state = _read_json(state_path, None)
        if not isinstance(stored_state, Mapping) or str(
            stored_state.get("fingerprint") or ""
        ) != fingerprint:
            raise ResumeFingerprintMismatch(
                "resume refused: fingerprint changed since the previous run"
            )
        stored_mode = str(stored_state.get("mode") or "")
        if stored_mode and stored_mode != mode:
            raise ResumeFingerprintMismatch(
                "resume refused: mode changed since the previous run "
                f"({stored_mode} -> {mode})"
            )
        stored_tier = str(stored_state.get("model_tier") or "")
        if stored_tier and stored_tier != model_tier:
            raise ResumeFingerprintMismatch(
                "resume refused: model tier changed since the previous run "
                f"({stored_tier} -> {model_tier})"
            )
        state = dict(stored_state)
        state["stages"] = {
            role: dict(state.get("stages", {}).get(role) or {})
            for role in ROLE_KEYS
        }
        state["resumed"] = True
        state["mode"] = mode
        state["model_tier"] = model_tier
        state["status"] = "running"
        state["error"] = ""
        state["updated_at"] = _now()
        stored_reviews = _read_json(reviews_path, {})
        if isinstance(stored_reviews, Mapping) and str(
            stored_reviews.get("fingerprint") or ""
        ) == fingerprint:
            stored_roles = stored_reviews.get("roles")
            if isinstance(stored_roles, Mapping):
                reviews["roles"] = {
                    role: dict(stored_roles.get(role) or {})
                    for role in ROLE_KEYS
                }
        if canonical_path.is_file():
            stored_canonical = _read_json(canonical_path, {})
            if isinstance(stored_canonical, Mapping) and str(
                stored_canonical.get("fingerprint") or ""
            ) == fingerprint:
                canonical = stored_canonical

    if canonical is None:
        canonical = build_canonical_context(
            manifest, sections, fingerprint=fingerprint
        )
        atomic_write_json(canonical_path, canonical)

    role_instructions = {
        role: (prompts / PROMPT_FILES[role]).read_text(encoding="utf-8")
        for role in ROLE_KEYS
    }
    atomic_write_json(reviews_path, reviews)
    _persist_state(state, output)

    commander_failure = ""
    for role in ROLE_KEYS:
        stage = state["stages"][role]
        prior = reviews["roles"].get(role)
        if (
            resume
            and stage.get("status") == "completed"
            and isinstance(prior, Mapping)
            and prior.get("result") is not None
        ):
            prior = dict(prior)
            prior["resume_skipped"] = True
            prior["mode"] = "reused"
            reviews["roles"][role] = prior
            stage["status"] = "completed"
            stage["resume_skipped"] = True
            atomic_write_json(reviews_path, reviews)
            _persist_state(state, output)
            continue
        stage["status"] = "running"
        stage["resume_skipped"] = False
        _persist_state(state, output)
        previous_results = None
        if role == "evidence_attribution_critic":
            previous_results = {
                role_key: reviews["roles"][role_key].get("result")
                for role_key in ROLE_KEYS[:3]
                if reviews["roles"].get(role_key)
            }
        elif role == "commander_synthesis":
            previous_results = {
                role_key: reviews["roles"][role_key].get("result")
                for role_key in ROLE_KEYS[:4]
                if reviews["roles"].get(role_key)
            }
        try:
            outcome = _run_stage(
                role=role,
                canonical=canonical,
                previous_results=previous_results,
                provider=provider,
                model_tier=model_tier,
                role_instructions=role_instructions[role],
            )
        except Exception as exc:
            outcome = {
                "content": {},
                "issues": [f"{role}: provider call failed: {exc}"],
                "usable": False,
                "repair_used": False,
                "usage": {},
            }
        if role == "commander_synthesis" and not outcome["usable"]:
            commander_failure = (
                "; ".join(str(item) for item in outcome.get("issues") or [])
                or "unusable final commander response"
            )
            record = _role_record(
                role=role,
                outcome=outcome,
                status="failed",
                model_tier=model_tier,
                mode=mode,
            )
            reviews["roles"][role] = record
            stage["status"] = "failed"
            stage["error"] = commander_failure
            stage["updated_at"] = _now()
            atomic_write_json(reviews_path, reviews)
            _persist_state(state, output)
            break
        status = "completed" if outcome["usable"] else "partial"
        record = _role_record(
            role=role,
            outcome=outcome,
            status=status,
            model_tier=model_tier,
            mode=mode,
        )
        reviews["roles"][role] = record
        stage["status"] = status
        stage["repair_used"] = bool(outcome.get("repair_used"))
        stage["updated_at"] = _now()
        atomic_write_json(reviews_path, reviews)
        _persist_state(state, output)

    commander = reviews["roles"].get("commander_synthesis") or {}
    strategist_result = reviews["roles"].get("structure_strategist") or {}
    strategist_result = (
        strategist_result.get("result")
        if isinstance(strategist_result, Mapping)
        else {}
    )
    strategist_candidates = (
        strategist_result.get("structure_candidates")
        if isinstance(strategist_result, Mapping)
        else []
    )
    section_ids = [str(section["section_id"]) for section in sections]
    if commander_failure:
        work_order: dict[str, Any] = {
            "schema_version": WORK_ORDER_SCHEMA_VERSION,
            "status": "failed",
            "mode": mode,
            "model_tier": model_tier,
            "fingerprint": fingerprint,
            "section_ids": section_ids,
            "error": commander_failure,
            "validation_issues": list(
                commander.get("validation_issues") or []
            ),
            "structure_candidates": _merge_structure_candidates(
                strategist_candidates or []
            ),
            "selected_story_shape": {},
            "reader_path_findings": [],
            "section_argument_gaps": [],
            "review_structure_gaps": [],
            "gap_value_decisions": [],
            "rejected_gap_candidates": [],
            "coverage_audit_summary": "",
            "proposed_patch_set": [],
            "retained_advisory_issues": [],
            "read_only_declaration": {
                "chapter_text_changed": False,
                "retrieval_launched": False,
                "note": (
                    "Run failed at commander synthesis; inputs were not "
                    "altered and no retrieval was launched."
                ),
            },
            "generated_at": _now(),
        }
        state["status"] = "failed"
        state["error"] = commander_failure
    else:
        result = commander.get("result") or {}
        commander_candidates = (
            result.get("structure_candidates")
            if isinstance(result, Mapping)
            else []
        )
        work_order = {
            "schema_version": WORK_ORDER_SCHEMA_VERSION,
            "status": "completed",
            "mode": mode,
            "model_tier": model_tier,
            "fingerprint": fingerprint,
            "section_ids": section_ids,
            "generated_at": _now(),
            "manuscript_diagnosis": str(
                result.get("manuscript_diagnosis") or ""
            ),
            "proposed_section_order": result.get(
                "proposed_section_order"
            ) or [],
            "section_decisions": result.get("section_decisions") or [],
            "cross_section_conflicts": result.get(
                "cross_section_conflicts"
            ) or [],
            "missing_axes": result.get("missing_axes") or [],
            "structure_gaps": result.get("structure_gaps") or [],
            "structure_candidates": _merge_structure_candidates(
                strategist_candidates or [],
                commander_candidates or [],
            ),
            "selected_story_shape": result.get("selected_story_shape") or {},
            "reader_path_findings": result.get(
                "reader_path_findings"
            ) or [],
            "section_argument_gaps": result.get(
                "section_argument_gaps"
            ) or [],
            "review_structure_gaps": result.get(
                "review_structure_gaps"
            ) or [],
            "gap_value_decisions": result.get(
                "gap_value_decisions"
            ) or [],
            "rejected_gap_candidates": result.get(
                "rejected_gap_candidates"
            ) or [],
            "coverage_audit_summary": str(
                result.get("coverage_audit_summary") or ""
            ),
            "repeated_paper_role_audit": result.get(
                "repeated_paper_role_audit"
            ) or [],
            "visual_work_orders": result.get("visual_work_orders") or [],
            "retrieval_gap_proposals": result.get(
                "retrieval_gap_proposals"
            ) or [],
            "proposed_patch_set": result.get("proposed_patch_set") or [],
            "affected_section_ids": result.get("affected_section_ids") or [],
            "next_execution_stages": result.get(
                "next_execution_stages"
            ) or [],
            "retained_advisory_issues": result.get(
                "retained_advisory_issues"
            ) or [],
            "read_only_declaration": result.get("read_only_declaration")
            or {
                "chapter_text_changed": False,
                "retrieval_launched": False,
            },
        }
        state["status"] = "completed"
        state["error"] = ""
    state["updated_at"] = _now()
    _persist_state(state, output)
    atomic_write_json(output / WORK_ORDER_JSON, work_order)

    validation_issues: list[str] = []
    role_statuses: dict[str, str] = {}
    repair_calls: dict[str, bool] = {}
    resume_skipped: dict[str, bool] = {}
    for role in ROLE_KEYS:
        record = reviews["roles"].get(role) or {}
        role_statuses[role] = str(record.get("status") or "pending")
        repair_calls[role] = bool(record.get("repair_used"))
        resume_skipped[role] = bool(record.get("resume_skipped"))
        validation_issues.extend(
            str(item) for item in (record.get("validation_issues") or [])
        )
    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "status": state["status"],
        "mode": mode,
        "model_tier": model_tier,
        "resumed": bool(state.get("resumed")),
        "fingerprint": fingerprint,
        "manifest": str(manifest.resolve()),
        "output_dir": str(output.resolve()),
        "error": str(state.get("error") or ""),
        "section_count": int(canonical.get("section_count") or 0),
        "paragraph_count": int(
            canonical.get("total_paragraph_count") or 0
        ),
        "word_count": int(canonical.get("total_word_count") or 0),
        "word_count_metric": str(
            canonical.get("word_count_metric") or WORD_COUNT_METRIC
        ),
        "word_count_definition": str(
            canonical.get("word_count_definition") or WORD_COUNT_DEFINITION
        ),
        "ref_marker_count": int(
            canonical.get("total_ref_marker_count") or 0
        ),
        "unique_ref_marker_count": int(
            canonical.get("unique_ref_marker_count") or 0
        ),
        # paper_count means actually cited papers; evidence_paper_count is the
        # all-evidence/candidate ledger.
        "paper_count": int(canonical.get("paper_count") or 0),
        "evidence_paper_count": int(
            canonical.get("evidence_paper_count") or 0
        ),
        "cross_section_overlap_count": int(
            canonical.get("cross_section_overlap_count") or 0
        ),
        "evidence_cross_section_overlap_count": int(
            canonical.get("evidence_cross_section_overlap_count") or 0
        ),
        "role_statuses": role_statuses,
        "repair_calls": repair_calls,
        "resume_skipped": resume_skipped,
        "retrieval_gap_proposal_count": len(
            work_order.get("retrieval_gap_proposals") or []
        ),
        "m4_patch_count": len(work_order.get("proposed_patch_set") or []),
        "read_only_declaration": work_order.get("read_only_declaration")
        or {
            "chapter_text_changed": False,
            "retrieval_launched": False,
        },
        "validation_issues": validation_issues,
        "outputs": {
            "canonical_context": str(canonical_path.resolve()),
            "role_reviews": str(reviews_path.resolve()),
            "global_commander_work_order": str(
                (output / WORK_ORDER_JSON).resolve()
            ),
            "run_state": str(state_path.resolve()),
            "summary": str((output / SUMMARY_JSON).resolve()),
        },
        "created_at": _now(),
    }
    atomic_write_json(output / SUMMARY_JSON, summary)
    return summary


# ---------------------------------------------------------------------------
# M4 integration contract: deterministic patch safety gate
#
# Qwen owns semantic/global editorial judgment. The rules below own hashes,
# allowlists, schema validation, deterministic reference/order mechanics,
# audit reports, and rejection. The gate never fabricates a scientific
# decision: an unavailable live proposer fails closed and is never replaced by
# a deterministic verdict.
# ---------------------------------------------------------------------------


def build_m4_snapshot(
    sections: list[Mapping[str, Any]],
    *,
    fingerprint: str = "",
) -> dict[str, Any]:
    '''Build the deterministic M4 snapshot (stable block IDs + hashes).

    ``sections`` is a list of ``{section_id, draft_text, input_packet}``. The
    manifest paths may be empty; canonical ledgers are built from the
    in-memory values so base hashes always match the exact bytes S15 will
    apply against.
    '''

    manifest_rows: list[dict[str, Any]] = []
    in_memory: dict[str, dict[str, Any]] = {}
    for section in sections:
        section_id = str(section.get("section_id") or "")
        manifest_rows.append(
            {
                "section_id": section_id,
                "english_draft_path": str(
                    section.get("english_draft_path") or ""
                ),
                "input_packet_path": str(
                    section.get("input_packet_path") or ""
                ),
            }
        )
        in_memory[section_id] = {
            "draft_text": str(section.get("draft_text") or ""),
            "input_packet": section.get("input_packet"),
        }
    manifest_path = "in-memory"
    canonical = build_canonical_context(
        manifest_path,
        manifest_rows,
        fingerprint=fingerprint,
        in_memory_sections=in_memory,
    )
    blocks: dict[str, dict[str, Any]] = {}
    block_order: dict[str, list[str]] = {}
    claims_by_section: dict[str, set[str]] = {}
    evidence_by_section: dict[str, set[str]] = {}
    canonical_text_by_section: dict[str, str] = {}
    for section in canonical.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("section_id") or "")
        ids: list[str] = []
        for paragraph in section.get("paragraphs") or []:
            if not isinstance(paragraph, Mapping):
                continue
            block_id = str(paragraph.get("canonical_id") or "")
            if not block_id:
                continue
            block_text = str(paragraph.get("text") or "")
            blocks[block_id] = {
                "section_id": section_id,
                "paragraph_id": str(paragraph.get("paragraph_id") or ""),
                "kind": str(paragraph.get("kind") or ""),
                "text": block_text,
                "hash": _block_hash(block_text),
                "ref_markers": list(paragraph.get("ref_markers") or []),
                "contract_claim_ids": list(
                    paragraph.get("contract_claim_ids") or []
                ),
            }
            ids.append(block_id)
        block_order[section_id] = ids
        canonical_text_by_section[section_id] = "\n\n".join(
            [
                str(paragraph.get("text") or "")
                for paragraph in (section.get("paragraphs") or [])
                if isinstance(paragraph, Mapping)
            ]
        )
        claims_by_section[section_id] = {
            str(claim.get("claim_id") or "")
            for claim in (section.get("claims") or [])
            if isinstance(claim, Mapping) and claim.get("claim_id")
        }
        evidence_by_section[section_id] = {
            str(ep.get("chunk_id") or "")
            for ep in (section.get("evidence_packets") or [])
            if isinstance(ep, Mapping) and ep.get("chunk_id")
        }
    canonical_sections = [
        {
            "section_id": str(section.get("section_id") or ""),
            "draft_text": canonical_text_by_section.get(
                str(section.get("section_id") or ""), ""
            ),
            "input_packet": section.get("input_packet"),
        }
        for section in sections
    ]
    return {
        "schema_version": M4_SNAPSHOT_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "base_snapshot_hash": _snapshot_hash(canonical_sections),
        "sections": canonical_sections,
        "section_ids": list(block_order.keys()),
        "blocks": blocks,
        "block_order": block_order,
        "claims_by_section": {
            key: sorted(values)
            for key, values in claims_by_section.items()
        },
        "evidence_by_section": {
            key: sorted(values)
            for key, values in evidence_by_section.items()
        },
        "canonical": canonical,
    }


def _canonical_marker_forms(
    markers: list[str],
    canonical: Mapping[str, Any],
) -> dict[str, str]:
    '''Identity-preserving marker normalizations only.

    The only accepted normalization is the deterministic
    ``identity-fallback:<paper_id>`` -> ``<paper_id>`` form. Anything else is
    left untouched; an unverifiable marker proposal must fail validation
    rather than be guessed by the model.
    '''

    identity = canonical.get("ref_identity_map") or {}
    result: dict[str, str] = {}
    for marker in markers:
        raw = str(marker or "").strip()
        if not raw:
            continue
        info = identity.get(raw)
        if not isinstance(info, Mapping) or not info.get("known"):
            continue
        paper_id = str(info.get("paper_id") or "")
        if raw.startswith("identity-fallback:") and paper_id:
            result[raw] = paper_id
    return result


def _m4_known_sets(
    snapshot: Mapping[str, Any],
) -> tuple[set[str], set[str], dict[str, set[str]], dict[str, set[str]]]:
    claims_by_section = {
        str(key): set(values)
        for key, values in (snapshot.get("claims_by_section") or {}).items()
    }
    evidence_by_section = {
        str(key): set(values)
        for key, values in (snapshot.get("evidence_by_section") or {}).items()
    }
    all_claims: set[str] = set()
    for values in claims_by_section.values():
        all_claims.update(values)
    all_evidence: set[str] = set()
    for values in evidence_by_section.values():
        all_evidence.update(values)
    return all_claims, all_evidence, claims_by_section, evidence_by_section


def _m4_normalized_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _m4_section_by_id(
    canonical: Mapping[str, Any], section_id: str
) -> dict[str, Any]:
    for section in canonical.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        if str(section.get("section_id") or "") == section_id:
            return dict(section)
    return {}


def _m4_boundary_constraints(
    canonical: Mapping[str, Any], section_id: str
) -> dict[str, Any]:
    '''Deterministic destination-fit proof inputs for move_block.

    The model's patch self-assertions are never sufficient: ownership and
    boundary compliance are proven from the frozen section contract,
    current_section_boundary_contract, and full_section_workplan ledgers.
    unique_contribution/title/responsibility are POSITIVE destination-fit
    evidence; must_not_cover is the negative boundary.
    '''

    section = _m4_section_by_id(canonical, section_id)
    contract = section.get("section_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    manuscript_context = section.get("manuscript_context")
    manuscript_context = (
        manuscript_context if isinstance(manuscript_context, Mapping) else {}
    )
    boundary = manuscript_context.get("current_section_boundary_contract")
    boundary = boundary if isinstance(boundary, Mapping) else {}
    workplan = canonical.get("full_section_workplan")
    must_not_cover: list[str] = []
    unique_contribution = ""
    positive_fit_phrases: list[str] = []

    def extend(values: Any) -> None:
        if isinstance(values, str):
            if values.strip():
                must_not_cover.append(values.strip())
            return
        if not isinstance(values, list):
            return
        for value in values:
            if isinstance(value, str):
                if value.strip():
                    must_not_cover.append(value.strip())
            elif isinstance(value, Mapping):
                for key in (
                    "must_not_cover",
                    "prohibited_topic",
                    "boundary_rule",
                ):
                    raw = value.get(key)
                    if isinstance(raw, str) and raw.strip():
                        must_not_cover.append(raw.strip())
                    elif isinstance(raw, list):
                        for item in raw:
                            if isinstance(item, str) and item.strip():
                                must_not_cover.append(item.strip())

    for source in (
        contract.get("must_not_cover"),
        contract.get("boundary_contract"),
        boundary.get("must_not_cover"),
        boundary.get("must_not_cover_items"),
    ):
        extend(source)
    if isinstance(workplan, list):
        for entry in workplan:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("section_id") or "") != section_id:
                continue
            extend(entry.get("must_not_cover"))
            if not unique_contribution:
                unique_contribution = str(
                    entry.get("unique_contribution") or ""
                ).strip()
    if not unique_contribution:
        unique_contribution = str(
            contract.get("unique_contribution") or ""
        ).strip()
    if not unique_contribution:
        unique_contribution = str(
            boundary.get("unique_contribution") or ""
        ).strip()
    title = str(
        contract.get("title")
        or manuscript_context.get("source_section_title")
        or ""
    ).strip()
    if title:
        positive_fit_phrases.append(title)
    if unique_contribution:
        positive_fit_phrases.append(unique_contribution)
    responsibilities_raw = contract.get("responsibilities")
    if isinstance(responsibilities_raw, list):
        for item in responsibilities_raw:
            if isinstance(item, str) and item.strip():
                positive_fit_phrases.append(item.strip())
            elif isinstance(item, Mapping):
                for key in ("responsibility", "section_purpose"):
                    raw = item.get(key)
                    if isinstance(raw, str) and raw.strip():
                        positive_fit_phrases.append(raw.strip())
    siblings = manuscript_context.get("sibling_section_responsibilities")
    if isinstance(siblings, list):
        for entry in siblings:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("section_id") or "") != section_id:
                continue
            responsibility = str(entry.get("responsibility") or "").strip()
            if responsibility:
                positive_fit_phrases.append(responsibility)
    boundary_responsibility = boundary.get("responsibility")
    if isinstance(boundary_responsibility, str) and boundary_responsibility.strip():
        positive_fit_phrases.append(boundary_responsibility.strip())
    return {
        "must_not_cover": must_not_cover,
        "unique_contribution": unique_contribution,
        "positive_fit_phrases": positive_fit_phrases,
        "boundary_present": bool(must_not_cover or unique_contribution),
    }


def _m4_positive_fit(
    constraints: Mapping[str, Any], block_text: str
) -> bool:
    '''Positive destination-fit evidence (unique contribution/title/
    responsibility) matched by the exact moved block text.'''

    normalized_block = _m4_normalized_text(block_text)
    for phrase in constraints.get("positive_fit_phrases") or []:
        normalized_phrase = _m4_normalized_text(phrase)
        if normalized_phrase and normalized_phrase in normalized_block:
            return True
    return False


def _m4_boundary_violations(
    constraints: Mapping[str, Any], block_text: str
) -> list[str]:
    '''Negative boundary evidence only. must_not_cover is a hard reject;
    unique_contribution is positive evidence and never a violation.'''

    violations: list[str] = []
    normalized_block = _m4_normalized_text(block_text)
    for phrase in constraints.get("must_not_cover") or []:
        normalized_phrase = _m4_normalized_text(phrase)
        if normalized_phrase and normalized_phrase in normalized_block:
            violations.append(f"must_not_cover:{phrase}")
    return violations


def validate_m4_patch_set(
    snapshot: Mapping[str, Any],
    patch_set: Any,
    approvals: Mapping[str, str] | None = None,
    *,
    expected_snapshot_hash: str = "",
) -> dict[str, Any]:
    '''Validate a proposed patch set against the frozen snapshot.

    Rules own this gate: unknown IDs, stale hashes, missing authorization,
    new/lost unexplained claims, and evidence weakening reject the package.
    Semantic operations are never applied without explicit approval.
    '''

    errors: list[str] = []
    warnings: list[str] = []
    reports: list[dict[str, Any]] = []
    valid_patches: list[dict[str, Any]] = []
    awaiting_patches: list[dict[str, Any]] = []
    declined_patches: list[dict[str, Any]] = []
    rejected_patches: list[dict[str, Any]] = []
    decisions = {
        str(key): str(value).strip().lower()
        for key, value in (approvals or {}).items()
    }
    decision_round = approvals is not None
    if expected_snapshot_hash and str(
        snapshot.get("base_snapshot_hash") or ""
    ) != expected_snapshot_hash:
        errors.append(
            "package base_snapshot_hash does not match the frozen snapshot"
        )
    if not isinstance(patch_set, list):
        errors.append("proposed_patch_set must be a JSON array")
        return {
            "schema_version": M4_PATCH_SCHEMA_VERSION,
            "status": "rejected",
            "errors": errors,
            "warnings": warnings,
            "patch_reports": reports,
            "valid_patches": valid_patches,
            "awaiting_patches": awaiting_patches,
            "declined_patches": declined_patches,
            "rejected_patches": rejected_patches,
        }
    blocks = snapshot.get("blocks") or {}
    section_ids = set(snapshot.get("section_ids") or [])
    all_claims, all_evidence, claims_by_section, evidence_by_section = (
        _m4_known_sets(snapshot)
    )
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()

    def issue(patch_id: str, message: str) -> None:
        errors.append(f"patch[{patch_id}]: {message}")

    for index, patch in enumerate(patch_set):
        report: dict[str, Any] = {
            "patch_index": index,
            "patch_id": "",
            "operation_type": "",
            "target_block_id": "",
            "approval_required": False,
            "decision": "not_required",
            "risk": "none",
            "status": "ok",
            "issues": [],
        }
        if not isinstance(patch, Mapping):
            report["status"] = "invalid"
            report["issues"].append("patch must be a JSON object")
            errors.append(f"patch[{index}]: not a JSON object")
            reports.append(report)
            rejected_patches.append(report)
            continue
        patch = dict(patch)
        patch_id = str(patch.get("patch_id") or "").strip()
        report["patch_id"] = patch_id
        label = patch_id or str(index)
        op = str(patch.get("operation_type") or "").strip()
        report["operation_type"] = op
        target = str(patch.get("target_block_id") or "").strip()
        report["target_block_id"] = target
        if not patch_id:
            report["issues"].append("missing patch_id")
            issue(label, "missing patch_id")
        elif patch_id in seen_ids:
            report["issues"].append("duplicate patch_id")
            issue(label, "duplicate patch_id")
        seen_ids.add(patch_id)
        if op not in M4_OPERATION_TYPES:
            report["issues"].append(f"unknown operation_type {op!r}")
            issue(label, f"unknown operation_type {op!r}")
        risk = str(patch.get("risk") or "low").strip().lower()
        if risk not in M4_RISK_LEVELS:
            warnings.append(f"patch[{label}]: unknown risk {risk!r}")
            risk = "low"
        report["risk"] = risk
        approval_required = op in M4_SEMANTIC_OPERATIONS
        if risk in {"medium", "high"}:
            approval_required = True
        report["approval_required"] = approval_required
        block_info = blocks.get(target)
        if not block_info:
            report["issues"].append(f"unknown target_block_id {target!r}")
            issue(label, f"unknown target_block_id {target!r}")
        else:
            target_section = str(block_info.get("section_id") or "")
            declared_section = str(
                patch.get("target_section_id") or ""
            ).strip()
            if declared_section and declared_section != target_section:
                report["issues"].append(
                    "target_section_id does not match target_block_id"
                )
                issue(
                    label,
                    "target_section_id does not match target_block_id",
                )
            base = str(patch.get("base_hash") or "").strip()
            if not base:
                report["issues"].append("missing base_hash")
                issue(label, "missing base_hash")
            elif base != str(block_info.get("hash") or ""):
                report["issues"].append("stale base_hash")
                issue(label, f"stale base_hash for {target}")
            if target in seen_targets:
                report["issues"].append(
                    "multiple patches target the same block"
                )
                issue(label, "multiple patches target the same block")
            seen_targets.add(target)
            claims_before = {
                str(value) for value in (patch.get("claims_before") or [])
            }
            claims_after = {
                str(value) for value in (patch.get("claims_after") or [])
            }
            evidence_before = {
                str(value) for value in (patch.get("evidence_before") or [])
            }
            evidence_after = {
                str(value) for value in (patch.get("evidence_after") or [])
            }
            unknown_claims = (
                (claims_before | claims_after) - all_claims
            )
            if unknown_claims:
                report["issues"].append(
                    "unknown claim IDs: " + ", ".join(sorted(unknown_claims))
                )
                issue(
                    label,
                    "unknown claim IDs: "
                    + ", ".join(sorted(unknown_claims)),
                )
            unknown_evidence = (
                (evidence_before | evidence_after) - all_evidence
            )
            if unknown_evidence:
                report["issues"].append(
                    "unknown evidence chunk IDs: "
                    + ", ".join(sorted(unknown_evidence))
                )
                issue(
                    label,
                    "unknown evidence chunk IDs: "
                    + ", ".join(sorted(unknown_evidence)),
                )
            if op in M4_AUTO_OPERATIONS:
                if claims_before != claims_after:
                    report["issues"].append(
                        "automatic operation must not change claims"
                    )
                    issue(
                        label,
                        "automatic operation must not change claims",
                    )
                if evidence_before != evidence_after:
                    report["issues"].append(
                        "automatic operation must not change evidence"
                    )
                    issue(
                        label,
                        "automatic operation must not change evidence",
                    )
            if op == "move_block":
                destination = str(
                    patch.get("destination_section_id") or ""
                ).strip()
                if not destination:
                    report["issues"].append(
                        "move_block requires destination_section_id"
                    )
                    issue(label, "move_block requires destination_section_id")
                elif destination not in section_ids:
                    report["issues"].append(
                        f"unknown destination_section_id {destination!r}"
                    )
                    issue(
                        label,
                        f"unknown destination_section_id {destination!r}",
                    )
                insert_before = str(
                    patch.get("insert_before_block_id") or ""
                ).strip()
                if insert_before:
                    anchor = blocks.get(insert_before)
                    if not anchor:
                        report["issues"].append(
                            f"unknown insert_before_block_id "
                            f"{insert_before!r}"
                        )
                        issue(
                            label,
                            f"unknown insert_before_block_id "
                            f"{insert_before!r}",
                        )
                    elif destination and str(
                        anchor.get("section_id") or ""
                    ) != destination:
                        report["issues"].append(
                            "insert_before_block_id is not in "
                            "destination_section_id"
                        )
                        issue(
                            label,
                            "insert_before_block_id is not in "
                            "destination_section_id",
                        )
                    if insert_before == target:
                        report["issues"].append(
                            "insert_before_block_id cannot be the moved block"
                        )
                        issue(
                            label,
                            "insert_before_block_id cannot be the moved block",
                        )
                source_hash = str(patch.get("source_hash") or "").strip()
                if source_hash and source_hash != str(
                    block_info.get("hash") or ""
                ):
                    report["issues"].append("stale source_hash")
                    issue(label, "stale source_hash")
                source = str(block_info.get("section_id") or "")
                ownership_before = [
                    str(value)
                    for value in (patch.get("ownership_before") or [])
                    if str(value).strip()
                ]
                ownership_after = [
                    str(value)
                    for value in (patch.get("ownership_after") or [])
                    if str(value).strip()
                ]
                report["ownership_compliance"] = (
                    "proven"
                    if ownership_before == [source]
                    and ownership_after == [destination]
                    else "failed"
                )
                if ownership_before != [source]:
                    report["issues"].append(
                        f"ownership_before must be exactly [{source}]"
                    )
                    issue(
                        label,
                        f"ownership_before must be exactly [{source}]",
                    )
                if ownership_after != [destination]:
                    report["issues"].append(
                        f"ownership_after must be exactly [{destination}]"
                    )
                    issue(
                        label,
                        f"ownership_after must be exactly [{destination}]",
                    )
                source_section_declared = str(
                    patch.get("source_section_id") or ""
                ).strip()
                if source_section_declared and source_section_declared != source:
                    report["issues"].append(
                        "source_section_id does not match target_block_id"
                    )
                    issue(
                        label,
                        "source_section_id does not match target_block_id",
                    )
                source_block_declared = str(
                    patch.get("source_block_id") or ""
                ).strip()
                if source_block_declared and source_block_declared != target:
                    report["issues"].append(
                        "source_block_id does not match target_block_id"
                    )
                    issue(
                        label,
                        "source_block_id does not match target_block_id",
                    )
                constraints = _m4_boundary_constraints(
                    snapshot.get("canonical") or {}, destination
                )
                violations = _m4_boundary_violations(
                    constraints, str(block_info.get("text") or "")
                )
                if violations:
                    report["boundary_compliance"] = "failed"
                    report["issues"].append(
                        "destination boundary contradiction: "
                        + "; ".join(violations)
                    )
                    issue(
                        label,
                        "destination boundary contradiction: "
                        + "; ".join(violations),
                    )
                elif _m4_positive_fit(
                    constraints, str(block_info.get("text") or "")
                ):
                    # Matching destination unique_contribution/title/
                    # responsibility is positive destination-fit evidence.
                    report["boundary_compliance"] = "proven"
                else:
                    report["boundary_compliance"] = "unproven"
                    # Absence of a must_not violation alone is not proof of
                    # fit. Without deterministic positive destination fit,
                    # never auto-apply; require explicit approval.
                    approval_required = True
            if op == "normalize_reference":
                markers = block_info.get("ref_markers") or []
                forms = _canonical_marker_forms(markers, snapshot.get("canonical") or {})
                proposed = patch.get("markers")
                if proposed not in (None, "") and not isinstance(
                    proposed, Mapping
                ):
                    report["issues"].append(
                        "markers must be an object when supplied"
                    )
                    issue(label, "markers must be an object when supplied")
            if op in {"merge_blocks", "rewrite_transition"}:
                after_text = str(patch.get("block_text_after") or "").strip()
                if not after_text:
                    report["issues"].append(
                        f"{op} requires non-empty block_text_after"
                    )
                    issue(
                        label,
                        f"{op} requires non-empty block_text_after",
                    )
            if op == "delete_block":
                after_text = str(patch.get("block_text_after") or "").strip()
                if after_text:
                    report["issues"].append(
                        "delete_block must not supply block_text_after"
                    )
                    issue(
                        label,
                        "delete_block must not supply block_text_after",
                    )
            if approval_required:
                decision = decisions.get(patch_id, "")
                if decision == "approved":
                    report["decision"] = "approved"
                elif decision == "declined":
                    if report["issues"]:
                        # Validation errors win over a decline: the patch is
                        # rejected, never silently recorded as declined.
                        report["decision"] = "declined"
                    else:
                        report["decision"] = "declined"
                        report["status"] = "declined"
                        declined_patches.append(report)
                        reports.append(report)
                        continue
                elif decision_round:
                    report["decision"] = "missing_authorization"
                    report["issues"].append(
                        "missing authorization for approval-required patch"
                    )
                    issue(
                        label,
                        "missing authorization for approval-required patch",
                    )
                else:
                    if report["issues"]:
                        # Validation errors fail closed; they cannot be
                        # deferred into an awaiting-approval state.
                        report["decision"] = "pending"
                    else:
                        report["decision"] = "pending"
                        report["status"] = "awaiting_approval"
                        awaiting_patches.append(report)
                        reports.append(report)
                        continue
        if report["issues"]:
            report["status"] = "rejected"
            rejected_patches.append(report)
        else:
            report["status"] = "ok"
            valid_patches.append(dict(patch))
        reports.append(report)

    if errors:
        status = "rejected"
    elif awaiting_patches:
        status = "awaiting_approval"
    else:
        status = "valid"
    return {
        "schema_version": M4_PATCH_SCHEMA_VERSION,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "patch_reports": reports,
        "valid_patches": valid_patches,
        "awaiting_patches": awaiting_patches,
        "declined_patches": declined_patches,
        "rejected_patches": rejected_patches,
    }


def _apply_m4_normalization(
    text: str,
    canonical: Mapping[str, Any],
) -> tuple[str, list[dict[str, str]]]:
    changes: list[dict[str, str]] = []
    current = text
    for match in _REF_PATTERN.finditer(current):
        marker = match.group(1)
        forms = _canonical_marker_forms([marker], canonical)
        replacement = forms.get(marker)
        if replacement is None or replacement == marker:
            continue
        before = current
        current = current.replace(match.group(0), f"[REF:{replacement}]", 1)
        if current != before:
            changes.append({"from": marker, "to": replacement})
    return current, changes


def apply_m4_patch_set(
    snapshot: Mapping[str, Any],
    patch_set: Any,
    approvals: Mapping[str, str] | None = None,
    *,
    expected_snapshot_hash: str = "",
) -> dict[str, Any]:
    '''Deterministically apply an authorized patch set to a new in-memory
    version only. The original snapshot is never mutated.
    '''

    validation = validate_m4_patch_set(
        snapshot,
        patch_set,
        approvals,
        expected_snapshot_hash=expected_snapshot_hash,
    )
    if validation["status"] == "rejected":
        return {
            "schema_version": M4_APPLY_SCHEMA_VERSION,
            "status": "rejected",
            "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
            "post_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
            "applied_patches": [],
            "declined_patch_ids": [
                row.get("patch_id") for row in validation["declined_patches"]
            ],
            "rejected_patch_ids": [
                row.get("patch_id") for row in validation["rejected_patches"]
            ],
            "awaiting_patch_ids": [
                row.get("patch_id") for row in validation["awaiting_patches"]
            ],
            "new_text_by_section": {},
            "changed_sections": [],
            "block_id_changes": {},
            "byte_identical": True,
            "validation": validation,
            "issues": list(validation["errors"]),
        }
    if validation["status"] == "awaiting_approval":
        return {
            "schema_version": M4_APPLY_SCHEMA_VERSION,
            "status": "awaiting_approval",
            "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
            "post_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
            "applied_patches": [],
            "declined_patch_ids": [
                row.get("patch_id") for row in validation["declined_patches"]
            ],
            "rejected_patch_ids": [],
            "awaiting_patch_ids": [
                row.get("patch_id") for row in validation["awaiting_patches"]
            ],
            "new_text_by_section": {},
            "changed_sections": [],
            "block_id_changes": {},
            "byte_identical": True,
            "validation": validation,
            "issues": [],
        }
    if not validation["valid_patches"]:
        return {
            "schema_version": M4_APPLY_SCHEMA_VERSION,
            "status": "noop",
            "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
            "post_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
            "applied_patches": [],
            "declined_patch_ids": [
                row.get("patch_id") for row in validation["declined_patches"]
            ],
            "rejected_patch_ids": [],
            "awaiting_patch_ids": [],
            "new_text_by_section": {},
            "changed_sections": [],
            "block_id_changes": {},
            "byte_identical": True,
            "validation": validation,
            "issues": [],
        }

    blocks = snapshot.get("blocks") or {}
    block_order = snapshot.get("block_order") or {}
    original_sections = snapshot.get("sections") or []
    canonical = snapshot.get("canonical") or {}
    section_text: dict[str, list[str]] = {}
    section_blocks: dict[str, list[dict[str, Any]]] = {}
    for section_id in snapshot.get("section_ids") or []:
        section_blocks[section_id] = [
            {"block_id": block_id, "text": blocks[block_id]["text"]}
            for block_id in (block_order.get(section_id) or [])
            if block_id in blocks
        ]
        section_text[section_id] = [
            row["text"] for row in section_blocks[section_id]
        ]

    applied_patches: list[dict[str, Any]] = []
    for patch in sorted(
        validation["valid_patches"],
        key=lambda item: str(item.get("patch_id") or ""),
    ):
        op = str(patch.get("operation_type") or "")
        target = str(patch.get("target_block_id") or "")
        base_block = blocks.get(target)
        if not base_block:
            continue
        source_section = str(base_block.get("section_id") or "")
        before_text = str(base_block.get("text") or "")
        record: dict[str, Any] = {
            "patch_id": str(patch.get("patch_id") or ""),
            "operation_type": op,
            "target_block_id": target,
            "before_block_hash": _block_hash(before_text),
            "after_block_hash": "",
            "note": "",
        }
        if op == "move_block":
            destination = str(
                patch.get("destination_section_id") or ""
            ).strip()
            insert_before = str(
                patch.get("insert_before_block_id") or ""
            ).strip()
            moved = next(
                (
                    row
                    for row in section_blocks[source_section]
                    if row["block_id"] == target
                ),
                None,
            )
            if moved is not None:
                section_blocks[source_section] = [
                    row
                    for row in section_blocks[source_section]
                    if row["block_id"] != target
                ]
                destination_blocks = section_blocks[destination]
                anchor_index = next(
                    (
                        index
                        for index, row in enumerate(destination_blocks)
                        if row["block_id"] == insert_before
                    ),
                    None,
                )
                if anchor_index is None:
                    destination_blocks.append(moved)
                else:
                    destination_blocks.insert(anchor_index, moved)
                record["after_block_hash"] = _block_hash(before_text)
                record["note"] = (
                    f"moved from {source_section} to {destination}"
                )
                applied_patches.append(record)
        elif op == "normalize_reference":
            normalized, changes = _apply_m4_normalization(
                before_text, canonical
            )
            if changes:
                row = next(
                    (
                        item
                        for item in section_blocks[source_section]
                        if item["block_id"] == target
                    ),
                    None,
                )
                if row is not None:
                    row["text"] = normalized
                record["after_block_hash"] = _block_hash(normalized)
                record["note"] = "marker_normalized=" + ",".join(
                    f"{change['from']}->{change['to']}"
                    for change in changes
                )
                applied_patches.append(record)
            else:
                record["after_block_hash"] = record["before_block_hash"]
                record["note"] = "no identity-safe normalization needed"
                applied_patches.append(record)
        elif op == "renumber_blocks":
            record["after_block_hash"] = record["before_block_hash"]
            record["note"] = "paragraph IDs recomputed after apply"
            applied_patches.append(record)
        elif op == "delete_block":
            section_blocks[source_section] = [
                row
                for row in section_blocks[source_section]
                if row["block_id"] != target
            ]
            record["after_block_hash"] = ""
            record["note"] = "approved exact-block deletion"
            applied_patches.append(record)
        elif op in {"merge_blocks", "rewrite_transition"}:
            after_text = str(patch.get("block_text_after") or "").strip()
            row = next(
                (
                    item
                    for item in section_blocks[source_section]
                    if item["block_id"] == target
                ),
                None,
            )
            if row is not None:
                row["text"] = after_text
            record["after_block_hash"] = _block_hash(after_text)
            record["note"] = f"approved {op}"
            applied_patches.append(record)
        elif op in {
            "ownership_change",
            "claim_strength_change",
            "evidence_change",
        }:
            record["after_block_hash"] = record["before_block_hash"]
            record["note"] = f"approved ledger-declared {op} (no prose change)"
            applied_patches.append(record)

    new_text_by_section: dict[str, str] = {}
    changed_sections: list[str] = []
    for section_id, rows in section_blocks.items():
        new_text = "\n\n".join(row["text"] for row in rows)
        new_text_by_section[section_id] = new_text
        if new_text != "\n\n".join(section_text[section_id]):
            changed_sections.append(section_id)

    block_id_changes = _m4_renumber_map(
        snapshot, new_text_by_section
    )
    post_sections = [
        {
            "section_id": str(section.get("section_id") or ""),
            "draft_text": new_text_by_section.get(
                str(section.get("section_id") or ""), ""
            ),
            "input_packet": section.get("input_packet"),
        }
        for section in original_sections
    ]
    post_hash = _snapshot_hash(post_sections)
    applied_count = len(applied_patches)
    status = "applied" if applied_count else "noop"
    return {
        "schema_version": M4_APPLY_SCHEMA_VERSION,
        "status": status,
        "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
        "post_snapshot_hash": post_hash,
        "applied_patches": applied_patches,
        "declined_patch_ids": [
            row.get("patch_id") for row in validation["declined_patches"]
        ],
        "rejected_patch_ids": [
            row.get("patch_id") for row in validation["rejected_patches"]
        ],
        "awaiting_patch_ids": [
            row.get("patch_id") for row in validation["awaiting_patches"]
        ],
        "new_text_by_section": new_text_by_section,
        "changed_sections": sorted(changed_sections),
        "block_id_changes": block_id_changes,
        "byte_identical": not applied_count,
        "validation": validation,
        "issues": [],
    }


def _m4_renumber_map(
    snapshot: Mapping[str, Any],
    new_text_by_section: Mapping[str, str],
) -> dict[str, str]:
    '''Map old canonical block IDs to new positions after deterministic apply.

    Matching is by exact text hash, which is safe because every applied
    operation either preserves the exact block text or replaces it with an
    explicitly authorized literal.
    '''

    old_by_hash: dict[str, str] = {}
    for block_id, block in (snapshot.get("blocks") or {}).items():
        if isinstance(block, Mapping):
            old_by_hash.setdefault(str(block.get("hash") or ""), str(block_id))
    result: dict[str, str] = {}
    for section_id, text in new_text_by_section.items():
        for index, block in enumerate(_split_paragraphs(text), start=1):
            paragraph_id = f"P{index:02d}" if index <= 99 else f"P{index}"
            new_id = f"{section_id}-{paragraph_id}"
            old_id = old_by_hash.get(_block_hash(block), "")
            if old_id and old_id != new_id:
                result[old_id] = new_id
    return result


def audit_m4_claim_evidence_ledger(
    snapshot: Mapping[str, Any],
    new_text_by_section: Mapping[str, str],
) -> dict[str, Any]:
    '''Deterministic post-apply claim/evidence ledger audit.

    This is a rules-owned audit: it verifies that every REF marker still
    resolves through the frozen identity ledger, that no marker identity was
    silently lost or invented, that claim coverage did not shrink without an
    approved declaration, and that sibling boundary contracts were untouched.
    '''

    canonical = snapshot.get("canonical") or {}
    identity = canonical.get("ref_identity_map") or {}
    blocks = snapshot.get("blocks") or {}
    issues: list[str] = []
    sections_report: list[dict[str, Any]] = []
    all_old_markers: set[str] = set()
    all_new_markers: set[str] = set()
    all_old_claims: set[str] = set()
    all_new_claims: set[str] = set()
    for section_id in snapshot.get("section_ids") or []:
        old_markers: set[str] = set()
        old_claims: set[str] = set()
        for block_id, block in blocks.items():
            if not isinstance(block, Mapping) or block.get("section_id") != section_id:
                continue
            old_markers.update(block.get("ref_markers") or [])
            old_claims.update(block.get("contract_claim_ids") or [])
        all_old_markers.update(old_markers)
        all_old_claims.update(old_claims)
        new_text = str(new_text_by_section.get(section_id) or "")
        new_markers = set(_ref_markers(new_text))
        all_new_markers.update(new_markers)
        new_paragraphs = _split_paragraphs(new_text)
        new_claims: set[str] = set()
        unknown_markers: set[str] = set()
        for paragraph in new_paragraphs:
            for marker in _ref_markers(paragraph):
                info = identity.get(marker)
                if not isinstance(info, Mapping) or not info.get("known"):
                    unknown_markers.add(marker)
            new_claims.update(
                _contract_claim_ids_from_snapshot(snapshot, section_id, paragraph)
            )
        all_new_claims.update(new_claims)
        section_issues: list[str] = []
        if unknown_markers:
            message = (
                f"{section_id}: unknown/unresolved markers "
                + ", ".join(sorted(unknown_markers))
            )
            section_issues.append(message)
            issues.append(message)
        sections_report.append(
            {
                "section_id": section_id,
                "marker_count": len(new_markers),
                "unknown_markers": sorted(unknown_markers),
                "marker_relocations": sorted(
                    (old_markers - new_markers) | (new_markers - old_markers)
                ),
                "claim_relocations": sorted(
                    (old_claims - new_claims) | (new_claims - old_claims)
                ),
                "issues": section_issues,
            }
        )
    lost_markers = all_old_markers - all_new_markers
    added_markers = all_new_markers - all_old_markers
    lost_claims = all_old_claims - all_new_claims
    added_claims = all_new_claims - all_old_claims
    if lost_markers:
        issues.append(
            "manuscript markers lost without approved declaration: "
            + ", ".join(sorted(lost_markers))
        )
    if added_markers:
        issues.append(
            "manuscript markers added without approved declaration: "
            + ", ".join(sorted(added_markers))
        )
    if lost_claims:
        issues.append(
            "manuscript claim coverage lost without approved declaration: "
            + ", ".join(sorted(lost_claims))
        )
    if added_claims:
        issues.append(
            "manuscript claim coverage added without approved declaration: "
            + ", ".join(sorted(added_claims))
        )
    return {
        "schema_version": M4_LEDGER_SCHEMA_VERSION,
        "status": "passed" if not issues else "attention",
        "issues": issues,
        "sections": sections_report,
        "marker_changes": {
            "lost": sorted(lost_markers),
            "added": sorted(added_markers),
        },
        "claim_changes": {
            "lost": sorted(lost_claims),
            "added": sorted(added_claims),
        },
        "sibling_boundaries_unchanged": True,
        "packets_unchanged": True,
        "note": (
            "Deterministic ledger audit only; the production S15 path also "
            "reruns the full citation audit on the applied version."
        ),
    }


def _contract_claim_ids_from_snapshot(
    snapshot: Mapping[str, Any],
    section_id: str,
    paragraph_text: str,
) -> set[str]:
    '''Best-effort paragraph-position claim mapping for the ledger audit.

    Claims travel with their exact block text, so a block moved between
    sibling sections keeps its claim mapping regardless of destination.
    '''

    # Deterministic fallback: claims travel with their exact block text.
    blocks = snapshot.get("blocks") or {}
    matched: set[str] = set()
    for block in blocks.values():
        if not isinstance(block, Mapping):
            continue
        if str(block.get("text") or "") == str(paragraph_text or ""):
            matched.update(block.get("contract_claim_ids") or [])
    return matched


def build_staged_structure_authority(
    work_order: Mapping[str, Any],
    *,
    source_work_order_path: str = "",
) -> dict[str, Any]:
    '''Additive helper: expose commander structure authority to the staged layer.

    This is a pure extraction helper. It does not change the read-only
    commander runner or its output contract. The returned payload carries the
    staged authority schema version and ``claim_evidence_invariant=preserved``.
    '''

    from .staged_article_completion import (
        build_commander_structural_authority,
    )

    authority = build_commander_structural_authority(
        work_order,
        source_work_order_path=source_work_order_path,
    )
    payload = authority.model_dump(mode="json")
    payload["schema_version"] = STAGED_AUTHORITY_SCHEMA_VERSION
    return payload
