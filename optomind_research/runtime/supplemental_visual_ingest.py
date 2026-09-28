"""Best-effort visual extraction for newly materialized OA full text.

Text ingestion must never fail merely because figure extraction fails.  This
module therefore performs deterministic local extraction, stores only
single-figure/subfigure candidates in canonical visual tables, and leaves them
in a pending multimodal-review state.  Composite parent figures are recorded
in an audit queue but are not promoted to active visual chunks.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List

from optomind_research.visual_asset_extractor import (
    extract_html_visual_assets,
    extract_pdf_visual_assets,
)

_PANEL_MARKER = re.compile(
    r"(?:\([a-z]\)|\b[a-z]\))(?:(?:\s*[,;]\s*|\s+(?:and|to)\s+)"
    r"(?:\([a-z]\)|\b[a-z]\)))",
    re.IGNORECASE,
)


def _safe(value: str, limit: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return (cleaned or "visual")[:limit]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS visual_assets(
          asset_id TEXT PRIMARY KEY,
          paper_id TEXT NOT NULL,
          doi TEXT,
          title TEXT,
          asset_type TEXT,
          label TEXT,
          caption TEXT,
          local_image_path TEXT,
          search_text TEXT,
          raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visual_chunks(
          chunk_id TEXT PRIMARY KEY,
          paper_id TEXT NOT NULL,
          doi TEXT,
          title TEXT,
          chunk_kind TEXT,
          parent_asset_id TEXT,
          parent_label TEXT,
          subfigure_label TEXT,
          visual_role TEXT,
          review_utility TEXT,
          local_image_path TEXT,
          caption TEXT,
          search_text TEXT,
          raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visual_candidate_queue(
          candidate_visual_id TEXT PRIMARY KEY,
          paper_id TEXT NOT NULL,
          local_image_path TEXT,
          status TEXT NOT NULL,
          exclusion_reason TEXT,
          raw_json TEXT NOT NULL
        );
        """
    )
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(visual_chunks)").fetchall()
    }
    for name, definition in (
        ("visual_argument_type", "TEXT NOT NULL DEFAULT ''"),
        (
            "visual_argument_status",
            "TEXT NOT NULL DEFAULT 'pending_multimodal_review'",
        ),
        ("visual_argument_confidence", "TEXT NOT NULL DEFAULT ''"),
        ("visual_argument_claim", "TEXT NOT NULL DEFAULT ''"),
        (
            "visual_argument_needs_human_review",
            "INTEGER NOT NULL DEFAULT 1",
        ),
        ("visual_argument_schema_version", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in existing:
            conn.execute(
                f"ALTER TABLE visual_chunks ADD COLUMN {name} {definition}"
            )
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS visual_asset_fts USING "
            "fts5(asset_id UNINDEXED,paper_id UNINDEXED,title,label,caption,search_text)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS visual_chunk_fts USING "
            "fts5(chunk_id UNINDEXED,paper_id UNINDEXED,title,visual_role,caption,search_text)"
        )
    except sqlite3.OperationalError:
        pass


def _raw_records(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = Path(str(summary.get("visual_assets_jsonl") or ""))
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except Exception:
            continue
    return records


def _record_images(record: Dict[str, Any]) -> Iterable[tuple[Path, str]]:
    if record.get("image_path"):
        yield Path(str(record["image_path"])), ""
    for index, value in enumerate(record.get("local_image_paths") or [], 1):
        yield Path(str(value)), str(index)


def _looks_composite(record: Dict[str, Any], image_count: int) -> bool:
    caption = str(record.get("caption_text") or "")
    if image_count > 1:
        # Multiple publisher image nodes are already useful subfigure-level
        # candidates; the containing parent is not promoted.
        return False
    panel_mentions = {
        match.lower()
        for match in re.findall(r"\(([a-z])\)", caption, flags=re.I)
    }
    return len(panel_mentions) >= 2 or bool(_PANEL_MARKER.search(caption))


def extract_visual_candidates(
    *,
    source_path: Path,
    staging_kb: Path,
    output_dir: Path,
    paper_id: str,
    doi: str,
    title: str,
    max_assets: int = 40,
) -> Dict[str, Any]:
    """Extract and register pending visual candidates from one local full text."""

    report: Dict[str, Any] = {
        "status": "skipped",
        "source_path": str(source_path),
        "paper_id": paper_id,
        "eligible_visual_chunks": 0,
        "composite_parents_excluded": 0,
        "duplicate_images_skipped": 0,
        "extraction_errors": [],
    }
    if not source_path.is_file():
        report["status"] = "source_missing"
        return report
    paper_dir = output_dir / _safe(paper_id, 64)
    paper_dir.mkdir(parents=True, exist_ok=True)
    try:
        if source_path.suffix.lower() == ".pdf":
            summary = extract_pdf_visual_assets(
                source_path,
                paper_dir / "pymupdf",
                max_pages=40,
                skip_first_pages=1,
                render_scale=2.2,
            )
        elif source_path.suffix.lower() in {".html", ".htm"}:
            summary = extract_html_visual_assets(
                source_path,
                paper_dir / "html",
                max_assets=max_assets,
                download_images=True,
            )
        else:
            report["status"] = "unsupported_source_format"
            return report
    except Exception as exc:
        report["status"] = "extractor_failed"
        report["extraction_errors"].append(
            f"{type(exc).__name__}: {str(exc)[:240]}"
        )
        return report

    records = _raw_records(summary)
    report["raw_candidate_count"] = len(records)
    seen_hashes: set[str] = set()
    conn = sqlite3.connect(str(staging_kb))
    try:
        _ensure_schema(conn)
        with conn:
            for raw_index, record in enumerate(records, 1):
                images = [
                    (path, label)
                    for path, label in _record_images(record)
                    if path.is_file()
                ]
                if not images:
                    continue
                composite = _looks_composite(record, len(images))
                caption = str(record.get("caption_text") or "").strip()
                nearby = str(record.get("nearby_text") or "").strip()
                label = str(record.get("label") or f"Figure {raw_index}")
                for image_index, (image_path, sublabel) in enumerate(images, 1):
                    try:
                        image_hash = _hash_file(image_path)
                    except OSError:
                        continue
                    if image_hash in seen_hashes:
                        report["duplicate_images_skipped"] += 1
                        continue
                    seen_hashes.add(image_hash)
                    visual_id = (
                        f"{_safe(paper_id, 44)}:"
                        f"{_safe(label, 24)}:{image_hash[:12]}"
                    )
                    queue_raw = {
                        "candidate_visual_id": visual_id,
                        "paper_id": paper_id,
                        "doi": doi,
                        "title": title,
                        "label": label,
                        "caption": caption,
                        "nearby_text": nearby,
                        "local_image_path": str(image_path),
                        "source_path": str(source_path),
                        "extraction_method": record.get(
                            "extraction_method", ""
                        ),
                        "image_sha256": image_hash,
                    }
                    if composite:
                        report["composite_parents_excluded"] += 1
                        conn.execute(
                            """INSERT OR REPLACE INTO visual_candidate_queue(
                               candidate_visual_id,paper_id,local_image_path,
                               status,exclusion_reason,raw_json)
                               VALUES(?,?,?,?,?,?)""",
                            (
                                visual_id,
                                paper_id,
                                str(image_path),
                                "excluded_parent_composite",
                                "multipanel_parent_requires_subfigure_separation",
                                json.dumps(queue_raw, ensure_ascii=False),
                            ),
                        )
                        continue

                    kind = "subfigure" if len(images) > 1 else "single_figure"
                    subfigure_label = sublabel if kind == "subfigure" else ""
                    search_text = " ".join(
                        part
                        for part in (title, label, caption, nearby)
                        if part
                    )[:8000]
                    canonical_raw = {
                        **queue_raw,
                        "schema_version": "supplemental_visual_candidate.v1",
                        "chunk_id": visual_id,
                        "chunk_kind": kind,
                        "parent_asset_id": visual_id,
                        "parent_label": label,
                        "subfigure_label": subfigure_label,
                        "visual_argument_status": (
                            "pending_multimodal_review"
                        ),
                        "visual_argument_needs_human_review": True,
                    }
                    conn.execute(
                        """INSERT OR REPLACE INTO visual_assets(
                           asset_id,paper_id,doi,title,asset_type,label,caption,
                           local_image_path,search_text,raw_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            visual_id,
                            paper_id,
                            doi,
                            title,
                            kind,
                            label,
                            caption,
                            str(image_path),
                            search_text,
                            json.dumps(canonical_raw, ensure_ascii=False),
                        ),
                    )
                    conn.execute(
                        """INSERT OR REPLACE INTO visual_chunks(
                           chunk_id,paper_id,doi,title,chunk_kind,parent_asset_id,
                           parent_label,subfigure_label,visual_role,review_utility,
                           local_image_path,caption,search_text,raw_json,
                           visual_argument_type,visual_argument_status,
                           visual_argument_confidence,visual_argument_claim,
                           visual_argument_needs_human_review,
                           visual_argument_schema_version)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            visual_id,
                            paper_id,
                            doi,
                            title,
                            kind,
                            visual_id,
                            label,
                            subfigure_label,
                            "",
                            "pending_review",
                            str(image_path),
                            caption,
                            search_text,
                            json.dumps(canonical_raw, ensure_ascii=False),
                            "",
                            "pending_multimodal_review",
                            "",
                            "",
                            1,
                            "supplemental_visual_candidate.v1",
                        ),
                    )
                    conn.execute(
                        """INSERT OR REPLACE INTO visual_candidate_queue(
                           candidate_visual_id,paper_id,local_image_path,status,
                           exclusion_reason,raw_json)
                           VALUES(?,?,?,?,?,?)""",
                        (
                            visual_id,
                            paper_id,
                            str(image_path),
                            "pending_multimodal_review",
                            "",
                            json.dumps(canonical_raw, ensure_ascii=False),
                        ),
                    )
                    for fts_table, fts_values in (
                        (
                            "visual_asset_fts",
                            (
                                visual_id,
                                paper_id,
                                title,
                                label,
                                caption,
                                search_text,
                            ),
                        ),
                        (
                            "visual_chunk_fts",
                            (
                                visual_id,
                                paper_id,
                                title,
                                "",
                                caption,
                                search_text,
                            ),
                        ),
                    ):
                        try:
                            id_col = (
                                "asset_id"
                                if fts_table == "visual_asset_fts"
                                else "chunk_id"
                            )
                            conn.execute(
                                f"DELETE FROM {fts_table} WHERE {id_col}=?",
                                (visual_id,),
                            )
                            placeholders = ",".join("?" for _ in fts_values)
                            conn.execute(
                                f"INSERT INTO {fts_table} VALUES({placeholders})",
                                fts_values,
                            )
                        except sqlite3.OperationalError:
                            pass
                    report["eligible_visual_chunks"] += 1
    finally:
        conn.close()

    report["status"] = (
        "pending_multimodal_review"
        if report["eligible_visual_chunks"]
        else "no_eligible_single_or_subfigure"
    )
    report["extractor_summary"] = summary
    report_path = paper_dir / "SUPPLEMENTAL_VISUAL_INGEST.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report["report_path"] = str(report_path)
    return report
