"""Cost-aware OA full-text escalation for the S2-first pipeline."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
import html
import json
import os
import re
import logging
import shutil
import sqlite3
import tempfile
import uuid

from optomind_research.m3_kb_ingest import (
    KBIngester,
    doi_to_slug,
    sanitize_audit_url,
)
from optomind_research.metadata_index import normalize_doi, title_match_score
from optomind_research.s2_schemas import S2PaperRecord


def resolve_oa_worker_count(
    value: int | None = None,
    *,
    default: int = 4,
) -> int:
    """Resolve a bounded OA acquisition worker count.

    Explicit parameters win over the environment, and the environment wins
    over the conservative default.  The result is always clamped to [1, 16]
    so a misconfiguration can never create hidden unlimited fanout.
    """

    configured = value
    if configured is None:
        configured = os.environ.get(
            "OPTOMIND_OA_FULLTEXT_WORKERS", str(default)
        )
    try:
        return max(1, min(int(configured), 16))
    except (TypeError, ValueError):
        return max(1, min(int(default), 16))


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _merge_paper_raw_json(existing_raw: Any, incoming_raw: Any) -> str:
    existing = _json_object(existing_raw)
    incoming = _json_object(incoming_raw)
    if not existing:
        return json.dumps(incoming, ensure_ascii=False, sort_keys=True)
    if not incoming:
        return json.dumps(existing, ensure_ascii=False, sort_keys=True)
    merged = {**existing, **incoming}
    updates: list[Any] = []
    seen: set[str] = set()
    for item in [
        *(existing.get("m3_real_oa_updates") or []),
        *(incoming.get("m3_real_oa_updates") or []),
    ]:
        key = json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key in seen:
            continue
        seen.add(key)
        updates.append(item)
    if updates:
        merged["m3_real_oa_updates"] = updates
    return json.dumps(merged, ensure_ascii=False, sort_keys=True)


def merge_kb_sqlite_into(target: Path, source: Path) -> None:
    """Merge one worker-local KB SQLite into a shared KB using one writer.

    This helper is only ever called from the deterministic main thread, in
    report/commit order.  Each source database was written by exactly one
    worker, so no two threads touch the shared connection.  Plain tables are
    merged with INSERT OR REPLACE; FTS virtual tables are re-keyed so the
    final shared state is a deterministic union of the worker artifacts.
    """

    source = Path(source)
    if not source.is_file():
        return
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(str(target)) as conn:
        src = sqlite3.connect(source_uri, uri=True)
        try:
            table_rows = src.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            fts_names = {
                str(name)
                for name, ddl in table_rows
                if str(ddl or "").upper().startswith("CREATE VIRTUAL TABLE")
            }
            shadow_suffixes = (
                "_config",
                "_content",
                "_data",
                "_docsize",
                "_idx",
            )
            for table, ddl in table_rows:
                if not ddl:
                    continue
                if any(
                    table == f"{fts}{suffix}"
                    for fts in fts_names
                    for suffix in shadow_suffixes
                ):
                    # FTS5 shadow tables are maintained by the virtual table
                    # itself; copying them directly corrupts the index.
                    continue
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    # The table already exists with a compatible schema.
                    pass
                src_cols = [
                    str(row[1])
                    for row in src.execute(f'PRAGMA table_info("{table}")')
                ]
                target_cols = {
                    str(row[1])
                    for row in conn.execute(f'PRAGMA table_info("{table}")')
                }
                common = [col for col in src_cols if col in target_cols]
                if not common:
                    continue
                col_list = ", ".join(f'"{col}"' for col in common)
                placeholders = ", ".join("?" for _ in common)
                rows = src.execute(
                    f'SELECT {col_list} FROM "{table}"'
                ).fetchall()
                if not rows:
                    continue
                if str(ddl).upper().startswith("CREATE VIRTUAL TABLE"):
                    key = common[0]
                    for (value,) in src.execute(
                        f'SELECT "{key}" FROM "{table}"'
                    ):
                        conn.execute(
                            f'DELETE FROM "{table}" WHERE "{key}" = ?',
                            (value,),
                        )
                    conn.executemany(
                        (
                            f'INSERT INTO "{table}" ({col_list}) '
                            f"VALUES ({placeholders})"
                        ),
                        rows,
                    )
                elif (
                    str(table) == "papers"
                    and "paper_id" in common
                    and "raw_json" in common
                ):
                    paper_id_idx = common.index("paper_id")
                    raw_json_idx = common.index("raw_json")
                    for row in rows:
                        values = list(row)
                        paper_id = str(values[paper_id_idx] or "")
                        if paper_id:
                            existing = conn.execute(
                                'SELECT raw_json FROM "papers" WHERE paper_id=?',
                                (paper_id,),
                            ).fetchone()
                            if existing is not None:
                                values[raw_json_idx] = _merge_paper_raw_json(
                                    existing[0],
                                    values[raw_json_idx],
                                )
                        conn.execute(
                            (
                                f'INSERT OR REPLACE INTO "{table}" ({col_list}) '
                                f"VALUES ({placeholders})"
                            ),
                            values,
                        )
                else:
                    conn.executemany(
                        (
                            f'INSERT OR REPLACE INTO "{table}" ({col_list}) '
                            f"VALUES ({placeholders})"
                        ),
                        rows,
                    )
        finally:
            src.close()


def _classify_chunk_ids(
    kb_sqlite: Path,
    chunk_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Split chunk IDs into new vs already-present against the shared KB."""

    ordered = list(dict.fromkeys(str(item) for item in chunk_ids if str(item)))
    existing: set[str] = set()
    if ordered and Path(kb_sqlite).is_file():
        uri = f"file:{Path(kb_sqlite).resolve().as_posix()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as conn:
                placeholders = ", ".join("?" for _ in ordered)
                rows = conn.execute(
                    (
                        "SELECT chunk_id FROM text_chunks "
                        f"WHERE chunk_id IN ({placeholders})"
                    ),
                    ordered,
                ).fetchall()
                existing = {str(row[0]) for row in rows}
        except sqlite3.OperationalError:
            existing = set()
    return (
        [chunk_id for chunk_id in ordered if chunk_id not in existing],
        [chunk_id for chunk_id in ordered if chunk_id in existing],
    )


logger = logging.getLogger(__name__)

# Backend-fix ticket 2.4: when roughly all committed visual rows point
# at files that do not exist, that is a pipeline defect (the be780761
# run shipped 260 candidates with 260 dangling paths) and must be loud,
# never silent.
_VISUAL_PATH_MISSING_ALARM_RATIO = 0.5
_VISUAL_PATH_ALARM_MIN_ROWS = 4


def _merge_tree_into(source: Path, target: Path) -> None:
    """Copy one worker-local artifact tree into the shared library."""

    source = Path(source)
    if not source.is_dir():
        return
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*"), key=lambda path: str(path)):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def _rewrite_json_strings(value: Any, prefix_pairs: list[tuple[str, str]]) -> Any:
    """Recursively remap temp-root prefixes inside parsed JSON values."""

    if isinstance(value, str):
        for old_prefix, new_prefix in prefix_pairs:
            if old_prefix and old_prefix in value:
                value = value.replace(old_prefix, new_prefix)
        return value
    if isinstance(value, list):
        return [_rewrite_json_strings(item, prefix_pairs) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_json_strings(item, prefix_pairs)
            for key, item in value.items()
        }
    return value


def _rewrite_staging_kb_paths(
    staging_kb: Path,
    prefix_pairs: list[tuple[str, str]],
) -> int:
    """Remap worker-temp absolute paths inside one staging KB (ticket 2.3).

    Must run before merge_kb_sqlite_into so merged rows and the relocated
    artifact files land in the shared library within the same commit;
    otherwise every merged path dangles once the temp root is removed.
    Returns the number of rewritten column values.
    """
    staging_kb = Path(staging_kb)
    if not staging_kb.is_file() or not prefix_pairs:
        return 0
    rewritten = 0
    conn = sqlite3.connect(str(staging_kb))
    try:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_config' "
                "AND name NOT LIKE '%_content' AND name NOT LIKE '%_data' "
                "AND name NOT LIKE '%_docsize' AND name NOT LIKE '%_idx'"
            ).fetchall()
        ]
        for table in tables:
            columns = [
                (str(row[1]), str(row[2] or '').upper())
                for row in conn.execute(f'PRAGMA table_info("{table}")')
            ]
            text_columns = [
                name for name, kind in columns if 'TEXT' in kind or not kind
            ]
            if not text_columns:
                continue
            col_list = ', '.join('"' + name + '"' for name in text_columns)
            try:
                rows = conn.execute(
                    'SELECT rowid, ' + col_list + ' FROM "' + table + '"'
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                rowid = row[0]
                updates: dict[str, Any] = {}
                for name, original in zip(text_columns, row[1:]):
                    if not isinstance(original, str) or not original:
                        continue
                    updated = original
                    for old_prefix, new_prefix in prefix_pairs:
                        if old_prefix and old_prefix in updated:
                            updated = updated.replace(old_prefix, new_prefix)
                    if name.lower().endswith('_json'):
                        try:
                            parsed = json.loads(updated)
                        except (ValueError, TypeError):
                            parsed = None
                        if isinstance(parsed, (dict, list)):
                            remapped = _rewrite_json_strings(
                                parsed, prefix_pairs
                            )
                            redumped = json.dumps(
                                remapped, ensure_ascii=False
                            )
                            if redumped != updated:
                                updated = redumped
                    if updated != original:
                        updates[name] = updated
                if updates:
                    set_clause = ', '.join(
                        '"' + name + '"=?' for name in updates
                    )
                    conn.execute(
                        'UPDATE "' + table + '" SET ' + set_clause
                        + ' WHERE rowid=?',
                        [*updates.values(), rowid],
                    )
                    rewritten += len(updates)
        conn.commit()
    finally:
        conn.close()
    return rewritten


def _safe_worker_name(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "paper")).strip("._")
    return (safe or "paper")[:64]


