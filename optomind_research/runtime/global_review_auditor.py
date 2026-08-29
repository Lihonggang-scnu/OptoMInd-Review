"""Global review auditor — two-layer cross-section quality checks.

Layer 1 (deterministic): duplicate content, terminology, visual gaps, classification
confusion, artifact completeness, CJK leak detection, citation validity.
Layer 2 (LLM-based): argument completion, methodology identity, synthesis verdict,
cross-section progression.  Layer 2 is optional; if unavailable it is skipped cleanly.
"""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import contextlib
import io
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .evidence_packet_parser import load_section_evidence_packet

_SKLEARN_LOADER_CACHE = None


def _load_sklearn_duplicate_accelerator():
    """Lazily import the optional sklearn duplicate accelerator.

    The import and first use are intentionally isolated so the rest of the
    auditor never pays an import/probe cost when sklearn is unavailable.
    """

    global _SKLEARN_LOADER_CACHE
    if _SKLEARN_LOADER_CACHE is not None:
        return _SKLEARN_LOADER_CACHE
    stderr_buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr_buffer), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
        _SKLEARN_LOADER_CACHE = (TfidfVectorizer, cosine_similarity)
    except (ImportError, ModuleNotFoundError, RuntimeError, ValueError):
        _SKLEARN_LOADER_CACHE = (None, None)
    return _SKLEARN_LOADER_CACHE

