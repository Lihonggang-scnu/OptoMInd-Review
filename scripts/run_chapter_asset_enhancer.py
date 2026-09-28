"""CLI for the standalone chapter asset enhancement layer.

Consumes an accepted section input packet plus its existing English chapter
draft and produces an enhanced chapter without rerunning upstream retrieval,
claim review, evidence binding, or the existing SectionWriter.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from llm.qwen_chat_client import call_qwen_chat  # noqa: E402
from optomind_research.chapter_asset_enhancer import (  # noqa: E402
    _COMMON_SCHOLARLY_TOKENS,
    DEFAULT_AUDITOR_TIER,
    DEFAULT_BLOCK_TIER,
    DEFAULT_CONTRACT_REPAIR_TIER,
    DEFAULT_EXPLANATORY_TIER,
    DEFAULT_APPLICATION_LOCAL_MAX_RESULTS,
    DEFAULT_APPLICATION_MAX_TARGETS,
    DEFAULT_APPLICATION_PER_TARGET_CAP,
    DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
    DEFAULT_APPLICATION_WRITER_TIER,
    DEFAULT_PATCH_TIER,
    DEFAULT_PLANNER_TIER,
    run_enhancement,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enhance an accepted section chapter from its input packet and "
            "existing English draft without rerunning upstream pipeline steps."
        )
    )
    parser.add_argument(
        "--packet-path",
        type=Path,
        required=True,
        help="Accepted section input_packet.json.",
    )
    parser.add_argument(
        "--old-draft",
        type=Path,
        required=True,
        help="Existing English chapter draft (SECTION_DRAFT_EN.md).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Required output directory for enhancement artifacts.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Invoke real Qwen calls. Without this flag the CLI only validates "
            "inputs and collision safety (dry run)."
        ),
    )
    parser.add_argument(
        "--planner-tier",
        default=DEFAULT_PLANNER_TIER,
        help=f"Model tier for the argument planner (default: {DEFAULT_PLANNER_TIER}).",
    )
    parser.add_argument(
        "--block-tier",
        default=DEFAULT_BLOCK_TIER,
        help=(
            f"Model tier for explanation block writers "
            f"(default: {DEFAULT_BLOCK_TIER})."
        ),
    )
    parser.add_argument(
        "--patch-tier",
        default=DEFAULT_PATCH_TIER,
        help=(
            f"Model tier for legacy gap patch writers "
            f"(default: {DEFAULT_PATCH_TIER})."
        ),
    )
    parser.add_argument(
        "--auditor-tier",
        default=DEFAULT_AUDITOR_TIER,
        help=(
            f"Model tier for the legacy gap auditor "
            f"(default: {DEFAULT_AUDITOR_TIER})."
        ),
    )
    parser.add_argument(
        "--repair-tier",
        default=DEFAULT_CONTRACT_REPAIR_TIER,
        help=(
            f"Model tier for bounded contract repair "
            f"(default: {DEFAULT_CONTRACT_REPAIR_TIER})."
        ),
    )
    parser.add_argument(
        "--disable-explanatory-citations",
        action="store_true",
        help="Skip the explanatory-citation side path entirely.",
    )
    parser.add_argument(
        "--explanatory-tier",
        default=DEFAULT_EXPLANATORY_TIER,
        help=(
            f"Model tier for explanatory citation planning "
            f"(default: {DEFAULT_EXPLANATORY_TIER})."
        ),
    )
    parser.add_argument(
        "--explanatory-max-results",
        type=int,
        default=10,
        help="Maximum metadata/abstract results per explanatory query (default: 10).",
    )
    parser.add_argument(
        "--disable-representative-applications",
        action="store_true",
        help="Disable the optional representative-application garnish.",
    )
    parser.add_argument(
        "--application-max-targets",
        type=int,
        default=DEFAULT_APPLICATION_MAX_TARGETS,
        help=(
            "Max representative-application targets the explanatory planner "
            f"should propose (default: {DEFAULT_APPLICATION_MAX_TARGETS})."
        ),
    )
    parser.add_argument(
        "--application-soft-min-targets",
        type=int,
        default=DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
        help=(
            "Advisory soft minimum of application targets; a single bounded "
            "supplemental planner call may run to close a shortfall "
            f"(default: {DEFAULT_APPLICATION_SOFT_MIN_TARGETS})."
        ),
    )
    parser.add_argument(
        "--application-per-target-cap",
        type=int,
        default=DEFAULT_APPLICATION_PER_TARGET_CAP,
        help=(
            "Max S2 results per application target "
            f"(default: {DEFAULT_APPLICATION_PER_TARGET_CAP})."
        ),
    )
    parser.add_argument(
        "--application-local-max-results",
        type=int,
        default=DEFAULT_APPLICATION_LOCAL_MAX_RESULTS,
        help=(
            "Max local metadata results per application target "
            f"(default: {DEFAULT_APPLICATION_LOCAL_MAX_RESULTS})."
        ),
    )
    parser.add_argument(
        "--application-writer-tier",
        default=DEFAULT_APPLICATION_WRITER_TIER,
        help=(
            "Cheap Qwen tier for the batched application writer "
            f"(default: {DEFAULT_APPLICATION_WRITER_TIER})."
        ),
    )
    parser.add_argument(
        "--local-metadata-store",
        type=Path,
        default=None,
        help=(
            "Optional existing local abstract/metadata SQLite store "
            "(LiteratureResourceLibrary). No database is created by this CLI."
        ),
    )
    parser.add_argument(
        "--s2-search",
        action="store_true",
        help=(
            "Allow Semantic Scholar metadata/abstract search as a fallback "
            "after local metadata search. No OA/full-text download is run."
        ),
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="Allow writing into an existing output directory.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    qwen_caller: Any = call_qwen_chat,
) -> int:
    args = build_arg_parser().parse_args(argv)
    local_search_callback = None
    if args.local_metadata_store is not None:
        local_search_callback = _build_local_metadata_callback(
            args.local_metadata_store
        )
    s2_search_callback = None
    if args.s2_search:
        s2_search_callback = _build_s2_metadata_callback()
    try:
        result = run_enhancement(
            packet_path=args.packet_path,
            old_draft_path=args.old_draft,
            output_dir=args.output_dir,
            live=args.live,
            allow_overwrite=args.allow_overwrite,
            planner_tier=args.planner_tier,
            block_tier=args.block_tier,
            patch_tier=args.patch_tier,
            auditor_tier=args.auditor_tier,
            contract_repair_tier=args.repair_tier,
            explanatory_citations_enabled=not args.disable_explanatory_citations,
            explanatory_tier=args.explanatory_tier,
            explanatory_max_results=args.explanatory_max_results,
            representative_applications_enabled=(
                not args.disable_representative_applications
            ),
            application_max_targets=args.application_max_targets,
            application_soft_min_targets=args.application_soft_min_targets,
            application_per_target_cap=args.application_per_target_cap,
            application_local_max_results=args.application_local_max_results,
            application_writer_tier=args.application_writer_tier,
            local_search_callback=local_search_callback,
            s2_search_callback=s2_search_callback,
            qwen_caller=qwen_caller,
        )
    finally:
        close = getattr(local_search_callback, "close", None)
        if callable(close):
            close()
    if result.get("mode") == "dry_run":
        print(
            f"Dry-run validated packet {args.packet_path} "
            f"(section {result.get('section_id') or '?'}); "
            "pass --live to invoke the enhancement pipeline."
        )
        return 0
    status = result.get("status") or "unknown"
    usage = result.get("model_usage") or {}
    print(
        f"Chapter asset enhancement complete: status={status}, "
        f"block_count={result.get('block_count')}, "
        f"model_calls={usage.get('call_count')}, "
        f"cost_cny={usage.get('total_estimated_cost_cny', 0.0):.6f}"
    )
    print(f"Artifacts written to {args.output_dir}")
    return 0


def _build_local_metadata_callback(db_path: Path):
    return _LocalMetadataCallback(Path(db_path))


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve().as_posix()
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _search_abstract_papers(
    conn: sqlite3.Connection,
    terms: list[str],
    max_results: int,
) -> list[dict]:
    rows_by_id: dict[str, dict] = {}
    for term in terms[:12]:
        like = f"%{term}%"
        rows = conn.execute(
            """
            SELECT paper_id, doi, semantic_scholar_id, openalex_id, title,
                   authors_json, year, venue, abstract, citation_count,
                   open_access, pdf_url, landing_page_url, source_apis_json,
                   query_used_json, matched_keywords_json, topic_tags_json,
                   embedding_id, raw_json
            FROM abstract_papers
            WHERE title LIKE ? OR abstract LIKE ? OR venue LIKE ?
            ORDER BY year DESC
            LIMIT ?
            """,
            (like, like, like, max(10, int(max_results))),
        ).fetchall()
        for row in rows:
            raw = {}
            try:
                raw_value = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                raw_value = {}
            if isinstance(raw_value, dict):
                raw = raw_value
            rows_by_id[row["paper_id"]] = {
                "paper_id": row["paper_id"],
                "doi": row["doi"] or "",
                "semantic_scholar_id": row["semantic_scholar_id"] or "",
                "openalex_id": row["openalex_id"] or "",
                "title": row["title"] or "",
                "authors": _normalize_author_names(
                    _json_list(row["authors_json"])
                ),
                "year": row["year"],
                "venue": row["venue"] or "",
                "abstract": row["abstract"] or "",
                "citation_count": row["citation_count"],
                "open_access": (
                    None
                    if row["open_access"] is None
                    else bool(row["open_access"])
                ),
                "pdf_url": row["pdf_url"] or "",
                "landing_page_url": row["landing_page_url"] or "",
                "source_apis": _json_list(row["source_apis_json"]),
                "query_used": _json_list(row["query_used_json"]),
                "matched_keywords": _json_list(
                    row["matched_keywords_json"]
                ),
                "topic_tags": _json_list(row["topic_tags_json"]),
                "embedding_id": row["embedding_id"] or "",
                "raw": raw,
                "source_audit": raw.get("source_audit") or {},
            }
    return list(rows_by_id.values())[: int(max_results)]


def _search_papers(
    conn: sqlite3.Connection,
    terms: list[str],
    max_results: int,
) -> list[dict]:
    rows_by_id: dict[str, dict] = {}
    for term in terms[:12]:
        like = f"%{term}%"
        rows = conn.execute(
            """
            SELECT paper_id, doi, title, year, venue, search_text, raw_json
            FROM papers
            WHERE title LIKE ? OR search_text LIKE ? OR raw_json LIKE ?
            ORDER BY year DESC
            LIMIT ?
            """,
            (like, like, like, max(10, int(max_results))),
        ).fetchall()
        for row in rows:
            raw = {}
            try:
                raw_value = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                raw_value = {}
            if isinstance(raw_value, dict):
                raw = raw_value
            rows_by_id[row["paper_id"]] = {
                "paper_id": row["paper_id"],
                "doi": row["doi"] or "",
                "title": row["title"] or "",
                "year": row["year"],
                "venue": row["venue"] or "",
                "authors": _normalize_author_names(
                    raw.get("authors") or []
                ),
                "abstract": (
                    raw.get("abstract")
                    or row["search_text"]
                    or ""
                ),
            }
    return list(rows_by_id.values())[: int(max_results)]


class _LocalMetadataCallback:
    """Read-only local metadata callback for the standalone CLI."""

    def __init__(self, db_path: Path) -> None:
        if not Path(db_path).is_file():
            raise ValueError(
                "Local metadata store does not exist; this CLI never creates "
                f"a database: {db_path}"
            )
        self.db_path = Path(db_path)

    def __call__(self, query: str, max_results: int) -> list[dict]:
        terms = _expand_local_query_terms(query)
        conn = _connect_read_only(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            if _sqlite_table_exists(conn, "abstract_papers"):
                try:
                    abstract_rows = _search_abstract_papers(
                        conn, terms, max_results
                    )
                except sqlite3.OperationalError:
                    abstract_rows = []
                if abstract_rows:
                    return abstract_rows
            if _sqlite_table_exists(conn, "papers"):
                return _search_papers(conn, terms, max_results)
            return []
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def close(self) -> None:
        return None


def _normalize_author_names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = next(
                (
                    item.get(key)
                    for key in (
                        "name",
                        "display_name",
                        "author",
                        "raw_author_name",
                    )
                    if item.get(key)
                ),
                "",
            )
        elif isinstance(item, str):
            name = item
        else:
            name = ""
        cleaned = str(name or "").strip()
        if cleaned:
            names.append(cleaned)
    return names


def _expand_local_query_terms(query: str) -> list[str]:
    """Derive a small bounded set of phrases/terms for local LIKE search."""
    phrase = " ".join(str(query or "").split())
    tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9]+", phrase)
        if len(token) >= 3
    ]
    distinctive = [
        token
        for token in tokens
        if token.lower() not in _COMMON_SCHOLARLY_TOKENS
        and (token.isupper() or any(char.isdigit() for char in token) or len(token) >= 8)
    ]
    if not distinctive:
        distinctive = [
            token
            for token in tokens
            if token.lower() not in _COMMON_SCHOLARLY_TOKENS
        ][:3]
    phrases: list[str] = []
    if len(tokens) <= 5:
        phrases.append(phrase)
    for n in (2, 3):
        for index in range(max(0, len(tokens) - n + 1)):
            phrases.append(" ".join(tokens[index : index + n]))
    terms = list(
        dict.fromkeys(
            [*distinctive[:3], *phrases[:3]]
        )
    )
    return [term for term in terms if term][:7]


def _build_s2_metadata_callback():
    from tools.academic_backends.semantic_scholar_backend import (
        SemanticScholarBackend,
    )

    backend = SemanticScholarBackend()

    def callback(query: str, max_results: int) -> list[dict]:
        return backend.search(query, max_results=max_results)

    return callback


if __name__ == "__main__":
    sys.exit(main())