@dataclass(slots=True)
class FulltextEscalationDecision:
    paper_id: str
    should_download: bool
    reason: str
    priority: float
    desired_assets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class S2FulltextAcquisitionResult:
    selected_paper_ids: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    new_chunk_ids: list[str] = field(default_factory=list)
    reused_chunk_ids: list[str] = field(default_factory=list)
    new_paper_ids: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Visual procurement manifest
# ---------------------------------------------------------------------------

VISUAL_PROCUREMENT_SCHEMA_VERSION = "optomind.visual_procurement_manifest.v1"
_MAX_VISUAL_PROCUREMENT_PAPERS = 8


@dataclass(slots=True)
class VisualProcurementManifest:
    """Bounded final-reference visual procurement manifest.

    Fail-open contract: a missing or rejected image never removes text that
    the article authoring chain already bound to a claim.
    ``pending_review_chunk_ids`` lists every visual chunk written in this
    procurement run so callers can track provenance through the review queue.
    """

    schema_version: str = VISUAL_PROCUREMENT_SCHEMA_VERSION
    papers_checked: int = 0
    cached_paper_ids: list[str] = field(default_factory=list)
    missing_paper_ids: list[str] = field(default_factory=list)
    procured_paper_ids: list[str] = field(default_factory=list)
    skipped_paper_ids: list[str] = field(default_factory=list)
    pending_review_chunk_ids: list[str] = field(default_factory=list)
    procurement_stats: dict[str, Any] = field(default_factory=dict)
    policy_summary: dict[str, Any] = field(default_factory=dict)
    fail_open: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _paper_has_visual_assets(
    paper_id: str,
    visual_cache_sqlite: "Path | None",
) -> bool:
    """True when the central visual cache already has an asset for this paper.

    Checks both ``visual_chunks`` and ``visual_assets`` tables with read-only
    access; returns False on any error so the caller falls through to
    procurement.
    """
    if not visual_cache_sqlite or not Path(visual_cache_sqlite).is_file():
        return False
    try:
        uri = (
            f"file:{Path(visual_cache_sqlite).resolve().as_posix()}?mode=ro"
        )
        with sqlite3.connect(uri, uri=True) as conn:
            for table in ("visual_chunks", "visual_assets"):
                try:
                    row = conn.execute(
                        f'SELECT 1 FROM "{table}" WHERE paper_id=? LIMIT 1',
                        (str(paper_id),),
                    ).fetchone()
                    if row:
                        return True
                except sqlite3.OperationalError:
                    pass
    except Exception:
        pass
    return False


def build_visual_procurement_manifest(
    papers: "list[S2PaperRecord]",
    *,
    visual_cache_sqlite: "Path | None" = None,
    kb_sqlite: "Path",
    download_dir: "Path",
    max_papers: int = _MAX_VISUAL_PROCUREMENT_PAPERS,
    max_successes: int = 4,
    max_workers: "int | None" = None,
    source_task_id: str = "visual_procurement_manifest",
    role_labels: "Iterable[str]" = (),
) -> VisualProcurementManifest:
    """Build and run a bounded final-reference visual procurement manifest.

    Central visual cache is checked first; ``S2FulltextAcquirer`` with
    ``need_visual_assets=True`` is called only for papers absent from the
    cache.  Cross-paper acquisition runs in parallel; the per-paper OA
    waterfall remains serial.  Worker-local SQLite/visual trees are merged
    via one deterministic main-thread writer (existing acquirer contract).

    Priority is the adaptive score from :func:`decide_fulltext_escalation`
    (high_value_role × 2.0 + need_visual_assets × 1.5 + citation_count/100)
    with no fixed source-image quota.

    The returned manifest is always fail-open: ``pending_review_chunk_ids``
    lists every new visual chunk for downstream traceable review, and no
    outcome here ever removes bound text.
    """
    kb_sqlite = Path(kb_sqlite)
    download_dir = Path(download_dir)
    manifest = VisualProcurementManifest(papers_checked=len(papers))
    if not papers:
        manifest.policy_summary = {"reason": "no_papers_supplied"}
        return manifest

    # 1. Central visual cache first: skip papers that already have assets.
    missing: list[S2PaperRecord] = []
    for paper in papers:
        if _paper_has_visual_assets(paper.paper_id, visual_cache_sqlite):
            manifest.cached_paper_ids.append(paper.paper_id)
        else:
            missing.append(paper)
    manifest.missing_paper_ids = [p.paper_id for p in missing]

    if not missing:
        manifest.policy_summary = {
            "reason": "all_papers_have_cached_visual_assets",
            "cached_count": len(manifest.cached_paper_ids),
        }
        return manifest

    # 2. Build adaptive-priority escalation decisions (need_visual_assets=True).
    selections: list[tuple[S2PaperRecord, FulltextEscalationDecision]] = []
    for paper in missing:
        decision = decide_fulltext_escalation(
            paper,
            role_labels=role_labels,
            need_visual_assets=True,
        )
        selections.append((paper, decision))

    # Sort by adaptive priority, but filter identity-bearing public routes
    # before applying the procurement cap. Otherwise a batch of title-only
    # records can consume every slot and prevent later DOI/OA candidates from
    # being tried, even though the latter are the only records that can yield
    # traceable source visuals.
    selections.sort(key=lambda item: item[1].priority, reverse=True)
    not_downloadable = [
        (paper, decision)
        for paper, decision in selections
        if not decision.should_download
    ]
    downloadable_candidates = [
        (paper, decision)
        for paper, decision in selections
        if decision.should_download
    ]
    cap = max(1, min(int(max_papers), len(downloadable_candidates)))
    downloadable = downloadable_candidates[:cap]
    over_cap = downloadable_candidates[cap:]
    manifest.skipped_paper_ids.extend(
        paper.paper_id for paper, _ in over_cap
    )
    manifest.skipped_paper_ids.extend(
        paper.paper_id for paper, _ in not_downloadable
    )

    if not downloadable:
        manifest.policy_summary = {
            "reason": "no_papers_with_oa_route",
            "missing_count": len(missing),
            "not_downloadable": len(not_downloadable),
            "fail_open": True,
        }
        return manifest

    # 3. Acquire: cross-paper parallel, single-paper route serial.
    #    Worker-local SQLite/visual trees; one-writer merge via acquirer.
    acquirer = S2FulltextAcquirer(
        kb_sqlite=kb_sqlite,
        download_dir=download_dir,
        max_workers=max_workers,
    )
    result = acquirer.acquire(
        downloadable,
        max_successes=max(1, min(int(max_successes), len(downloadable))),
        source_task_id=source_task_id,
    )

    # 4. Collect procured papers and pending_review visual chunk IDs.
    manifest.procured_paper_ids = list(result.new_paper_ids)
    # New chunks include visual candidates in pending_review state.
    manifest.pending_review_chunk_ids = list(result.new_chunk_ids)
    manifest.procurement_stats = dict(result.stats)
    manifest.policy_summary = {
        "reason": "visual_procurement_complete",
        "missing_count": len(missing),
        "downloadable_count": len(downloadable),
        "procured_count": len(manifest.procured_paper_ids),
        "pending_review_chunk_count": len(
            manifest.pending_review_chunk_ids
        ),
        "visual_candidate_count": int(
            (result.stats or {}).get("visual_candidate_count", 0)
        ),
        "body_visual_policy": (
            "0-2_per_body_section_abstract_conclusion_zero"
            "_introduction_cap"
        ),
        "fail_open": True,
        "pending_review_candidates_traceable": True,
    }
    return manifest


