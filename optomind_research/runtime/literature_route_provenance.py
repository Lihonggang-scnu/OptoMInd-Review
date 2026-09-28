"""Build an auditable report of literature data routes used by one harness run."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .artifact_store import atomic_write_json


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _paper_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    return key[4:] if key.startswith("doi:") else key


def _counts(values: list[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value or "unknown") for value in values).items()))


def _citation_route(
    citation: dict[str, Any],
    paper_routes: dict[str, list[tuple[str, str]]],
) -> str:
    chunk_ids = [str(item) for item in citation.get("chunk_ids") or []]
    if any(item.startswith("s2chunk:") for item in chunk_ids):
        return "s2_direct_structured_snippet"
    routes = paper_routes.get(_paper_key(citation.get("paper_id")), [])
    if routes:
        backend, status = sorted(
            routes,
            key=lambda item: (
                item[1] != "fulltext",
                item[0] != "semantic_scholar",
            ),
        )[0]
        if backend == "semantic_scholar" and status == "fulltext":
            return "s2_discovery_then_fulltext_parse"
        if backend == "semantic_scholar" and status == "abstract_only":
            return "s2_discovery_then_abstract_fallback"
        if backend == "openalex" and status == "fulltext":
            return "openalex_discovery_then_fulltext_parse"
        if backend == "local_review_kb":
            return "local_prior_fulltext"
        return f"{backend}_{status}"
    if any(item.startswith("m3gap:") for item in chunk_ids):
        return "legacy_materialized_chunk_unresolved_origin"
    return "unclassified"


def build_literature_route_provenance(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    s2_report = _read(
        run_dir / "s2_literature_intelligence" / "S2_BOOTSTRAP_REPORT.json"
    )
    sections_root = run_dir / "section_coverage" / "sections"
    candidate_rows: list[dict[str, Any]] = []
    materialized_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for section_dir in sorted(sections_root.glob("S*")):
        section_id = section_dir.name
        for row in _read(section_dir / "OA_CANDIDATE_LEDGER.json").get(
            "candidates", []
        ):
            if isinstance(row, dict):
                candidate_rows.append({**row, "_section_id": section_id})
        for row in _read(section_dir / "MATERIALIZATION_MANIFEST.json").get(
            "papers", []
        ):
            if isinstance(row, dict):
                materialized_rows.append({**row, "_section_id": section_id})
        for row in _read(section_dir / "SECTION_SOURCE_LEDGER.json").get(
            "sources", []
        ):
            if isinstance(row, dict):
                source_rows.append({**row, "_section_id": section_id})

    candidate_by_id = {
        (row["_section_id"], str(row.get("candidate_id") or "")): row
        for row in candidate_rows
    }
    materialization_routes: list[str] = []
    for row in materialized_rows:
        candidate = candidate_by_id.get(
            (row["_section_id"], str(row.get("candidate_id") or "")), {}
        )
        backend = "+".join(sorted(candidate.get("backends") or [])) or "unknown"
        materialization_routes.append(
            f"{backend} -> {row.get('acquisition_status') or 'unknown'}"
        )

    paper_routes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    unique_papers_by_route: dict[str, set[str]] = defaultdict(set)
    canonical_chunks: set[str] = set()
    for row in source_rows:
        paper = _paper_key(row.get("doi") or row.get("paper_id"))
        backend = str(row.get("retrieval_backend") or "unknown")
        status = str(row.get("acquisition_status") or "unknown")
        paper_routes[paper].append((backend, status))
        unique_papers_by_route[f"{backend} -> {status}"].add(paper)
        canonical_chunks.update(str(item) for item in row.get("canonical_chunk_ids") or [])

    citation_map = _read(
        run_dir
        / "authoring"
        / "full_review"
        / "FULL_REVIEW_CITATION_MAP.json"
    )
    citations = [
        row
        for row in citation_map.get("citations", [])
        if isinstance(row, dict)
    ]
    for row in citations:
        row["_route"] = _citation_route(row, paper_routes)

    candidate_backend_sets = [
        "+".join(sorted(row.get("backends") or [])) or "unknown"
        for row in candidate_rows
    ]
    failures = [
        {
            "section_id": row["_section_id"],
            "title": str(row.get("title") or ""),
            "doi": str(row.get("doi") or ""),
            "status": str(row.get("acquisition_status") or ""),
            "error": str(
                row.get("parse_failure_reason")
                or row.get("download_error")
                or ""
            ),
        }
        for row in materialized_rows
        if row.get("acquisition_status") in {"failed", "abstract_only", "metadata_only"}
    ]
    direct_citations = sum(
        row["_route"] == "s2_direct_structured_snippet" for row in citations
    )
    fallback_citations = len(citations) - direct_citations
    report = {
        "schema_version": "research_harness.literature_route_provenance.v1",
        "run_dir": str(run_dir),
        "semantic_scholar_direct": {
            "portfolio_candidates": int(s2_report.get("portfolio_candidates", 0) or 0),
            "retained_candidates": int(s2_report.get("retained_candidates", 0) or 0),
            "accepted_body_chunks": int(s2_report.get("accepted_s2_body_chunks", 0) or 0),
            "papers_ingested": int(
                (s2_report.get("kb_ingest") or {}).get("papers_upserted", 0) or 0
            ),
            "graph": s2_report.get("graph_summary") or {},
            "external_query_runs": len(s2_report.get("external_query_runs") or []),
        },
        "section_candidate_recall": {
            "candidate_records": len(candidate_rows),
            "backend_distribution": _counts(candidate_backend_sets),
            "decision_distribution": _counts(
                [row.get("decision") for row in candidate_rows]
            ),
            "scope_distribution": _counts(
                [row.get("scope_fit") for row in candidate_rows]
            ),
        },
        "fulltext_and_fallback_materialization": {
            "attempt_records": len(materialized_rows),
            "unique_attempted_papers": len(
                {
                    _paper_key(row.get("doi") or row.get("paper_id"))
                    for row in materialized_rows
                }
            ),
            "route_outcomes": _counts(materialization_routes),
            "content_type_distribution": _counts(
                [row.get("content_type_detected") for row in materialized_rows]
            ),
            "failure_or_degraded_records": failures,
        },
        "adopted_section_sources": {
            "ledger_records": len(source_rows),
            "unique_papers": len(
                {
                    _paper_key(row.get("doi") or row.get("paper_id"))
                    for row in source_rows
                }
            ),
            "unique_papers_by_route": {
                key: len(value)
                for key, value in sorted(unique_papers_by_route.items())
            },
            "unique_canonical_chunks": len(canonical_chunks),
            "chunk_protocol_distribution": _counts(
                [item.split(":", 1)[0] for item in canonical_chunks]
            ),
        },
        "final_review_usage": {
            "citation_entries": len(citations),
            "unique_cited_papers": len(
                {_paper_key(row.get("paper_id")) for row in citations}
            ),
            "citation_route_distribution": _counts(
                [row["_route"] for row in citations]
            ),
            "direct_s2_citation_share": round(
                direct_citations / max(1, len(citations)), 4
            ),
            "fallback_citation_share": round(
                fallback_citations / max(1, len(citations)), 4
            ),
            "entailment_distribution": _counts(
                [row.get("entailment_verdict") for row in citations]
            ),
            "trace_status_distribution": _counts(
                [row.get("trace_status") for row in citations]
            ),
        },
        "route_definitions": {
            "s2_direct_structured_snippet": (
                "Semantic Scholar Graph/Snippet/Recommendations metadata, "
                "relations, or structured body text used without re-downloading."
            ),
            "s2_discovery_then_fulltext_parse": (
                "Semantic Scholar discovered the paper; the legacy OA resolver "
                "downloaded full text and the local parser normalized/chunked it."
            ),
            "s2_discovery_then_abstract_fallback": (
                "Semantic Scholar discovered the paper, but no parseable full "
                "text was obtained; abstract-level material was retained."
            ),
            "openalex_discovery_then_fulltext_parse": (
                "OpenAlex supplied a supplementary candidate that was then "
                "downloaded and parsed by the same local full-text route."
            ),
            "local_prior_fulltext": (
                "A previously normalized local full-text record was reused."
            ),
        },
        "upgrade_priorities": [
            {
                "priority": "P0",
                "action": "Persist one route_provenance field on every paper and chunk.",
                "reason": "Current route reconstruction still joins several ledgers after the run.",
            },
            {
                "priority": "P1",
                "action": "Raise direct S2 snippet use where snippets cover the requested claim.",
                "reason": (
                    "S2 dominates recall, but final citations still rely mainly "
                    "on locally materialized chunks."
                ),
            },
            {
                "priority": "P1",
                "action": "Use S2 citation/recommendation neighborhoods for targeted gap expansion.",
                "reason": "This should reduce broad repeated searches and improve historical lineage.",
            },
            {
                "priority": "P1",
                "action": "Separate fulltext failure, abstract fallback, and metadata-only states.",
                "reason": "These states have different downstream writing permissions.",
            },
            {
                "priority": "P2",
                "action": "Audit factual citations marked not_entailed before publication.",
                "reason": (
                    "Traceability is complete, but contextual relevance must not "
                    "be presented as direct factual entailment."
                ),
            },
        ],
    }
    return report


def write_literature_route_provenance(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    report = build_literature_route_provenance(run_dir)
    json_path = run_dir / "LITERATURE_ROUTE_PROVENANCE.json"
    md_path = run_dir / "LITERATURE_ROUTE_PROVENANCE.md"
    atomic_write_json(json_path, report)
    s2 = report["semantic_scholar_direct"]
    recall = report["section_candidate_recall"]
    material = report["fulltext_and_fallback_materialization"]
    adopted = report["adopted_section_sources"]
    final = report["final_review_usage"]
    markdown = f"""# 文献数据路线溯源报告