_MIN_SECTION_WORDS = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _semantic_audit_fingerprint(
    merged_draft: str,
    section_registry: Dict[str, Any],
    blueprint: Dict[str, Any],
    role_prompt: str,
) -> str:
    """Return a stable cache key for the exact semantic-audit input.

    Audit results may be reused only when the article, its section contract,
    the review question, and the managing-editor instructions are unchanged.
    Round numbers alone are not safe cache keys because a resumed revision run
    can rewrite the article while reusing the same round number.
    """

    payload = {
        # Bump whenever the compact audit context semantics change.  Without
        # this, a corrected deterministic context builder can silently reuse
        # an obsolete paid verdict for identical manuscript prose.
        "audit_contract_version": 2,
        "merged_draft": merged_draft,
        "sections": [
            {
                "section_id": section.get("section_id", ""),
                "title": section.get("title", ""),
                "argument_role": section.get("argument_role", ""),
                "status": section.get("status", ""),
            }
            for section in section_registry.get("sections", [])
            if isinstance(section, dict)
        ],
        "input_context": blueprint.get("input_context", {}),
        "full_review_argument": blueprint.get("full_review_argument", ""),
        "role_prompt": role_prompt,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _tokenize_sentences(text: str) -> List[str]:
    """Simple sentence tokenizer — splits on .?! followed by space."""
    sentences = re.split(r'[.?!]\s+', text)
    return [s.strip() for s in sentences if len(s.strip()) > 20]


def _extract_last_paragraph(text: str) -> str:
    """Extract the last paragraph (double-newline separated)."""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    return paragraphs[-1] if paragraphs else ""


def _normalized_tokens(text: str) -> List[str]:
    return [
        token
        for token in re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
        if token
    ]


def _pure_python_cosine_similarity(left: str, right: str) -> float:
    left_tokens = _normalized_tokens(left)
    right_tokens = _normalized_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    left_counts: Dict[str, int] = defaultdict(int)
    right_counts: Dict[str, int] = defaultdict(int)
    for token in left_tokens:
        left_counts[token] += 1
    for token in right_tokens:
        right_counts[token] += 1
    common = set(left_counts).intersection(right_counts)
    dot = sum(left_counts[token] * right_counts[token] for token in common)
    left_norm = sum(value * value for value in left_counts.values()) ** 0.5
    right_norm = sum(value * value for value in right_counts.values()) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class GlobalReviewAuditor:
    """Deterministic global audit of a merged review draft."""

    def __init__(self, duplicate_threshold: float = 0.85) -> None:
        self.duplicate_threshold = duplicate_threshold

    def audit(
        self,
        merged_draft_path: Path,
        section_registry: Dict[str, Any],
        work_dir: Path,
        round_num: int = 1,
    ) -> Dict[str, Any]:
        """Run all deterministic audits and produce GLOBAL_AUDIT_REPORT.json."""
        flags = []

        # Read merged draft
        if not merged_draft_path.exists():
            return self._empty_report(round_num, work_dir)
        merged_text = merged_draft_path.read_text(encoding="utf-8")

        # Build section-text mapping
        section_texts = self._extract_section_texts(merged_text, section_registry)

        # Run detection rules
        flags.extend(self._detect_duplicates(section_texts))
        flags.extend(self._detect_terminology_inconsistencies(section_texts, work_dir))
        flags.extend(self._detect_visual_gaps(section_registry, work_dir))
        flags.extend(self._detect_classification_confusion(section_registry))
        flags.extend(self._detect_artifact_completeness(section_registry))
        flags.extend(self._detect_cjk_leak(section_texts))
        flags.extend(self._detect_citation_validity(section_texts, section_registry))

        # Assign flag IDs
        for i, flag in enumerate(flags, start=1):
            flag["flag_id"] = f"F{i:02d}"

        total = len(flags)
        blocking = sum(1 for f in flags if f.get("blocking", False))

        report = {
            "schema_version": "phase4.global_audit.v1",
            "round": round_num,
            "total_flags": total,
            "blocking_flags": blocking,
            "flags": flags,
            "created_at": _now(),
        }

        # Write report
        report_path = work_dir / f"audit_round_{round_num}" / "GLOBAL_AUDIT_REPORT.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        return report

    def _empty_report(self, round_num: int, work_dir: Path) -> Dict[str, Any]:
        """Empty audit report when no draft exists."""
        report = {
            "schema_version": "phase4.global_audit.v1",
            "round": round_num,
            "total_flags": 0,
            "blocking_flags": 0,
            "flags": [],
            "created_at": _now(),
        }
        report_path = work_dir / f"audit_round_{round_num}" / "GLOBAL_AUDIT_REPORT.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    def _extract_section_texts(
        self, merged_text: str, section_registry: Dict[str, Any]
    ) -> Dict[str, str]:
        """Extract per-section text from merged draft using section headers."""
        section_texts = {}
        sections = section_registry.get("sections", [])

        for i, section in enumerate(sections):
            section_id = section["section_id"]
            # Simple heuristic: find section by title or ID marker
            # For now, just split by ## headers (markdown H2)
            pattern = rf"##\s+.*?{re.escape(section_id)}.*?\n(.*?)(?=##\s+|\Z)"
            match = re.search(pattern, merged_text, re.DOTALL | re.IGNORECASE)
            if match:
                section_texts[section_id] = match.group(1).strip()
            else:
                # Fallback: read from section's work_dir
                work_dir = Path(section.get("work_dir", ""))
                draft_path = work_dir / "SECTION_DRAFT_EN.md"
                if draft_path.exists():
                    section_texts[section_id] = draft_path.read_text(encoding="utf-8")

        return section_texts

    def _detect_duplicates(self, section_texts: Dict[str, str]) -> List[Dict[str, Any]]:
        """Detect near-verbatim reuse, not merely legitimate thematic overlap.

        Cross-section scientific prose must share terminology and may briefly
        reconnect an earlier argument. A permissive 0.85 cosine threshold
        treated such continuity as duplication, while the deterministic fixer
        can safely remove only near-verbatim sentences. Keep those two
        contracts aligned; broader conceptual repetition belongs to the
        semantic article-level reviewer.
        """
        flags = []

        # Build sentence-to-section mapping
        sentence_map = {}  # sentence -> (section_id, idx)
        all_sentences = []
        for section_id, text in section_texts.items():
            sentences = _tokenize_sentences(text)
            for idx, sent in enumerate(sentences):
                all_sentences.append(sent)
                sentence_map[len(all_sentences) - 1] = (section_id, idx)

        if len(all_sentences) < 2:
            return flags

        similarities = None
        vectorizer_cls, cosine_similarity_fn = _load_sklearn_duplicate_accelerator()
        if vectorizer_cls is not None and cosine_similarity_fn is not None:
            stderr_buffer = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr_buffer), warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    vectorizer = vectorizer_cls(min_df=1, stop_words='english')
                    tfidf_matrix = vectorizer.fit_transform(all_sentences)
                    similarities = cosine_similarity_fn(tfidf_matrix)
            except (ImportError, ModuleNotFoundError, RuntimeError, ValueError):
                similarities = None

        effective_threshold = max(float(self.duplicate_threshold), 0.97)
        seen_pairs = set()
        if similarities is not None:
            for i in range(len(all_sentences)):
                for j in range(i + 1, len(all_sentences)):
                    if similarities[i, j] >= effective_threshold:
                        sec_i, idx_i = sentence_map[i]
                        sec_j, idx_j = sentence_map[j]
                        if sec_i != sec_j:
                            pair = tuple(sorted([sec_i, sec_j]))
                            if pair not in seen_pairs:
                                flags.append({
                                    "type": "duplicate_content",
                                    "severity": "warning",
                                    "blocking": False,
                                    "section_ids": list(pair),
                                    "description": f"Sections {pair[0]} and {pair[1]} share similar content (similarity: {similarities[i,j]:.2f})",
                                    "sentence_snippets": [all_sentences[i][:80], all_sentences[j][:80]],
                                })
                                seen_pairs.add(pair)
        else:
            for i in range(len(all_sentences)):
                for j in range(i + 1, len(all_sentences)):
                    similarity = _pure_python_cosine_similarity(
                        all_sentences[i],
                        all_sentences[j],
                    )
                    if similarity >= effective_threshold:
                        sec_i, idx_i = sentence_map[i]
                        sec_j, idx_j = sentence_map[j]
                        if sec_i != sec_j:
                            pair = tuple(sorted([sec_i, sec_j]))
                            if pair not in seen_pairs:
                                flags.append({
                                    "type": "duplicate_content",
                                    "severity": "warning",
                                    "blocking": False,
                                    "section_ids": list(pair),
                                    "description": f"Sections {pair[0]} and {pair[1]} share similar content (similarity: {similarity:.2f})",
                                    "sentence_snippets": [all_sentences[i][:80], all_sentences[j][:80]],
                                })
                                seen_pairs.add(pair)
        return flags

    def _detect_terminology_inconsistencies(
        self, section_texts: Dict[str, str], work_dir: Path
    ) -> List[Dict[str, Any]]:
        """Detect terminology inconsistencies across sections."""
        flags = []

        # Read terminology ledger if it exists
        ledger_path = work_dir / "TERMINOLOGY_LEDGER.json"
        if not ledger_path.exists():
            return flags

        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            terms = ledger.get("terms", {})

            # Check each term appears consistently in all sections
            for term, definition in terms.items():
                # Look for variations (case-insensitive, with/without hyphens)
                term_normalized = term.lower().replace("-", "").replace(" ", "")

                for section_id, text in section_texts.items():
                    # Find all term-like words in text
                    words = re.findall(r'\b\w+(?:-\w+)*\b', text.lower())
                    for word in set(words):
                        word_normalized = word.replace("-", "").replace(" ", "")
                        if word_normalized == term_normalized and word != term.lower():
                            flags.append({
                                "type": "terminology_inconsistency",
                                "severity": "info",
                                "blocking": False,
                                "section_ids": [section_id],
                                "description": f"Section {section_id} uses '{word}' instead of standardized term '{term}'",
                                "canonical_term": term,
                                "variant_found": word,
                            })
                            break
        except Exception:
            pass

        return flags

    def _detect_visual_gaps(
        self,
        section_registry: Dict[str, Any],
        work_dir: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        """Detect sections with visual_argument_slots but no visual placement."""
        flags = []
        sections = section_registry.get("sections", [])
        requested_sections: Set[str] = set()
        request_path = (
            work_dir / "CONCEPTUAL_FIGURE_REQUESTS.json"
            if work_dir is not None
            else None
        )
        if request_path is not None and request_path.exists():
            try:
                requests = json.loads(
                    request_path.read_text(encoding="utf-8")
                )
                requested_sections = {
                    str(item.get("section_id"))
                    for item in requests.get("requests", [])
                    if isinstance(item, dict) and item.get("section_id")
                }
            except Exception:
                requested_sections = set()

        for section in sections:
            section_id = section["section_id"]
            visual_slots = section.get("visual_argument_slots", [])
            # The prose-stage auditor owns detection, not image creation.  A
            # durable conceptual request means the issue has been handed to
            # the downstream Visual Editor and must not consume every revision
            # round again.
            if section_id in requested_sections:
                continue
            work_dir = Path(section.get("work_dir", ""))
            placement_path = work_dir / "SECTION_VISUAL_PLACEMENT.json"

            if visual_slots and placement_path.exists():
                try:
                    placement = json.loads(placement_path.read_text(encoding="utf-8"))
                    if not placement.get("placements"):
                        flags.append({
                            "type": "visual_gap",
                            "severity": "info",
                            "blocking": False,
                            "section_ids": [section_id],
                            "description": f"Section {section_id} has {len(visual_slots)} visual_argument_slots but no visual placements",
                        })
                except Exception:
                    pass
            elif visual_slots and not placement_path.exists():
                flags.append({
                    "type": "visual_gap",
                    "severity": "warning",
                    "blocking": False,
                    "section_ids": [section_id],
                    "description": f"Section {section_id} has {len(visual_slots)} visual_argument_slots but SECTION_VISUAL_PLACEMENT.json missing",
                })

        return flags

    def _detect_classification_confusion(self, section_registry: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Detect sections with same argument_role and high chunk overlap."""
        flags = []
        sections = section_registry.get("sections", [])

        # Group sections by argument_role
        role_groups = defaultdict(list)
        for section in sections:
            role = section.get("argument_role", "")
            if role:
                role_groups[role].append(section)

        # Check for high overlap within each role group
        for role, group in role_groups.items():
            if len(group) < 2:
                continue

            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    sec_i = group[i]
                    sec_j = group[j]

                    # Get chunk_ids from packages
                    chunks_i = self._get_section_chunk_ids(sec_i)
                    chunks_j = self._get_section_chunk_ids(sec_j)

                    if not chunks_i or not chunks_j:
                        continue

                    overlap = len(chunks_i & chunks_j)
                    union = len(chunks_i | chunks_j)
                    if union > 0:
                        jaccard = overlap / union
                        if jaccard > 0.6:
                            flags.append({
                                "type": "classification_confusion",
                                "severity": "error",
                                "blocking": True,
                                "section_ids": [sec_i["section_id"], sec_j["section_id"]],
                                "description": f"Sections {sec_i['section_id']} and {sec_j['section_id']} both have argument_role '{role}' with {jaccard:.1%} chunk overlap",
                                "overlap_ratio": jaccard,
                            })

        return flags

    def _get_section_chunk_ids(self, section: Dict[str, Any]) -> Set[str]:
        """Extract chunk_ids used by a section via the canonical evidence packet parser."""
        work_dir = Path(section.get("work_dir", ""))
        evidence_path = work_dir / "SECTION_EVIDENCE_PACKET.json"
        if not evidence_path.exists():
            return set()
        try:
            packet = load_section_evidence_packet(evidence_path)
            return {item.chunk_id for item in packet.items if item.chunk_id}
        except Exception:
            return set()

    def _detect_artifact_completeness(
        self, section_registry: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Flag sections missing expected artifacts AND sections with empty/invalid draft content."""
        flags = []
        expected = [
            "SECTION_AUTHORING_CONTEXT.json",
            "SECTION_ARGUMENT_PLAN.json",
            "SECTION_DRAFT_EN.md",
            "SECTION_AUTHORING_PACKAGE.json",
        ]
        for section in section_registry.get("sections", []):
            section_id = section["section_id"]
            work_dir = Path(section.get("work_dir", ""))

            # 1. File existence
            missing = [f for f in expected if not (work_dir / f).exists()]
            if missing:
                flags.append({
                    "type": "missing_artifact",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": f"Section {section_id} missing artifacts: {', '.join(missing)}",
                    "missing_files": missing,
                })
                continue  # no point reading content if file is missing

            # 2. Draft content quality
            draft_path = work_dir / "SECTION_DRAFT_EN.md"
            try:
                draft_text = draft_path.read_text(encoding="utf-8").strip()
            except Exception:
                draft_text = ""

            if not draft_text:
                flags.append({
                    "type": "empty_section_draft",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": f"Section {section_id} SECTION_DRAFT_EN.md is empty",
                })
                continue

            # Only-title check: strip markdown headers and check what's left
            body_without_headers = re.sub(r'^\s*#{1,6}\s+.*$', '', draft_text, flags=re.MULTILINE).strip()
            if not body_without_headers:
                flags.append({
                    "type": "empty_section_draft",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": f"Section {section_id} draft contains only headers, no prose body",
                })
                continue

            word_count = len(body_without_headers.split())
            if word_count < _MIN_SECTION_WORDS:
                flags.append({
                    "type": "draft_too_short",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": (
                        f"Section {section_id} draft has only {word_count} words "
                        f"(minimum {_MIN_SECTION_WORDS})"
                    ),
                })

            # 3. word_count consistency with SECTION_AUTHORING_PACKAGE.json
            pkg_path = work_dir / "SECTION_AUTHORING_PACKAGE.json"
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                pkg_words = int(pkg.get("word_count", 0))
                if pkg_words > 0 and abs(pkg_words - word_count) > max(10, pkg_words * 0.20):
                    flags.append({
                        "type": "stale_authoring_package",
                        "severity": "warning",
                        "blocking": False,
                        "section_ids": [section_id],
                        "description": (
                            f"Section {section_id} SECTION_AUTHORING_PACKAGE word_count={pkg_words} "
                            f"but current draft has {word_count} words — package needs regeneration"
                        ),
                    })
            except Exception:
                pass

            # 4. Citation audit stale marker
            stale_marker = work_dir / ".citation_audit_stale"
            if stale_marker.exists():
                flags.append({
                    "type": "stale_citation_audit",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": (
                        f"Section {section_id} draft was modified after last citation audit; "
                        "re-run citation audit before proceeding"
                    ),
                })

        return flags

    def _detect_cjk_leak(
        self, section_texts: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        """Detect CJK characters in English-language section drafts."""
        flags = []
        cjk_pattern = re.compile(r'[一-鿿぀-ヿ가-힯]')
        for section_id, text in section_texts.items():
            matches = cjk_pattern.findall(text)
            if matches:
                flags.append({
                    "type": "cjk_leak",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": f"Section {section_id} contains {len(matches)} CJK character(s) in English draft",
                    "sample": "".join(matches[:10]),
                })
        return flags

    def _detect_citation_validity(
        self, section_texts: Dict[str, str], section_registry: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Check that chunk_ids in evidence packets exist in the section KB."""
        flags = []
        for section in section_registry.get("sections", []):
            section_id = section["section_id"]
            work_dir = Path(section.get("work_dir", ""))
            evidence_path = work_dir / "SECTION_EVIDENCE_PACKET.json"
            if not evidence_path.exists():
                continue
            try:
                packet = load_section_evidence_packet(evidence_path)
            except ValueError as exc:
                flags.append({
                    "type": "invalid_evidence_packet",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": f"Section {section_id} evidence packet schema error: {exc}",
                })
                continue
            except Exception:
                continue

            # Validate chunk_ids against KB if kb path is known
            kb_path = self._resolve_kb_path(section)
            orphaned = []
            for item in packet.items:
                chunk_id = item.chunk_id
                if not chunk_id or "/" in chunk_id or "\\" in chunk_id:
                    orphaned.append(chunk_id)
                    continue
                if kb_path is not None:
                    if not self._chunk_exists_in_kb(kb_path, chunk_id):
                        orphaned.append(chunk_id)

            if orphaned:
                flags.append({
                    "type": "invalid_citation",
                    "severity": "error",
                    "blocking": True,
                    "section_ids": [section_id],
                    "description": (
                        f"Section {section_id} has {len(orphaned)} chunk_id(s) not found in KB: "
                        f"{orphaned[:3]}"
                    ),
                    "orphaned_chunk_ids": orphaned[:5],
                })
        return flags

    def _resolve_kb_path(self, section: Dict[str, Any]) -> Optional[Path]:
        """Try to find the KB sqlite path for a section."""
        work_dir = Path(section.get("work_dir", ""))
        # Check SECTION_AUTHORING_CONTEXT.json for kb_sqlite_path
        ctx_path = work_dir / "SECTION_AUTHORING_CONTEXT.json"
        if ctx_path.exists():
            try:
                ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
                kb = ctx.get("kb_sqlite_path") or ctx.get("kb_sqlite")
                if kb:
                    p = Path(kb)
                    if p.exists():
                        return p
            except Exception:
                pass
        return None

    def _chunk_exists_in_kb(self, kb_path: Path, chunk_id: str) -> bool:
        """Return True if chunk_id exists in text_chunks table."""
        try:
            import sqlite3 as _sq3
            conn = _sq3.connect(str(kb_path))
            try:
                row = conn.execute(
                    "SELECT 1 FROM text_chunks WHERE chunk_id=? LIMIT 1", (chunk_id,)
                ).fetchone()
                return row is not None
            finally:
                conn.close()
        except Exception:
            return True  # if KB unreadable, don't raise a false positive


# ---------------------------------------------------------------------------
# Layer 2 — LLM-based audit (optional, skipped cleanly if unavailable)
# ---------------------------------------------------------------------------

class LLMAuditLayer:
    """LLM-based structural and argumentative audit of the merged draft.

    Called only when a ResearchWorker-compatible model is available.
    If the model is unavailable or returns non-conformant output, emits a
    single `layer2_audit_failed` warning flag and does not crash.
    """

    _AUDIT_SCHEMA_KEYS = {"type", "severity", "section_ids", "description", "blocking"}

    def __init__(
        self,
        model_tier: str = "premium_model",
        model_override: Any = None,
        cost_budget_cny: float = 4.0,
        m1_library_path: Optional[Path] = None,
    ) -> None:
        self.model_tier = model_tier
        self.model_override = model_override
        self.cost_budget_cny = cost_budget_cny
        self.m1_library_path = m1_library_path

    def audit(
        self,
        merged_draft: str,
        section_registry: Dict[str, Any],
        blueprint: Dict[str, Any],
        work_dir: Path,
        round_num: int,
    ) -> List[Dict[str, Any]]:
        """Return LLM-generated audit flags or a single failure flag."""
        try:
            return self._run_audit(merged_draft, section_registry, blueprint, work_dir, round_num)
        except Exception as exc:
            return [{
                "type": "layer2_audit_failed",
                "severity": "warning",
                "blocking": False,
                "section_ids": [],
                "description": f"Layer 2 LLM audit skipped: {exc}",
            }]

    def _run_audit(
        self,
        merged_draft: str,
        section_registry: Dict[str, Any],
        blueprint: Dict[str, Any],
        work_dir: Path,
        round_num: int,
    ) -> List[Dict[str, Any]]:
        # Lazy import — ResearchWorker may not be importable in all environments
        try:
            from optomind_research.runtime.research_worker import ResearchWorker
            from optomind_research.runtime.global_audit_tool_provider import GlobalAuditToolProvider
        except ImportError as exc:
            raise RuntimeError(f"Layer 2 dependencies unavailable: {exc}") from exc

        tool_provider = GlobalAuditToolProvider(
            merged_draft=merged_draft,
            section_registry=section_registry,
            blueprint=blueprint,
            work_dir=work_dir,
            round_num=round_num,
            m1_library_path=self.m1_library_path,
        )

        prompt = self._build_prompt(section_registry, blueprint)
        role_prompt_path = (
            Path(__file__).resolve().parents[2]
            / "prompts"
            / "roles"
            / "Managing Editor.txt"
        )
        role_prompt = role_prompt_path.read_text(encoding="utf-8")
        audit_work_dir = work_dir / f"audit_round_{round_num}"
        audit_fingerprint = _semantic_audit_fingerprint(
            merged_draft,
            section_registry,
            blueprint,
            role_prompt,
        )
        # Keep each paid semantic audit in a content-addressed runtime
        # directory.  The provider still writes the canonical flags to the
        # round directory, while ResearchWorker can safely reuse an identical
        # audit and must rerun when either the article or audit instructions
        # change.
        worker_work_dir = (
            audit_work_dir / "worker_runs" / audit_fingerprint
        )
        worker = ResearchWorker(
            tool_provider=tool_provider,
            _model_override=self.model_override,
            _system_prompt_override=role_prompt,
            _work_dir_override=worker_work_dir,
        )

        from optomind_research.runtime.task_contract import TaskContract
        contract = TaskContract(
            # Stable identity makes an interrupted/retried audit idempotent
            # inside its round directory.
            run_id=f"layer2_audit_r{round_num}_{audit_fingerprint}",
            task_id="global_audit",
            goal=prompt,
            allowed_tools=tool_provider.get_allowed_tool_names(),
            skill_ids=["global-review-audit"],
            model_tier=self.model_tier,
            max_iters=24,
            token_budget=120_000,
            cost_budget_cny=self.cost_budget_cny,
            next_call_cost_reserve_cny=0.35,
            wall_time_budget_seconds=240.0,
            # The provider owns the canonical output in audit_work_dir and its
            # deterministic validator is the completion gate.  Requiring the
            # same file inside the isolated ResearchWorker directory would
            # couple runtime state to scientific artifacts.
            expected_outputs=[],
        )
        result = worker.run(contract)
        if result.status.value != "completed":
            raise RuntimeError(
                "Layer 2 worker did not pass validation: "
                f"{result.status.value}: {result.stop_reason}"
            )

        flags_path = work_dir / f"audit_round_{round_num}" / "LAYER2_AUDIT_FLAGS.json"
        fingerprint_flags_path = worker_work_dir / "LAYER2_AUDIT_FLAGS.json"
        # A cached identical worker must restore its own matching output rather
        # than accidentally expose flags written by a newer/different draft.
        if fingerprint_flags_path.exists():
            flags_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fingerprint_flags_path, flags_path)
        elif flags_path.exists():
            worker_work_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(flags_path, fingerprint_flags_path)
        if not flags_path.exists():
            raise RuntimeError("Layer 2 audit produced no output file")

        raw = json.loads(flags_path.read_text(encoding="utf-8"))
        flags = raw if isinstance(raw, list) else raw.get("flags", [])
        return [f for f in flags if self._validate_flag(f)]

    @staticmethod
    def _build_prompt_legacy(section_registry: Dict[str, Any], blueprint: Dict[str, Any]) -> str:
        section_ids = [s["section_id"] for s in section_registry.get("sections", [])]
        user_question = blueprint.get("input_context", {}).get("user_question", "")
        return (
            "You are auditing a merged multi-section literature review draft. "
            f"The review addresses: {user_question!r}. "
            f"Sections in order: {section_ids}. "
            "Start with load_global_review_context. Read full section text only "
            "where needed, then submit_audit_flags with a JSON list of flags. "
            "Each flag must have: type (string), severity (info|warning|error), "
            "section_ids (list), description (string), blocking (bool). "
            "Check: argument task completion, user question coverage, cross-section progression, "
            "repeated background introductions, unsupported 'further research' statements, "
            "classification taxonomy coherence, methodology identity, synthesis verdict, "
            "abstract/intro/body/conclusion alignment, figure argumentative function. "
            "Output ONLY the submit_audit_flags call — no prose commentary."
        )

    @staticmethod
    def _build_prompt(section_registry: Dict[str, Any], blueprint: Dict[str, Any]) -> str:
        """Build the task-specific goal; stable role rules live in the prompt file."""

        section_ids = [
            section["section_id"]
            for section in section_registry.get("sections", [])
        ]
        user_question = blueprint.get("input_context", {}).get(
            "user_question", ""
        )
        return (
            "Audit this multi-section scientific review as one article. "
            f"User question: {user_question!r}. "
            f"Section order: {section_ids}. "
            "Start with load_global_review_context. Treat its paragraph map and "
            "citation-concentration statistics as the primary audit evidence. "
            "You may inspect at most two section windows, and you must not read "
            "the article section by section. Then submit the smallest sufficient "
            "root-cause flag set and call "
            "validate_global_audit_package. Use English and tools only."
        )

    def _validate_flag(self, flag: Any) -> bool:
        if not isinstance(flag, dict):
            return False
        return all(k in flag for k in ("type", "severity", "section_ids", "description", "blocking"))