def decide_fulltext_escalation(
    paper: S2PaperRecord,
    *,
    role_labels: Iterable[str] = (),
    need_visual_assets: bool = False,
    need_complete_context: bool = False,
) -> FulltextEscalationDecision:
    roles = {str(role).casefold() for role in role_labels}
    high_value_role = bool(
        roles
        & {
            "foundation",
            "landmark",
            "mechanism",
            "method",
            "controversy",
            "frontier",
            "review",
        }
    )
    oa_url = paper.s2_open_access_candidate_url
    # Visual-only procurement needs an identity-bearing public route.  The
    # complete-context waterfall retains the historical title-only fallback;
    # a title by itself is not enough to claim an OA visual route.
    has_public_route = bool(
        oa_url
        or paper.doi
        or paper.external_ids
        or (paper.title and need_complete_context)
    )
    should_download = bool(
        has_public_route
        and (need_visual_assets or need_complete_context or high_value_role)
    )
    reason = "s2_chunk_is_sufficient"
    if not has_public_route:
        reason = (
            "non_oa_and_no_public_url_or_doi_for_oa_resolution"
            if paper.is_oa is False
            else "no_public_url_or_doi_for_oa_resolution"
        )
    elif not oa_url and paper.doi:
        reason = "doi_available_for_public_oa_resolution"
    elif not oa_url:
        reason = "metadata_available_for_public_oa_resolution"
    elif should_download:
        reason = "high_value_full_context_or_visual_assets_required"
    priority = (
        (2.0 if high_value_role else 0.0)
        + (1.5 if need_visual_assets else 0.0)
        + (1.0 if need_complete_context else 0.0)
        + min(1.0, paper.influential_citation_count / 100.0)
    )
    desired: list[str] = []
    if need_complete_context:
        desired.append("complete_text")
    if need_visual_assets:
        desired.append("visual_assets")
    return FulltextEscalationDecision(
        paper_id=paper.paper_id,
        should_download=should_download,
        reason=reason,
        priority=round(priority, 4),
        desired_assets=desired,
    )


@lru_cache(maxsize=2048)
def _unpaywall_routes(doi: str) -> tuple[str, ...]:
    if not doi:
        return ()
    try:
        from tools.academic_backends.unpaywall_backend import UnpaywallBackend

        info = UnpaywallBackend().lookup(doi) or {}
    except Exception:
        return ()
    urls: list[str] = [str(info.get("best_oa_url") or "")]
    for location in info.get("oa_locations") or []:
        if isinstance(location, dict):
            urls.extend(
                [
                    str(location.get("url_for_pdf") or ""),
                    str(location.get("url") or ""),
                ]
            )
    return tuple(dict.fromkeys(url for url in urls if url.startswith("http")))


def _clean_urls(values: Iterable[Any]) -> list[str]:
    urls: list[str] = []
    for value in values:
        url = str(value or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        if url.startswith("http://arxiv.org/"):
            url = "https://arxiv.org/" + url.removeprefix("http://arxiv.org/")
        if url not in urls:
            urls.append(url)
    return urls


def _nested_public_urls(value: Any, *, parent_key: str = "") -> list[str]:
    """Collect explicit content/PDF/OA URLs, excluding generic metadata links."""

    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).casefold()
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                route_key = f"{parent_key} {key_l}"
                if any(cue in route_key for cue in ("pdf", "fulltext", "content", "open_access", "oa_url", "repository")):
                    out.append(item)
            elif isinstance(item, (dict, list, tuple)):
                out.extend(_nested_public_urls(item, parent_key=key_l))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_nested_public_urls(item, parent_key=parent_key))
    return _clean_urls(out)


def _external_id(paper: S2PaperRecord, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for key, value in (paper.external_ids or {}).items():
        if str(key).casefold() in wanted and str(value or "").strip():
            return str(value).strip()
    return ""


def _direct_public_routes(paper: S2PaperRecord) -> list[str]:
    urls: list[str] = [paper.s2_open_access_candidate_url]
    arxiv_id = _external_id(paper, "arxiv", "arxiv_id")
    if arxiv_id:
        clean_id = re.sub(r"^(?:arxiv:)?\s*", "", arxiv_id, flags=re.I)
        urls.append(f"https://arxiv.org/pdf/{clean_id}.pdf")
    pmcid = _external_id(paper, "pmcid", "pubmedcentral", "pmc")
    if pmcid:
        clean_pmcid = pmcid.upper()
        if not clean_pmcid.startswith("PMC"):
            clean_pmcid = "PMC" + clean_pmcid
        urls.append(
            f"https://pmc.ncbi.nlm.nih.gov/articles/{clean_pmcid}/?report=xml"
        )
    urls.extend(_nested_public_urls(paper.raw_metadata))
    return _clean_urls(urls)


def _years_compatible(expected: int | None, observed: Any, *, tolerance: int = 1) -> bool:
    if not expected or not observed:
        return True
    try:
        return abs(int(expected) - int(observed)) <= tolerance
    except (TypeError, ValueError):
        return True


def _strict_identity_match(
    paper: S2PaperRecord, record: dict[str, Any], *, doi_match: bool = False
) -> bool:
    if doi_match and normalize_doi(record.get("doi")) == normalize_doi(paper.doi):
        return _years_compatible(paper.year, record.get("year"), tolerance=2)
    return bool(
        title_match_score(paper.title, record.get("title")) >= 0.92
        and _years_compatible(paper.year, record.get("year"), tolerance=2)
    )


def _clean_verified_abstract(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split()).strip()
    if len(text) < 40:
        return ""
    alpha_ratio = sum(char.isalpha() for char in text) / max(1, len(text))
    return text if alpha_ratio >= 0.35 else ""


def _enrich_verified_abstract(
    paper: S2PaperRecord,
    record: dict[str, Any],
    *,
    provider: str,
) -> bool:
    """Keep a resolver abstract only after that resolver passed identity checks."""

    if str(paper.abstract or "").strip():
        return False
    abstract = _clean_verified_abstract(
        record.get("abstract_or_snippet") or record.get("abstract")
    )
    if not abstract:
        return False
    paper.abstract = abstract
    if provider not in paper.sources:
        paper.sources.append(provider)
    paper.route_events.append(
        {
            "event": "abstract_enriched_from_verified_public_metadata",
            "provider": provider,
            "identity_check": "doi_or_strict_title_and_year_match",
            "content_depth": "abstract",
        }
    )
    return True


def _record_public_routes(record: dict[str, Any]) -> list[str]:
    content_urls = record.get("content_urls") or {}
    values: list[Any] = []
    if isinstance(content_urls, dict):
        for key in ("grobid_xml", "tei_xml", "jats_xml", "pdf"):
            values.append(content_urls.get(key))
        values.extend(content_urls.values())
    values.extend(
        [
            record.get("pdf_url"),
            record.get("url_for_pdf"),
            record.get("open_access_url"),
            record.get("best_oa_url"),
        ]
    )
    for location in record.get("oa_locations") or []:
        if isinstance(location, dict):
            values.extend([location.get("url_for_pdf"), location.get("url")])
    if bool(record.get("is_oa")):
        values.extend([record.get("source_url"), record.get("url_or_doi")])
    return _clean_urls(values)


def _resolve_unpaywall_wave(doi: str) -> tuple[list[str], str]:
    if not doi:
        return [], "resolver_no_route"
    try:
        from tools.academic_backends.unpaywall_backend import UnpaywallBackend

        backend = UnpaywallBackend()
        info = backend.lookup(doi) or {}
    except Exception:
        return [], "resolver_error"
    urls = _record_public_routes(info)
    if not urls:
        return [], "resolver_error" if getattr(backend, "last_error", "") else "resolver_no_route"
    return urls, "resolved"


def _resolve_openalex_wave(
    paper: S2PaperRecord, *, doi: str = ""
) -> tuple[list[str], str, dict[str, Any]]:
    try:
        from tools.academic_backends.openalex_backend import OpenAlexBackend

        backend = OpenAlexBackend()
        if doi:
            record = backend.get_work(doi) or {}
            matches = bool(record) and _strict_identity_match(paper, record, doi_match=True)
        else:
            rows = backend.search(paper.title, max_results=5) if paper.title else []
            record = next(
                (row for row in rows if _strict_identity_match(paper, row)), {}
            )
            matches = bool(record)
    except Exception:
        return [], "resolver_error", {}
    if not record:
        status = "resolver_error" if getattr(backend, "last_error", "") else "resolver_no_route"
        return [], status, {}
    if not matches:
        return [], "identity_mismatch", {}
    urls = _record_public_routes(record)
    return urls, ("resolved" if urls else "resolver_no_route"), record


def _resolve_crossref_wave(
    paper: S2PaperRecord,
) -> tuple[list[str], str, dict[str, Any]]:
    if not paper.title:
        return [], "resolver_no_route", {}
    try:
        from tools.academic_backends.crossref_backend import CrossrefBackend

        rows = CrossrefBackend().search(paper.title, max_results=5)
    except Exception:
        return [], "resolver_error", {}
    if not rows:
        return [], "resolver_no_route", {}
    record = next((row for row in rows if _strict_identity_match(paper, row)), {})
    if not record:
        return [], "identity_mismatch", {}
    doi = normalize_doi(record.get("doi"))
    # Crossref links are not themselves proof of open access.  This wave only
    # recovers a verified DOI, which is then passed to Unpaywall/OpenAlex.
    status = "resolved" if doi else "resolver_no_route"
    return [], status, record


def _resolve_core_wave(
    paper: S2PaperRecord,
) -> tuple[list[str], str, dict[str, Any]]:
    """Resolve a title/DOI through CORE's OA repository index when configured."""

    if not paper.title:
        return [], "resolver_no_route", {}
    try:
        from tools.academic_backends.core_backend import CoreBackend

        backend = CoreBackend()
        rows = backend.search(paper.title, max_results=5)
    except Exception:
        return [], "resolver_error", {}
    if not rows:
        return [], "resolver_no_route", {}
    record = next(
        (
            row
            for row in rows
            if _strict_identity_match(
                paper,
                row,
                doi_match=bool(
                    paper.doi
                    and normalize_doi(row.get("doi"))
                    == normalize_doi(paper.doi)
                ),
            )
        ),
        {},
    )
    if not record:
        return [], "identity_mismatch", {}
    urls = _clean_urls(
        [
            record.get("url_or_doi"),
            record.get("source_url"),
            *(_nested_public_urls(record.get("raw_metadata") or {})),
        ]
    )
    return urls, ("resolved" if urls else "resolver_no_route"), record


def _resolve_arxiv_wave(
    paper: S2PaperRecord,
) -> tuple[list[str], str, dict[str, Any]]:
    if not paper.title:
        return [], "resolver_no_route", {}
    try:
        from tools.academic_backends.arxiv_backend import ArxivBackend

        rows = ArxivBackend().search(paper.title, max_results=5)
    except Exception:
        return [], "resolver_error", {}
    if not rows:
        return [], "resolver_no_route", {}
    record = next((row for row in rows if _strict_identity_match(paper, row)), {})
    if not record:
        return [], "identity_mismatch", {}
    urls = _record_public_routes({**record, "is_oa": True})
    return urls, ("resolved" if urls else "resolver_no_route"), record


def _candidate_from_s2(
    paper: S2PaperRecord,
    *,
    enrich_oa_routes: bool = False,
    route_urls: Iterable[str] | None = None,
    resolved_doi: str = "",
    materialization_route: str = "s2_direct_public_route",
) -> dict[str, Any]:
    direct_urls = _clean_urls(
        list(route_urls) if route_urls is not None else _direct_public_routes(paper)
    )
    if enrich_oa_routes:
        direct_urls.extend(
            url for url in _unpaywall_routes(paper.doi) if url not in direct_urls
        )
    oa_url = direct_urls[0] if direct_urls else ""
    return {
        "candidate_id": f"s2:{paper.paper_id}",
        "paper_id": paper.paper_id,
        "corpus_id": paper.corpus_id,
        "doi": normalize_doi(resolved_doi or paper.doi),
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "abstract": paper.abstract,
        "tldr": paper.tldr,
        "is_oa": paper.is_oa,
        "pdf_url": oa_url,
        "open_access_url": oa_url,
        "source_url": oa_url,
        "alternate_urls": direct_urls[1:],
        "backends": ["semantic_scholar"],
        "relevance_tier": "s2_high_value",
        "llm_scope_fit": "in_domain",
        "llm_retrieval_role": "evidence_candidate",
        "llm_relevance_grade": "direct",
        "scope_audit_status": "s2_deterministic_selection",
        "discovery_route": paper.discovery_route or "semantic_scholar_graph",
        "materialization_route": materialization_route,
        # S2 metadata/abstract is already stored by the S2 KB bridge.  A
        # failed full-text escalation must not masquerade as a successful
        # upgrade by writing another abstract-only chunk.
        "skip_abstract_fallback": True,
        "raw_metadata": paper.raw_metadata,
    }


_REFERENCE_CUE = re.compile(
    r"\b(?:doi:|https?://|et al\.|journal|proceedings|arxiv)\b", re.IGNORECASE
)
_NUMBERED_REFERENCE_CUE = re.compile(
    r"(?:^|\s)[\[(]\d{1,4}[\])]\s*", re.IGNORECASE
)
_NUMBERED_AUTHOR_REFERENCE_CUE = re.compile(
    r"(?:^|\s)[\[(]\d{1,4}[\])]\s*"
    r"[A-Z][A-Za-z'\-]{1,30},\s*"
    r"(?:[A-Z]\.(?:[-\s]*[A-Z]\.)?|[A-Z][a-z]{1,30})",
)
_WEB_NAVIGATION_CUES = (
    "skip to main content",
    "an official website of the united states government",
    "official websites use .gov",
    "secure .gov websites use https",
    "share sensitive information only on official",
    "search login",
    "search log in",
)
_PUBLISHER_HEADER_CUES = (
    "view online",
    "export citation",
    "articles you may be interested in",
    "article content",
)


def _non_body_reason(text: str) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) < 120:
        return "short_or_footer"
    lower = compact.casefold()
    if sum(cue in lower for cue in _WEB_NAVIGATION_CUES) >= 2:
        return "web_navigation"
    if sum(cue in lower for cue in _PUBLISHER_HEADER_CUES) >= 2:
        return "publisher_header"
    if lower.startswith(("references ", "bibliography ")):
        return "bibliography"
    numbered_count = len(_NUMBERED_REFERENCE_CUE.findall(compact))
    numbered_author_count = len(_NUMBERED_AUTHOR_REFERENCE_CUE.findall(compact))
    year_count = len(re.findall(r"\b(?:19|20)\d{2}\b", compact))
    cue_count = len(_REFERENCE_CUE.findall(compact))
    # Parsed PDFs and repository HTML often omit the "References" heading.
    # Dense runs of numbered entries plus publication years are a much more
    # stable signal than journal-name allowlists across disciplines.
    if numbered_author_count >= 2 and year_count >= 2:
        return "bibliography"
    if numbered_author_count >= 1 and numbered_count >= 2 and year_count >= 2 and cue_count >= 1:
        return "bibliography"
    return ""


