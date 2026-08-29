"""Visual-aware ConceptMapBuilder for OptoMind review planning.

This module builds a reusable, multi-view concept map from a
ReviewKnowledgeBase.  It is intentionally deterministic by default: the goal is
to create a stable structural scaffold before asking high-tier LLMs to propose
or refine review blueprints.

Key design choices:
- Multiple views, not one universal taxonomy.
- Every useful node should expose both text evidence and visual evidence when
  available.
- Overview/blueprint planning can use this map; final writing still must bind
  claims back to text chunks, captions, figures, and original source paths.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .review_knowledge_base import fts_query, safe_slug


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_DIR = PROJECT_ROOT / "outputs" / "review_knowledge_base" / "core58-rkb-v1-20260703"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "concept_maps" / "core58-visual-aware-v1-20260703"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def compact(value: Any, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def load_raw(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    raw = row["raw_json"] if isinstance(row, sqlite3.Row) else row.get("raw_json")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def maturity_bucket(year: Any) -> str:
    try:
        y = int(year)
    except Exception:
        return "unknown"
    if y <= 2018:
        return "foundation"
    if y <= 2023:
        return "development"
    return "frontier"


def score_visual_utility(value: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(str(value or "").lower(), 0)


@dataclass
class ConceptMapInputs:
    kb_dir: Path = DEFAULT_KB_DIR
    query_plan_path: Path | None = None

    @property
    def sqlite_path(self) -> Path:
        return self.kb_dir / "review_knowledge_base.sqlite"


@dataclass
class ConceptMapResult:
    output_dir: Path
    concept_map_path: Path
    validation_path: Path
    markdown_path: Path
    counts: dict[str, int]
    passed: bool


class VisualAwareConceptMapBuilder:
    def __init__(self, inputs: ConceptMapInputs, output_dir: Path, *, max_nodes_per_view: int = 12) -> None:
        self.inputs = inputs
        self.output_dir = output_dir
        self.max_nodes_per_view = max_nodes_per_view
        self.conn: sqlite3.Connection | None = None
        self.topic_context = self._load_topic_context(inputs.query_plan_path)
        self.topic_tokens = self._topic_tokens(self.topic_context)

    @staticmethod
    def _load_topic_context(path: Path | None) -> str:
        if path is None or not Path(path).is_file():
            return ""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return ""

    @staticmethod
    def _topic_tokens(value: str) -> set[str]:
        stop = {
            "about", "including", "review", "research", "question", "existing",
            "methods", "method", "design", "optical", "paper", "papers", "work",
            "works", "study", "studies", "system", "systems", "using", "used",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", str(value or "").casefold())
            if token not in stop
        }

    def _topic_overlap(self, label: str) -> int:
        if not self.topic_tokens:
            return 0
        tokens = self._topic_tokens(label)
        return len(tokens.intersection(self.topic_tokens))

    def build(self) -> ConceptMapResult:
        if not self.inputs.sqlite_path.exists():
            raise FileNotFoundError(f"ReviewKnowledgeBase SQLite not found: {self.inputs.sqlite_path}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.inputs.sqlite_path))
        self.conn.row_factory = sqlite3.Row
        concept_map = {
            "schema_version": "visual_aware_concept_map.v1",
            "created_at": utc_now(),
            "source_kb_dir": str(self.inputs.kb_dir),
            "source_sqlite": str(self.inputs.sqlite_path),
            "source_query_plan": str(self.inputs.query_plan_path) if self.inputs.query_plan_path else "",
            "design_principles": [
                "Use multiple complementary views rather than one flat taxonomy.",
                "Treat visual chunks as first-class planning assets, not decoration.",
                "Use the map for planning; bind final claims back to source text and image records.",
                "Keep review-example integration optional for a later Blueprint stage.",
            ],
            "views": [],
            "cross_view_signals": {},
        }

        views = [
            self._concept_view(
                "mechanism_view",
                "Mechanism view",
                "Mechanistic routes and physical explanations that can organize review arguments.",
                ["mechanism"],
                min_source_count=1,
                label_filter=self._mechanism_filter,
                min_topic_directness=2,
                min_topic_overlap=1,
            ),
            self._concept_view(
                "material_structure_view",
                "Material and structure view",
                "Material platforms, device structures, and film architectures.",
                ["material_or_structure"],
                min_source_count=1,
                label_filter=self._material_filter,
                min_topic_directness=2,
            ),
            self._concept_view(
                "spectral_metric_view",
                "Spectral and metric view",
                "Wavelength bands, optical quantities, thermal metrics, and performance axes.",
                ["wavelength_range", "metric_or_axis", "physical_quantity"],
                min_source_count=1,
                label_filter=self._metric_filter,
                min_topic_directness=2,
            ),
            self._concept_view(
                "method_instrument_view",
                "Method and characterization view",
                "Fabrication, simulation, characterization, and measurement methods.",
                ["method_or_instrument"],
                min_source_count=1,
                label_filter=self._method_filter,
                min_topic_directness=1,
            ),
            self._keyword_view(
                "application_context_view",
                "Application context view",
                "Application spaces that need different review narratives and evidence types.",
                self._retrieval_feature_specs("application"),
            ),
            self._concept_view(
                "bottleneck_view",
                "Bottleneck and trade-off view",
                "Deployment constraints, trade-offs, and unresolved questions.",
                ["limitation_or_gap"],
                min_source_count=1,
                label_filter=self._gap_filter,
                min_topic_directness=2,
                min_topic_overlap=1,
            ),
            self._visual_argument_view(),
            self._maturity_timeline_view(),
        ]
        concept_map["views"] = views
        concept_map["planning_advisories"] = [
            "application_context_view_has_no_grounded_nodes; preserve the main review scope and trigger targeted retrieval before making application claims"
        ] if not next((view.get("nodes") for view in views if view.get("view_id") == "application_context_view"), []) else []
        concept_map["cross_view_signals"] = self._cross_view_signals(views)
        validation = self._validate(concept_map)

        concept_map_path = self.output_dir / "concept_map.visual_aware.v1.json"
        validation_path = self.output_dir / "concept_map.validation.json"
        markdown_path = self.output_dir / "concept_map.summary.md"
        write_json(concept_map_path, concept_map)
        write_json(validation_path, validation)
        write_text(markdown_path, self._markdown(concept_map, validation))
        self.conn.close()
        self.conn = None
        counts = {
            "views": len(concept_map["views"]),
            "nodes": sum(len(v.get("nodes", [])) for v in concept_map["views"]),
            "edges": sum(len(v.get("edges", [])) for v in concept_map["views"]),
            "visual_evidence_items": sum(
                len(n.get("representative_visual_chunks", []))
                for v in concept_map["views"]
                for n in v.get("nodes", [])
            ),
        }
        return ConceptMapResult(
            output_dir=self.output_dir,
            concept_map_path=concept_map_path,
            validation_path=validation_path,
            markdown_path=markdown_path,
            counts=counts,
            passed=bool(validation.get("passed")),
        )

    @property
    def db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not open")
        return self.conn

    def _concept_view(
        self,
        view_id: str,
        name: str,
        purpose: str,
        concept_kinds: list[str],
        *,
        min_source_count: int,
        label_filter: Callable[[str], bool],
        min_topic_directness: int = 0,
        min_topic_overlap: int = 0,
    ) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in concept_kinds)
        rows = self.db.execute(
            f"""
            SELECT c.concept_id, c.kind, c.label, c.source_count,
                   MAX(COALESCE(p.topic_directness_score, 0)) AS max_topic_directness,
                   COUNT(DISTINCT CASE WHEN COALESCE(p.topic_directness_score, 0) >= 4 THEN p.paper_id END) AS direct_paper_count
            FROM concepts c
            LEFT JOIN concept_mentions m ON m.concept_id=c.concept_id
            LEFT JOIN papers p ON p.paper_id=m.paper_id
            WHERE c.kind IN ({placeholders}) AND c.source_count >= ?
            GROUP BY c.concept_id, c.kind, c.label, c.source_count
            HAVING MAX(COALESCE(p.topic_directness_score, 0)) >= ?
            ORDER BY max_topic_directness DESC, direct_paper_count DESC, c.source_count DESC, c.label ASC
            LIMIT 200
            """,
            (*concept_kinds, min_source_count, min_topic_directness),
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                self._topic_overlap(str(row["label"] or "")),
                int(row["max_topic_directness"] or 0),
                int(row["direct_paper_count"] or 0),
                int(row["source_count"] or 0),
            ),
            reverse=True,
        )
        nodes: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        for row in rows:
            label = str(row["label"] or "").strip()
            if not label or not label_filter(label):
                continue
            if self.topic_tokens and self._topic_overlap(label) < min_topic_overlap:
                continue
            key = self._canonical_label(label)
            if key in seen_labels:
                continue
            seen_labels.add(key)
            node = self._node_from_concept(view_id, row)
            node["topic_overlap_score"] = self._topic_overlap(label)
            if node["evidence_counts"]["paper_count"] == 0:
                continue
            nodes.append(node)
            if len(nodes) >= self.max_nodes_per_view:
                break
        return self._finalize_view(view_id, name, purpose, nodes)

    def _keyword_view(self, view_id: str, name: str, purpose: str, specs: list[dict[str, str]]) -> dict[str, Any]:
        nodes = []
        for spec in specs[: self.max_nodes_per_view]:
            nodes.append(self._node_from_query(view_id, spec["label"], spec["query"], spec.get("purpose", "")))
        nodes = [n for n in nodes if n["evidence_counts"]["paper_count"] > 0]
        return self._finalize_view(view_id, name, purpose, nodes)

    def _visual_argument_view(self) -> dict[str, Any]:
        rows = self.db.execute(
            """
            SELECT visual_role, COUNT(*) AS n
            FROM visual_chunks
            WHERE local_image_path IS NOT NULL AND local_image_path != ''
            GROUP BY visual_role
            ORDER BY n DESC
            LIMIT 16
            """
        ).fetchall()
        nodes = []
        for row in rows:
            role = str(row["visual_role"] or "unclear")
            if role in {"unclear", ""}:
                continue
            query = role.replace("_", " ")
            purpose = self._visual_role_purpose(role)
            node = self._node_from_visual_role("visual_argument_view", role, query, purpose)
            if node["evidence_counts"]["visual_chunk_count"] > 0:
                nodes.append(node)
            if len(nodes) >= self.max_nodes_per_view:
                break
        return self._finalize_view(
            "visual_argument_view",
            "Visual argument view",
            "How figures, subfigures, spectra, schematics, and benchmark plots can serve review arguments.",
            nodes,
        )

    def _maturity_timeline_view(self) -> dict[str, Any]:
        specs = [
            {
                "bucket": "foundation",
                "label": "Foundation layer",
                "purpose": "Earlier work and review anchors that establish core physical principles and field vocabulary.",
            },
            {
                "bucket": "development",
                "label": "Development layer",
                "purpose": "Intermediate-stage route expansion, material diversification, and performance benchmarking.",
            },
            {
                "bucket": "frontier",
                "label": "Frontier layer",
                "purpose": "Recent directions, emerging hybrid systems, and application-driven refinements.",
            },
        ]
        nodes = []
        for spec in specs:
            rows = self.db.execute(
                """
                SELECT paper_id, doi, title, year, venue, quality_tier, query_relevance
                FROM papers
                WHERE year IS NOT NULL
                ORDER BY year ASC
                """
            ).fetchall()
            paper_ids = [r["paper_id"] for r in rows if maturity_bucket(r["year"]) == spec["bucket"]]
            nodes.append(self._node_from_paper_set("maturity_timeline_view", spec["label"], spec["purpose"], paper_ids))
        return self._finalize_view(
            "maturity_timeline_view",
            "Foundation / Development / Frontier timeline",
            "Temporal and maturity-axis view for historical-to-frontier blueprint organization.",
            [n for n in nodes if n["evidence_counts"]["paper_count"] > 0],
        )

    def _node_from_concept(self, view_id: str, row: sqlite3.Row) -> dict[str, Any]:
        label = str(row["label"])
        mentions = self.db.execute(
            """
            SELECT source_type, source_id, paper_id, relation, confidence
            FROM concept_mentions
            WHERE concept_id=?
            ORDER BY confidence DESC
            LIMIT 300
            """,
            (row["concept_id"],),
        ).fetchall()
        paper_ids = {m["paper_id"] for m in mentions if m["paper_id"]}
        text_ids = [m["source_id"] for m in mentions if m["source_type"] == "text_chunk"]
        visual_ids = [m["source_id"] for m in mentions if m["source_type"] == "visual_chunk"]
        node = self._base_node(
            view_id=view_id,
            label=label,
            node_type=str(row["kind"]),
            query=label,
            purpose=f"Concept node derived from {row['kind']} mentions.",
            paper_ids=paper_ids,
            text_ids=text_ids,
            visual_ids=visual_ids,
        )
        if len(node["representative_text_chunks"]) < 3 or len(node["representative_visual_chunks"]) < 2:
            self._supplement_node_by_query(node, label)
        return node

    def _node_from_query(self, view_id: str, label: str, query: str, purpose: str) -> dict[str, Any]:
        text_rows = self._search_text_chunks(query, 12)
        visual_rows = self._search_visual_chunks(query, 12)
        paper_rows = self._search_papers(query, 12)
        paper_ids = {r["paper_id"] for r in text_rows + visual_rows + paper_rows if r["paper_id"]}
        node = self._base_node(
            view_id=view_id,
            label=label,
            node_type="keyword_cluster",
            query=query,
            purpose=purpose,
            paper_ids=paper_ids,
            text_ids=[r["chunk_id"] for r in text_rows],
            visual_ids=[r["chunk_id"] for r in visual_rows],
        )
        node["topic_overlap_score"] = self._topic_overlap(f"{label} {query}")
        return node

    def _node_from_visual_role(self, view_id: str, label: str, query: str, purpose: str) -> dict[str, Any]:
        rows = self.db.execute(
            """
            SELECT chunk_id, paper_id
            FROM visual_chunks
            WHERE visual_role=?
            ORDER BY
              CASE review_utility WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
              chunk_id ASC
            LIMIT 60
            """,
            (label,),
        ).fetchall()
        paper_ids = {r["paper_id"] for r in rows if r["paper_id"]}
        visual_ids = [r["chunk_id"] for r in rows]
        node = self._base_node(
            view_id=view_id,
            label=label,
            node_type="visual_role",
            query=query,
            purpose=purpose,
            paper_ids=paper_ids,
            text_ids=[],
            visual_ids=visual_ids,
        )
        if len(node["representative_text_chunks"]) < 2:
            self._supplement_node_by_query(node, query)
        return node

    def _node_from_paper_set(self, view_id: str, label: str, purpose: str, paper_ids: list[str]) -> dict[str, Any]:
        text_ids = [
            r["chunk_id"]
            for r in self.db.execute(
                f"""
                SELECT chunk_id
                FROM text_chunks
                WHERE paper_id IN ({",".join("?" for _ in paper_ids[:80])})
                ORDER BY paper_id, ordinal
                LIMIT 24
                """,
                tuple(paper_ids[:80]),
            ).fetchall()
        ] if paper_ids else []
        visual_ids = [
            r["chunk_id"]
            for r in self.db.execute(
                f"""
                SELECT chunk_id
                FROM visual_chunks
                WHERE paper_id IN ({",".join("?" for _ in paper_ids[:80])})
                ORDER BY
                  CASE review_utility WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
                  chunk_id
                LIMIT 24
                """,
                tuple(paper_ids[:80]),
            ).fetchall()
        ] if paper_ids else []
        return self._base_node(
            view_id=view_id,
            label=label,
            node_type="maturity_layer",
            query=label,
            purpose=purpose,
            paper_ids=set(paper_ids),
            text_ids=text_ids,
            visual_ids=visual_ids,
        )

    def _base_node(
        self,
        *,
        view_id: str,
        label: str,
        node_type: str,
        query: str,
        purpose: str,
        paper_ids: set[str],
        text_ids: list[str],
        visual_ids: list[str],
    ) -> dict[str, Any]:
        paper_ids = set(x for x in paper_ids if x)
        text_rows = self._fetch_text_chunks(text_ids, 8)
        visual_rows = self._fetch_visual_chunks(visual_ids, 8)
        for row in text_rows + visual_rows:
            if row.get("paper_id"):
                paper_ids.add(row["paper_id"])
        paper_rows = self._fetch_papers(sorted(paper_ids), 8)
        maturity = Counter(maturity_bucket(r.get("year")) for r in paper_rows)
        high_visual = [v for v in visual_rows if str(v.get("review_utility", "")).lower() == "high"]
        return {
            "node_id": f"{view_id}:{safe_slug(label, limit=80).lower()}",
            "label": label,
            "node_type": node_type,
            "purpose": purpose,
            "retrieval_query": query,
            "planning_value": self._planning_value(view_id, label, node_type),
            "blueprint_roles": self._blueprint_roles(view_id, label, node_type),
            "evidence_counts": {
                "paper_count": len(paper_ids),
                "text_chunk_count": len(set(text_ids)),
                "visual_chunk_count": len(set(visual_ids)),
                "high_utility_visual_count": len(high_visual),
            },
            "maturity_distribution": dict(maturity),
            "representative_papers": paper_rows,
            "representative_text_chunks": text_rows,
            "representative_visual_chunks": visual_rows,
            "visual_use_suggestions": self._visual_use_suggestions(visual_rows),
            "audit": {
                "has_text_evidence": bool(text_rows),
                "has_visual_evidence": bool(visual_rows),
                "needs_human_review": len(visual_rows) == 0 and view_id in {"visual_argument_view", "application_context_view"},
            },
        }

    def _supplement_node_by_query(self, node: dict[str, Any], query: str) -> None:
        existing_text = {x["chunk_id"] for x in node.get("representative_text_chunks", [])}
        existing_visual = {x["chunk_id"] for x in node.get("representative_visual_chunks", [])}
        add_text = [r for r in self._search_text_chunks(query, 8) if r["chunk_id"] not in existing_text]
        add_visual = [r for r in self._search_visual_chunks(query, 8) if r["chunk_id"] not in existing_visual]
        node["representative_text_chunks"] = (node.get("representative_text_chunks", []) + add_text)[:8]
        node["representative_visual_chunks"] = (node.get("representative_visual_chunks", []) + add_visual)[:8]
        paper_ids = {p["paper_id"] for p in node.get("representative_papers", []) if p.get("paper_id")}
        for row in add_text + add_visual:
            if row.get("paper_id"):
                paper_ids.add(row["paper_id"])
        node["representative_papers"] = self._fetch_papers(sorted(paper_ids), 8)
        node["evidence_counts"]["paper_count"] = max(node["evidence_counts"]["paper_count"], len(paper_ids))
        node["evidence_counts"]["text_chunk_count"] = max(node["evidence_counts"]["text_chunk_count"], len(node["representative_text_chunks"]))
        node["evidence_counts"]["visual_chunk_count"] = max(node["evidence_counts"]["visual_chunk_count"], len(node["representative_visual_chunks"]))
        node["evidence_counts"]["high_utility_visual_count"] = sum(
            1 for v in node["representative_visual_chunks"] if str(v.get("review_utility", "")).lower() == "high"
        )
        node["audit"]["has_text_evidence"] = bool(node["representative_text_chunks"])
        node["audit"]["has_visual_evidence"] = bool(node["representative_visual_chunks"])

    def _fetch_papers(self, paper_ids: list[str], limit: int) -> list[dict[str, Any]]:
        if not paper_ids:
            return []
        rows = self.db.execute(
            f"""
            SELECT paper_id, doi, title, year, venue, quality_tier, query_relevance,
                   topic_relevance_class, topic_directness_score, downstream_use_policy
            FROM papers
            WHERE paper_id IN ({",".join("?" for _ in paper_ids)})
            ORDER BY COALESCE(topic_directness_score, 0) DESC, year DESC, title ASC
            LIMIT ?
            """,
            (*paper_ids, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_text_chunks(self, chunk_ids: list[str], limit: int) -> list[dict[str, Any]]:
        ids = [x for x in dict.fromkeys(chunk_ids) if x][:limit * 3]
        if not ids:
            return []
        rows = self.db.execute(
            f"""
            SELECT chunk_id, paper_id, doi, title, ordinal, section_path, char_start, char_end,
                   substr(text, 1, 650) AS text_preview
            FROM text_chunks
            WHERE chunk_id IN ({",".join("?" for _ in ids)})
            ORDER BY paper_id, ordinal
            LIMIT ?
            """,
            (*ids, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_visual_chunks(self, chunk_ids: list[str], limit: int) -> list[dict[str, Any]]:
        ids = [x for x in dict.fromkeys(chunk_ids) if x][:limit * 4]
        if not ids:
            return []
        rows = self.db.execute(
            f"""
            SELECT chunk_id, paper_id, doi, title, chunk_kind, parent_label, subfigure_label,
                   visual_role, review_utility, local_image_path, substr(caption, 1, 650) AS caption_preview,
                   raw_json
            FROM visual_chunks
            WHERE chunk_id IN ({",".join("?" for _ in ids)})
            ORDER BY
              CASE review_utility WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
              paper_id, parent_label, subfigure_label
            LIMIT ?
            """,
            (*ids, limit),
        ).fetchall()
        return [self._visual_row(dict(r)) for r in rows]

    def _search_papers(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = fts_query(query)
        if not q:
            return []
        try:
            rows = self.db.execute(
                """
                SELECT p.paper_id,p.doi,p.title,p.year,p.venue,p.quality_tier,p.query_relevance,
                       p.topic_relevance_class,p.topic_directness_score,p.downstream_use_policy
                FROM paper_fts JOIN papers p ON paper_fts.paper_id=p.paper_id
                WHERE paper_fts MATCH ?
                ORDER BY COALESCE(p.topic_directness_score, 0) DESC, bm25(paper_fts)
                LIMIT ?
                """,
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def _search_text_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = fts_query(query)
        if not q:
            return []
        try:
            rows = self.db.execute(
                """
                SELECT c.chunk_id,c.paper_id,c.doi,c.title,c.ordinal,c.section_path,c.char_start,c.char_end,
                       substr(c.text, 1, 650) AS text_preview
                FROM text_chunk_fts JOIN text_chunks c ON text_chunk_fts.chunk_id=c.chunk_id
                JOIN papers p ON p.paper_id=c.paper_id
                WHERE text_chunk_fts MATCH ?
                ORDER BY COALESCE(p.topic_directness_score, 0) DESC, bm25(text_chunk_fts)
                LIMIT ?
                """,
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def _search_visual_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = fts_query(query)
        if not q:
            return []
        try:
            rows = self.db.execute(
                """
                SELECT v.chunk_id,v.paper_id,v.doi,v.title,v.chunk_kind,v.parent_label,v.subfigure_label,
                       v.visual_role,v.review_utility,v.local_image_path,substr(v.caption, 1, 650) AS caption_preview,
                       v.raw_json
                FROM visual_chunk_fts JOIN visual_chunks v ON visual_chunk_fts.chunk_id=v.chunk_id
                JOIN papers p ON p.paper_id=v.paper_id
                WHERE visual_chunk_fts MATCH ?
                ORDER BY
                  COALESCE(p.topic_directness_score, 0) DESC,
                  CASE v.review_utility WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
                  bm25(visual_chunk_fts)
                LIMIT ?
                """,
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._visual_row(dict(r)) for r in rows]

    def _visual_row(self, row: dict[str, Any]) -> dict[str, Any]:
        raw = load_raw(row)
        profile = raw.get("visual_profile") if isinstance(raw.get("visual_profile"), dict) else {}
        intr = profile.get("intrinsic_visual_labels") if isinstance(profile.get("intrinsic_visual_labels"), dict) else {}
        task = profile.get("review_task_labels") if isinstance(profile.get("review_task_labels"), dict) else {}
        card = profile.get("visual_card") if isinstance(profile.get("visual_card"), dict) else {}
        qa = profile.get("qa") if isinstance(profile.get("qa"), dict) else {}
        row = dict(row)
        row.pop("raw_json", None)
        row["direct_use_candidate"] = task.get("direct_use_candidate", "")
        row["redraw_recommendation"] = task.get("redraw_recommendation", "")
        row["quality_flags"] = intr.get("quality_flags") if isinstance(intr.get("quality_flags"), list) else []
        row["caption_alignment"] = intr.get("caption_alignment", "")
        row["visual_card_summary"] = card.get("one_sentence_summary", "")
        row["best_use_in_review"] = card.get("best_use_in_review", "")
        row["needs_human_review"] = bool(qa.get("needs_human_review", False))
        return row

    def _finalize_view(self, view_id: str, name: str, purpose: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
        nodes = sorted(
            nodes,
            key=lambda n: (
                -int(n.get("topic_overlap_score") or 0),
                -n["evidence_counts"].get("paper_count", 0),
                -n["evidence_counts"].get("visual_chunk_count", 0),
                str(n.get("label", "")).lower(),
            ),
        )[: self.max_nodes_per_view]
        edges = self._build_edges(nodes)
        return {
            "view_id": view_id,
            "name": name,
            "purpose": purpose,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": nodes,
            "edges": edges,
        }

    def _build_edges(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        edges = []
        for i, left in enumerate(nodes):
            left_papers = {p["paper_id"] for p in left.get("representative_papers", []) if p.get("paper_id")}
            for right in nodes[i + 1 :]:
                right_papers = {p["paper_id"] for p in right.get("representative_papers", []) if p.get("paper_id")}
                shared = sorted(left_papers & right_papers)
                if shared:
                    edges.append(
                        {
                            "source_node_id": left["node_id"],
                            "target_node_id": right["node_id"],
                            "relation": "co_supported_by_papers",
                            "weight": len(shared),
                            "shared_paper_ids": shared[:8],
                        }
                    )
        edges.sort(key=lambda e: (-e["weight"], e["source_node_id"], e["target_node_id"]))
        return edges[:40]

    def _cross_view_signals(self, views: list[dict[str, Any]]) -> dict[str, Any]:
        paper_counter: Counter[str] = Counter()
        visual_counter: Counter[str] = Counter()
        for view in views:
            for node in view.get("nodes", []):
                for paper in node.get("representative_papers", []):
                    paper_counter[paper.get("paper_id", "")] += 1
                for visual in node.get("representative_visual_chunks", []):
                    visual_counter[visual.get("chunk_id", "")] += 1
        top_papers = []
        for paper_id, n in paper_counter.most_common(12):
            if not paper_id:
                continue
            paper = self._fetch_papers([paper_id], 1)
            if paper:
                top_papers.append({"paper_id": paper_id, "view_node_mentions": n, **paper[0]})
        top_visuals = []
        for chunk_id, n in visual_counter.most_common(12):
            visual = self._fetch_visual_chunks([chunk_id], 1)
            if visual:
                top_visuals.append({"chunk_id": chunk_id, "view_node_mentions": n, **visual[0]})
        return {
            "cross_view_hub_papers": top_papers,
            "cross_view_hub_visual_chunks": top_visuals,
            "interpretation": "Hub papers and visual chunks are useful candidates for review-level figures, overview tables, and section anchors.",
        }

    def _validate(self, concept_map: dict[str, Any]) -> dict[str, Any]:
        views = concept_map.get("views", [])
        nodes = [n for v in views for n in v.get("nodes", [])]
        nodes_with_text = [n for n in nodes if n.get("representative_text_chunks")]
        nodes_with_visual = [n for n in nodes if n.get("representative_visual_chunks")]
        missing_images = []
        high_visual_nodes = []
        for node in nodes:
            if node["evidence_counts"].get("high_utility_visual_count", 0) >= 2:
                high_visual_nodes.append(node["node_id"])
            for visual in node.get("representative_visual_chunks", []):
                path = visual.get("local_image_path")
                if path and not Path(path).exists():
                    missing_images.append({"node_id": node["node_id"], "chunk_id": visual.get("chunk_id"), "path": path})
        view_ids = {v.get("view_id") for v in views}
        node_counts_by_view = {str(v.get("view_id") or ""): len(v.get("nodes", [])) for v in views}
        checks = {
            "has_required_views": {"visual_argument_view", "maturity_timeline_view", "mechanism_view", "application_context_view"}.issubset(view_ids),
            "enough_views": len(views) >= 8,
            "substantive_core_views": all(
                node_counts_by_view.get(view_id, 0) >= minimum
                for view_id, minimum in {
                    "mechanism_view": 3,
                    "material_structure_view": 3,
                    "spectral_metric_view": 3,
                    "method_instrument_view": 3,
                    "bottleneck_view": 3,
                    "visual_argument_view": 3,
                    "maturity_timeline_view": 2,
                }.items()
            ),
            "text_coverage_ok": len(nodes_with_text) / max(len(nodes), 1) >= 0.80,
            "visual_coverage_ok": len(nodes_with_visual) / max(len(nodes), 1) >= 0.65,
            "has_high_utility_visual_nodes": len(high_visual_nodes) >= 10,
            "all_representative_images_exist": not missing_images,
        }
        return {
            "schema_version": "visual_aware_concept_map_validation.v1",
            "created_at": utc_now(),
            "passed": all(checks.values()),
            "checks": checks,
            "counts": {
                "views": len(views),
                "nodes": len(nodes),
                "nodes_with_text": len(nodes_with_text),
                "nodes_with_visual": len(nodes_with_visual),
                "high_utility_visual_nodes": len(high_visual_nodes),
                "missing_representative_images": len(missing_images),
                "nodes_by_view": node_counts_by_view,
            },
            "coverage_advisories": [
                "application_context_view_has_no_grounded_nodes; infer cautiously from the user scope or trigger targeted retrieval"
            ] if node_counts_by_view.get("application_context_view", 0) == 0 else [],
            "samples": {
                "high_utility_visual_nodes": high_visual_nodes[:12],
                "missing_representative_images": missing_images[:10],
            },
        }

    def _markdown(self, concept_map: dict[str, Any], validation: dict[str, Any]) -> str:
        lines = [
            "# Visual-aware ConceptMapBuilder v1 Summary",
            "",
            f"- Source KB: `{concept_map.get('source_kb_dir')}`",
            f"- Passed validation: `{validation.get('passed')}`",
            f"- Views: {validation.get('counts', {}).get('views')}",
            f"- Nodes: {validation.get('counts', {}).get('nodes')}",
            f"- Nodes with text evidence: {validation.get('counts', {}).get('nodes_with_text')}",
            f"- Nodes with visual evidence: {validation.get('counts', {}).get('nodes_with_visual')}",
            "",
            "## Views",
            "",
        ]
        for view in concept_map.get("views", []):
            lines.extend([f"### {view.get('name')}", "", str(view.get("purpose") or ""), ""])
            for node in view.get("nodes", [])[:5]:
                counts = node.get("evidence_counts", {})
                lines.append(
                    f"- `{node.get('label')}`: papers={counts.get('paper_count')}, text={len(node.get('representative_text_chunks', []))}, visual={len(node.get('representative_visual_chunks', []))}"
                )
            lines.append("")
        return "\n".join(lines)

    def _planning_value(self, view_id: str, label: str, node_type: str) -> str:
        if view_id == "visual_argument_view":
            return "Use this node to decide what kind of visual evidence can carry a section argument."
        if view_id == "maturity_timeline_view":
            return "Use this node to organize historical evolution and current frontier positioning."
        if view_id == "bottleneck_view":
            return "Use this node to frame critical limitations, unresolved constraints, and future work."
        if view_id == "application_context_view":
            return "Use this node to adapt review structure to application-specific constraints."
        return "Use this node as a review-planning concept cluster with traceable text and visual evidence."

    def _blueprint_roles(self, view_id: str, label: str, node_type: str) -> list[str]:
        mapping = {
            "mechanism_view": ["explain_physics", "build_mechanism_section"],
            "material_structure_view": ["compare_routes", "organize_material_platforms"],
            "spectral_metric_view": ["define_metrics", "build_tradeoff_argument"],
            "method_instrument_view": ["describe_evaluation_methods", "support_benchmarking"],
            "application_context_view": ["shape_application_sections", "identify_context_constraints"],
            "bottleneck_view": ["critical_discussion", "future_opportunities"],
            "visual_argument_view": ["figure_planning", "visual_evidence_selection"],
            "maturity_timeline_view": ["historical_evolution", "frontier_positioning"],
        }
        return mapping.get(view_id, ["review_planning"])

    def _visual_use_suggestions(self, visual_rows: list[dict[str, Any]]) -> list[str]:
        roles = Counter(str(v.get("visual_role") or "unclear") for v in visual_rows)
        suggestions = []
        if roles.get("schematic") or roles.get("device_structure"):
            suggestions.append("Use schematics/device structures to introduce routes or mechanisms.")
        if roles.get("graph") or roles.get("spectrum") or roles.get("benchmark_plot"):
            suggestions.append("Use graphs/spectra/benchmark plots to support trade-off and metric discussions.")
        if roles.get("micrograph") or roles.get("material_structure"):
            suggestions.append("Use micrographs/material-structure images to connect morphology with optical behavior.")
        if roles.get("photograph") or roles.get("experimental_setup"):
            suggestions.append("Use photographs/experimental setups for real-world deployment and validation context.")
        if not suggestions and visual_rows:
            suggestions.append("Use representative figures as visual anchors after human inspection.")
        return suggestions[:4]

    def _visual_role_purpose(self, role: str) -> str:
        return {
            "graph": "Quantitative plots for metrics, trade-offs, and performance comparisons.",
            "schematic": "Conceptual diagrams for mechanisms, device structures, and route explanation.",
            "mixed": "Multi-panel figures that can support integrated mechanism-result narratives.",
            "photograph": "Real samples, outdoor validation, deployment scenes, and practical context.",
            "micrograph": "Morphology and structure evidence linked to optical or thermal behavior.",
            "benchmark_plot": "Comparative performance evidence for section-level claims.",
            "spectrum": "Spectral evidence for wavelength-selective design arguments.",
            "experimental_setup": "Measurement and validation setup explanation.",
            "workflow": "Design, fabrication, or inverse-design workflow explanation.",
            "simulation_result": "Modeling and simulation evidence.",
        }.get(role, "Visual role cluster for figure planning.")

    def _retrieval_feature_specs(self, role: str) -> list[dict[str, str]]:
        """Derive topic-aware planning nodes from the current run's facets.

        Earlier versions embedded radiative-cooling examples here, which made
        an otherwise new ReviewKnowledgeBase inherit the old test topic.  The
        retrieval features are produced upstream from the current user
        question, so they are the correct reusable bridge into concept-map
        planning.
        """
        rows = self.db.execute(
            """
            SELECT label, source_count
            FROM concepts
            WHERE kind='retrieval_feature'
            ORDER BY source_count DESC, label ASC
            LIMIT 80
            """
        ).fetchall()
        keyword_map = {
            "mechanism": {
                "mechanism", "physical", "principle", "dispersion", "interaction",
                "interference", "resonance", "coupling", "scattering", "phase",
            },
            "application": {
                "application", "integration", "imaging", "deployment", "clinical",
                "communication", "sensing", "telecom", "sensor", "spectroscopy",
            },
            "bottleneck": {
                "gap", "bottleneck", "limit", "challenge", "fabrication",
                "scalability", "manufacturing", "reliability", "benchmark", "evidence",
            },
        }
        terms = keyword_map.get(role, set())
        selected: list[tuple[str, int]] = []
        fallback: list[tuple[str, int]] = []
        for row in rows:
            label = re.sub(r"^F\d+\s*:\s*", "", str(row["label"] or "")).strip()
            if not label:
                continue
            pair = (label, int(row["source_count"] or 0))
            fallback.append(pair)
            raw_tokens = set(re.findall(r"[a-z][a-z0-9-]+", label.casefold()))
            tokens = raw_tokens | {token[:-1] for token in raw_tokens if token.endswith("s") and len(token) > 4}
            if tokens.intersection(terms):
                selected.append(pair)
        # Empty is an honest coverage signal.  Padding an application or
        # mechanism view with merely high-frequency but semantically wrong
        # facets silently distorts the downstream review blueprint.
        purpose = {
            "mechanism": "Organize mechanisms, design routes, and causal explanations for the current review topic.",
            "application": "Connect the current topic to application, integration, and system-level evidence.",
            "bottleneck": "Frame limitations, missing evidence, scale-up constraints, and unresolved trade-offs.",
        }.get(role, "Organize current-topic evidence for review planning.")
        return [
            {"label": label, "query": label, "purpose": purpose, "source_count": str(count)}
            for label, count in selected[: self.max_nodes_per_view]
        ]

    def _application_specs(self) -> list[dict[str, str]]:
        return [
            {"label": "Agricultural greenhouse and crop-light management", "query": "greenhouse film photosynthesis PAR NIR crop cooling transparent", "purpose": "Connect cooling films to plant-relevant light and temperature constraints."},
            {"label": "Building envelope, windows, and skylights", "query": "building window skylight envelope transparent radiative cooling energy saving", "purpose": "Organize building-integrated transparent and opaque cooling routes."},
            {"label": "Atmospheric water harvesting and condensation", "query": "atmospheric water harvesting condensation hydrogel radiative evaporative cooling", "purpose": "Represent energy-water coupling and humidity-dependent systems."},
            {"label": "Wearable and personal thermal management", "query": "wearable textile personal thermal management radiative cooling fabric metafabric", "purpose": "Capture human-centered cooling applications and fabric platforms."},
            {"label": "Colored and aesthetic radiative cooling", "query": "colored color preserving aesthetic transmissive radiative cooling structural color", "purpose": "Track color, transparency, and aesthetics as review constraints."},
            {"label": "Urban scale and system energy modeling", "query": "urban energy modeling building energy saving cooling power radiative cooling", "purpose": "Connect material performance to system-level deployment and energy impact."},
        ]

    def _bottleneck_specs(self) -> list[dict[str, str]]:
        return [
            {"label": "Humidity, clouds, and atmospheric variability", "query": "humidity cloud water vapor weather resilient atmospheric window radiative cooling", "purpose": "Frame weather sensitivity and atmospheric boundary conditions."},
            {"label": "Transparency, PAR, and cooling trade-off", "query": "transparent PAR visible transmittance NIR rejection MIR emission trade-off", "purpose": "Frame the central spectral trade-off in transparent/agricultural films."},
            {"label": "Scalability and manufacturability", "query": "scalable fabrication roll to roll polymer coating large area radiative cooling", "purpose": "Identify manufacturing routes and scale-up limitations."},
            {"label": "Durability, soiling, aging, and outdoor stability", "query": "durability soiling aging outdoor stability weatherability radiative cooling coating", "purpose": "Capture long-term reliability constraints."},
            {"label": "Benchmarking and standardization", "query": "benchmark standard cooling power outdoor comparison measurement radiative cooling", "purpose": "Capture reproducibility and evaluation-comparison issues."},
            {"label": "Cost, life cycle, and commercialization", "query": "cost life cycle commercialization techno economics radiative cooling coating", "purpose": "Track economic and deployment barriers."},
        ]

    def _mechanism_specs(self) -> list[dict[str, str]]:
        return [
            {"label": "Direct thermal emission through the atmospheric window", "query": "8 13 micrometer atmospheric window thermal emission outer space radiative cooling", "purpose": "Explain the core heat-rejection pathway of passive radiative cooling."},
            {"label": "Solar heat rejection and parasitic absorption suppression", "query": "solar reflectance parasitic absorption solar heat gain radiative cooling", "purpose": "Frame why solar-band management is necessary for daytime cooling."},
            {"label": "Selective versus broadband thermal emission", "query": "selective emitter broadband emitter emissivity atmospheric window cooling power", "purpose": "Compare route-level choices in mid-infrared emission design."},
            {"label": "Mie scattering and porous broadband reflectors", "query": "Mie scattering porous polymer nanoparticle broadband solar reflectance radiative cooling", "purpose": "Connect morphology and scattering physics to high solar reflectance."},
            {"label": "Multilayer interference and photonic resonance", "query": "multilayer interference Fabry Perot photonic film resonance radiative cooling", "purpose": "Represent optical thin-film and photonic routes."},
            {"label": "Transparent spectral splitting for PAR or visible transmission", "query": "transparent PAR visible transmission NIR rejection MIR emission spectral splitting", "purpose": "Explain transparent/agricultural spectral trade-offs."},
            {"label": "Radiative-evaporative coupling", "query": "radiative evaporative cooling hydrogel water evaporation passive cooling", "purpose": "Capture hybrid cooling routes that combine radiation and phase change."},
            {"label": "Atmospheric water condensation and harvesting", "query": "radiative cooling condensation atmospheric water harvesting dew point", "purpose": "Link subambient surfaces to water harvesting and humidity constraints."},
            {"label": "Dynamic or adaptive emissivity modulation", "query": "dynamic adaptive switchable emissivity radiative cooling thermochromic electrochromic", "purpose": "Represent active or passive switching mechanisms for variable environments."},
            {"label": "Thermal insulation and conduction management", "query": "thermal insulation thermal conductivity conduction management radiative cooling film", "purpose": "Explain non-radiative heat transfer constraints and material design choices."},
        ]

    def _not_too_long(self, label: str) -> bool:
        return 3 <= len(label) <= 140

    def _canonical_label(self, label: str) -> str:
        value = label.lower()
        value = value.replace("sio₂", "sio2").replace("sio₂", "sio2").replace("tio₂", "tio2")
        tokens = re.findall(r"[a-z0-9]+", value)
        tokens = [
            token[:-1]
            if token.endswith("s") and not token.endswith("ss") and len(token) > 4
            else token
            for token in tokens
        ]
        return "".join(tokens)

    def _material_filter(self, label: str) -> bool:
        low = label.lower()
        reject = {"sample"}
        if "all-dielectric" in self.topic_context.casefold() and any(
            token in low for token in ("gold", "silver", "metallic", "aluminum", "ag layer", "au layer")
        ):
            return False
        return self._not_too_long(label) and low not in reject and not low.startswith("figure")

    def _mechanism_filter(self, label: str) -> bool:
        low = label.lower()
        reject = ("future research", "not discussed", "not tested")
        return self._not_too_long(label) and not any(term in low for term in reject)

    def _gap_filter(self, label: str) -> bool:
        low = label.lower()
        # Keep explicit or carefully marked inferred limitations, but exclude
        # future-work boilerplate that is not itself a scientific gap.
        return self._not_too_long(label) and not low.startswith("explicit: future research")

    def _metric_filter(self, label: str) -> bool:
        low = label.lower()
        topic_low = self.topic_context.casefold()
        if any(token in topic_low for token in ("1550 nm", "telecom wavelength")):
            if low in {"visible", "par", "ultraviolet", "uv"}:
                return False
        keep_terms = [
            "wavelength",
            "emiss",
            "emit",
            "reflect",
            "transmit",
            "temperature",
            "cooling",
            "solar",
            "humidity",
            "mir",
            "nir",
            "visible",
            "par",
            "conductivity",
            "efficiency",
            "bandwidth",
            "resolution",
            "focal",
            "field of view",
            "modulation",
            "loss",
            "quality factor",
            "strehl",
            "contrast",
            "fidelity",
            "psnr",
            "ssim",
        ]
        return self._not_too_long(label) and any(t in low for t in keep_terms)

    def _method_filter(self, label: str) -> bool:
        low = label.lower()
        reject = {"simulation", "spectroscopy"}
        return self._not_too_long(label) and low not in reject


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a visual-aware concept map from a ReviewKnowledgeBase.")
    parser.add_argument("--kb-dir", default=str(DEFAULT_KB_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--query-plan", default="", help="Optional current-topic Query Planner JSON used to rank/filter concept nodes.")
    parser.add_argument("--max-nodes-per-view", type=int, default=12)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = VisualAwareConceptMapBuilder(
        ConceptMapInputs(
            kb_dir=Path(args.kb_dir),
            query_plan_path=Path(args.query_plan) if args.query_plan else None,
        ),
        Path(args.output_dir),
        max_nodes_per_view=int(args.max_nodes_per_view),
    ).build()
    print(
        json.dumps(
            {
                "ok": True,
                "passed": result.passed,
                "output_dir": str(result.output_dir),
                "concept_map_path": str(result.concept_map_path),
                "validation_path": str(result.validation_path),
                "markdown_path": str(result.markdown_path),
                "counts": result.counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.passed else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
