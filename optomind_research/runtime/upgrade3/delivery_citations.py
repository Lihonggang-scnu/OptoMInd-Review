"""Work order 04: unified figures, citations and cross-references.

Stage order (fixed by Astra review of the first implementation):

1. Figure assets are attached to the HANDLE draft first — captions may carry
   ``[Pxxxx]`` citations, so they must exist before citation numbering.
2. ONE function — :func:`build_delivery_citation_map` — then scans the
   figure-bearing handle draft in reading order (introduction, body prose,
   table rows, figure captions) and produces the final citation map.
3. The reader draft is rendered from that map, and its old handle-form
   reference section is REPLACED by the numbered list generated from the
   same map.

Identity rules: citation tokens may be namespace-qualified — ``[ns::Pxxxx]``
resolves through the catalog whose ``namespace`` is ``ns``, while a bare
``[Pxxxx]`` resolves through the PRIMARY catalog (the first one supplied).
Two runs' ``P0001`` are therefore different papers whenever the text carries
the disambiguated token; catalog order alone never decides ownership.
Unknown tokens stay visible in the text and in the report — nothing is
deleted or invented.

Figures attach by stable local ID (``FIG:<id>``); display numbers are
assigned by POSITION IN THE FINAL TEXT (an asset passed earlier but anchored
later is figure 2, not figure 1).  Asset files are copied into the stage's
``assets/`` directory and referenced by a relative path that resolves from
the manuscript.  Markdown structure (table pipes, math, escapes) is
preserved: numbering rewrites only bracket citation tokens.

No model calls, no network: metadata comes from the local catalogs only, and
missing fields stay missing.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HANDLE_TOKEN_RE = re.compile(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}")
BRACKET_RE = re.compile(r"\[([^\[\]]{1,240})\]")
FIGURE_TOKEN_RE = re.compile(r"@@FIG:([^@]+)@@")
CROSSREF_TABLE_RE = re.compile(r"(详见表|见表|如表|参见表|表)\s*(\d+)")
CROSSREF_FIGURE_RE = re.compile(r"(如图|见图|图)\s*(\d+)")


class CitationMapError(ValueError):
    pass


# ---------------------------------------------------------------------------
# the ONE final citation map
# ---------------------------------------------------------------------------


def _iter_catalog_identities(catalog: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    records = catalog.get("references") or catalog.get("entries") or []
    if isinstance(records, Mapping):
        return [dict(v, source_key=k) if isinstance(v, Mapping) else {"source_key": k}
                for k, v in records.items()]
    return [row for row in (records or []) if isinstance(row, Mapping)]


def _catalog_rows_with_namespace(catalog: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    namespace = str(catalog.get("namespace") or "")
    return [(namespace, row) for row in _iter_catalog_identities(catalog)]


def build_delivery_citation_map(
    *,
    final_handle_draft: str,
    identity_catalogs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The single source of truth for delivery citations.

    Scans ``final_handle_draft`` in reading order.  A bracket citation token
    is either ``Pxxxx`` (resolved through the PRIMARY catalog — the first
    one supplied) or ``ns::Pxxxx`` (resolved through the catalog whose
    ``namespace`` is ``ns``).  Namespaced resolution does not depend on
    catalog order: ``run_b::P0001`` always resolves to run_b's paper even
    when another catalog was supplied first, so two runs' same-number
    handles become two distinct references.
    """

    # Index by the exact token the text may carry.
    index_by_token: dict[str, tuple[str, Mapping[str, Any]]] = {}
    primary_namespace = str(identity_catalogs[0].get("namespace") or "") \
        if identity_catalogs else ""
    namespaces_seen: "OrderedDict[str, set[str]]" = OrderedDict()
    for catalog in identity_catalogs:
        namespace = str(catalog.get("namespace") or "")
        for _, row in _catalog_rows_with_namespace(catalog):
            handles = [
                str(h) for h in (row.get("handles")
                                 or [row.get("source_handle") or row.get("handle")
                                     or row.get("source_key") or ""])
                if str(h).strip()
            ]
            for handle in handles:
                token = f"{namespace}::{handle}" if namespace else handle
                # First declaration wins per exact token; namespaced tokens
                # are unique per run, so ownership never depends on catalog
                # order.
                index_by_token.setdefault(token, (namespace, row))
                if namespace:
                    namespaces_seen.setdefault(namespace, set()).add(handle)

    order: list[str] = []
    token_by_citation: dict[str, str] = {}
    by_canonical: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    unknown: list[str] = []

    def _identity_key(row: Mapping[str, Any]) -> str:
        """Identity of the PAPER, independent of which run cited it.

        Normalized DOI first, then paper_id, then the primary handle.  The
        same paper cited by two runs merges into ONE reference, while
        genuinely different papers stay distinct.
        """
        doi = str(row.get("doi") or "").strip().lower()
        if doi:
            return "doi:" + doi
        paper_id = str(row.get("paper_id") or "").strip()
        if paper_id:
            return "pid:" + paper_id
        handles = row.get("handles") or []
        return "handle:" + (str(handles[0]) if handles else "unknown")

    def resolve(token: str) -> tuple[str, Mapping[str, Any]] | None:
        indexed = index_by_token.get(token)
        if indexed is None and "::" not in token and primary_namespace:
            indexed = index_by_token.get(f"{primary_namespace}::{token}")
        return indexed

    for match in BRACKET_RE.finditer(final_handle_draft):
        for token in re.findall(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}", match.group(1)):
            resolved = resolve(token)
            if resolved is None:
                if token not in unknown:
                    unknown.append(token)
                continue
            namespace, row = resolved
            key = _identity_key(row)
            token_by_citation[token] = key
            if key not in by_canonical:
                by_canonical[key] = {
                    "canonical": key,
                    "handles": [],
                    "title": str(row.get("title") or ""),
                    "year": str(row.get("year") or ""),
                    "doi": str(row.get("doi") or ""),
                    "paper_id": str(row.get("paper_id") or row.get("handles") or ""),
                    "namespace": namespace,
                }
                order.append(key)
            if token not in by_canonical[key]["handles"]:
                by_canonical[key]["handles"].append(token)

    number_by_token = {token: index for index, token in enumerate(order_tokens(order, by_canonical), start=1)} \
        if False else {}
    # number by canonical, then derive token numbers
    number_by_canonical = {key: index for index, key in enumerate(order, start=1)}
    references = []
    for key in order:
        row = by_canonical[key]
        references.append({
            "reference_number": number_by_canonical[key],
            "canonical": key,
            "handles": list(row["handles"]),
            "title": row["title"],
            "year": row["year"],
            "doi": row["doi"],
            "paper_id": row["paper_id"],
            "namespace": row["namespace"],
        })
    number_by_token = {}
    token_details = {}
    handle_to_number = {}
    for token, key in token_by_citation.items():
        number = number_by_canonical[key]
        number_by_token[token] = number
        # The FULL namespaced token, its source namespace and its number are
        # exported so downstream consumers see the complete run→paper mapping
        # (not a lossy bare handle).  `handle_to_number` stays as the
        # bare-handle convenience view for single-run manuscripts.
        source_namespace = (token.split("::", 1)[0]
                            if "::" in token else primary_namespace)
        token_details[token] = {"number": number,
                                "namespace": source_namespace,
                                "canonical": key}
        bare = token.split("::")[-1]
        handle_to_number.setdefault(bare, number)
    return {
        "references_order": order,
        "reference_count": len(order),
        "number_by_canonical": number_by_canonical,
        "number_by_token": number_by_token,
        "token_details": token_details,
        "handle_to_number": handle_to_number,
        "references": references,
        "unknown_handles": unknown,
        "namespaces": {ns: sorted(hs) for ns, hs in namespaces_seen.items()},
    }


