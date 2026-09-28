"""Canonicalize local paper fulltexts into a uniform literature record.

This module is intentionally small and conservative:
- deterministic code preserves the full parsed text and chunks;
- Qwen only extracts structured metadata/abstract/outline from excerpts;
- database updates are optional and guarded by validation.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.literature_resource_builder import (
    AbstractPaperRecord,
    FulltextRecord,
    LiteratureResourceLibrary,
    USER_FULLTEXTS_DIR,
    DEFAULT_LIBRARY_DB,
    DEFAULT_OUTPUT_ROOT,
    normalize_doi,
    normalize_space,
    parse_json_like,
    read_text_file,
    safe_filename,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Fulltext Canonicalizer.txt"


@dataclass
class DocumentGroup:
    group_id: str
    doi: str
    title: str
    venue: str
    year: int | None
    source_url: str
    files: list[Path]
    metadata: dict[str, Any]


def safe_doi_stem(doi: str, fallback: str) -> str:
    if doi:
        return "DOI_" + re.sub(r"[^A-Za-z0-9]+", "_", doi.lower()).strip("_")
    return safe_filename(fallback or "paper")


def doi_from_stem(stem: str) -> str:
    text = stem
    if text.startswith("DOI_"):
        text = text[4:]
    # Best-effort fallback for files without .source.json.
    parts = text.split("_")
    if len(parts) >= 3 and parts[0] == "10":
        return f"10.{parts[1]}/" + ".".join(parts[2:])
    return ""


def load_resource_bundle_metadata(bundle_path: Path | None) -> dict[str, dict[str, Any]]:
    by_doi: dict[str, dict[str, Any]] = {}
    if not bundle_path or not bundle_path.exists():
        return by_doi
    try:
        data = json.loads(bundle_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return by_doi

    def visit(obj: Any) -> None:
        if isinstance(obj, dict):
            doi = normalize_doi(obj.get("doi") or "")
            title = normalize_space(str(obj.get("title") or ""))
            if doi and title:
                current = by_doi.setdefault(doi, {})
                # Keep richer values; never overwrite with empty.
                for key in ("paper_id", "doi", "title", "authors", "year", "venue", "abstract", "landing_page_url", "pdf_url", "open_access", "citation_count"):
                    value = obj.get(key)
                    if value not in (None, "", [], {}):
                        if key not in current or not current.get(key):
                            current[key] = value
                current.setdefault("source", "resource_bundle")
            for value in obj.values():
                visit(value)
        elif isinstance(obj, list):
            for item in obj:
                visit(item)

    visit(data)
    return by_doi


def discover_document_groups(input_dir: Path, bundle_metadata: dict[str, dict[str, Any]]) -> list[DocumentGroup]:
    input_dir = Path(input_dir)
    source_jsons = sorted(input_dir.glob("DOI_*.source.json"))
    groups: dict[str, DocumentGroup] = {}

    for src in source_jsons:
        try:
            meta = json.loads(src.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            meta = {}
        doi = normalize_doi(meta.get("doi") or doi_from_stem(src.stem.replace(".source", "")))
        prefix = src.stem.replace(".source", "")
        files = sorted([p for p in input_dir.glob(prefix + "*") if p.is_file()])
        bundle_meta = bundle_metadata.get(doi, {})
        merged = {**meta, **bundle_meta}
        source_url = str(meta.get("source_url") or merged.get("source_url") or merged.get("landing_page_url") or "")
        group_id = safe_doi_stem(doi, prefix)
        groups[group_id] = DocumentGroup(
            group_id=group_id,
            doi=doi,
            title=normalize_space(str(merged.get("title") or "")),
            venue=normalize_space(str(merged.get("venue") or merged.get("journal") or "")),
            year=safe_year(merged.get("year")),
            source_url=source_url,
            files=files,
            metadata=merged,
        )

    # Include manually downloaded files that do not have a source JSON yet.
    known = {p.resolve() for g in groups.values() for p in g.files}
    for p in sorted(input_dir.glob("*")):
        if not p.is_file() or p.resolve() in known or p.name.startswith(".") or p.name.lower() == "readme.md":
            continue
        if p.suffix.lower() not in {".pdf", ".txt", ".html", ".xml", ".md"}:
            continue
        stem = p.stem
        doi = normalize_doi(doi_from_stem(stem))
        group_id = safe_doi_stem(doi, stem)
        groups.setdefault(
            group_id,
            DocumentGroup(
                group_id=group_id,
                doi=doi,
                title="",
                venue="",
                year=None,
                source_url="",
                files=[],
                metadata={},
            ),
        ).files.append(p)

    return sorted(groups.values(), key=lambda g: (g.doi or g.group_id))


def safe_year(value: Any) -> int | None:
    try:
        year = int(value)
        return year if 1500 <= year <= 2100 else None
    except Exception:
        return None


def read_pdf_text(path: Path, max_pages: int = 80) -> str:
    try:
        import fitz  # PyMuPDF

        parts: list[str] = []
        with fitz.open(str(path)) as doc:
            for page in doc[: min(len(doc), max_pages)]:
                parts.append(page.get_text("text"))
        return normalize_space("\n".join(parts))
    except Exception:
        return ""


def read_html_text(path: Path) -> str:
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()
    return normalize_space(soup.get_text(" ", strip=True))


def read_text_file_loose(path: Path) -> str:
    try:
        return normalize_space(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""


def choose_best_text(group: DocumentGroup) -> tuple[str, Path | None, str]:
    candidates: list[tuple[int, Path, str, str]] = []
    priority = {
        ".txt": 40,
        ".md": 35,
        ".xml": 34,
        ".html": 30,
        ".pdf": 28,
    }
    for path in group.files:
        suffix = path.suffix.lower()
        if path.name.endswith(".source.json") or suffix == ".json":
            continue
        if suffix == ".pdf":
            text = read_pdf_text(path)
        elif suffix in {".html", ".htm"}:
            text = read_html_text(path)
        else:
            text = read_text_file_loose(path)
        if not text:
            continue
        score = len(text) + priority.get(suffix, 0) * 1000
        candidates.append((score, path, suffix.lstrip(".") or "text", text))
    if not candidates:
        return "", None, ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    _score, path, fmt, text = candidates[0]
    return text, path, fmt


def make_chunks(text: str, *, chunk_size: int = 7000, overlap: int = 600) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    text = normalize_space(text)
    if not text:
        return chunks
    start = 0
    idx = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append({"chunk_id": idx, "start_char": start, "end_char": end, "text": text[start:end]})
        idx += 1
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks


def section_hints(text: str, limit: int = 18) -> list[str]:
    labels = [
        "Abstract", "Introduction", "Results", "Discussion", "Methods", "Materials",
        "Experimental", "Conclusion", "References", "\u6458\u8981", "\u5f15\u8a00", "\u7ed3\u679c", "\u8ba8\u8bba", "\u65b9\u6cd5", "\u7ed3\u8bba",
    ]
    hits: list[str] = []
    lowered = text.lower()
    for label in labels:
        if label.lower() in lowered and label not in hits:
            hits.append(label)
        if len(hits) >= limit:
            break
    return hits


def text_excerpt(text: str, max_part: int = 6000) -> dict[str, str]:
    text = normalize_space(text)
    if len(text) <= max_part * 2:
        return {"beginning": text[:max_part], "middle": "", "ending": text[-max_part:]}
    mid = len(text) // 2
    return {
        "beginning": text[:max_part],
        "middle": text[max(0, mid - max_part // 2): mid + max_part // 2],
        "ending": text[-max_part:],
    }


def qwen_extract(prompt: str, group: DocumentGroup, text: str, model_tier: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "metadata": {
            "title": group.title,
            "doi": group.doi,
            "venue": group.venue,
            "year": group.year,
            "source_url": group.source_url,
        },
        "text_chars": len(text),
        "section_hints": section_hints(text),
        "text_excerpt": text_excerpt(text),
    }
    result = call_qwen_chat(
        "FulltextCanonicalizerAgent",
        [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        model_tier=model_tier,
        temperature=0,
        max_tokens=3000,
        response_format={"type": "json_object"},
    )
    parsed = parse_json_like(result.get("content", ""), fallback={})
    return (parsed if isinstance(parsed, dict) else {}), result.get("_llm_usage", {})


def validate_extraction(group: DocumentGroup, parsed: dict[str, Any], text: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    identity = parsed.get("paper_identity") if isinstance(parsed.get("paper_identity"), dict) else {}
    title = normalize_space(str(identity.get("title") or group.title or ""))
    doi = normalize_doi(identity.get("doi") or group.doi or "")
    abstract = normalize_space(str(parsed.get("abstract") or ""))
    quality = str(parsed.get("fulltext_quality") or "uncertain")
    if group.doi and doi and normalize_doi(group.doi) != doi:
        warnings.append(f"doi_mismatch: expected {group.doi}, got {doi}")
    if not title:
        errors.append("missing_title")
    if len(text) < 5000:
        warnings.append("source_text_short")
    if len(abstract) < 60:
        warnings.append("abstract_too_short_or_missing")
    if abstract and not re.search(r"[A-Za-z]", abstract):
        warnings.append("abstract_not_english")
    if quality not in {"fulltext", "partial_fulltext", "metadata_only", "uncertain"}:
        warnings.append(f"unknown_fulltext_quality:{quality}")
    if quality == "metadata_only" and len(text) > 12000:
        warnings.append("quality_says_metadata_only_but_text_is_long")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def canonicalize_one(
    group: DocumentGroup,
    *,
    prompt: str,
    output_dir: Path,
    model_tier: str,
    library: LiteratureResourceLibrary | None = None,
    commit_to_db: bool = False,
) -> dict[str, Any]:
    text, source_path, source_format = choose_best_text(group)
    if not text:
        return {"group_id": group.group_id, "doi": group.doi, "ok": False, "errors": ["no_readable_text"], "files": [str(p) for p in group.files]}

    records_dir = output_dir / "records"
    texts_dir = output_dir / "normalized_texts"
    chunks_dir = output_dir / "chunks"
    for d in (records_dir, texts_dir, chunks_dir):
        d.mkdir(parents=True, exist_ok=True)

    stem = safe_doi_stem(group.doi, group.group_id)
    normalized_text_path = texts_dir / f"{stem}.txt"
    chunks_path = chunks_dir / f"{stem}.jsonl"
    record_path = records_dir / f"{stem}.canonical.json"
    normalized_text_path.write_text(text, encoding="utf-8", errors="replace")
    chunks = make_chunks(text)
    chunks_path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in chunks), encoding="utf-8")

    parsed, usage = qwen_extract(prompt, group, text, model_tier)
    if isinstance(parsed, dict) and parsed.get("fulltext_quality") == "metadata_only" and len(text) > 12000:
        parsed["fulltext_quality"] = "partial_fulltext"
        note = normalize_space(str(parsed.get("notes") or ""))
        parsed["notes"] = (note + "; " if note else "") + "The program changed fulltext_quality from metadata_only to partial_fulltext based on body-text length."
    validation = validate_extraction(group, parsed, text)
    identity = parsed.get("paper_identity") if isinstance(parsed.get("paper_identity"), dict) else {}
    doi = normalize_doi(group.doi or identity.get("doi") or "")
    title = normalize_space(str(group.title or identity.get("title") or ""))
    abstract = normalize_space(str(parsed.get("abstract") or ""))
    authors = identity.get("authors") if isinstance(identity.get("authors"), list) else []
    year = safe_year(group.year or identity.get("year"))
    venue = normalize_space(str(group.venue or identity.get("venue") or ""))

    canonical = {
        "schema_version": "fulltext_canonical_record.v1",
        "paper_id": f"doi:{doi}" if doi else f"local:{group.group_id}",
        "doi": doi,
        "title": title,
        "authors": [str(a) for a in authors][:50],
        "year": year,
        "venue": venue,
        "abstract": {
            "text": abstract,
            "source": "qwen_from_fulltext_excerpt",
            "is_original_abstract": False,
            "confidence": 0.72 if abstract else 0.0,
        },
        "fulltext": {
            "status": "available" if validation["ok"] else "needs_review",
            "format": source_format,
            "source_file": str(source_path) if source_path else "",
            "normalized_text_path": str(normalized_text_path),
            "chunk_index_path": str(chunks_path),
            "text_chars": len(text),
            "chunk_count": len(chunks),
            "source_url": group.source_url,
        },
        "qwen_extraction": parsed,
        "validation": validation,
        "source_files": [str(p) for p in group.files],
        "llm_usage": usage,
        "created_at": utc_now(),
    }
    record_path.write_text(json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8")

    if commit_to_db and validation["ok"] and library:
        existing_probe = AbstractPaperRecord(paper_id=canonical["paper_id"], doi=doi, title=title)
        existing_id = library.find_existing_paper_id(existing_probe)
        existing = library.get_abstract(existing_id) if existing_id else None
        abstract_for_db = abstract
        if existing and normalize_space(existing.abstract) and len(normalize_space(existing.abstract)) >= 80:
            abstract_for_db = existing.abstract
        raw = {
            "canonical_fulltext_record_path": str(record_path),
            "canonical_fulltext_schema": canonical["schema_version"],
            "qwen_fulltext_extraction": parsed,
            "source_files": canonical["source_files"],
            "machine_abstract_from_fulltext": bool(abstract),
            "machine_abstract_used_for_abstract_field": bool(abstract_for_db == abstract),
        }
        paper = AbstractPaperRecord(
            paper_id=canonical["paper_id"],
            doi=doi,
            title=title,
            authors=canonical["authors"],
            year=year,
            venue=venue,
            abstract=abstract_for_db,
            landing_page_url=group.source_url,
            source_apis=["local_fulltext_canonicalizer"],
            topic_tags=["local_fulltext", "canonicalized_fulltext"],
            raw=raw,
        )
        paper_id, _is_new = library.upsert_abstract(paper)
        library.save_fulltext_record(
            FulltextRecord(
                paper_id=paper_id,
                doi=doi,
                title=title,
                fulltext_status="available",
                fulltext_type=source_format or "text",
                local_file_path=str(source_path) if source_path else "",
                parsed_text_path=str(normalized_text_path),
                chunk_index_path=str(chunks_path),
                source_url=group.source_url,
                access_method="local_fulltext_canonicalizer",
                downloaded_at=utc_now(),
                used_for_queries=[],
                human_uploaded=True,
                error="",
            )
        )
        canonical["committed_to_db"] = True
        canonical["db_paper_id"] = paper_id
        record_path.write_text(json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        canonical["committed_to_db"] = False

    return {
        "group_id": group.group_id,
        "doi": doi,
        "title": title,
        "ok": bool(validation["ok"]),
        "warnings": validation["warnings"],
        "errors": validation["errors"],
        "fulltext_chars": len(text),
        "chunks": len(chunks),
        "record_path": str(record_path),
        "normalized_text_path": str(normalized_text_path),
        "chunk_index_path": str(chunks_path),
        "source_file": str(source_path) if source_path else "",
        "committed_to_db": bool(canonical.get("committed_to_db")),
    }


def backup_db(db_path: Path, output_dir: Path) -> str:
    if not db_path.exists():
        return ""
    backup_dir = output_dir / "db_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{db_path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(db_path, dst)
    return str(dst)


def run(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir or USER_FULLTEXTS_DIR)
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / "fulltext_canonicalizer" / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt = read_text_file(args.prompt_path or DEFAULT_PROMPT)
    if not prompt:
        raise SystemExit(f"Prompt not found: {args.prompt_path or DEFAULT_PROMPT}")

    bundle_metadata = load_resource_bundle_metadata(Path(args.resource_bundle)) if args.resource_bundle else {}
    groups = discover_document_groups(input_dir, bundle_metadata)
    if args.skip_existing_output:
        existing_stems = {p.stem.replace(".canonical", "") for p in (output_dir / "records").glob("*.canonical.json")}
        groups = [g for g in groups if safe_doi_stem(g.doi, g.group_id) not in existing_stems]
    start = max(0, int(args.offset or 0))
    end = None if int(args.limit or 0) <= 0 else start + int(args.limit)
    selected = groups[start:end]

    library: LiteratureResourceLibrary | None = None
    db_backup = ""
    if args.commit_to_db:
        db_path = Path(args.db_path or DEFAULT_LIBRARY_DB)
        db_backup = backup_db(db_path, output_dir) if args.backup_db else ""
        library = LiteratureResourceLibrary(db_path)

    results: list[dict[str, Any]] = []
    try:
        for idx, group in enumerate(selected, 1):
            print(json.dumps({"event": "start", "index": idx, "total": len(selected), "doi": group.doi, "title": group.title[:100]}, ensure_ascii=False), flush=True)
            result = canonicalize_one(
                group,
                prompt=prompt,
                output_dir=output_dir,
                model_tier=args.model_tier,
                library=library,
                commit_to_db=bool(args.commit_to_db),
            )
            results.append(result)
            print(json.dumps({"event": "done", **{k: result.get(k) for k in ("doi", "ok", "warnings", "errors", "fulltext_chars", "committed_to_db")}}, ensure_ascii=False), flush=True)
            time.sleep(float(args.sleep_seconds or 0))
    finally:
        if library:
            library.close()

    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "prompt_path": str(args.prompt_path or DEFAULT_PROMPT),
        "model_tier": args.model_tier,
        "commit_to_db": bool(args.commit_to_db),
        "db_backup": db_backup,
        "discovered_groups": len(groups),
        "processed": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results,
    }
    (output_dir / "canonicalization_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    jsonl = output_dir / "canonical_records.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for result in results:
            record_path = result.get("record_path")
            if record_path and Path(record_path).exists():
                f.write(Path(record_path).read_text(encoding="utf-8", errors="replace").strip() + "\n")
    print(json.dumps({"event": "summary", "summary_path": str(output_dir / "canonicalization_summary.json"), "canonical_records_jsonl": str(jsonl), "ok": summary["ok"], "failed": summary["failed"], "db_backup": db_backup}, ensure_ascii=False), flush=True)
    return 0 if summary["failed"] == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonicalize local fulltext files into uniform literature records.")
    parser.add_argument("--input-dir", default=str(USER_FULLTEXTS_DIR))
    parser.add_argument("--resource-bundle", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT))
    parser.add_argument("--model-tier", default="standard_model")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--commit-to-db", action="store_true")
    parser.add_argument("--db-path", default=str(DEFAULT_LIBRARY_DB))
    parser.add_argument("--backup-db", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-existing-output", action="store_true")
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