## Semantic Scholar 直接贡献

- 初始组合候选：{s2['portfolio_candidates']}
- 接收的结构化正文片段：{s2['accepted_body_chunks']}
- 写入运行时知识库的论文：{s2['papers_ingested']}
- 文献关系图：{s2['graph'].get('node_count', 0)} 个节点、{s2['graph'].get('edge_count', 0)} 条边
- 章节检索候选后端分布：{json.dumps(recall['backend_distribution'], ensure_ascii=False)}

## 回源全文与旧链兜底

- 全文物化尝试：{material['attempt_records']} 条
- 路线结果：{json.dumps(material['route_outcomes'], ensure_ascii=False)}
- 最终采用论文路线：{json.dumps(adopted['unique_papers_by_route'], ensure_ascii=False)}
- 最终知识块协议：{json.dumps(adopted['chunk_protocol_distribution'], ensure_ascii=False)}

## 终稿实际使用

- 引用条目：{final['citation_entries']}；唯一论文：{final['unique_cited_papers']}
- 引用路线：{json.dumps(final['citation_route_distribution'], ensure_ascii=False)}
- S2 直接片段占比：{final['direct_s2_citation_share']:.1%}
- 回源/本地材料占比：{final['fallback_citation_share']:.1%}
- 证据蕴含结果：{json.dumps(final['entailment_distribution'], ensure_ascii=False)}

## 升级重点

1. 每篇论文和每个块在生成时直接写入路线字段，避免事后拼表。
2. 对已足以支撑主张的 S2 正文片段优先直用，减少不必要全文下载。
3. 用引文与推荐网络做定点补证和历史脉络，而不是反复宽泛检索。
4. 明确区分全文、摘要兜底、仅元数据三种下游权限。
5. 对“可追溯但不直接蕴含”的事实型引用进入发表前复核队列。
"""
    md_path.write_text(markdown, encoding="utf-8", newline="\n")
    report["artifacts"] = {"json": str(json_path), "markdown": str(md_path)}
    atomic_write_json(json_path, report)
    return report