def _looks_like_reference_or_footer(text: str) -> bool:
    return bool(_non_body_reason(text))


def _quarantine_non_body_chunks(
    kb_sqlite: Path, chunk_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Remove boilerplate and bibliography tails from a new full-text batch."""

    if not chunk_ids or not kb_sqlite.exists():
        return chunk_ids, []
    conn = sqlite3.connect(str(kb_sqlite))
    try:
        with conn:
            rows: list[tuple[str, str, int, str]] = []
            for chunk_id in chunk_ids:
                row = conn.execute(
                    "SELECT paper_id,ordinal,text FROM text_chunks WHERE chunk_id=?",
                    (chunk_id,),
                ).fetchone()
                if row:
                    rows.append(
                        (
                            chunk_id,
                            str(row[0] or ""),
                            int(row[1] if row[1] is not None else -1),
                            str(row[2] or ""),
                        )
                    )

            bibliography_start: dict[str, int] = {}
            direct_reasons: dict[str, str] = {}
            for chunk_id, paper_id, ordinal, text in rows:
                reason = _non_body_reason(text)
                if reason:
                    direct_reasons[chunk_id] = reason
                if reason == "bibliography" and ordinal >= 0:
                    bibliography_start[paper_id] = min(
                        ordinal, bibliography_start.get(paper_id, ordinal)
                    )

            removed_set: set[str] = set()
            for chunk_id, paper_id, ordinal, _text in rows:
                is_bibliography_tail = (
                    paper_id in bibliography_start
                    and ordinal >= bibliography_start[paper_id]
                )
                if chunk_id in direct_reasons or is_bibliography_tail:
                    conn.execute(
                        "DELETE FROM text_chunk_fts WHERE chunk_id=?", (chunk_id,)
                    )
                    conn.execute("DELETE FROM text_chunks WHERE chunk_id=?", (chunk_id,))
                    removed_set.add(chunk_id)
            kept = [chunk_id for chunk_id in chunk_ids if chunk_id not in removed_set]
            removed = [chunk_id for chunk_id in chunk_ids if chunk_id in removed_set]
        return kept, removed
    finally:
        conn.close()


class S2FulltextAcquirer:
    """Escalate selected S2 OA papers through the existing legal OA waterfall."""

    def __init__(
        self,
        *,
        kb_sqlite: str | Path,
        download_dir: str | Path,
        max_workers: int | None = None,
    ) -> None:
        self.kb_sqlite = Path(kb_sqlite)
        self.download_dir = Path(download_dir)
        self.max_workers = resolve_oa_worker_count(max_workers)

    def _locate_local_source(
        self,
        paper: S2PaperRecord,
        *,
        download_dir: Path | None = None,
    ) -> Path | None:
        """Locate the file written by the OA ingester for this paper."""
        slug = doi_to_slug(paper.doi)
        search_root = download_dir or self.download_dir
        candidates: list[Path] = []
        if slug:
            for suffix in (".pdf", ".html", ".htm"):
                candidates.extend(search_root.rglob(f"{slug}{suffix}"))
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime_ns)

    def _extract_visuals_after_text_success(
        self,
        paper: S2PaperRecord,
        *,
        source_task_id: str,
        download_dir: Path | None = None,
        output_dir: Path | None = None,
        staging_kb: Path | None = None,
    ) -> dict[str, Any]:
        """Extract pending figure candidates without making text ingest fragile."""
        source_path = self._locate_local_source(
            paper, download_dir=download_dir
        )
        if source_path is None:
            return {
                "status": "source_not_located",
                "reason": "text_ingest_succeeded_but_downloaded_source_path_was_not_found",
            }
        try:
            from optomind_research.runtime.supplemental_visual_ingest import (
                extract_visual_candidates,
            )

            report = extract_visual_candidates(
                source_path=source_path,
                staging_kb=staging_kb or self.kb_sqlite,
                output_dir=(
                    output_dir
                    or self.download_dir.parent / "visual_candidates"
                ),
                paper_id=paper.paper_id,
                doi=paper.doi,
                title=paper.title,
            )
            return {
                **dict(report or {}),
                "source_task_id": source_task_id,
                "source_path": str(source_path),
            }
        except Exception as exc:
            return {
                "status": "extractor_failed_nonblocking",
                "source_path": str(source_path),
                "source_task_id": source_task_id,
                "extraction_errors": [f"{type(exc).__name__}: {str(exc)[:240]}"],
            }

    def acquire(
        self,
        selections: list[
            tuple[S2PaperRecord, FulltextEscalationDecision]
        ],
        *,
        max_successes: int = 1,
        source_task_id: str = "s2_fulltext_escalation",
        max_workers: int | None = None,
    ) -> S2FulltextAcquisitionResult:
        """Acquire OA full text with bounded cross-paper parallelism.

        Per-paper route order, text-before-visual order, max_successes,
        cache reuse and report order are preserved.  With more than one
        worker, papers are downloaded/parsed into per-paper worker
        directories and every shared SQLite/library write happens in one
        deterministic main-thread commit pass; max_workers=1 (or the
        OPTOMIND_OA_FULLTEXT_WORKERS env var set to 1) keeps the historical
        fully serial path.
        """

        selected = [
            (paper, decision)
            for paper, decision in selections
            if decision.should_download
        ]
        selected.sort(key=lambda item: item[1].priority, reverse=True)
        result = S2FulltextAcquisitionResult(
            selected_paper_ids=[paper.paper_id for paper, _ in selected],
            skipped=[
                {"paper_id": paper.paper_id, "reason": decision.reason}
                for paper, decision in selections
                if not decision.should_download
            ],
        )
        if not selected or max_successes <= 0:
            result.stats = {"attempted": 0, "downloaded": 0, "paper_outcomes": []}
            return result
        worker_count = resolve_oa_worker_count(
            max_workers if max_workers is not None else self.max_workers
        )
        if worker_count <= 1 or len(selected) <= 1:
            return self._acquire_serial(
                selections,
                max_successes=max_successes,
                source_task_id=source_task_id,
            )
        return self._acquire_parallel(
            selected,
            result,
            max_successes=max_successes,
            source_task_id=source_task_id,
            worker_count=worker_count,
        )

    @staticmethod
    def _empty_aggregate_stats() -> dict[str, Any]:
        return {
            "attempted": 0,
            "downloaded": 0,
            "parse_failed": 0,
            "resolver_waves": 0,
            "abstracts_enriched": 0,
            "abstract_enrichment_sources": {},
            "paper_outcomes": [],
            "successful_papers": 0,
            "success_budget_skipped_attempts": 0,
        }

    @staticmethod
    def _finalize_stats(aggregate_stats: dict[str, Any]) -> dict[str, Any]:
        outcomes = aggregate_stats["paper_outcomes"]
        return {
            **aggregate_stats,
            "visual_candidate_count": sum(
                int(outcome.get("visual_candidate_count") or 0)
                for outcome in outcomes
            ),
            "visual_ingest_status_counts": {
                status: sum(
                    1
                    for outcome in outcomes
                    if str(
                        (outcome.get("visual_ingest") or {}).get("status") or ""
                    )
                    == status
                )
                for status in sorted({
                    str((outcome.get("visual_ingest") or {}).get("status") or "")
                    for outcome in outcomes
                    if str(
                        (outcome.get("visual_ingest") or {}).get("status") or ""
                    )
                })
            },
            "non_body_chunks_quarantined": sum(
                len(wave.get("quarantined_chunk_ids") or [])
                for outcome in outcomes
                for wave in outcome.get("waves") or []
            ),
        }

    def _acquire_parallel(
        self,
        selected: list[tuple[S2PaperRecord, FulltextEscalationDecision]],
        result: S2FulltextAcquisitionResult,
        *,
        max_successes: int,
        source_task_id: str,
        worker_count: int,
    ) -> S2FulltextAcquisitionResult:
        max_budget = max(0, int(max_successes))
        aggregate_stats = self._empty_aggregate_stats()
        temp_parent = (
            self.download_dir.parent
            if self.download_dir
            else Path(tempfile.gettempdir())
        )
        temp_parent.mkdir(parents=True, exist_ok=True)
        payload_by_paper: dict[str, dict[str, Any]] = {}
        tmp_root_path = (
            Path(temp_parent)
            / f"s2_fulltext_workers_{uuid.uuid4().hex[:8]}"
        )
        tmp_root_path.mkdir(parents=True, exist_ok=True)
        try:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="s2-fulltext",
            ) as pool:
                pending_index = 0
                futures: dict[Future, S2PaperRecord] = {}
                completed_successes = 0

                def submit_next() -> bool:
                    nonlocal pending_index, completed_successes
                    while (
                        pending_index < len(selected)
                        and len(futures) < worker_count
                    ):
                        # Treat every in-flight paper as a potential success
                        # slot.  This preserves the serial max_successes
                        # contract for external OA attempts: failures free a
                        # slot, but concurrent successes can never overshoot
                        # the requested success budget.
                        if completed_successes + len(futures) >= max_budget:
                            return False
                        paper, _decision = selected[pending_index]
                        future = pool.submit(
                            self._acquire_paper_worker,
                            paper,
                            source_task_id=source_task_id,
                            temp_root=tmp_root_path,
                        )
                        futures[future] = paper
                        pending_index += 1
                    return True

                submit_next()
                while futures:
                    done, _pending = wait(
                        futures, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        paper = futures.pop(future)
                        try:
                            payload = future.result()
                        except Exception as exc:
                            payload = {
                                "paper_id": paper.paper_id,
                                "title": paper.title,
                                "doi": paper.doi,
                                "status": "oa_worker_failed",
                                "worker_error": (
                                    f"{type(exc).__name__}: {str(exc)[:240]}"
                                ),
                                "materialized_chunk_count": 0,
                            }
                        payload_by_paper[paper.paper_id] = payload
                        if payload.get("materialized_chunk_count", 0) > 0:
                            completed_successes += 1
                    submit_next()

            # One deterministic main-thread commit pass in priority order.
            # Workers only ever write per-paper temporary SQLite/download
            # directories; every shared SQLite/library write happens here.
            committed_successes = 0
            for paper, _decision in selected:
                payload = payload_by_paper.get(paper.paper_id)
                if payload is None:
                    result.skipped.append(
                        {
                            "paper_id": paper.paper_id,
                            "reason": "oa_success_budget_reached",
                        }
                    )
                    continue
                if committed_successes >= max_budget:
                    result.skipped.append(
                        {
                            "paper_id": paper.paper_id,
                            "reason": "oa_success_budget_reached",
                        }
                    )
                    if payload.get("materialized_chunk_count", 0) > 0:
                        aggregate_stats["success_budget_skipped_attempts"] += 1
                    continue
                if payload.get("worker_error"):
                    outcome = {
                        "paper_id": paper.paper_id,
                        "title": paper.title,
                        "doi": paper.doi,
                        "status": "oa_worker_failed",
                        "success_wave": "",
                        "route_count": 0,
                        "new_chunk_count": 0,
                        "reused_chunk_count": 0,
                        "materialized_chunk_count": 0,
                        "abstract_enriched_source": "",
                        "visual_ingest": {},
                        "visual_candidate_count": 0,
                        "waves": [],
                        "error": str(
                            payload.get("worker_error") or "worker_failed"
                        ),
                    }
                    aggregate_stats["paper_outcomes"].append(outcome)
                    continue
                _outcome, materialized = self._commit_paper_outcome(
                    payload,
                    aggregate_stats=aggregate_stats,
                    result=result,
                    commit_files=True,
                    temp_paths=payload.get("temp_paths") or {},
                )
                if materialized:
                    committed_successes += 1
                    aggregate_stats["successful_papers"] += 1
                aggregate_stats["paper_outcomes"].append(_outcome)
        finally:
            shutil.rmtree(tmp_root_path, ignore_errors=True)
        result.stats = self._finalize_stats(aggregate_stats)
        return result

    def _commit_paper_outcome(
        self,
        payload: dict[str, Any],
        *,
        aggregate_stats: dict[str, Any],
        result: S2FulltextAcquisitionResult,
        commit_files: bool,
        temp_paths: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        new_chunk_ids = list(payload.get("new_chunk_ids") or [])
        reused_chunk_ids = list(payload.get("reused_chunk_ids") or [])
        new_paper_ids = list(payload.get("new_paper_ids") or [])
        materialized = payload.get("materialized_chunk_count", 0) > 0
        temp_paths = temp_paths or {}
        if commit_files and materialized:
            temp_kb = Path(str(temp_paths.get("kb") or ""))
            temp_download = Path(str(temp_paths.get("download_dir") or ""))
            temp_visual = Path(str(temp_paths.get("visual_dir") or ""))
            # Cache reuse is decided against the real shared KB, exactly as
            # the serial path decides it, before the worker-local DB is
            # merged by the single main-thread writer.
            new_chunk_ids, reused_chunk_ids = _classify_chunk_ids(
                self.kb_sqlite,
                [*new_chunk_ids, *reused_chunk_ids],
            )
            # Ticket 2.3: files move first, then the staging-KB paths are
            # remapped onto their shared locations, then the rows merge --
            # all inside this single commit pass.  The previous order merged
            # rows untouched and moved files afterwards, so every committed
            # local_image_path dangled as soon as the temp root was removed
            # (the be780761 run: 260 candidates / 260 dangling paths).
            shared_visual_dir = (
                self.download_dir.parent / "visual_candidates"
            )
            if temp_download.is_dir():
                _merge_tree_into(temp_download, self.download_dir)
            if temp_visual.is_dir():
                _merge_tree_into(temp_visual, shared_visual_dir)
            prefix_pairs: list[tuple[str, str]] = []
            if temp_download.is_dir():
                prefix_pairs.append(
                    (str(temp_download), str(self.download_dir))
                )
            if temp_visual.is_dir():
                prefix_pairs.append((str(temp_visual), str(shared_visual_dir)))
            if temp_kb.is_file() and prefix_pairs:
                _rewrite_staging_kb_paths(temp_kb, prefix_pairs)
            if temp_kb.is_file():
                merge_kb_sqlite_into(self.kb_sqlite, temp_kb)
        result.new_chunk_ids.extend(new_chunk_ids)
        result.reused_chunk_ids.extend(reused_chunk_ids)
        result.new_paper_ids.extend(new_paper_ids)
        ingest_stats = payload.get("ingest_stats") or {}
        aggregate_stats["attempted"] += int(
            ingest_stats.get("attempted", 0) or 0
        )
        aggregate_stats["downloaded"] += int(
            ingest_stats.get("downloaded", 0) or 0
        )
        aggregate_stats["parse_failed"] += int(
            ingest_stats.get("parse_failed", 0) or 0
        )
        aggregate_stats["resolver_waves"] += int(
            payload.get("resolver_waves", 0) or 0
        )
        enriched = str(payload.get("abstract_enriched_source") or "")
        visual_path_integrity: dict[str, Any] = {}
        if commit_files and materialized:
            # The per-paper worker directory is handed to the verifier so a
            # committed row that still points inside the doomed staging tree
            # is counted now, rather than becoming invisible breakage after
            # the temp root is removed.
            staging_root: Path | None = None
            staged_visual = Path(str(temp_paths.get("visual_dir") or ""))
            if str(staged_visual) and staged_visual.parent.is_dir():
                staging_root = staged_visual.parent
            visual_path_integrity = self._verify_committed_visual_paths(
                str(payload.get("paper_id") or ""),
                staging_root=staging_root,
            )
            checked_rows = int(visual_path_integrity.get("checked") or 0)
            unresolvable_ratio = float(
                visual_path_integrity.get("unresolvable_ratio")
                or visual_path_integrity.get("missing_ratio")
                or 0.0
            )
            if (
                checked_rows >= _VISUAL_PATH_ALARM_MIN_ROWS
                and unresolvable_ratio >= _VISUAL_PATH_MISSING_ALARM_RATIO
            ):
                logger.error(
                    "visual path integrity failure for paper %s: %s missing "
                    "and %s still inside the worker staging tree, of %s "
                    "committed visual rows",
                    payload.get("paper_id"),
                    visual_path_integrity.get("missing"),
                    visual_path_integrity.get("staged"),
                    checked_rows,
                )
        if enriched:
            aggregate_stats["abstracts_enriched"] += 1
            sources = aggregate_stats["abstract_enrichment_sources"]
            sources[enriched] = int(sources.get(enriched, 0) or 0) + 1
        outcome = {
            "paper_id": payload.get("paper_id", ""),
            "title": payload.get("title", ""),
            "doi": payload.get("doi", ""),
            "status": payload.get("status", "oa_worker_failed"),
            "success_wave": payload.get("success_wave", ""),
            "route_count": payload.get("route_count", 0),
            "new_chunk_count": len(new_chunk_ids),
            "reused_chunk_count": len(reused_chunk_ids),
            "materialized_chunk_count": payload.get(
                "materialized_chunk_count", 0
            ),
            "abstract_enriched_source": enriched,
            "visual_ingest": payload.get("visual_report") or {},
            "visual_candidate_count": payload.get(
                "visual_candidate_count", 0
            ),
            "visual_path_integrity": visual_path_integrity,
            "waves": payload.get("waves") or [],
        }
        return outcome, materialized

    def _verify_committed_visual_paths(
        self,
        paper_id: str,
        *,
        staging_root: Path | None = None,
    ) -> dict[str, Any]:
        """Check committed visual rows for dangling image paths (ticket 2.4).

        Read-only over the shared KB; the result is embedded in the paper
        outcome so a near-100% missing ratio is visible in the acquisition
        report instead of only in a quality_audit JSON nobody reads.

        `is_file()` alone cannot see the failure this check exists to catch.
        In parallel mode the check runs inside the commit pass, while the
        worker temp root is still on disk -- a row that leaked a staging path
        therefore resolves here and only starts dangling minutes later, when
        _acquire_parallel removes the temp root.  That blind spot is why the
        be780761 run reported missing_ratio 0.0 for every paper and still
        shipped 260 dangling candidates.  Passing `staging_root` makes the
        doomed-tree case countable at commit time.
        """

        empty: dict[str, Any] = {
            "checked": 0,
            "missing": 0,
            "staged": 0,
            "missing_ratio": 0.0,
            "unresolvable_ratio": 0.0,
        }
        if not paper_id:
            return empty
        try:
            conn = sqlite3.connect(
                f"file:{Path(self.kb_sqlite).resolve().as_posix()}?mode=ro",
                uri=True,
            )
        except sqlite3.Error:
            return empty
        try:
            rows = conn.execute(
                "SELECT local_image_path FROM visual_chunks "
                "WHERE paper_id=? AND COALESCE(local_image_path, '') != ''",
                (paper_id,),
            ).fetchall()
        except sqlite3.Error:
            return empty
        finally:
            conn.close()
        checked = len(rows)
        if not checked:
            return empty
        resolved_root: Path | None = None
        if staging_root is not None:
            try:
                resolved_root = Path(staging_root).resolve()
            except OSError:
                resolved_root = None
        missing = 0
        staged = 0
        for (path,) in rows:
            candidate = Path(str(path))
            if not candidate.is_file():
                missing += 1
                continue
            if resolved_root is None:
                continue
            try:
                if candidate.resolve().is_relative_to(resolved_root):
                    staged += 1
            except (OSError, ValueError):
                continue
        return {
            "checked": checked,
            "missing": missing,
            "staged": staged,
            "missing_ratio": round(missing / checked, 4),
            "unresolvable_ratio": round((missing + staged) / checked, 4),
        }

    def _run_paper_waterfall(
        self,
        paper: S2PaperRecord,
        *,
        source_task_id: str,
        kb_sqlite: Path,
        download_dir: Path,
        visual_dir: Path,
    ) -> dict[str, Any]:
        """Run one paper through the ordered legal OA waterfall.

        Every route is attempted in the historical order (direct, Unpaywall,
        OpenAlex, Crossref, arXiv) and visual extraction runs only after text
        ingest succeeds.  In parallel mode this runs inside one paper worker
        against worker-local SQLite/download/visual directories; no shared
        state is mutated here in parallel mode.
        """

        ingester = KBIngester(
            kb_sqlite=kb_sqlite,
            download_dir=download_dir,
            require_scope_audit=False,
        )
        wave_audit: list[dict[str, Any]] = []
        attempted_routes: list[str] = []
        paper_new: list[str] = []
        paper_reused: list[str] = []
        paper_ids: list[str] = []
        success_wave = ""
        abstract_enriched_source = ""
        visual_report: dict[str, Any] = {}
        resolver_waves = 0
        ingest_stats: dict[str, Any] = {
            "attempted": 0,
            "downloaded": 0,
            "parse_failed": 0,
        }

        def enrich_abstract(
            record: dict[str, Any], provider: str
        ) -> None:
            nonlocal abstract_enriched_source
            if _enrich_verified_abstract(paper, record, provider=provider):
                abstract_enriched_source = provider

        def attempt_wave(
            wave: str,
            urls: Iterable[str],
            resolver_status: str,
            *,
            resolved_doi: str = "",
        ) -> bool:
            nonlocal resolver_waves
            clean_urls = _clean_urls(urls)
            audit_row: dict[str, Any] = {
                "wave": wave,
                "resolver_status": resolver_status,
                "route_count": len(clean_urls),
                "url_attempts": [],
            }
            wave_audit.append(audit_row)
            resolver_waves += 1
            if resolver_status != "resolved" or not clean_urls:
                return False
            attempted_routes.extend(clean_urls)
            candidate = _candidate_from_s2(
                paper,
                route_urls=clean_urls,
                resolved_doi=resolved_doi,
                materialization_route=f"public_oa:{wave}",
            )
            ingest = ingester.ingest_oa_candidates(
                [candidate],
                claim={
                    "claim_id": f"{source_task_id}:{paper.paper_id}",
                    "section_id": "s2_first",
                    "statement": (
                        "Acquire complete public full text for one paper "
                        "missing S2 body material."
                    ),
                    "supporting_text_chunk_ids": [],
                    "saturation_score": 0.0,
                },
                max_successes=1,
            )
            audit_row["url_attempts"] = list(
                ingest.stats.get("download_attempts") or []
            )
            ingest_stats["attempted"] += int(
                ingest.stats.get("candidates_tried") or 0
            )
            ingest_stats["downloaded"] += int(
                ingest.stats.get("downloaded") or 0
            )
            ingest_stats["parse_failed"] += int(
                ingest.stats.get("parse_failed") or 0
            )
            kept_new, quarantined_new = _quarantine_non_body_chunks(
                kb_sqlite, list(ingest.new_chunk_ids)
            )
            kept_reused, quarantined_reused = _quarantine_non_body_chunks(
                kb_sqlite, list(ingest.reused_chunk_ids)
            )
            audit_row["quarantined_chunk_ids"] = (
                quarantined_new + quarantined_reused
            )
            audit_row["materialized_chunk_count"] = len(kept_new) + len(
                kept_reused
            )
            if not audit_row["materialized_chunk_count"]:
                return False
            paper_new.extend(kept_new)
            paper_reused.extend(kept_reused)
            paper_ids.extend(ingest.new_paper_ids)
            return True

        direct_urls = _direct_public_routes(paper)
        success = attempt_wave(
            "s2_direct",
            direct_urls,
            "resolved" if direct_urls else "resolver_no_route",
        )
        if success:
            success_wave = "s2_direct"

        known_doi = normalize_doi(paper.doi)
        if not success:
            urls, resolver_status = _resolve_unpaywall_wave(known_doi)
            success = attempt_wave(
                "unpaywall", urls, resolver_status, resolved_doi=known_doi
            )
            if success:
                success_wave = "unpaywall"

        openalex_record: dict[str, Any] = {}
        if not success:
            urls, resolver_status, openalex_record = _resolve_openalex_wave(
                paper, doi=known_doi
            )
            if openalex_record:
                enrich_abstract(openalex_record, "openalex")
            recovered_doi = normalize_doi(openalex_record.get("doi"))
            success = attempt_wave(
                "openalex",
                urls,
                resolver_status,
                resolved_doi=recovered_doi or known_doi,
            )
            if success:
                success_wave = "openalex"
            elif recovered_doi and recovered_doi != known_doi:
                urls, resolver_status = _resolve_unpaywall_wave(recovered_doi)
                success = attempt_wave(
                    "openalex_doi_unpaywall",
                    urls,
                    resolver_status,
                    resolved_doi=recovered_doi,
                )
                if success:
                    success_wave = "openalex_doi_unpaywall"

        if not success:
            urls, resolver_status, crossref_record = _resolve_crossref_wave(
                paper
            )
            if crossref_record:
                enrich_abstract(crossref_record, "crossref")
            crossref_doi = normalize_doi(crossref_record.get("doi"))
            success = attempt_wave(
                "crossref_direct",
                urls,
                resolver_status,
                resolved_doi=crossref_doi,
            )
            if success:
                success_wave = "crossref_direct"
            elif crossref_doi:
                urls, resolver_status = _resolve_unpaywall_wave(crossref_doi)
                success = attempt_wave(
                    "crossref_doi_unpaywall",
                    urls,
                    resolver_status,
                    resolved_doi=crossref_doi,
                )
                if success:
                    success_wave = "crossref_doi_unpaywall"
            if not success and crossref_doi:
                urls, resolver_status, crossref_openalex_record = (
                    _resolve_openalex_wave(paper, doi=crossref_doi)
                )
                if crossref_openalex_record:
                    enrich_abstract(crossref_openalex_record, "openalex")
                success = attempt_wave(
                    "crossref_doi_openalex",
                    urls,
                    resolver_status,
                    resolved_doi=crossref_doi,
                )
                if success:
                    success_wave = "crossref_doi_openalex"

        if not success:
            urls, resolver_status, core_record = _resolve_core_wave(paper)
            if core_record:
                enrich_abstract(core_record, "core")
            core_doi = normalize_doi(core_record.get("doi"))
            success = attempt_wave(
                "core_api",
                urls,
                resolver_status,
                resolved_doi=core_doi,
            )
            if success:
                success_wave = "core_api"
            elif core_doi:
                urls, resolver_status = _resolve_unpaywall_wave(core_doi)
                success = attempt_wave(
                    "core_doi_unpaywall",
                    urls,
                    resolver_status,
                    resolved_doi=core_doi,
                )
                if success:
                    success_wave = "core_doi_unpaywall"

        if not success:
            urls, resolver_status, arxiv_record = _resolve_arxiv_wave(paper)
            if arxiv_record:
                enrich_abstract(arxiv_record, "arxiv")
            success = attempt_wave(
                "arxiv_title",
                urls,
                resolver_status,
                resolved_doi=normalize_doi(arxiv_record.get("doi")),
            )
            if success:
                success_wave = "arxiv_title"

        if success:
            visual_report = self._extract_visuals_after_text_success(
                paper,
                source_task_id=source_task_id,
                download_dir=download_dir,
                output_dir=visual_dir,
                staging_kb=kb_sqlite,
            )

        materialized_chunk_count = len(paper_new) + len(paper_reused)
        if materialized_chunk_count:
            status = "oa_fulltext_success"
        elif attempted_routes:
            status = "oa_public_routes_exhausted"
        else:
            status = "no_public_oa_route_after_resolvers"
        return {
            "paper_id": paper.paper_id,
            "title": paper.title,
            "doi": paper.doi,
            "status": status,
            "success_wave": success_wave,
            "route_count": len(set(attempted_routes)),
            "new_chunk_ids": paper_new,
            "reused_chunk_ids": paper_reused,
            "new_paper_ids": paper_ids,
            "materialized_chunk_count": materialized_chunk_count,
            "abstract_enriched_source": abstract_enriched_source,
            "visual_report": visual_report,
            "visual_candidate_count": int(
                visual_report.get("eligible_visual_chunks", 0) or 0
            ),
            "waves": wave_audit,
            "resolver_waves": resolver_waves,
            "ingest_stats": ingest_stats,
        }

    def _acquire_paper_worker(
        self,
        paper: S2PaperRecord,
        *,
        source_task_id: str,
        temp_root: Path,
    ) -> dict[str, Any]:
        """Acquire one paper into worker-local directories only.

        The returned payload carries the ordered wave audit plus the local
        artifact paths.  The caller commits those artifacts to the shared
        SQLite/library through one deterministic main-thread writer.
        """

        try:
            work_dir = Path(temp_root) / _safe_worker_name(paper.paper_id)
            work_dir.mkdir(parents=True, exist_ok=True)
            payload = self._run_paper_waterfall(
                paper,
                source_task_id=source_task_id,
                kb_sqlite=work_dir / "kb.sqlite",
                download_dir=work_dir / "downloads",
                visual_dir=work_dir / "visual_candidates",
            )
            payload["temp_paths"] = {
                "kb": str(work_dir / "kb.sqlite"),
                "download_dir": str(work_dir / "downloads"),
                "visual_dir": str(work_dir / "visual_candidates"),
            }
            return payload
        except Exception as exc:
            return {
                "paper_id": paper.paper_id,
                "title": paper.title,
                "doi": paper.doi,
                "status": "oa_worker_failed",
                "worker_error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "materialized_chunk_count": 0,
            }

    # Historical fully-serial acquisition path.  It mirrors the route
    # waterfall implemented by _run_paper_waterfall; keep both in sync when
    # the legal OA route chain changes.
    def _acquire_serial(
        self,
        selections: list[
            tuple[S2PaperRecord, FulltextEscalationDecision]
        ],
        *,
        max_successes: int = 1,
        source_task_id: str = "s2_fulltext_escalation",
    ) -> S2FulltextAcquisitionResult:
        selected = [
            (paper, decision)
            for paper, decision in selections
            if decision.should_download
        ]
        selected.sort(key=lambda item: item[1].priority, reverse=True)
        result = S2FulltextAcquisitionResult(
            selected_paper_ids=[paper.paper_id for paper, _ in selected],
            skipped=[
                {"paper_id": paper.paper_id, "reason": decision.reason}
                for paper, decision in selections
                if not decision.should_download
            ],
        )
        if not selected or max_successes <= 0:
            result.stats = {"attempted": 0, "downloaded": 0, "paper_outcomes": []}
            return result
        ingester = KBIngester(
            kb_sqlite=self.kb_sqlite,
            download_dir=self.download_dir,
            require_scope_audit=False,
        )
        aggregate_stats: dict[str, Any] = {
            "attempted": 0,
            "downloaded": 0,
            "parse_failed": 0,
            "resolver_waves": 0,
            "abstracts_enriched": 0,
            "abstract_enrichment_sources": {},
            "paper_outcomes": [],
        }
        successful_papers = 0
        for paper, _decision in selected:
            if successful_papers >= max(0, int(max_successes)):
                result.skipped.append(
                    {"paper_id": paper.paper_id, "reason": "oa_success_budget_reached"}
                )
                continue
            wave_audit: list[dict[str, Any]] = []
            attempted_routes: list[str] = []
            paper_new: list[str] = []
            paper_reused: list[str] = []
            paper_ids: list[str] = []
            success_wave = ""
            abstract_enriched_source = ""
            visual_report: dict[str, Any] = {}

            def enrich_abstract(record: dict[str, Any], provider: str) -> None:
                nonlocal abstract_enriched_source
                if _enrich_verified_abstract(paper, record, provider=provider):
                    abstract_enriched_source = provider
                    aggregate_stats["abstracts_enriched"] += 1
                    sources = aggregate_stats["abstract_enrichment_sources"]
                    sources[provider] = int(sources.get(provider, 0)) + 1

            def attempt_wave(
                wave: str,
                urls: Iterable[str],
                resolver_status: str,
                *,
                resolved_doi: str = "",
            ) -> bool:
                clean_urls = _clean_urls(urls)
                audit_row: dict[str, Any] = {
                    "wave": wave,
                    "resolver_status": resolver_status,
                    "route_count": len(clean_urls),
                    "url_attempts": [],
                }
                wave_audit.append(audit_row)
                aggregate_stats["resolver_waves"] += 1
                if resolver_status != "resolved" or not clean_urls:
                    return False
                attempted_routes.extend(clean_urls)
                candidate = _candidate_from_s2(
                    paper,
                    route_urls=clean_urls,
                    resolved_doi=resolved_doi,
                    materialization_route=f"public_oa:{wave}",
                )
                ingest = ingester.ingest_oa_candidates(
                    [candidate],
                    claim={
                        "claim_id": f"{source_task_id}:{paper.paper_id}",
                        "section_id": "s2_first",
                        "statement": "Acquire complete public full text for one paper missing S2 body material.",
                        "supporting_text_chunk_ids": [],
                        "saturation_score": 0.0,
                    },
                    max_successes=1,
                )
                audit_row["url_attempts"] = list(
                    ingest.stats.get("download_attempts") or []
                )
                aggregate_stats["attempted"] += int(
                    ingest.stats.get("candidates_tried") or 0
                )
                aggregate_stats["downloaded"] += int(
                    ingest.stats.get("downloaded") or 0
                )
                aggregate_stats["parse_failed"] += int(
                    ingest.stats.get("parse_failed") or 0
                )
                kept_new, quarantined_new = _quarantine_non_body_chunks(
                    self.kb_sqlite, list(ingest.new_chunk_ids)
                )
                kept_reused, quarantined_reused = _quarantine_non_body_chunks(
                    self.kb_sqlite, list(ingest.reused_chunk_ids)
                )
                audit_row["quarantined_chunk_ids"] = (
                    quarantined_new + quarantined_reused
                )
                audit_row["materialized_chunk_count"] = len(kept_new) + len(
                    kept_reused
                )
                if not audit_row["materialized_chunk_count"]:
                    return False
                paper_new.extend(kept_new)
                paper_reused.extend(kept_reused)
                paper_ids.extend(ingest.new_paper_ids)
                return True

            direct_urls = _direct_public_routes(paper)
            success = attempt_wave(
                "s2_direct",
                direct_urls,
                "resolved" if direct_urls else "resolver_no_route",
            )
            if success:
                success_wave = "s2_direct"

            known_doi = normalize_doi(paper.doi)
            if not success:
                urls, resolver_status = _resolve_unpaywall_wave(known_doi)
                success = attempt_wave(
                    "unpaywall", urls, resolver_status, resolved_doi=known_doi
                )
                if success:
                    success_wave = "unpaywall"

            openalex_record: dict[str, Any] = {}
            if not success:
                urls, resolver_status, openalex_record = _resolve_openalex_wave(
                    paper, doi=known_doi
                )
                if openalex_record:
                    enrich_abstract(openalex_record, "openalex")
                recovered_doi = normalize_doi(openalex_record.get("doi"))
                success = attempt_wave(
                    "openalex",
                    urls,
                    resolver_status,
                    resolved_doi=recovered_doi or known_doi,
                )
                if success:
                    success_wave = "openalex"
                elif recovered_doi and recovered_doi != known_doi:
                    urls, resolver_status = _resolve_unpaywall_wave(recovered_doi)
                    success = attempt_wave(
                        "openalex_doi_unpaywall",
                        urls,
                        resolver_status,
                        resolved_doi=recovered_doi,
                    )
                    if success:
                        success_wave = "openalex_doi_unpaywall"

            if not success:
                urls, resolver_status, crossref_record = _resolve_crossref_wave(paper)
                if crossref_record:
                    enrich_abstract(crossref_record, "crossref")
                crossref_doi = normalize_doi(crossref_record.get("doi"))
                success = attempt_wave(
                    "crossref_direct",
                    urls,
                    resolver_status,
                    resolved_doi=crossref_doi,
                )
                if success:
                    success_wave = "crossref_direct"
                elif crossref_doi:
                    urls, resolver_status = _resolve_unpaywall_wave(crossref_doi)
                    success = attempt_wave(
                        "crossref_doi_unpaywall",
                        urls,
                        resolver_status,
                        resolved_doi=crossref_doi,
                    )
                    if success:
                        success_wave = "crossref_doi_unpaywall"
                if not success and crossref_doi:
                    urls, resolver_status, crossref_openalex_record = _resolve_openalex_wave(
                        paper, doi=crossref_doi
                    )
                    if crossref_openalex_record:
                        enrich_abstract(crossref_openalex_record, "openalex")
                    success = attempt_wave(
                        "crossref_doi_openalex",
                        urls,
                        resolver_status,
                        resolved_doi=crossref_doi,
                    )
                    if success:
                        success_wave = "crossref_doi_openalex"

            if not success:
                urls, resolver_status, arxiv_record = _resolve_arxiv_wave(paper)
                if arxiv_record:
                    enrich_abstract(arxiv_record, "arxiv")
                success = attempt_wave(
                    "arxiv_title",
                    urls,
                    resolver_status,
                    resolved_doi=normalize_doi(arxiv_record.get("doi")),
                )
                if success:
                    success_wave = "arxiv_title"

            if success:
                visual_report = self._extract_visuals_after_text_success(
                    paper,
                    source_task_id=source_task_id,
                )

            result.new_chunk_ids.extend(paper_new)
            result.reused_chunk_ids.extend(paper_reused)
            result.new_paper_ids.extend(paper_ids)
            materialized_chunk_count = len(paper_new) + len(paper_reused)
            if materialized_chunk_count:
                successful_papers += 1
                status = "oa_fulltext_success"
            elif attempted_routes:
                status = "oa_public_routes_exhausted"
            else:
                status = "no_public_oa_route_after_resolvers"
            aggregate_stats["paper_outcomes"].append(
                {
                    "paper_id": paper.paper_id,
                    "title": paper.title,
                    "doi": paper.doi,
                    "status": status,
                    "success_wave": success_wave,
                    "route_count": len(set(attempted_routes)),
                    "new_chunk_count": len(paper_new),
                    "reused_chunk_count": len(paper_reused),
                    "materialized_chunk_count": materialized_chunk_count,
                    "abstract_enriched_source": abstract_enriched_source,
                    "visual_ingest": visual_report,
                    "visual_candidate_count": int(
                        visual_report.get("eligible_visual_chunks", 0) or 0
                    ),
                    "waves": wave_audit,
                }
            )
        result.stats = {
            **aggregate_stats,
            "successful_papers": successful_papers,
            "success_budget_skipped_attempts": 0,
            "visual_candidate_count": sum(
                int(outcome.get("visual_candidate_count") or 0)
                for outcome in aggregate_stats["paper_outcomes"]
            ),
            "visual_ingest_status_counts": {
                status: sum(
                    1
                    for outcome in aggregate_stats["paper_outcomes"]
                    if str((outcome.get("visual_ingest") or {}).get("status") or "") == status
                )
                for status in sorted({
                    str((outcome.get("visual_ingest") or {}).get("status") or "")
                    for outcome in aggregate_stats["paper_outcomes"]
                    if str((outcome.get("visual_ingest") or {}).get("status") or "")
                })
            },
            "non_body_chunks_quarantined": sum(
                len(wave.get("quarantined_chunk_ids") or [])
                for outcome in aggregate_stats["paper_outcomes"]
                for wave in outcome.get("waves") or []
            ),
        }
        return result
