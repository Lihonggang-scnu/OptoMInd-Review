"""Persist S2-first literature assets in the shared ReviewKnowledgeBase.

S2 body snippets are first-class text chunks.  Their provenance is retained
for audit, but they are not treated as abstracts or penalized merely because
they were parsed by Semantic Scholar rather than locally.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from optomind_research.m3_kb_ingest import _ensure_text_chunks_table
from optomind_research.runtime.review_quality_contract import normalize_scope_fit
from optomind_research.s2_literature_graph import LiteratureGraph
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _paper_search_text(paper: S2PaperRecord) -> str:
    return " ".join(
        value
        for value in (paper.title, paper.abstract, paper.tldr, paper.venue)
        if value
    )[:6000]


def _s2_identity_alias(value: Any) -> str:
    """Normalize one provider/canonical identity for exact alias matching."""

    text = " ".join(str(value or "").split()).strip().casefold()
    if not text:
        return ""
    text = re.sub(r"^corpusid\s*:\s*", "corpusid:", text)
    return text


def _s2_aliases(value: Any) -> set[str]:
    alias = _s2_identity_alias(value)
    if not alias:
        return set()
    aliases = {alias}
    if alias.startswith("corpusid:"):
        aliases.add(alias.removeprefix("corpusid:"))
    elif alias.isdigit():
        aliases.add(f"corpusid:{alias}")
    return aliases


def _paper_aliases(paper: S2PaperRecord) -> set[str]:
    aliases: set[str] = set()
    for value in (paper.paper_id, paper.doi, paper.title):
        aliases.update(_s2_aliases(value))
    if paper.corpus_id not in (None, ""):
        aliases.update(_s2_aliases(paper.corpus_id))
        aliases.update(_s2_aliases(f"CorpusId:{paper.corpus_id}"))
    for value in (paper.external_ids or {}).values():
        aliases.update(_s2_aliases(value))
    raw = paper.raw_metadata if isinstance(paper.raw_metadata, Mapping) else {}
    for key in ("paperId", "corpusId", "title"):
        aliases.update(_s2_aliases(raw.get(key)))
    return aliases


def _chunk_aliases(chunk: UnifiedTextChunk) -> set[str]:
    aliases: set[str] = set()
    for value in (chunk.paper_id, chunk.doi, chunk.title, chunk.corpus_id):
        aliases.update(_s2_aliases(value))
    if chunk.corpus_id not in (None, ""):
        aliases.update(_s2_aliases(f"CorpusId:{chunk.corpus_id}"))
    raw = chunk.raw_metadata if isinstance(chunk.raw_metadata, Mapping) else {}
    item = raw.get("s2_item") if isinstance(raw, Mapping) else {}
    parent = item.get("paper") if isinstance(item, Mapping) else {}
    if isinstance(parent, Mapping):
        for key in ("paperId", "corpusId", "title"):
            aliases.update(_s2_aliases(parent.get(key)))
        external_ids = parent.get("externalIds") or {}
        if isinstance(external_ids, Mapping):
            for value in external_ids.values():
                aliases.update(_s2_aliases(value))
    return aliases


def _rebind_chunks_to_papers(
    papers: Iterable[S2PaperRecord],
    chunks: Iterable[UnifiedTextChunk],
) -> tuple[list[UnifiedTextChunk], list[dict[str, Any]]]:
    """Bind provider chunk parents to the exact canonical S2 paper ID.

    S2 snippet responses commonly identify a parent as ``CorpusId:<n>`` while
    section ledgers use the stable ``paperId`` hash.  The aliases are useful
    provenance, but only the canonical ID is written to ``text_chunks``.
    Ambiguous aliases are left unresolved and fail closed before insertion.
    """

    paper_list = [paper for paper in papers if paper.paper_id]
    aliases: dict[str, set[str]] = {}
    for paper in paper_list:
        for alias in _paper_aliases(paper):
            aliases.setdefault(alias, set()).add(paper.paper_id)

    rebound: list[UnifiedTextChunk] = []
    events: list[dict[str, Any]] = []
    for chunk in chunks:
        if chunk.paper_id in {paper.paper_id for paper in paper_list}:
            rebound.append(chunk)
            continue
        matches = {
            paper_id
            for alias in _chunk_aliases(chunk)
            for paper_id in aliases.get(alias, set())
        }
        if len(matches) != 1:
            rebound.append(chunk)
            continue
        canonical_parent = next(iter(matches))
        provider_aliases = sorted(_chunk_aliases(chunk))
        canonical_aliases = sorted(_paper_aliases(next(
            paper for paper in paper_list if paper.paper_id == canonical_parent
        )))
        route = dict(chunk.route_provenance or {})
        route["paper_id"] = canonical_parent
        route["provider_paper_id"] = str(chunk.paper_id or "")
        route["identity_resolution"] = {
            "provider_parent_id": str(chunk.paper_id or ""),
            "canonical_parent_id": canonical_parent,
            "provider_aliases": provider_aliases,
            "s2_aliases": sorted(set(provider_aliases) | set(canonical_aliases)),
            "method": "exact_s2_alias",
        }
        rebound.append(replace(chunk, paper_id=canonical_parent, route_provenance=route))
        events.append({
            "chunk_id": chunk.chunk_id,
            "provider_parent_id": chunk.paper_id,
            "canonical_parent_id": canonical_parent,
            "provider_aliases": provider_aliases,
            "s2_aliases": sorted(set(provider_aliases) | set(canonical_aliases)),
            "method": "exact_s2_alias",
        })
    return rebound, events


def _synthetic_paper_for_chunk(chunk: UnifiedTextChunk) -> S2PaperRecord | None:
    """Create a minimal parent when a legacy caller supplies chunks only."""

    paper_id = str(chunk.paper_id or "").strip()
    if not paper_id:
        return None
    corpus_id = chunk.corpus_id
    raw = chunk.raw_metadata if isinstance(chunk.raw_metadata, Mapping) else {}
    item = raw.get("s2_item") if isinstance(raw, Mapping) else {}
    parent = item.get("paper") if isinstance(item, Mapping) else {}
    if isinstance(parent, Mapping):
        try:
            corpus_id = int(parent.get("corpusId")) if parent.get("corpusId") not in (None, "") else corpus_id
        except (TypeError, ValueError):
            pass
    route = dict(chunk.route_provenance or {})
    return S2PaperRecord(
        paper_id=paper_id,
        corpus_id=corpus_id,
        doi=str(chunk.doi or ""),
        title=str(chunk.title or ""),
        content_depth=str(chunk.content_depth or "structured_snippet"),
        use_permission=str(chunk.use_permission or "discovery_only"),
        scope_fit=normalize_scope_fit(chunk.scope_fit),
        discovery_route=str(route.get("discovery_route") or "semantic_scholar_snippet_search"),
        materialization_route=str(
            route.get("materialization_route") or "s2_structured_body_snippet"
        ),
        literature_roles=list(dict.fromkeys([
            *(str(item).strip().casefold() for item in (route.get("requested_roles") or [])),
            *(str(item).strip().casefold() for item in (chunk.relation_roles or [])),
        ])),
        route_events=[{
            "event": "synthetic_parent_upserted_for_chunk",
            "route": str(route.get("discovery_route") or "semantic_scholar_snippet_search"),
        }],
        raw_metadata=dict(parent) if isinstance(parent, Mapping) else {},
    )


def _foreign_parent_consistency(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT tc.chunk_id, tc.paper_id
        FROM text_chunks AS tc
        LEFT JOIN papers AS p ON p.paper_id = tc.paper_id
        WHERE tc.paper_id IS NULL OR trim(tc.paper_id)='' OR p.paper_id IS NULL
        ORDER BY tc.chunk_id
        """
    ).fetchall()
    orphan_rows = [
        {"chunk_id": str(row[0] or ""), "paper_id": str(row[1] or "")}
        for row in rows
    ]
    return {
        "valid": not orphan_rows,
        "orphan_count": len(orphan_rows),
        "orphan_chunks": orphan_rows[:50],
    }


