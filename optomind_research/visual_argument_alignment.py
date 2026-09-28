"""M4 — Visual Argument Alignment.

Validates visual chunks in ReviewKnowledgeBase against the 8-type protocol,
maps them to blueprint sections and claims, and produces an auditable report.

Does NOT re-run visual models. Reads already-classified visual chunks only.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.visual_argument_protocol import derive_visual_argument_fields

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_VISUAL_ARGUMENT_TYPES: frozenset[str] = frozenset({
    "mechanism_anchor",
    "taxonomy_or_roadmap",
    "method_or_workflow",
    "quantitative_comparison",
    "trend_or_parameter_map",
    "representative_example",
    "anomaly_or_limitation",
    "synthesis_overview",
})

# Evidence-type → recommended visual_argument_types (for claim recommendations)
EVIDENCE_TYPE_TO_VISUAL_TYPES: dict[str, list[str]] = {
    "mechanism": ["mechanism_anchor", "method_or_workflow", "taxonomy_or_roadmap"],
    "measurement": ["quantitative_comparison", "trend_or_parameter_map", "representative_example"],
    "comparison": ["quantitative_comparison", "trend_or_parameter_map", "anomaly_or_limitation"],
    "application": [
        "representative_example",
        "method_or_workflow",
        "synthesis_overview",
        "anomaly_or_limitation",
    ],
}

DOMINANT_TYPE_WARNING_THRESHOLD = 0.70

# Section visual support thresholds
_STRONG_MIN_OK = 3
_STRONG_MIN_TYPES = 2


def merge_verified_visual_support(blueprint: dict, report: dict) -> dict:
    """Attach only claim-direct, entity-aligned M4 evidence to a blueprint."""
    import copy

    merged = copy.deepcopy(blueprint)
    by_claim = {
        str(row.get("claim_id") or ""): row
        for row in (report.get("claim_visual_support") or [])
        if isinstance(row, dict) and row.get("claim_id")
    }
    promoted = 0
    provisional = 0
    for section in merged.get("sections") or []:
        section_verified: list[str] = []
        for claim in section.get("claims") or []:
            row = by_claim.get(str(claim.get("claim_id") or ""), {})
            accepted: list[dict] = []
            provisional_rows: list[dict] = []
            for visual in row.get("reranked_visual_chunks") or []:
                is_accepted = (
                    visual.get("evidence_mode") == "vision_image_text"
                    and visual.get("directness") == "direct"
                    and visual.get("support_strength") in {"strong", "medium"}
                    and visual.get("entity_alignment") in {"exact", "compatible"}
                    and not visual.get("needs_human_review")
                )
                (accepted if is_accepted else provisional_rows).append(dict(visual))
            claim["verified_visual_evidence"] = accepted
            claim["provisional_visual_candidates"] = provisional_rows
            claim["supporting_visual_chunk_ids"] = list(dict.fromkeys(
                str(x.get("chunk_id")) for x in accepted if x.get("chunk_id")
            ))
            section_verified.extend(claim["supporting_visual_chunk_ids"])
            promoted += len(accepted)
            provisional += len(provisional_rows)
        section["verified_visual_chunk_ids"] = list(dict.fromkeys(section_verified))
    merged["visual_alignment_status"] = {
        "schema_version": "m4.visual_merge.v1",
        "promoted_direct_visuals": promoted,
        "provisional_visuals": provisional,
        "source_report_schema": report.get("schema_version", ""),
        "policy": "vision+direct+strong_or_medium+entity_aligned+no_human_review",
    }
    return merged


def summarize_post_rerank_claim_support(claim_support: list[dict]) -> dict[str, Any]:
    """Summarize *verified* visual usefulness without treating candidates as support.

    A candidate list only means that retrieval found something worth inspecting.
    It becomes usable support only after the reranker judges it direct or partial.
    Claims skipped because of a smoke-test budget remain ``not_evaluated`` rather
    than being mislabeled as visual gaps.
    """
    counts: Counter[str] = Counter()
    gap_claim_ids: list[str] = []
    unevaluated_claim_ids: list[str] = []
    section_statuses: dict[str, list[str]] = {}

    for record in claim_support:
        accepted = list(record.get("reranked_visual_chunks") or [])
        rejected = list(record.get("rejected_visual_chunks") or [])
        candidates = list(record.get("candidate_visual_recommendations") or [])
        evaluated = accepted + rejected

        has_direct = any(
            str(row.get("directness") or "") == "direct"
            and str(row.get("support_strength") or "") in {"strong", "medium"}
            for row in accepted
        )
        has_provisional = any(
            str(row.get("directness") or "") in {"direct", "partial"}
            and str(row.get("support_strength") or "") == "weak"
            for row in accepted
        )
        if has_direct:
            status = "direct_support"
        elif has_provisional:
            status = "provisional_support"
        elif evaluated:
            status = "background_only"
            gap_claim_ids.append(str(record.get("claim_id") or ""))
        elif candidates:
            status = "not_evaluated"
            unevaluated_claim_ids.append(str(record.get("claim_id") or ""))
        else:
            status = "no_candidates"
            gap_claim_ids.append(str(record.get("claim_id") or ""))

        record["post_rerank_support_status"] = status
        counts[status] += 1
        section_id = str(record.get("section_id") or "")
        if section_id:
            section_statuses.setdefault(section_id, []).append(status)

    gap_section_ids: list[str] = []
    unknown_section_ids: list[str] = []
    for section_id, statuses in section_statuses.items():
        if any(status in {"direct_support", "provisional_support"} for status in statuses):
            continue
        if any(status in {"background_only", "no_candidates"} for status in statuses):
            gap_section_ids.append(section_id)
        elif statuses and all(status == "not_evaluated" for status in statuses):
            unknown_section_ids.append(section_id)

    return {
        "evaluated_claims": int(counts["direct_support"] + counts["provisional_support"] + counts["background_only"]),
        "direct_support_claims": int(counts["direct_support"]),
        "provisional_support_claims": int(counts["provisional_support"]),
        "background_only_claims": int(counts["background_only"]),
        "no_candidate_claims": int(counts["no_candidates"]),
        "not_evaluated_claims": int(counts["not_evaluated"]),
        "visual_gap_claim_ids": [claim_id for claim_id in gap_claim_ids if claim_id],
        "visual_gap_section_ids": sorted(gap_section_ids),
        "unevaluated_claim_ids": [claim_id for claim_id in unevaluated_claim_ids if claim_id],
        "unevaluated_section_ids": sorted(unknown_section_ids),
    }


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class VisualArgumentAlignmentResult:
    schema_version: str = "visual_argument_alignment.v1"
    total_visual_chunks: int = 0
    ok_visual_chunks: int = 0
    invalid_visual_chunks: int = 0   # status=ok but type not in valid set
    missing_type_count: int = 0      # status=ok but type empty/None
    failed_chunks: int = 0           # status=failed or unknown
    type_distribution: dict = field(default_factory=dict)
    dominant_type: str = ""
    dominant_type_ratio: float = 0.0
    warnings: list[str] = field(default_factory=list)
    section_visual_support: list[dict] = field(default_factory=list)
    claim_visual_support: list[dict] = field(default_factory=list)
    visual_gap_sections: list[str] = field(default_factory=list)
    sample_records: list[dict] = field(default_factory=list)
    # auto-recommend fields
    auto_recommended_sections_count: int = 0
    sections_without_visual_support_count: int = 0
    total_recommended_visual_chunks: int = 0
    section_recommendation_samples: list[dict] = field(default_factory=list)
    claim_recommendation_samples: list[dict] = field(default_factory=list)
    # rerank quality stats (populated when rerank=True)
    rerank_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version,
            "total_visual_chunks": self.total_visual_chunks,
            "ok_visual_chunks": self.ok_visual_chunks,
            "invalid_visual_chunks": self.invalid_visual_chunks,
            "missing_type_count": self.missing_type_count,
            "failed_chunks": self.failed_chunks,
            "type_distribution": self.type_distribution,
            "dominant_type": self.dominant_type,
            "dominant_type_ratio": self.dominant_type_ratio,
            "warnings": self.warnings,
            "section_visual_support": self.section_visual_support,
            "claim_visual_support": self.claim_visual_support,
            "visual_gap_sections": self.visual_gap_sections,
            "sample_records": self.sample_records,
            "auto_recommended_sections_count": self.auto_recommended_sections_count,
            "sections_without_visual_support_count": self.sections_without_visual_support_count,
            "total_recommended_visual_chunks": self.total_recommended_visual_chunks,
            "section_recommendation_samples": self.section_recommendation_samples,
            "claim_recommendation_samples": self.claim_recommendation_samples,
        }
        if self.rerank_stats:
            d["rerank_stats"] = self.rerank_stats
        return d


# ---------------------------------------------------------------------------
# Aligner
# ---------------------------------------------------------------------------

class VisualArgumentAligner:
    """Validates, maps, and reports visual argument alignment for a blueprint."""

    def __init__(self, domain_config_path: Path | None = None) -> None:
        try:
            from optomind_research.domain_config_loader import (
                get_preferred_visual_types,
                get_section_role_keywords,
                load_domain_config,
            )
            cfg = load_domain_config(domain_config_path)
            self.domain_preferred_visual_types = get_preferred_visual_types(cfg)
            self.section_role_keywords = get_section_role_keywords(cfg)
        except Exception:
            self.domain_preferred_visual_types = []
            self.section_role_keywords = {}

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    def load_visual_chunks_from_jsonl(self, path: Path) -> list[dict]:
        chunks: list[dict] = []
        if not Path(path).exists():
            return chunks
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    chunks.append(row)
            except json.JSONDecodeError:
                pass
        return chunks

    def load_visual_chunks_from_sqlite(self, sqlite_path: Path) -> list[dict]:
        chunks: list[dict] = []
        p = Path(sqlite_path)
        if not p.exists():
            return chunks
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        try:
            available = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(visual_chunks)").fetchall()
            }
            wanted = [
                "chunk_id", "paper_id", "doi", "title", "caption", "chunk_kind",
                "visual_role", "review_utility", "parent_label", "subfigure_label",
                "search_text", "visual_argument_type", "visual_argument_status",
                "visual_argument_confidence", "visual_argument_claim",
                "visual_argument_needs_human_review", "visual_argument_schema_version",
                "local_image_path", "raw_json",
            ]
            selected = [name for name in wanted if name in available]
            if not selected:
                return []
            rows = conn.execute(
                f"SELECT {', '.join(selected)} FROM visual_chunks"
            ).fetchall()
            for r in rows:
                db_row = dict(r)
                raw: dict[str, Any] = {}
                try:
                    raw = json.loads(str(db_row.pop("raw_json", "") or "{}"))
                except Exception:
                    raw = {}
                merged = {
                    **raw,
                    **{key: value for key, value in db_row.items() if value not in (None, "")},
                }
                for key, value in derive_visual_argument_fields(merged).items():
                    if merged.get(key) in (None, ""):
                        merged[key] = value
                chunks.append(merged)
        except Exception as exc:
            # A schema mismatch must be visible. Returning an empty list made a
            # populated 569-image KB look like a legitimate no-image corpus.
            raise RuntimeError(f"Failed to load visual_chunks from {p}: {exc}") from exc
        finally:
            conn.close()
        return chunks

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_visual_chunk(self, chunk: dict) -> tuple[bool, str]:
        """Return (is_valid, reject_reason). Valid = status ok + type in valid set."""
        status = chunk.get("visual_argument_status", "")
        if status == "failed":
            return False, "status_failed"
        if status != "ok":
            return False, f"unknown_status:{status or 'empty'}"
        vtype = chunk.get("visual_argument_type", "") or ""
        if not vtype:
            return False, "missing_type"
        if vtype not in VALID_VISUAL_ARGUMENT_TYPES:
            return False, f"invalid_type:{vtype}"
        return True, ""

    # ------------------------------------------------------------------
    # Distribution summary
    # ------------------------------------------------------------------

    def summarize_distribution(self, chunks: list[dict]) -> dict[str, Any]:
        ok = [c for c in chunks if c.get("visual_argument_status") == "ok"
              and c.get("visual_argument_type", "") in VALID_VISUAL_ARGUMENT_TYPES]
        type_counts = Counter(c.get("visual_argument_type", "") for c in ok)
        total_ok = len(ok)
        if type_counts:
            dom_type, dom_count = type_counts.most_common(1)[0]
        else:
            dom_type, dom_count = "", 0
        return {
            "total": len(chunks),
            "ok": total_ok,
            "type_distribution": dict(type_counts),
            "dominant_type": dom_type,
            "dominant_type_ratio": round(dom_count / total_ok, 4) if total_ok else 0.0,
        }

    # ------------------------------------------------------------------
    # Section-level mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _section_support_status(ok_count: int, type_count: int) -> str:
        if ok_count == 0:
            return "no_visual_support"
        if ok_count >= _STRONG_MIN_OK and type_count >= _STRONG_MIN_TYPES:
            return "strong_visual_support"
        return "some_visual_support"

    # ------------------------------------------------------------------
    # Retrieval utilities
    # ------------------------------------------------------------------

    # Generic English stopwords + academic boilerplate + year pattern
    _STOPWORDS: frozenset[str] = frozenset({
        "the", "and", "for", "this", "that", "with", "from", "are", "was",
        "has", "have", "its", "can", "may", "but", "not", "also", "all",
        "than", "been", "more", "such", "these", "their", "they", "both",
        "some", "well", "any", "our", "one", "two", "fig", "figure", "table",
        "above", "below", "left", "right", "shown", "show", "shows", "used",
        "using", "where", "when", "which", "were", "will", "than", "into",
        "under", "over", "each", "other", "while", "here", "thus", "then",
        "between", "through", "within", "during", "after", "upon", "about",
        "result", "results", "study", "studies", "analysis", "value", "values",
        "sample", "samples", "method", "methods", "data", "use", "due", "per",
        "via", "see", "new", "high", "low", "large", "small", "black", "white",
        "red", "blue", "green", "color", "top", "bottom", "panel", "panels",
        # visual field name boilerplate
        "argument", "visual", "claim", "type", "status", "chunk", "supported",
        "aspect", "confidence", "schema", "version", "role", "anchor",
        # English writing glue words
        "achieve", "achieved", "achieving", "provide", "provides", "provided",
        "demonstrate", "demonstrated", "shows", "show", "indicate", "indicates",
        "present", "presents", "compare", "compared", "comparing", "based",
        "obtain", "obtained", "measure", "measured", "calculate", "calculated",
        "improve", "improved", "increase", "increased", "decrease", "decreased",
        "significant", "significantly", "difference", "different",
        "better", "worse", "higher", "lower", "greater", "smaller",
        "body", "text", "section", "page", "background", "related",
        "work", "research", "paper", "approach", "various", "several",
        "first", "second", "third", "note", "main", "typical",
        "correspond", "corresponding", "apply", "applied", "given",
        "together", "along", "across", "around", "without", "although",
        "however", "therefore", "therefore", "respectively",
    })

    @classmethod
    def _tokenize_for_retrieval(cls, text: str) -> set[str]:
        """Domain-aware tokenizer: min 4 chars, no stopwords, no numerics/years, no URL fragments."""
        tokens = set()
        for w in re.split(r'[\W_]+', text.lower()):
            if len(w) < 4:
                continue
            # reject URL fragments and overly long compound tokens (e.g. "advancedsciencenews")
            if len(w) > 18:
                continue
            if w in cls._STOPWORDS:
                continue
            # reject pure numeric tokens (years, page numbers, etc.)
            if re.fullmatch(r'\d+', w):
                continue
            # reject tokens that are digits with a single letter suffix (e.g. "2021a")
            if re.fullmatch(r'\d+[a-z]?', w):
                continue
            tokens.add(w)
        return tokens

    @staticmethod
    def _doi_from_chunk_id(chunk_id: str) -> str:
        """Extract a plausible DOI prefix from a chunk id, if present.

        Legacy ids look like ``doi-10.xxx:type:sNNNN`` or ``10.xxx:type:sNNNN``.
        Arbitrary namespace prefixes (for example ``unit:visual:*``) never
        become DOIs; callers fall back to the explicit candidate DOI instead.
        """

        prefix = chunk_id.split(":", 1)[0] if ":" in chunk_id else ""
        if prefix.startswith("doi-"):
            prefix = prefix[4:]
        if prefix.startswith("10.") and "." in prefix:
            return prefix
        return ""

    def _preferred_visual_types_for_section(self, section: dict) -> set[str]:
        """Infer useful visual argument types from a section's argumentative role."""
        text = " ".join([
            section.get("title") or "",
            section.get("argument_role") or "",
        ]).lower()
        preferred: set[str] = set()

        role_types = {
            "mechanism": {"mechanism_anchor", "method_or_workflow", "trend_or_parameter_map"},
            "materials": {"method_or_workflow", "mechanism_anchor", "representative_example"},
            "performance": {"quantitative_comparison", "trend_or_parameter_map", "mechanism_anchor"},
            "applications": {"representative_example", "method_or_workflow", "quantitative_comparison", "anomaly_or_limitation"},
            "challenges": {"anomaly_or_limitation", "quantitative_comparison", "trend_or_parameter_map"},
            "methods": {"method_or_workflow", "quantitative_comparison", "trend_or_parameter_map"},
            "future": {"taxonomy_or_roadmap", "synthesis_overview", "trend_or_parameter_map"},
        }
        for role, keywords in (self.section_role_keywords or {}).items():
            if any(str(keyword).lower() in text for keyword in keywords):
                preferred.update(role_types.get(role, set()))

        # Generic fallback when a domain config does not define the role.
        generic_rules = {
            "mechanism": ("mechanism", "physical", "principle", "theory"),
            "materials": ("material", "structure", "fabrication"),
            "performance": ("metric", "measurement", "performance", "benchmark"),
            "applications": ("application", "deployment", "device", "system"),
            "challenges": ("trade-off", "tradeoff", "bottleneck", "limitation", "challenge", "gap"),
            "methods": ("method", "characterization", "simulation", "workflow"),
            "future": ("future", "frontier", "roadmap", "outlook"),
        }
        for role, keywords in generic_rules.items():
            if any(keyword in text for keyword in keywords):
                preferred.update(role_types[role])

        if not preferred:
            preferred.update(self.domain_preferred_visual_types[:3])

        return preferred

    def build_visual_retrieval_index(
        self,
        chunks: list[dict],
        *,
        allowed_statuses: tuple[str, ...] = ("ok",),
    ) -> list[dict]:
        """Build a lightweight retrieval index from allowed visual states.

        The canonical KB keeps newly extracted figures in
        ``pending_multimodal_review`` until a vision model has inspected them.
        Requiring a corpus-wide vision pass before retrieval made useful
        current-topic figures invisible and spent money on assets that would
        never be selected.  Callers that only accept fully classified assets
        retain the historical default (``("ok",)``).  The article visual
        editor may additionally retrieve pending assets, then audit only the
        small selected shortlist.
        """
        index = []
        allowed = {str(value) for value in allowed_statuses}
        for c in chunks:
            status = str(c.get("visual_argument_status") or "")
            if status not in allowed:
                continue
            vtype = c.get("visual_argument_type", "")
            if vtype not in VALID_VISUAL_ARGUMENT_TYPES:
                continue
            text_parts = [
                c.get("title") or "",
                c.get("caption") or "",
                c.get("search_text") or "",        # rich semantic text from KB
                c.get("parent_label") or "",
                c.get("visual_argument_claim") or "",
                c.get("visual_argument_supported_aspect") or "",
                c.get("visual_role") or "",
                vtype,
            ]
            combined = " ".join(t for t in text_parts if t)
            cid = c.get("chunk_id", "")
            doi = str(c.get("doi") or "") or self._doi_from_chunk_id(cid)
            index.append({
                "chunk_id": cid,
                "paper_id": c.get("paper_id", ""),
                "doi": doi,
                "title": (c.get("title") or "")[:100],
                "caption": c.get("caption") or "",
                "local_image_path": c.get("local_image_path", ""),
                "visual_argument_type": vtype,
                "visual_argument_status": status,
                "visual_argument_confidence": c.get("visual_argument_confidence", ""),
                "visual_argument_claim": c.get("visual_argument_claim") or "",
                "visual_argument_needs_human_review": bool(
                    c.get("visual_argument_needs_human_review", False)
                ),
                "tokens": self._tokenize_for_retrieval(combined),
            })
        return index

    def recommend_visuals_for_section(
        self,
        section: dict,
        index: list[dict],
        top_k: int = 6,
    ) -> list[dict]:
        """Lexical scoring to recommend visual chunks for a section."""
        query_parts = [
            section.get("title") or "",
            section.get("argument_role") or "",
        ]
        for tc in (section.get("candidate_text_chunks") or [])[:4]:
            query_parts.append(tc.get("text_preview") or "")
        for claim in (section.get("claims") or [])[:4]:
            query_parts.append(claim.get("statement") or claim.get("claim_seed") or "")

        query_text = " ".join(q for q in query_parts if q)
        query_tokens = self._tokenize_for_retrieval(query_text)
        if not query_tokens:
            return []

        combined_role = (
            (section.get("argument_role") or "") + " " + (section.get("title") or "")
        ).lower()
        preferred_types = self._preferred_visual_types_for_section(section)

        scored = []
        for entry in index:
            chunk_tokens = entry["tokens"]
            if not chunk_tokens:
                continue
            intersection = len(query_tokens & chunk_tokens)
            if intersection == 0:
                continue
            union = len(query_tokens | chunk_tokens)
            score = intersection / union

            if entry["caption"]:
                score += 0.05

            vtype = entry["visual_argument_type"]
            reason_bits: list[str] = []
            if vtype in preferred_types:
                score += 0.12
                reason_bits.append("preferred type for section role")
            elif vtype == "anomaly_or_limitation" and "anomaly_or_limitation" not in preferred_types:
                score -= 0.05

            if any(kw in combined_role for kw in ("mechanism", "process", "how", "principle")):
                if vtype in ("mechanism_anchor", "method_or_workflow"):
                    score += 0.10
            if any(kw in combined_role for kw in ("comparison", "benchmark", "performance", "versus")):
                if vtype in ("quantitative_comparison", "trend_or_parameter_map"):
                    score += 0.10
            if any(kw in combined_role for kw in ("overview", "taxonomy", "classification", "landscape")):
                if vtype in ("taxonomy_or_roadmap", "synthesis_overview"):
                    score += 0.10
            if any(kw in combined_role for kw in ("limitation", "challenge", "gap", "anomaly")):
                if vtype in ("anomaly_or_limitation",):
                    score += 0.10

            if entry["visual_argument_confidence"] == "high":
                score += 0.05
            elif entry["visual_argument_confidence"] == "medium":
                score += 0.02

            matched = sorted(query_tokens & chunk_tokens)[:5]
            reason_tail = "; ".join(reason_bits)
            reason = f"lexical match [{', '.join(matched[:3])}]; type={vtype}"
            if reason_tail:
                reason = f"{reason}; {reason_tail}"
            scored.append({
                "chunk_id": entry["chunk_id"],
                "paper_id": entry.get("paper_id", ""),
                "visual_argument_type": vtype,
                "title": entry["title"],
                "doi": entry["doi"],
                "local_image_path": entry.get("local_image_path", ""),
                "caption_preview": (entry["caption"] or "")[:120],
                "visual_argument_status": entry.get(
                    "visual_argument_status", ""
                ),
                "visual_argument_needs_human_review": bool(
                    entry.get("visual_argument_needs_human_review", False)
                ),
                "score": round(score, 4),
                "reason": reason,
                "_doi_key": entry["doi"],
            })

        scored.sort(key=lambda x: -x["score"])

        result: list[dict] = []
        doi_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        max_per_type = max(2, top_k // 3)

        def try_add(item: dict, *, enforce_type_cap: bool) -> bool:
            d = item["_doi_key"]
            if doi_counts.get(d, 0) >= 2:
                return False
            t = item["visual_argument_type"]
            if enforce_type_cap and type_counts.get(t, 0) >= max_per_type:
                return False
            doi_counts[d] = doi_counts.get(d, 0) + 1
            type_counts[t] = type_counts.get(t, 0) + 1
            result.append({k: v for k, v in item.items() if k != "_doi_key"})
            return True

        for item in scored:
            try_add(item, enforce_type_cap=True)
            if len(result) >= top_k:
                break
        if len(result) < top_k:
            for item in scored:
                if item["chunk_id"] in {r["chunk_id"] for r in result}:
                    continue
                try_add(item, enforce_type_cap=False)
                if len(result) >= top_k:
                    break
        return result

    # ------------------------------------------------------------------
    # Section-level mapping
    # ------------------------------------------------------------------

    def map_visuals_to_sections(
        self,
        blueprint: dict,
        chunk_index: dict[str, dict],
        retrieval_index: list[dict] | None = None,
        section_top_k: int = 6,
    ) -> list[dict]:
        results = []
        for section in blueprint.get("sections") or []:
            sid = section.get("section_id", "")
            raw_visual = section.get("candidate_visual_chunks") or []

            ok_chunks: list[dict] = []
            for vc in raw_visual:
                cid = vc.get("chunk_id", "")
                full = chunk_index.get(cid, vc)
                status = full.get("visual_argument_status") or vc.get("visual_argument_status", "")
                vtype = full.get("visual_argument_type") or vc.get("visual_argument_type", "")
                if status == "ok" and vtype in VALID_VISUAL_ARGUMENT_TYPES:
                    ok_chunks.append({"chunk_id": cid, "visual_argument_type": vtype})

            type_set = {c["visual_argument_type"] for c in ok_chunks}
            recommended: list[dict] = []

            # A blueprint pool is only a seed, not a reason to stop searching the
            # visual KB.  Always augment it when retrieval is enabled, otherwise
            # a small early pool can permanently hide a much better figure.
            if retrieval_index is not None:
                recommended = self.recommend_visuals_for_section(
                    section, retrieval_index, top_k=section_top_k
                )
                existing_ids = {c["chunk_id"] for c in ok_chunks}
                recommended = [r for r in recommended if r.get("chunk_id") not in existing_ids]
                rec_count = len(recommended)
                combined_types = type_set | {r["visual_argument_type"] for r in recommended}
                combined_count = len(ok_chunks) + rec_count
                if ok_chunks and rec_count > 0:
                    source = "provided_and_augmented_from_kb"
                    support_status = self._section_support_status(combined_count, len(combined_types))
                    rec_reason = f"{rec_count} additional chunk(s) recommended from KB"
                elif ok_chunks:
                    source = "provided_by_blueprint"
                    support_status = self._section_support_status(len(ok_chunks), len(type_set))
                    rec_reason = "no additional KB match"
                elif rec_count > 0:
                    source = "auto_recommended_from_kb"
                    support_status = self._section_support_status(rec_count, len(combined_types))
                    rec_reason = f"{rec_count} chunk(s) auto-recommended from KB"
                else:
                    source = "none"
                    support_status = "no_visual_support"
                    rec_reason = "no matching chunks in KB"
            elif ok_chunks:
                source = "provided_by_blueprint"
                support_status = self._section_support_status(len(ok_chunks), len(type_set))
                rec_reason = ""
            else:
                source = "none"
                support_status = "no_visual_support"
                rec_reason = ""

            entry: dict = {
                "section_id": sid,
                "title": (section.get("title") or "")[:80],
                "total_candidate_visual_chunks": len(raw_visual),
                "ok_visual_chunks": len(ok_chunks),
                "visual_argument_type_distribution": dict(
                    Counter(
                        c["visual_argument_type"] for c in (
                            ok_chunks if ok_chunks else recommended
                        )
                    )
                ),
                "visual_support_status": support_status,
                "source": source,
            }
            if recommended:
                entry["recommended_visual_chunks"] = recommended
                entry["recommendation_reason_summary"] = rec_reason
            results.append(entry)
        return results

    # ------------------------------------------------------------------
    # Claim-level mapping
    # ------------------------------------------------------------------

    def map_visuals_to_claims(
        self,
        blueprint: dict,
        chunk_index: dict[str, dict],
        claim_top_k: int = 3,
        auto_pools: dict[str, list[dict]] | None = None,
        retrieval_index_by_id: dict[str, dict] | None = None,
    ) -> list[dict]:
        results = []
        for section in blueprint.get("sections") or []:
            sid = section.get("section_id", "")
            ok_in_section: dict[str, str] = {}
            source_by_id: dict[str, str] = {}
            for vc in section.get("candidate_visual_chunks") or []:
                cid = vc.get("chunk_id", "")
                full = chunk_index.get(cid, vc)
                status = full.get("visual_argument_status") or vc.get("visual_argument_status", "")
                vtype = full.get("visual_argument_type") or vc.get("visual_argument_type", "")
                if status == "ok" and vtype in VALID_VISUAL_ARGUMENT_TYPES:
                    ok_in_section[cid] = vtype
                    source_by_id[cid] = "provided_by_blueprint"

            # KB retrieval augments the blueprint pool instead of being used only
            # as an empty-pool fallback.
            if auto_pools and sid in auto_pools:
                for rec in auto_pools[sid]:
                    rcid = rec.get("chunk_id", "")
                    rvtype = rec.get("visual_argument_type", "")
                    if rcid and rvtype in VALID_VISUAL_ARGUMENT_TYPES:
                        ok_in_section[rcid] = rvtype
                        source_by_id.setdefault(rcid, "auto_recommended_from_kb")

            for claim in section.get("claims") or []:
                cid = claim.get("claim_id", "")
                etype = claim.get("evidence_type", "")
                existing_ids = list(claim.get("supporting_visual_chunk_ids") or [])

                # A section-level pool is useful for layout planning but is
                # too small for claim-level evidence retrieval. Search the
                # entire validated visual KB for each claim, then let the
                # multimodal reranker decide whether a candidate is direct,
                # partial, contextual, or unrelated.
                claim_candidate_pool = dict(ok_in_section)
                if retrieval_index_by_id:
                    for visual_id, entry in retrieval_index_by_id.items():
                        visual_type = str(entry.get("visual_argument_type") or "")
                        if visual_id and visual_type in VALID_VISUAL_ARGUMENT_TYPES:
                            claim_candidate_pool.setdefault(visual_id, visual_type)
                            source_by_id.setdefault(visual_id, "claim_retrieved_from_kb")

                valid_existing = [vid for vid in existing_ids if vid in claim_candidate_pool]
                warn: list[str] = []

                if existing_ids and not valid_existing:
                    warn.append(
                        "supporting_visual_chunk_ids present but none found in section ok pool"
                    )

                recommended_types = set(EVIDENCE_TYPE_TO_VISUAL_TYPES.get(etype, []))

                if valid_existing and recommended_types:
                    existing_types = {claim_candidate_pool[vid] for vid in valid_existing}
                    if not (existing_types & recommended_types):
                        warn.append(
                            f"existing visual types {sorted(existing_types)} may not align with "
                            f"evidence_type '{etype}' (recommended: {sorted(recommended_types)})"
                        )

                # Enhanced scoring for candidate recommendations
                claim_text = " ".join([
                    claim.get("statement") or "",
                    claim.get("claim_seed") or "",
                    section.get("argument_role") or "",
                    " ".join(
                        str(row.get("component") or "")
                        for row in (claim.get("evidence_component_map") or [])
                        if isinstance(row, dict)
                    ),
                    " ".join(
                        str(value or "")
                        for value in (claim.get("missing_evidence_components") or [])
                    ),
                ])
                claim_tokens = self._tokenize_for_retrieval(claim_text)

                scored: list[dict] = []
                for vid, vtype in claim_candidate_pool.items():
                    if vid in existing_ids:
                        continue
                    # Prefer chunk_index (full DB record), fall back to retrieval index
                    full_c = chunk_index.get(vid) or (
                        retrieval_index_by_id.get(vid, {}) if retrieval_index_by_id else {}
                    )
                    chunk_text = " ".join([
                        full_c.get("title") or "",
                        full_c.get("caption") or "",
                        full_c.get("search_text") or "",
                        full_c.get("visual_argument_claim") or "",
                        full_c.get("visual_argument_supported_aspect") or "",
                    ])
                    chunk_tokens = self._tokenize_for_retrieval(chunk_text)

                    score = 0.0
                    reason_parts: list[str] = []
                    matched: list[str] = []

                    if vtype in recommended_types:
                        score += 0.3
                        reason_parts.append(f"type={vtype} matches evidence_type={etype}")

                    if claim_tokens and chunk_tokens:
                        overlap = claim_tokens & chunk_tokens
                        if overlap:
                            jaccard = len(overlap) / len(claim_tokens | chunk_tokens)
                            score += jaccard * 0.5
                            matched = sorted(overlap)[:5]
                            reason_parts.append(f"term overlap [{', '.join(matched[:3])}]")

                    conf = full_c.get("visual_argument_confidence", "")
                    if conf == "high":
                        score += 0.10
                        reason_parts.append("high confidence")
                    elif conf == "medium":
                        score += 0.05

                    if score > 0:
                        doi = str(full_c.get("doi") or "") or self._doi_from_chunk_id(vid)
                        scored.append({
                            "chunk_id": vid,
                            "visual_argument_type": vtype,
                            "score": round(score, 4),
                            "reason": "; ".join(reason_parts) or "type candidate",
                            "matched_terms": matched,
                            "source": source_by_id.get(vid, "provided_by_blueprint"),
                            "_doi_key": doi,
                        })

                scored.sort(key=lambda x: -x["score"])
                recs: list[dict] = []
                doi_used: dict[str, int] = {}
                for item in scored:
                    d = item["_doi_key"]
                    if doi_used.get(d, 0) >= 2:
                        continue
                    doi_used[d] = doi_used.get(d, 0) + 1
                    recs.append({k: v for k, v in item.items() if k != "_doi_key"})
                    if len(recs) >= claim_top_k:
                        break

                results.append({
                    "claim_id": cid,
                    "section_id": section.get("section_id", ""),
                    "claim_statement": claim.get("statement", ""),
                    "evidence_type": etype,
                    "evidence_binding_reason": claim.get("evidence_binding_reason", ""),
                    "evidence_component_map": list(claim.get("evidence_component_map") or []),
                    "missing_evidence_components": list(claim.get("missing_evidence_components") or []),
                    "existing_visual_ids": existing_ids,
                    "valid_existing_visual_ids": valid_existing,
                    "recommended_visual_argument_types": sorted(recommended_types),
                    "candidate_visual_recommendations": recs,
                    "warnings": warn,
                })
        return results

    # ------------------------------------------------------------------
    # Main report builder
    # ------------------------------------------------------------------

    def build_alignment_report(
        self,
        chunks: list[dict],
        blueprint: dict | None = None,
        auto_recommend: bool = False,
        section_top_k: int = 6,
        claim_top_k: int = 3,
        rerank: bool = False,
        rerank_real_llm: bool = False,
        rerank_model_tier: str = "standard_model",
        rerank_workers: int = 4,
        rerank_cache: Path | str | None = None,
        rerank_max_items: int | None = None,
        rerank_load_bearing_only: bool = False,
    ) -> VisualArgumentAlignmentResult:
        warnings: list[str] = []

        ok_chunks: list[dict] = []
        invalid_type: list[dict] = []
        missing_type: list[dict] = []
        failed: list[dict] = []

        for c in chunks:
            valid, reason = self.validate_visual_chunk(c)
            if valid:
                ok_chunks.append(c)
            elif reason == "status_failed":
                failed.append(c)
            elif reason == "missing_type":
                missing_type.append(c)
            elif reason.startswith("invalid_type"):
                invalid_type.append(c)
            else:
                failed.append(c)  # unknown status

        type_dist = dict(Counter(c.get("visual_argument_type", "") for c in ok_chunks))
        total_ok = len(ok_chunks)

        if type_dist:
            dom_type, dom_count = Counter(type_dist).most_common(1)[0]
        else:
            dom_type, dom_count = "", 0
        dom_ratio = round(dom_count / total_ok, 4) if total_ok else 0.0

        if dom_ratio > DOMINANT_TYPE_WARNING_THRESHOLD:
            warnings.append(
                f"Dominant type '{dom_type}' represents {dom_ratio:.0%} of ok chunks "
                f"(threshold {DOMINANT_TYPE_WARNING_THRESHOLD:.0%})"
            )
        if missing_type:
            warnings.append(
                f"{len(missing_type)} chunk(s) have status=ok but missing or empty visual_argument_type"
            )
        if invalid_type:
            warnings.append(
                f"{len(invalid_type)} chunk(s) have status=ok but invalid visual_argument_type"
            )

        chunk_index: dict[str, dict] = {c.get("chunk_id", ""): c for c in chunks}

        section_support: list[dict] = []
        claim_support: list[dict] = []
        gap_sections: list[str] = []
        auto_rec_count = 0
        no_support_count = 0
        total_rec_chunks = 0
        sec_rec_samples: list[dict] = []
        claim_rec_samples: list[dict] = []
        rerank_stats_out: dict = {}

        if blueprint:
            retrieval_index = self.build_visual_retrieval_index(chunks) if auto_recommend else None
            section_support = self.map_visuals_to_sections(
                blueprint, chunk_index,
                retrieval_index=retrieval_index,
                section_top_k=section_top_k,
            )
            # Build auto_pools: section_id → [{chunk_id, visual_argument_type}, ...]
            auto_pools: dict[str, list[dict]] | None = None
            retrieval_index_by_id: dict[str, dict] | None = None
            if auto_recommend and retrieval_index is not None:
                auto_pools = {}
                for s in section_support:
                    recs = s.get("recommended_visual_chunks") or []
                    if recs:
                        auto_pools[s["section_id"]] = recs
                retrieval_index_by_id = {e["chunk_id"]: e for e in retrieval_index}
            claim_support = self.map_visuals_to_claims(
                blueprint, chunk_index, claim_top_k=claim_top_k,
                auto_pools=auto_pools,
                retrieval_index_by_id=retrieval_index_by_id,
            )
            if rerank and blueprint:
                from optomind_research.visual_evidence_reranker import VisualEvidenceReranker
                reranker = VisualEvidenceReranker(
                    real_llm=rerank_real_llm,
                    model_tier=rerank_model_tier,
                    workers=rerank_workers,
                    cache_path=rerank_cache,
                )
                claim_support = reranker.rerank_claim_support(
                    claim_support, blueprint, chunk_index, retrieval_index_by_id,
                    max_items=rerank_max_items,
                    load_bearing_only=rerank_load_bearing_only,
                )
                rerank_stats_out = reranker.compute_rerank_stats(claim_support)
                post_rerank = summarize_post_rerank_claim_support(claim_support)
                rerank_stats_out.update(post_rerank)
                # Once visual content has actually been inspected, these
                # evidence-aware gaps supersede the pre-rerank "candidate
                # exists" status. Budget-skipped claims remain unknown.
                gap_sections = list(post_rerank["visual_gap_section_ids"])
                no_support_count = len(gap_sections)
                if post_rerank["not_evaluated_claims"]:
                    warnings.append(
                        f"{post_rerank['not_evaluated_claims']} claim(s) were not reranked "
                        "because the evaluation budget was exhausted; their visual status is unknown."
                    )
            for s in section_support:
                if s.get("source") in {"auto_recommended_from_kb", "provided_and_augmented_from_kb"}:
                    auto_rec_count += 1
                    recs = s.get("recommended_visual_chunks") or []
                    total_rec_chunks += len(recs)
                    if recs and len(sec_rec_samples) < 3:
                        sec_rec_samples.append({
                            "section_id": s["section_id"],
                            "title": s.get("title", "")[:60],
                            "top_recommendation": recs[0] if recs else {},
                        })
                if not rerank and s["visual_support_status"] == "no_visual_support":
                    no_support_count += 1
            if not rerank:
                gap_sections = [
                    s["section_id"]
                    for s in section_support
                    if s["visual_support_status"] == "no_visual_support"
                ]
            for cr in claim_support:
                recs = cr.get("candidate_visual_recommendations") or []
                if recs and len(claim_rec_samples) < 3:
                    claim_rec_samples.append({
                        "claim_id": cr["claim_id"],
                        "section_id": cr["section_id"],
                        "top_recommendation": recs[0] if recs else {},
                    })

        sample_records = [
            {
                "chunk_id": c.get("chunk_id", ""),
                "doi": c.get("doi", ""),
                "title": (c.get("title") or "")[:80],
                "visual_argument_type": c.get("visual_argument_type", ""),
                "visual_argument_status": c.get("visual_argument_status", ""),
                "visual_argument_confidence": c.get("visual_argument_confidence", ""),
            }
            for c in ok_chunks[:5]
        ]

        return VisualArgumentAlignmentResult(
            total_visual_chunks=len(chunks),
            ok_visual_chunks=total_ok,
            invalid_visual_chunks=len(invalid_type),
            missing_type_count=len(missing_type),
            failed_chunks=len(failed),
            type_distribution=type_dist,
            dominant_type=dom_type,
            dominant_type_ratio=dom_ratio,
            warnings=warnings,
            section_visual_support=section_support,
            claim_visual_support=claim_support,
            visual_gap_sections=gap_sections,
            sample_records=sample_records,
            auto_recommended_sections_count=auto_rec_count,
            sections_without_visual_support_count=no_support_count,
            total_recommended_visual_chunks=total_rec_chunks,
            section_recommendation_samples=sec_rec_samples,
            claim_recommendation_samples=claim_rec_samples,
            rerank_stats=rerank_stats_out,
        )

    # ------------------------------------------------------------------
    # Report writer
    # ------------------------------------------------------------------

    def write_report(
        self,
        result: VisualArgumentAlignmentResult,
        output_dir: Path,
    ) -> tuple[Path, Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        report_path = output_dir / "visual_alignment_report.json"
        report_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Markdown summary
        total = result.ok_visual_chunks or 1
        lines = [
            "# Visual Argument Alignment Report\n",
            f"Schema version: `{result.schema_version}`\n",
            "## Summary\n",
            f"- Total visual chunks: {result.total_visual_chunks}",
            f"- OK (valid type): {result.ok_visual_chunks}",
            f"- Failed / unknown: {result.failed_chunks}",
            f"- Invalid type: {result.invalid_visual_chunks}",
            f"- Missing type: {result.missing_type_count}",
            "\n## Type Distribution\n",
        ]
        for vtype, count in sorted(result.type_distribution.items(), key=lambda x: -x[1]):
            lines.append(f"- `{vtype}`: {count} ({count / total:.1%})")
        if result.dominant_type_ratio > DOMINANT_TYPE_WARNING_THRESHOLD:
            lines.append(
                f"\n> **WARNING** Dominant type `{result.dominant_type}` = "
                f"{result.dominant_type_ratio:.1%}"
            )
        if result.warnings:
            lines.append("\n## Warnings\n")
            for w in result.warnings:
                lines.append(f"- {w}")
        if result.visual_gap_sections:
            lines.append("\n## Visual Gap Sections\n")
            for s in result.visual_gap_sections:
                lines.append(f"- `{s}`")
        if result.rerank_stats:
            rs = result.rerank_stats
            lines.append("\n## Rerank Quality Stats\n")
            lines.append(f"- Vision success (image inspected): {rs.get('vision_success_count', 0)}")
            lines.append(f"- Fallback (text-only): {rs.get('fallback_count', 0)}")
            lines.append(f"- LLM failures (rejected): {rs.get('failed_count', 0)}")
            lines.append(f"- Needs human review: {rs.get('human_review_count', 0)}")
            lines.append(f"- Claims with direct visual support: {rs.get('direct_support_claims', 0)}")
            lines.append(f"- Claims with provisional visual support: {rs.get('provisional_support_claims', 0)}")
            lines.append(f"- Claims with background-only candidates: {rs.get('background_only_claims', 0)}")
            lines.append(f"- Claims not evaluated due to budget: {rs.get('not_evaluated_claims', 0)}")
            ssd = rs.get("support_strength_distribution") or {}
            if ssd:
                lines.append("\n### Support Strength Distribution\n")
                for strength, cnt in sorted(ssd.items(), key=lambda x: -x[1]):
                    lines.append(f"- `{strength}`: {cnt}")
            emd = rs.get("evidence_mode_distribution") or {}
            if emd:
                lines.append("\n### Evidence Mode Distribution\n")
                for mode, cnt in sorted(emd.items(), key=lambda x: -x[1]):
                    lines.append(f"- `{mode}`: {cnt}")
            dd = rs.get("directness_distribution") or {}
            if dd:
                lines.append("\n### Directness Distribution\n")
                for directness, cnt in sorted(dd.items(), key=lambda x: -x[1]):
                    lines.append(f"- `{directness}`: {cnt}")

        summary_path = output_dir / "visual_alignment_summary.md"
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        return report_path, summary_path