# ---------------------------------------------------------------------------
# consumers
# ---------------------------------------------------------------------------


def render_reader_citations(
    handle_draft: str,
    citation_map: Mapping[str, Any],
) -> tuple[str, int, list[str]]:
    """Replace citation tokens with ``[N]`` using the ONE final map.

    Bracket citations may be bare ``[Pxxxx]`` or namespaced
    ``[ns::Pxxxx]``; each resolves through the map's ``number_by_token``.
    Unmapped tokens stay visible and are returned for the report.  Bare
    ``Pxxxx``-shaped prose tokens outside brackets are untouched.
    """

    number_by_token = citation_map["number_by_token"]
    residual: list[str] = []

    def bracket_sub(match: re.Match[str]) -> str:
        inner = match.group(1)
        tokens = re.findall(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}", inner)
        if not tokens:
            return match.group(0)
        numbers = []
        for token in tokens:
            number = number_by_token.get(token)
            if number is None:
                if token not in residual:
                    residual.append(token)
                numbers.append(token)
            else:
                numbers.append(str(number))
        return "[" + "][".join(numbers) + "]"

    out = BRACKET_RE.sub(bracket_sub, handle_draft)
    return out, len(number_by_token), residual


# ---------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------


def attach_figures(
    manuscript: str,
    figure_assets: Sequence[Mapping[str, Any]],
    *,
    assets_dir: str | Path | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    """Insert figure assets into the handle draft, numbering by position.

    Insertion happens with a placeholder token (``@@FIG:<id>@@``) in the
    caption; the caller numbers the tokens afterwards by scanning the FINAL
    text, so an asset passed earlier but anchored later is figure 2 — the
    display order always follows the manuscript, not the input order.  Each
    real asset file is copied into ``assets_dir`` (default
    ``<manuscript dir>/assets``) and referenced by a relative path, so the
    Markdown resolves next to the manuscript.
    """

    if assets_dir is None:
        assets_dir = Path(manuscript).parent / "assets" if isinstance(manuscript, (str, Path)) else Path("assets")
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    lines = manuscript.splitlines(keepends=True)
    placed: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    resolved: dict[str, dict[str, Any]] = {}

    def _asset_filename(fig_id: str, source: Path) -> str:
        # Stable per-figure filename: the sanitized figure_id keeps two runs'
        # same-named assets from overwriting each other; the original suffix
        # is preserved so the Markdown still renders.
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", fig_id).strip("_") or "figure"
        return f"{safe}{source.suffix.lower()}"

    for asset in figure_assets:
        fig_id = str(asset.get("figure_id") or "")
        source = Path(str(asset.get("path") or ""))
        if source.is_file():
            dest = assets_dir / _asset_filename(fig_id, source)
            if source.resolve() != dest.resolve():
                shutil.copyfile(source, dest)
            resolved[fig_id] = {**dict(asset), "rel_path": f"assets/{dest.name}"}
        else:
            missing.append({"figure_id": fig_id, "path": str(source),
                            "reason": "asset_file_missing",
                            "caption": str(asset.get("caption") or "")})

    for asset in figure_assets:
        fig_id = str(asset.get("figure_id") or "")
        if fig_id not in resolved:
            continue
        entry = resolved[fig_id]
        probe = str(entry.get("anchor_probe") or "")
        anchor_idx = next((i for i, line in enumerate(lines) if probe and probe in line), None)
        if anchor_idx is None:
            missing.append({"figure_id": fig_id, "path": str(entry.get("path")),
                            "reason": "anchor_not_found",
                            "caption": str(entry.get("caption") or "")})
            continue
        token = f"@@FIG:{fig_id}@@"
        caption = str(entry.get("caption") or "")
        alt = re.sub(r"（fixture[^）]*）", "", caption).strip() or fig_id
        block = (f"![{alt}]({entry['rel_path']})\n\n"
                 f"**图 {token}. {caption}**\n\n")
        lines.insert(anchor_idx + 1, block)
    captions_by_id = {rid: str(entry.get("caption") or "")
                      for rid, entry in resolved.items()}
    return "".join(lines), captions_by_id, missing


def number_figure_tokens(
    manuscript_with_tokens: str,
    placed: list[dict[str, Any]],
    captions_by_id: Mapping[str, str] | None = None,
) -> str:
    """Replace ``@@FIG:<id>@@`` tokens with display numbers in reading order.

    Each encountered token appends a ``placed`` row (figure_id,
    display_number, caption), so the report reflects the manuscript's actual
    reading order rather than the asset input order.
    """

    captions_by_id = captions_by_id or {}
    number = 1

    def _num(match: re.Match[str]) -> str:
        nonlocal number
        fig_id = match.group(1)
        placed.append({"figure_id": fig_id, "display_number": number,
                       "caption": captions_by_id.get(fig_id, "")})
        value = str(number)
        number += 1
        return value

    return FIGURE_TOKEN_RE.sub(_num, manuscript_with_tokens)


def update_cross_references(
    manuscript: str,
    table_moves: Mapping[int, int] | None = None,
    figure_moves: Mapping[int, int] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Rewrite ``见表 N`` / ``如图 N`` cross-references after moves.

    ``table_moves``/``figure_moves`` map OLD display numbers to NEW ones.
    Each rewrite is returned for the audit trail; unmatched numbers are left
    alone.
    """

    table_moves = dict(table_moves or {})
    figure_moves = dict(figure_moves or {})
    rewrites: list[dict[str, str]] = []

    def table_sub(match: re.Match[str]) -> str:
        prefix, old = match.group(1), int(match.group(2))
        new = table_moves.get(old)
        if new is None or new == old:
            return match.group(0)
        rewrites.append({"kind": "table", "old": old, "new": new})
        return f"{prefix} {new}"

    def figure_sub(match: re.Match[str]) -> str:
        prefix, old = match.group(1), int(match.group(2))
        new = figure_moves.get(old)
        if new is None or new == old:
            return match.group(0)
        rewrites.append({"kind": "figure", "old": old, "new": new})
        return f"{prefix} {new}"

    # Each cross-reference phrase is rewritten exactly once per sub pass, and
    # a pass never revisits its own output, so newly written numbers (e.g.
    # the generated figure numbers) are not re-transformed.
    text = CROSSREF_TABLE_RE.sub(table_sub, manuscript)
    text = CROSSREF_FIGURE_RE.sub(figure_sub, text)
    return text, rewrites


# ---------------------------------------------------------------------------
# references section replacement
# ---------------------------------------------------------------------------


def render_references_section(citation_map: Mapping[str, Any]) -> str:
    """The numbered reference list generated from the ONE final map."""

    lines = ["## 参考文献", ""]
    for ref in citation_map["references"]:
        line = (f"[{ref['reference_number']}] {ref['title']}"
                + (f"（{ref['year']}）" if ref["year"] else ""))
        if ref["doi"]:
            line += f". DOI: {ref['doi']}"
        lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def replace_references_section(reader_text: str, citation_map: Mapping[str, Any]) -> tuple[str, bool]:
    """Swap the old handle-form reference section for the numbered one.

    Returns ``(new_text, replaced)``; without an existing section the
    numbered list is appended at the end.
    """

    block = render_references_section(citation_map)
    pattern = re.compile(
        r"^#{2,3}\s+参考文献[^\n]*\n(?:(?!^#{2,3}\s).)*",
        re.MULTILINE | re.DOTALL,
    )
    if pattern.search(reader_text):
        return pattern.sub(lambda _m: block, reader_text, count=1), True
    return reader_text.rstrip() + "\n\n" + block, False


# ---------------------------------------------------------------------------
# stage runner
# ---------------------------------------------------------------------------


def run_figures_citations_stage(
    *,
    final_draft_path: str | Path,
    identity_catalogs: Sequence[Mapping[str, Any]],
    out_dir: str | Path,
    figure_assets: Sequence[Mapping[str, Any]] = (),
    table_moves: Mapping[int, int] | None = None,
    figure_moves: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    """Work-order 04 stage over the FINAL manuscript (03 output).

    Fixed order (per Astra rework):
    1. attach figures into the handle draft — captions may cite;
    2. convert OLD cross-references while figure numbers are still
       placeholders (``@@FIG:id@@``), so freshly generated final numbers are
       never re-transformed by the move rules;
    3. generate final figure numbers by reading order;
    4. build the ONE citation map over the figure-bearing draft, render the
       numbered reader draft, and replace its old reference list.
    """

    final_draft_path = Path(final_draft_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    handle_text = final_draft_path.read_text(encoding="utf-8")

    figure_text, _inserted, missing_assets = attach_figures(
        handle_text, figure_assets, assets_dir=out_dir / "assets")
    # Step 2 before step 3: old cross-references are converted while the
    # inserted figures carry placeholder tokens, so the move rules can never
    # rewrite freshly generated numbers.
    placed: list[dict[str, Any]] = []
    if table_moves or figure_moves:
        figure_text, rewrites = update_cross_references(
            figure_text, table_moves, figure_moves)
    else:
        rewrites = []
    figure_text = number_figure_tokens(figure_text, placed)

    citation_map = build_delivery_citation_map(
        final_handle_draft=figure_text, identity_catalogs=identity_catalogs)
    reader_text, _mapped, residual = render_reader_citations(figure_text, citation_map)
    reader_text, references_replaced = replace_references_section(reader_text, citation_map)

    handles_path = out_dir / "MANUSCRIPT_HANDLES.md"
    handles_path.write_text(figure_text, encoding="utf-8", newline="\n")
    reader_path = out_dir / "MANUSCRIPT_READER.md"
    reader_path.write_text(reader_text, encoding="utf-8", newline="\n")
    references_path = out_dir / "REFERENCES.json"
    _write_json(references_path, {
        "schema_version": "review_v2_delivery.04.references.v1",
        "citation_map_source": "build_delivery_citation_map",
        "references": citation_map["references"],
        "token_details": citation_map["token_details"],
        "handle_to_reference": citation_map["handle_to_number"],
        "unknown_handles": citation_map["unknown_handles"],
    })
    mapping_path = out_dir / "FIGURE_CITATION_MAP.json"
    _write_json(mapping_path, {
        "schema_version": "review_v2_delivery.04.maps.v1",
        "citation_map_source": "build_delivery_citation_map",
        "figures_placed": placed,
        "figures_missing": missing_assets,
        "cross_reference_rewrites": rewrites,
    })
    report = {
        "stage": "figures_citations",
        "status": "pending" if residual else "complete",
        "reference_count": len(citation_map["references"]),
        "unknown_citation_tokens": residual,
        "unknown_identity_handles": citation_map["unknown_handles"],
        "cross_run_namespaces": citation_map["namespaces"],
        "figures_placed": placed,
        "figures_missing": missing_assets,
        "cross_reference_rewrites": rewrites,
        "references_section_replaced": references_replaced,
        "handles_draft": str(handles_path),
        "reader_draft": str(reader_path),
        "references_path": str(references_path),
        "mapping_path": str(mapping_path),
        "assets_dir": str(out_dir / "assets"),
        "model_calls": 0,
        "external_requests": 0,
    }
    _write_json(out_dir / "STAGE_REPORT.json", report)
    return report


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