def validate_foreign_parent_consistency(kb_sqlite: str | Path) -> dict[str, Any]:
    """Validate that every staged text chunk has a canonical paper parent."""

    with sqlite3.connect(str(kb_sqlite)) as conn:
        _ensure_s2_tables(conn)
        return _foreign_parent_consistency(conn)


def _ensure_s2_tables(conn: sqlite3.Connection) -> None:
    _ensure_text_chunks_table(conn)
    # Additive migrations keep existing ReviewKnowledgeBase files readable.
    # Route fields are queryable columns; the complete event history remains
    # in the JSON payload for lossless audit.
    for table, columns in {
        "papers": {
            "discovery_route": "TEXT NOT NULL DEFAULT 'unknown'",
            "materialization_route": "TEXT NOT NULL DEFAULT 'not_materialized'",
            "content_depth": "TEXT NOT NULL DEFAULT 'metadata'",
            "use_permission": "TEXT NOT NULL DEFAULT 'discovery_only'",
            "scope_fit": "TEXT NOT NULL DEFAULT 'unreviewed'",
            "route_provenance_json": "TEXT NOT NULL DEFAULT '{}'",
            "literature_roles_json": "TEXT NOT NULL DEFAULT '[]'",
            "relation_roles_json": "TEXT NOT NULL DEFAULT '[]'",
        },
        "text_chunks": {
            "route_provenance_json": "TEXT NOT NULL DEFAULT '{}'",
            "content_depth": "TEXT NOT NULL DEFAULT 'fulltext'",
            "use_permission": "TEXT NOT NULL DEFAULT 'contextual_or_qualified_support'",
            "context_complete": "INTEGER NOT NULL DEFAULT 1",
            "allowed_claim_kinds_json": "TEXT NOT NULL DEFAULT '[]'",
            "scope_fit": "TEXT NOT NULL DEFAULT 'unreviewed'",
            "relation_roles_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    }.items():
        existing = {
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
                )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s2_literature_graph_nodes(
            paper_id TEXT PRIMARY KEY,
            corpus_id INTEGER,
            title TEXT NOT NULL DEFAULT '',
            year INTEGER,
            active_for_lineage INTEGER NOT NULL DEFAULT 1,
            annotations_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s2_literature_graph_edges(
            edge_id TEXT PRIMARY KEY,
            source_paper_id TEXT NOT NULL,
            target_paper_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            edge_origin TEXT NOT NULL DEFAULT 's2_api',
            observed_relation TEXT NOT NULL DEFAULT '',
            semantic_relation TEXT NOT NULL DEFAULT '',
            relation_status TEXT NOT NULL DEFAULT 'observed',
            relation_basis_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            context TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    edge_columns = {
        str(row[1])
        for row in conn.execute(
            'PRAGMA table_info("s2_literature_graph_edges")'
        ).fetchall()
    }
    for column, definition in {
        "observed_relation": "TEXT NOT NULL DEFAULT ''",
        "semantic_relation": "TEXT NOT NULL DEFAULT ''",
        "relation_status": "TEXT NOT NULL DEFAULT 'observed'",
        "relation_basis_chunk_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        if column not in edge_columns:
            conn.execute(
                f'ALTER TABLE "s2_literature_graph_edges" ADD COLUMN "{column}" {definition}'
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_s2_graph_edge_source "
        "ON s2_literature_graph_edges(source_paper_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_s2_graph_edge_target "
        "ON s2_literature_graph_edges(target_paper_id)"
    )


class S2KnowledgeBaseBridge:
    """Write S2 papers, body snippets and typed graph edges idempotently."""

    def __init__(self, kb_sqlite: str | Path) -> None:
        self.kb_sqlite = Path(kb_sqlite)

    def _connect(self) -> sqlite3.Connection:
        self.kb_sqlite.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.kb_sqlite))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_s2_tables(conn)
        return conn

    @staticmethod
    def _upsert_paper(conn: sqlite3.Connection, paper: S2PaperRecord) -> None:
        existing = conn.execute(
            "SELECT raw_json FROM papers WHERE paper_id=?", (paper.paper_id,)
        ).fetchone()
        existing_raw: dict[str, Any] = {}
        if existing:
            try:
                parsed = json.loads(existing[0] or "{}")
                existing_raw = parsed if isinstance(parsed, dict) else {}
            except (TypeError, json.JSONDecodeError):
                existing_raw = {}
        raw_json = json.dumps(
            {
                **existing_raw,
                **paper.to_dict(),
                "ingest_source": "semantic_scholar",
                "ingest_sources": list(dict.fromkeys([
                    *list(existing_raw.get("ingest_sources") or []),
                    str(existing_raw.get("ingest_source") or ""),
                    "semantic_scholar",
                ])),
                "ingested_at": _utc_now(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        conn.execute(
            """
            INSERT INTO papers(
                paper_id,doi,title,year,venue,quality_tier,
                query_relevance,search_text,raw_json,discovery_route,
                materialization_route,content_depth,use_permission,scope_fit,
                route_provenance_json,literature_roles_json,relation_roles_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(paper_id) DO UPDATE SET
                doi=CASE WHEN excluded.doi<>'' THEN excluded.doi ELSE papers.doi END,
                title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE papers.title END,
                year=COALESCE(excluded.year,papers.year),
                venue=CASE WHEN excluded.venue<>'' THEN excluded.venue ELSE papers.venue END,
                search_text=CASE WHEN excluded.search_text<>'' THEN excluded.search_text ELSE papers.search_text END,
                discovery_route=CASE WHEN excluded.discovery_route<>'' THEN excluded.discovery_route ELSE papers.discovery_route END,
                materialization_route=CASE
                    WHEN excluded.materialization_route NOT IN ('','not_materialized')
                    THEN excluded.materialization_route ELSE papers.materialization_route END,
                content_depth=CASE
                    WHEN excluded.content_depth NOT IN ('','metadata','abstract')
                    THEN excluded.content_depth ELSE papers.content_depth END,
                use_permission=CASE
                    WHEN excluded.use_permission NOT IN ('','discovery_only','background_and_candidate_only')
                    THEN excluded.use_permission ELSE papers.use_permission END,
                scope_fit=CASE
                    WHEN excluded.scope_fit NOT IN ('','unreviewed')
                    THEN excluded.scope_fit ELSE papers.scope_fit END,
                route_provenance_json=excluded.route_provenance_json,
                literature_roles_json=excluded.literature_roles_json,
                relation_roles_json=excluded.relation_roles_json,
                raw_json=excluded.raw_json
            """,
            (
                paper.paper_id,
                paper.doi,
                paper.title,
                paper.year,
                paper.venue,
                "s2_verified_metadata",
                "s2_first",
                _paper_search_text(paper),
                raw_json,
                paper.discovery_route,
                paper.materialization_route,
                paper.content_depth,
                paper.use_permission,
                normalize_scope_fit(paper.scope_fit),
                json.dumps(
                    {
                        "route_events": paper.route_events,
                        "metadata_conflicts": paper.metadata_conflicts,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(paper.literature_roles, ensure_ascii=False),
                json.dumps(paper.relation_roles, ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM paper_fts WHERE paper_id=?", (paper.paper_id,))
        conn.execute(
            "INSERT INTO paper_fts(paper_id,title,search_text) VALUES(?,?,?)",
            (paper.paper_id, paper.title, _paper_search_text(paper)),
        )

    def ingest(
        self,
        *,
        papers: Iterable[S2PaperRecord] = (),
        chunks: Iterable[UnifiedTextChunk] = (),
    ) -> dict[str, Any]:
        paper_map = {paper.paper_id: paper for paper in papers if paper.paper_id}
        chunk_list = [chunk for chunk in chunks if chunk.chunk_id and chunk.text]
        chunk_list, identity_rebindings = _rebind_chunks_to_papers(
            paper_map.values(), chunk_list
        )
        if paper_map:
            unresolved = [
                chunk for chunk in chunk_list if chunk.paper_id not in paper_map
            ]
            if unresolved:
                raise ValueError(
                    "S2 chunk parent identity could not be resolved to a canonical paper: "
                    + ", ".join(
                        f"{chunk.chunk_id}={chunk.paper_id}"
                        for chunk in unresolved[:8]
                    )
                )
        else:
            # Keep legacy chunk-only callers referentially closed.  The
            # section short path supplies the stronger stable S2 paper hash;
            # this fallback still prevents an orphan text_chunks row.
            for chunk in chunk_list:
                synthetic = _synthetic_paper_for_chunk(chunk)
                if synthetic is not None:
                    paper_map.setdefault(synthetic.paper_id, synthetic)
        inserted = 0
        reused = 0
        inserted_paper_ids: list[str] = []
        inserted_chunk_ids: list[str] = []
        reused_chunk_ids: list[str] = []
        conn = self._connect()
        try:
            with conn:
                for paper in paper_map.values():
                    existing_paper = conn.execute(
                        "SELECT 1 FROM papers WHERE paper_id=?", (paper.paper_id,)
                    ).fetchone()
                    self._upsert_paper(conn, paper)
                    if not existing_paper:
                        inserted_paper_ids.append(paper.paper_id)
                for ordinal, chunk in enumerate(chunk_list):
                    existing = conn.execute(
                        "SELECT 1 FROM text_chunks WHERE chunk_id=?", (chunk.chunk_id,)
                    ).fetchone()
                    raw = {
                        **chunk.to_dict(),
                        "ingest_source": "s2_first",
                        "ingested_at": _utc_now(),
                    }
                    provenance = {
                        "provider": str(
                            chunk.source_locator.get("provider")
                            or "semantic_scholar"
                        ),
                        "text_provenance": chunk.text_provenance,
                        "source_locator": chunk.source_locator,
                        "query_links": chunk.query_links,
                        "quality_status": chunk.quality_status,
                        "context_limitations": chunk.context_limitations,
                        "reference_mentions": chunk.reference_mentions,
                        "sentence_spans": chunk.sentence_spans,
                        "route_provenance": chunk.route_provenance,
                        "content_depth": chunk.content_depth,
                        "use_permission": chunk.use_permission,
                        "context_complete": chunk.context_complete,
                        "allowed_claim_kinds": chunk.allowed_claim_kinds,
                        "scope_fit": chunk.scope_fit,
                        "relation_roles": chunk.relation_roles,
                    }
                    if chunk.content_depth == "structured_snippet":
                        provenance.setdefault(
                            "materialization_route",
                            "s2_structured_body_snippet",
                        )
                    conn.execute(
                        """
                        INSERT INTO text_chunks(
                            chunk_id,paper_id,doi,title,ordinal,section_path,
                            char_start,char_end,char_count,boilerplate_score,
                            text,search_text,raw_json,evidence_level,source_kind,
                            provenance_json,route_provenance_json,content_depth,
                            use_permission,context_complete,allowed_claim_kinds_json,
                            scope_fit,relation_roles_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(chunk_id) DO UPDATE SET
                            paper_id=excluded.paper_id,doi=excluded.doi,
                            title=excluded.title,ordinal=excluded.ordinal,
                            section_path=excluded.section_path,
                            char_start=excluded.char_start,char_end=excluded.char_end,
                            char_count=excluded.char_count,text=excluded.text,
                            search_text=excluded.search_text,raw_json=excluded.raw_json,
                            evidence_level=excluded.evidence_level,
                            source_kind=excluded.source_kind,
                            provenance_json=excluded.provenance_json,
                            route_provenance_json=excluded.route_provenance_json,
                            content_depth=excluded.content_depth,
                            use_permission=excluded.use_permission,
                            context_complete=excluded.context_complete,
                            allowed_claim_kinds_json=excluded.allowed_claim_kinds_json,
                            scope_fit=excluded.scope_fit,
                            relation_roles_json=excluded.relation_roles_json
                        """,
                        (
                            chunk.chunk_id,
                            chunk.paper_id,
                            chunk.doi,
                            chunk.title,
                            ordinal,
                            chunk.section,
                            chunk.source_locator.get("offset_start"),
                            chunk.source_locator.get("offset_end"),
                            len(chunk.text),
                            0.0,
                            chunk.text,
                            chunk.text[:2000],
                            json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                            (
                                "abstract"
                                if chunk.content_depth == "abstract_claim"
                                else "text_chunk"
                            ),
                            (
                                "abstract"
                                if chunk.content_depth == "abstract_claim"
                                else chunk.text_provenance
                            ),
                            json.dumps(
                                provenance, ensure_ascii=False, separators=(",", ":")
                            ),
                            json.dumps(
                                chunk.route_provenance,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            chunk.content_depth,
                            chunk.use_permission,
                            int(bool(chunk.context_complete)),
                            json.dumps(chunk.allowed_claim_kinds, ensure_ascii=False),
                            normalize_scope_fit(chunk.scope_fit),
                            json.dumps(chunk.relation_roles, ensure_ascii=False),
                        ),
                    )
                    conn.execute(
                        "DELETE FROM text_chunk_fts WHERE chunk_id=?", (chunk.chunk_id,)
                    )
                    conn.execute(
                        """
                        INSERT INTO text_chunk_fts(
                            chunk_id,paper_id,title,section_path,text
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            chunk.chunk_id,
                            chunk.paper_id,
                            chunk.title,
                            chunk.section,
                            chunk.text,
                        ),
                    )
                    if existing:
                        reused += 1
                        reused_chunk_ids.append(chunk.chunk_id)
                    else:
                        inserted += 1
                        inserted_chunk_ids.append(chunk.chunk_id)
                integrity = _foreign_parent_consistency(conn)
                if not integrity["valid"]:
                    raise ValueError(
                        "S2 KB foreign-parent consistency failed: "
                        + json.dumps(integrity, ensure_ascii=False)
                    )
            return {
                "papers_upserted": len(paper_map),
                "papers_inserted": len(inserted_paper_ids),
                "inserted_paper_ids": inserted_paper_ids,
                "chunks_inserted": inserted,
                "chunks_reused": reused,
                "inserted_chunk_ids": inserted_chunk_ids,
                "reused_chunk_ids": reused_chunk_ids,
                "identity_rebindings": identity_rebindings,
                "foreign_parent_consistency": integrity,
                "kb_sqlite": str(self.kb_sqlite),
            }
        finally:
            conn.close()

    def ingest_graph(self, graph: LiteratureGraph) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                for paper_id, paper in graph.nodes.items():
                    self._upsert_paper(conn, paper)
                    annotations = graph.node_annotations.get(paper_id, {})
                    conn.execute(
                        """
                        INSERT INTO s2_literature_graph_nodes(
                            paper_id,corpus_id,title,year,active_for_lineage,
                            annotations_json,raw_json,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(paper_id) DO UPDATE SET
                            corpus_id=excluded.corpus_id,title=excluded.title,
                            year=excluded.year,
                            active_for_lineage=excluded.active_for_lineage,
                            annotations_json=excluded.annotations_json,
                            raw_json=excluded.raw_json,updated_at=excluded.updated_at
                        """,
                        (
                            paper_id,
                            paper.corpus_id,
                            paper.title,
                            paper.year,
                            int(bool(annotations.get("active_for_lineage", True))),
                            json.dumps(
                                annotations, ensure_ascii=False, separators=(",", ":")
                            ),
                            json.dumps(
                                paper.to_dict(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            _utc_now(),
                        ),
                    )
                for edge in graph.edges:
                    conn.execute(
                        """
                        INSERT INTO s2_literature_graph_edges(
                            edge_id,source_paper_id,target_paper_id,edge_type,
                            edge_origin,observed_relation,semantic_relation,
                            relation_status,relation_basis_chunk_ids_json,
                            context,raw_json,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(edge_id) DO UPDATE SET
                            context=excluded.context,raw_json=excluded.raw_json,
                            observed_relation=excluded.observed_relation,
                            semantic_relation=excluded.semantic_relation,
                            relation_status=excluded.relation_status,
                            relation_basis_chunk_ids_json=excluded.relation_basis_chunk_ids_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            edge.edge_id,
                            edge.source_paper_id,
                            edge.target_paper_id,
                            edge.edge_type,
                            edge.edge_origin,
                            edge.observed_relation,
                            edge.semantic_relation,
                            edge.status,
                            json.dumps(
                                edge.relation_basis_chunk_ids,
                                ensure_ascii=False,
                            ),
                            edge.context,
                            json.dumps(
                                edge.to_dict(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            _utc_now(),
                        ),
                    )
            return {
                "nodes_upserted": len(graph.nodes),
                "edges_upserted": len(graph.edges),
                "kb_sqlite": str(self.kb_sqlite),
            }
        finally:
            conn.close()
