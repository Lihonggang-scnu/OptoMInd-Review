"""Build an arXiv-compatible LaTeX manuscript from Review Harness assets.

The renderer is deliberately deterministic.  Scientific prose, citations, and
verified visual assets are received from upstream stages; this module only
normalizes them into a portable LaTeX source tree, compiles the tree, validates
the resulting PDF, and creates a clean arXiv upload archive.

Pandoc is used for Markdown-to-LaTeX conversion instead of maintaining a
home-grown Markdown parser.  Standard TeX Live packages and PDFLaTeX are used
so that the source remains portable to arXiv's supported TeX environment.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

from ftfy import fix_text

from .artifact_store import atomic_write_json
from .publication_figure_processor import prepare_publication_figure
from .publication_integrity import prepare_publication_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REF_PATTERN = re.compile(r"\[REF:([^\]]+)\]")
MOJIBAKE_PATTERN = re.compile(
    r"(?:鈥|銆|锟|鏂|浣|渭|眉|怑|攚|攖|攕|攏|攅|鈭|鈮)"
)
LATEX_WARNING_PATTERN = re.compile(
    r"(?:LaTeX Warning: Citation .* undefined|"
    r"LaTeX Warning: Reference .* undefined|"
    r"There were undefined references|"
    r"Unicode character .* not set up for use with LaTeX)",
    re.IGNORECASE,
)
DOI_PATTERN = re.compile(r"^doi:(10\.\d{4,9}/\S+)$", re.IGNORECASE)
DOI_VALUE_PATTERN = re.compile(r"^(10\.\d{4,9}/\S+)$", re.IGNORECASE)
SAFE_FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _contains_likely_mojibake(text: str) -> bool:
    """Distinguish repairable encoding damage from legitimate CJK text.

    Several mojibake sentinel code points are also valid Chinese characters.
    A hit is therefore actionable only when ``ftfy`` can actually reduce the
    sentinel count. The Unicode replacement character remains unambiguous.
    """

    value = str(text or "")
    if "\ufffd" in value:
        return True
    before = len(MOJIBAKE_PATTERN.findall(value))
    if not before:
        return False
    repaired = fix_text(value)
    return len(MOJIBAKE_PATTERN.findall(repaired)) < before


def _read_json(path: Optional[Path]) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _resolve_artifact_path(raw: Any, *, package_path: Path) -> Optional[Path]:
    value = str(raw or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    # Relative references are owned by the JSON they appear in, so the
    # package directory takes precedence; PROJECT_ROOT remains a legacy
    # fallback for older packages that used repository-relative addresses.
    for base in (package_path.parent, PROJECT_ROOT):
        resolved = base / candidate
        if resolved.exists():
            return resolved
    return package_path.parent / candidate


def _portable_ref(
    path: Any,
    *,
    output_dir: Path,
    package_root: Optional[Path] = None,
) -> str:
    """Serialize a path relative to the report directory when it is packaged.

    The portable package root owns all scientific assets (the content JSON and
    the visual inputs under ``visual_assets/``) and may be an ancestor of the
    report directory, so serialized refs can legitimately contain ``..``
    segments (for example ``../../visual_assets/fig.png``).  Truly external
    targets keep their absolute address so legacy inputs remain inspectable,
    and absolute inputs are still accepted for reading at runtime.
    """

    value = str(path or "").strip()
    if not value:
        return ""
    candidate = Path(value)
    if not candidate.is_absolute():
        return candidate.as_posix()
    resolved_target = candidate.resolve()
    resolved_output = output_dir.resolve()
    if package_root is not None:
        resolved_root = package_root.resolve()
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError:
            pass
        else:
            return Path(
                os.path.relpath(resolved_target, resolved_output)
            ).as_posix()
    try:
        relative = resolved_target.relative_to(resolved_output)
    except ValueError:
        return str(candidate)
    return relative.as_posix()


def _portable_tool_ref(path: Any) -> str:
    """Serialize an executable location as its portable command basename.

    Machine installation locations must not leak into relocatable reports;
    the command basename together with the version/status fields is enough to
    inspect the build on another machine.
    """

    value = str(path or "").strip()
    if not value:
        return ""
    return Path(value).name


def _portable_report_refs(
    report: dict[str, Any],
    *,
    output_dir: Path,
    package_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Return a serializable copy of the build report with portable refs."""

    portable = dict(report)
    portable["content_package_path"] = _portable_ref(
        report.get("content_package_path"),
        output_dir=output_dir,
        package_root=package_root,
    )
    portable["preview_paths"] = [
        _portable_ref(path, output_dir=output_dir, package_root=package_root)
        for path in (report.get("preview_paths") or [])
    ]
    portable["rejected_visual_placements"] = [
        _portable_rejected_entry(
            row,
            output_dir=output_dir,
            package_root=package_root,
        )
        for row in (report.get("rejected_visual_placements") or [])
    ]
    portable["artifacts"] = {
        key: _portable_ref(
            value,
            output_dir=output_dir,
            package_root=package_root,
        )
        for key, value in (report.get("artifacts") or {}).items()
    }
    pandoc = report.get("pandoc")
    if isinstance(pandoc, dict):
        portable["pandoc"] = {
            **pandoc,
            "path": _portable_tool_ref(pandoc.get("path")),
        }
    return portable


def _portable_audit_entry(
    audit: dict[str, Any],
    *,
    output_dir: Path,
    package_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Copy a figure asset audit entry with portable path fields."""

    portable = dict(audit)
    for key in ("source_path", "publication_path"):
        if key in portable:
            portable[key] = _portable_ref(
                portable[key],
                output_dir=output_dir,
                package_root=package_root,
            )
    return portable


def _portable_rejected_entry(
    row: dict[str, Any],
    *,
    output_dir: Path,
    package_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Copy a rejected-figure row with a portable image reference."""

    portable = dict(row)
    if "local_image_path" in portable:
        portable["local_image_path"] = _portable_ref(
            portable["local_image_path"],
            output_dir=output_dir,
            package_root=package_root,
        )
    return portable


def _clean_unicode(value: Any) -> str:
    """Repair mojibake and normalize punctuation before Pandoc sees it."""

    text = html.unescape(fix_text(str(value or "")))
    if MOJIBAKE_PATTERN.search(text):
        try:
            repaired = text.encode("gbk").decode("utf-8")
            if len(MOJIBAKE_PATTERN.findall(repaired)) < len(
                MOJIBAKE_PATTERN.findall(text)
            ):
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    # Academic metadata occasionally carries lightweight HTML such as
    # ``<i>q</i>`` or ``<sub>2</sub>``.  Raw tags are never valid manuscript
    # text and otherwise leak visibly into the bibliography.
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = unicodedata.normalize("NFC", text)
    replacements = {
        "\u00a0": " ",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "--",
        "\u2014": "---",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


_GREEK_TEX = {
    "Α": "A", "Β": "B", "Γ": r"\Gamma", "Δ": r"\Delta",
    "Ε": "E", "Ζ": "Z", "Η": "H", "Θ": r"\Theta",
    "Ι": "I", "Κ": "K", "Λ": r"\Lambda", "Μ": "M",
    "Ν": "N", "Ξ": r"\Xi", "Ο": "O", "Π": r"\Pi",
    "Ρ": "P", "Σ": r"\Sigma", "Τ": "T", "Υ": r"\Upsilon",
    "Φ": r"\Phi", "Χ": "X", "Ψ": r"\Psi", "Ω": r"\Omega",
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma",
    "δ": r"\delta", "ε": r"\epsilon", "ϵ": r"\varepsilon",
    "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ϑ": r"\vartheta", "ι": r"\iota", "κ": r"\kappa",
    "ϰ": r"\varkappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "ο": "o", "π": r"\pi",
    "ϖ": r"\varpi", "ρ": r"\rho", "ϱ": r"\varrho",
    "σ": r"\sigma", "ς": r"\varsigma", "τ": r"\tau",
    "υ": r"\upsilon", "φ": r"\phi", "ϕ": r"\varphi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
}

_SCIENTIFIC_SYMBOL_TEX = {
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div",
    "·": r"\cdot", "∙": r"\cdot", "≤": r"\leq", "≥": r"\geq",
    "≪": r"\ll", "≫": r"\gg", "≈": r"\approx", "≃": r"\simeq",
    "≅": r"\cong", "≠": r"\neq", "≡": r"\equiv", "∝": r"\propto",
    "∞": r"\infty", "∂": r"\partial", "∇": r"\nabla",
    "∈": r"\in", "∉": r"\notin", "∋": r"\ni", "⊂": r"\subset",
    "⊆": r"\subseteq", "⊃": r"\supset", "⊇": r"\supseteq",
    "∪": r"\cup", "∩": r"\cap", "∑": r"\sum", "∏": r"\prod",
    "∫": r"\int", "∮": r"\oint", "∧": r"\wedge", "∨": r"\vee",
    "¬": r"\neg", "⊕": r"\oplus", "⊗": r"\otimes",
    "⊥": r"\perp", "∥": r"\parallel", "∠": r"\angle",
    "→": r"\rightarrow", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "↦": r"\mapsto", "↑": r"\uparrow", "↓": r"\downarrow",
    "∼": r"\sim", "′": r"^\prime", "″": r"^{\prime\prime}",
    "ℏ": r"\hbar", "ℓ": r"\ell", "ℜ": r"\Re", "ℑ": r"\Im",
    "⟨": r"\langle", "⟩": r"\rangle", "〈": r"\langle",
    "〉": r"\rangle", "°": r"^\circ",
}
_SUPERSCRIPT_TRANSLATION = str.maketrans(
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ",
    "0123456789+-=()n",
)
_SUBSCRIPT_MAP = {
    **dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+-=()")),
    "ₐ": "a", "ₑ": "e", "ₕ": "h", "ᵢ": "i", "ⱼ": "j",
    "ₖ": "k", "ₗ": "l", "ₘ": "m", "ₙ": "n", "ₒ": "o",
    "ₚ": "p", "ᵣ": "r", "ₛ": "s", "ₜ": "t", "ₓ": "x",
}
_PROTECTED_SCIENTIFIC_PATTERN = re.compile(
    r"```.*?```|`[^`\n]*`|\$\$.*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$",
    re.DOTALL,
)
_RISKY_UNICODE_RANGES = (
    (0x0370, 0x03FF),
    (0x2070, 0x209F),
    (0x2100, 0x214F),
    (0x2190, 0x21FF),
    (0x2200, 0x22FF),
    (0x27C0, 0x27EF),
    (0x1D400, 0x1D7FF),
)


def _protect_existing_math_and_code(text: str) -> tuple[str, list[str]]:
    protected: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = f"OPTOMINDPROTECTED{len(protected):06d}TOKEN"
        protected.append(match.group(0))
        return token

    return _PROTECTED_SCIENTIFIC_PATTERN.sub(replace, text), protected


def _restore_existing_math_and_code(text: str, protected: list[str]) -> str:
    for index, value in enumerate(protected):
        text = text.replace(
            f"OPTOMINDPROTECTED{index:06d}TOKEN",
            value,
        )
    return text


def _convert_scientific_unicode(text: str) -> str:
    """Convert Unicode notation by class while preserving TeX and code."""

    text, protected = _protect_existing_math_and_code(text)
    text = "".join(
        unicodedata.normalize("NFKC", char)
        if 0x1D400 <= ord(char) <= 0x1D7FF
        else char
        for char in text
    )
    # Preserve a common bra-ket unit before converting its individual glyphs.
    text = re.sub(
        r"\|([A-Za-zΑ-Ωα-ωϑϕϵϖϱϰ]+)[⟩〉]",
        lambda match: r"$\lvert "
        + "".join(_GREEK_TEX.get(char, char) for char in match.group(1))
        + r"\rangle$",
        text,
    )
    superscripts = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ"
    subscripts = "".join(_SUBSCRIPT_MAP)
    text = re.sub(
        f"[{re.escape(superscripts)}]+",
        lambda match: "$^{"
        + match.group(0).translate(_SUPERSCRIPT_TRANSLATION)
        + "}$",
        text,
    )
    text = re.sub(
        f"[{re.escape(subscripts)}]+",
        lambda match: "$_{"
        + "".join(_SUBSCRIPT_MAP[char] for char in match.group(0))
        + "}$",
        text,
    )
    radical_operand = (
        "".join(_GREEK_TEX)
        + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    )
    text = re.sub(
        rf"√\s*([{re.escape(radical_operand)}])",
        lambda match: r"$\sqrt{"
        + _GREEK_TEX.get(match.group(1), match.group(1))
        + "}$",
        text,
    )
    for char, tex in {**_GREEK_TEX, **_SCIENTIFIC_SYMBOL_TEX}.items():
        text = text.replace(char, f" ${tex}$ ")
    text = text.replace("√", r" $\sqrt{\cdot}$ ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return _restore_existing_math_and_code(text, protected)


def _scientific_unicode_preflight(text: str) -> dict[str, Any]:
    """Report Unicode that may require a Unicode-native TeX engine."""

    remaining: dict[str, dict[str, Any]] = {}
    dangerous: dict[str, dict[str, Any]] = {}
    for char in text:
        codepoint = ord(char)
        category = unicodedata.category(char)
        row = {
            "character": char,
            "codepoint": f"U+{codepoint:04X}",
            "name": unicodedata.name(char, "UNKNOWN"),
            "category": category,
        }
        if char == "\ufffd" or (
            category in {"Cc", "Cs"} and char not in "\n\r\t"
        ):
            dangerous[char] = row
        if codepoint > 127 and (
            category == "Sm" or any(
            lower <= codepoint <= upper
            for lower, upper in _RISKY_UNICODE_RANGES
            )
        ):
            remaining[char] = row
    return {
        "remaining_risky_characters": list(remaining.values()),
        "dangerous_characters": list(dangerous.values()),
        "remaining_risky_count": len(remaining),
        "dangerous_count": len(dangerous),
        "unicode_native_engine_required": bool(remaining),
    }


def _prepare_scientific_markdown_with_integrity(
    text: str,
    *,
    preserve_verification_labels: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Normalize publication prose and return its deterministic audit."""

    text, integrity = prepare_publication_markdown(
        _clean_unicode(text),
        preserve_verification_labels=preserve_verification_labels,
    )
    # Internal workflow section IDs are traceability metadata, not reader-
    # facing manuscript titles.  Keep them in JSON ledgers and remove them
    # from rendered Markdown headings.
    text = re.sub(
        r"^(#{1,6}\s+)S\d{1,3}\s*[:：—-]\s*",
        r"\1",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    # Review Harness sections currently start at H2 because the title is kept
    # outside the manuscript body.  Shift all headings up by one level so the
    # generated paper has top-level \section commands.
    heading_levels = [
        len(match.group(1))
        for match in re.finditer(r"^(#{1,6})\s+", text, re.MULTILINE)
    ]
    if heading_levels and min(heading_levels) > 1:
        text = re.sub(
            r"^(#{2,6})(?=\s+)",
            lambda match: match.group(1)[1:],
            text,
            flags=re.MULTILINE,
        )
    return text, integrity


def _prepare_scientific_markdown(text: str) -> str:
    """Backward-compatible public helper used by existing callers and tests."""

    return _prepare_scientific_markdown_with_integrity(text)[0]


def _strip_embedded_title_and_abstract(text: str) -> str:
    """Avoid duplicating metadata when the complete review includes front matter."""

    lines = str(text or "").splitlines()
    if lines and re.match(r"^#\s+[^#]", lines[0]):
        lines = lines[1:]
    abstract_start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(
                r"^#{1,3}\s+(?:abstract|摘要|中文摘要)\s*$",
                line.strip(),
                re.IGNORECASE,
            )
        ),
        None,
    )
    if abstract_start is not None:
        abstract_end = next(
            (
                index
                for index in range(abstract_start + 1, len(lines))
                if lines[index].startswith("## ")
            ),
            len(lines),
        )
        del lines[abstract_start:abstract_end]
    return "\n".join(lines).strip()


def _citation_key(paper_id: str) -> str:
    value = str(paper_id or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    digest = hashlib.sha1(str(paper_id).encode("utf-8")).hexdigest()[:8]
    base = value[:64] or "reference"
    return f"{base}_{digest}"


def _reference_alias_map_from_catalog(
    catalog: dict[str, Any],
) -> dict[str, str]:
    """Return unambiguous source-marker to canonical-reference mappings."""

    candidates: dict[str, set[str]] = {}
    for entry in catalog.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        canonical = str(
            entry.get("canonical_identity") or entry.get("identity") or ""
        ).strip()
        if not canonical:
            continue
        aliases = [
            canonical,
            entry.get("identity"),
            *(entry.get("markers") or []),
            *(entry.get("aliases") or []),
        ]
        for raw_alias in aliases:
            alias = str(raw_alias or "").strip()
            if alias:
                candidates.setdefault(alias, set()).add(canonical)
    return {
        alias: next(iter(canonical_ids))
        for alias, canonical_ids in candidates.items()
        if len(canonical_ids) == 1
    }


def _load_reference_alias_map(
    package: dict[str, Any],
    *,
    package_path: Path,
) -> dict[str, str]:
    raw_path = package.get("metadata_catalog_path")
    if not raw_path:
        raw_path = package.get("artifacts", {}).get("metadata_catalog")
    catalog_path = _resolve_artifact_path(
        raw_path,
        package_path=package_path,
    )
    return _reference_alias_map_from_catalog(_read_json(catalog_path))


def _replace_reference_markers(
    markdown: str,
    *,
    canonical_id_by_alias: Optional[dict[str, str]] = None,
) -> tuple[str, list[str], dict[str, str]]:
    ordered_ids: list[str] = []
    key_by_id: dict[str, str] = {}
    aliases = canonical_id_by_alias or {}

    def replace(match: re.Match[str]) -> str:
        source_id = match.group(1).strip()
        paper_id = aliases.get(source_id, source_id)
        if paper_id not in key_by_id:
            ordered_ids.append(paper_id)
            key_by_id[paper_id] = _citation_key(paper_id)
        return f"[@{key_by_id[paper_id]}]"

    return REF_PATTERN.sub(replace, markdown), ordered_ids, key_by_id


def _metadata_score(record: dict[str, Any]) -> int:
    score = 0
    for field_name in ("title", "authors", "venue", "doi"):
        value = record.get(field_name)
        if value not in (None, "", []):
            score += 1
    year = str(record.get("year") or "").strip()
    if re.fullmatch(r"\d{4}", year):
        score += 2
    elif year:
        score += 1
    if record.get("acquisition_status") == "fulltext":
        score += 1
    return score


def _collect_source_metadata(run_dir: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    patterns = (
        "section_coverage/sections/*/SECTION_SOURCE_LEDGER.json",
        "section_coverage/sections/*/SECTION_MATERIAL_PACKAGE.json",
    )
    for pattern in patterns:
        for path in sorted(run_dir.glob(pattern)):
            value = _read_json(path)
            candidates: list[Any] = []
            for key in ("sources", "papers", "selected_sources"):
                if isinstance(value.get(key), list):
                    candidates.extend(value[key])
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                paper_id = str(row.get("paper_id") or "").strip()
                if not paper_id:
                    continue
                prior = records.get(paper_id)
                if prior is None or _metadata_score(row) > _metadata_score(prior):
                    records[paper_id] = dict(row)
    return records


def _collect_catalog_metadata(
    package: dict[str, Any],
    *,
    package_path: Path,
) -> dict[str, dict[str, Any]]:
    """Load resolved catalog records into the bibliography merge plane."""

    raw_path = package.get("metadata_catalog_path")
    if not raw_path:
        raw_path = package.get("artifacts", {}).get("metadata_catalog")
    catalog_path = _resolve_artifact_path(
        raw_path,
        package_path=package_path,
    )
    catalog = _read_json(catalog_path)
    records: dict[str, dict[str, Any]] = {}
    for entry in catalog.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        canonical = str(
            entry.get("canonical_identity") or entry.get("identity") or ""
        ).strip()
        if not canonical:
            continue
        records[canonical] = {
            "paper_id": canonical,
            "title": entry.get("title") or "",
            "authors": list(entry.get("authors") or []),
            "year": entry.get("year") or "",
            "venue": entry.get("venue") or "",
            "doi": entry.get("doi") or "",
            "url": entry.get("url") or "",
            "s2_id": entry.get("s2_id") or "",
            "metadata_source": "publication_metadata_catalog",
        }
    return records


def _collect_kb_metadata(
    kb_path: Optional[Path],
    paper_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    if not kb_path or not Path(kb_path).is_file():
        return {}
    wanted = {str(value) for value in paper_ids}
    if not wanted:
        return {}
    records: dict[str, dict[str, Any]] = {}
    def enrich_from_raw(record: dict[str, Any], raw: Any) -> dict[str, Any]:
        """Recover S2 snippet metadata nested below a canonical text chunk."""

        if not isinstance(raw, dict):
            return record
        candidates = [raw]
        nested = raw.get("raw_metadata")
        if isinstance(nested, dict):
            candidates.append(nested)
        for candidate in list(candidates):
            s2_item = candidate.get("s2_item") if isinstance(candidate, dict) else None
            if isinstance(s2_item, dict):
                candidates.append(s2_item)
                paper = s2_item.get("paper")
                if isinstance(paper, dict):
                    candidates.append(paper)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            external = candidate.get("externalIds") or candidate.get("external_ids") or {}
            if isinstance(external, dict) and not record.get("doi"):
                record["doi"] = str(external.get("DOI") or external.get("doi") or "")
            oa = candidate.get("openAccessInfo") or candidate.get("openAccessPdf") or {}
            if isinstance(oa, dict) and not record.get("url"):
                record["url"] = str(oa.get("url") or "")
            for key in ("title", "year", "venue", "url"):
                if not record.get(key) and candidate.get(key) not in (None, "", []):
                    record[key] = candidate.get(key)
            venue = candidate.get("publicationVenue") or candidate.get("journal") or {}
            if not record.get("venue") and isinstance(venue, dict):
                record["venue"] = str(venue.get("name") or "")
            authors = candidate.get("authors")
            if not record.get("authors") and isinstance(authors, list):
                names = []
                for author in authors:
                    if isinstance(author, dict):
                        name = str(author.get("name") or "").strip()
                    else:
                        name = str(author or "").strip()
                    if name:
                        names.append(name)
                if names:
                    record["authors"] = names
        return record

    connection = sqlite3.connect(str(kb_path))
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "papers" in tables:
            columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(papers)").fetchall()
            }
        else:
            columns = set()
        placeholders = ",".join("?" for _ in wanted)
        if {"paper_id", "raw_json"}.issubset(columns):
            query = (
                "SELECT paper_id, doi, title, year, venue, raw_json "
                f"FROM papers WHERE paper_id IN ({placeholders})"
            )
            for row in connection.execute(query, sorted(wanted)):
                record = dict(row)
                raw = {}
                try:
                    raw = json.loads(str(record.pop("raw_json") or "{}"))
                except Exception:
                    raw = {}
                records[str(record.get("paper_id") or "")] = enrich_from_raw(record, raw)
        # S2 body snippets are canonical evidence in their own right.  The
        # first S2-first run stored their identity in text_chunks even when no
        # corresponding papers row existed.  Treat that as a first-class source
        # of title/authorship metadata, not as a degraded fallback.
        if "text_chunks" in tables:
            chunk_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(text_chunks)").fetchall()
            }
            required = {"paper_id", "raw_json"}
            if required.issubset(chunk_columns):
                selected = ["paper_id", "raw_json"]
                for name in ("doi", "title"):
                    if name in chunk_columns:
                        selected.append(name)
                query = (
                    "SELECT " + ", ".join(selected) + " FROM text_chunks "
                    f"WHERE paper_id IN ({placeholders})"
                )
                for row in connection.execute(query, sorted(wanted)):
                    record = dict(row)
                    paper_id = str(record.get("paper_id") or "")
                    if not paper_id:
                        continue
                    raw = {}
                    try:
                        raw = json.loads(str(record.pop("raw_json") or "{}"))
                    except Exception:
                        raw = {}
                    record = enrich_from_raw(record, raw)
                    prior = records.get(paper_id, {})
                    merged = dict(prior)
                    for key, value in record.items():
                        if value not in (None, "", []) and not merged.get(key):
                            merged[key] = value
                    records[paper_id] = merged
    finally:
        connection.close()
    return records


def _s2_metadata_for_ids(
    paper_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Resolve sparse S2 records in one cached, rate-limited batch request."""

    identifiers = [str(item) for item in dict.fromkeys(paper_ids) if item]
    if not identifiers:
        return {}
    try:
        from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway

        papers, response = S2IntelligenceGateway().batch_papers(identifiers)
        if not response.ok:
            return {}
    except Exception:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for paper in papers:
        row = {
            "paper_id": str(paper.paper_id or ""),
            "s2_paper_id": str(paper.paper_id or ""),
            "corpus_id": paper.corpus_id,
            "doi": str(paper.doi or ""),
            "title": str(paper.title or ""),
            "authors": list(paper.authors or []),
            "year": paper.year,
            "venue": str(paper.venue or ""),
            "url": str(
                paper.s2_open_access_candidate_url
                or (
                    f"https://www.semanticscholar.org/paper/{paper.paper_id}"
                    if paper.paper_id
                    else ""
                )
            ),
            "metadata_source": "semantic_scholar_graph",
        }
        if row["paper_id"]:
            records[row["paper_id"]] = row
        if paper.corpus_id is not None:
            records[f"CorpusId:{paper.corpus_id}"] = row
    return records


def _crossref_metadata(
    doi: str,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(
        doi,
        safe="",
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "OptoMind-LaTeX-Renderer/1.0 "
                "(mailto:local-research-workflow@example.invalid)"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {}
    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    if not isinstance(message, dict):
        return {}
    authors = []
    for author in message.get("author", []) or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(
            part
            for part in (
                str(author.get("given") or "").strip(),
                str(author.get("family") or "").strip(),
            )
            if part
        )
        if name:
            authors.append(name)
    date_parts = (
        message.get("published-print", {}).get("date-parts")
        or message.get("published-online", {}).get("date-parts")
        or message.get("issued", {}).get("date-parts")
        or []
    )
    year = None
    if date_parts and isinstance(date_parts[0], list) and date_parts[0]:
        year = date_parts[0][0]
    title_values = message.get("title") or []
    venue_values = message.get("container-title") or []
    return {
        "doi": str(message.get("DOI") or doi),
        "title": str(title_values[0]) if title_values else "",
        "authors": authors,
        "year": year,
        "venue": str(venue_values[0]) if venue_values else "",
        "volume": str(message.get("volume") or ""),
        "issue": str(message.get("issue") or ""),
        "pages": str(message.get("page") or ""),
        "url": str(message.get("URL") or ""),
        "metadata_source": "crossref",
    }


def _canonical_doi(value: Any) -> str:
    """Return a bare DOI from a record value, URL, or internal paper ID.

    Evidence identities in the review KB can be ``CorpusId:*`` while their
    metadata record already carries a DOI.  Publication enrichment must use
    that DOI rather than assuming that only a ``doi:*`` paper ID is eligible.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I).strip()
    text = text.rstrip(".,;:)]}")
    return text if DOI_VALUE_PATTERN.match(text) else ""


def _metadata_year(message: dict[str, Any]) -> int | None:
    date_parts = (
        message.get("published-print", {}).get("date-parts")
        or message.get("published-online", {}).get("date-parts")
        or message.get("issued", {}).get("date-parts")
        or []
    )
    if date_parts and isinstance(date_parts[0], list) and date_parts[0]:
        value = date_parts[0][0]
        return value if isinstance(value, int) else None
    return None


def _metadata_authors(message: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in message.get("author", []) or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(
            part
            for part in (
                str(author.get("given") or "").strip(),
                str(author.get("family") or "").strip(),
            )
            if part
        )
        if name:
            authors.append(name)
    return authors


def _crossref_row_metadata(message: dict[str, Any], *, source: str) -> dict[str, Any]:
    title_values = message.get("title") or []
    venue_values = message.get("container-title") or []
    return {
        "doi": str(message.get("DOI") or ""),
        "title": str(title_values[0]) if title_values else "",
        "authors": _metadata_authors(message),
        "year": _metadata_year(message),
        "venue": str(venue_values[0]) if venue_values else "",
        "volume": str(message.get("volume") or ""),
        "issue": str(message.get("issue") or ""),
        "pages": str(message.get("page") or ""),
        "url": str(message.get("URL") or ""),
        "metadata_source": source,
    }


def _bibliographic_title_similarity(left: Any, right: Any) -> float:
    """Conservative similarity for title-based Crossref recovery."""

    left_text = _clean_unicode(left).lower()
    right_text = _clean_unicode(right).lower()
    left_tokens = set(re.findall(r"[a-z0-9]{2,}", left_text))
    right_tokens = set(re.findall(r"[a-z0-9]{2,}", right_text))
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left_text, right_text).ratio()
    return (0.6 * jaccard) + (0.4 * sequence)


def _crossref_metadata_by_bibliographic_record(
    record: dict[str, Any],
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Recover a sparse record only when Crossref has a high-confidence match.

    This is deliberately a recovery path for S2 records without a DOI/venue,
    not a broad web search.  Ambiguous candidates are rejected rather than
    silently borrowing metadata from a different paper.
    """

    title = _clean_unicode(record.get("title") or "").strip()
    if not title:
        return {}
    author_values = record.get("authors") or []
    if isinstance(author_values, str):
        author_values = [author_values]
    first_author = str(author_values[0] if author_values else "").strip()
    query = " ".join(part for part in (title, first_author) if part)
    params = urllib.parse.urlencode(
        {"query.bibliographic": query, "rows": "5"},
        quote_via=urllib.parse.quote,
    )
    request = urllib.request.Request(
        "https://api.crossref.org/works?" + params,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "OptoMind-LaTeX-Renderer/1.0 "
                "(mailto:local-research-workflow@example.invalid)"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {}
    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    candidates = message.get("items") if isinstance(message, dict) else []
    if not isinstance(candidates, list):
        return {}

    expected_year = record.get("year")
    expected_surname = ""
    if first_author:
        expected_surname = re.findall(r"[A-Za-z0-9]+", first_author.lower())[-1]
    selected: tuple[float, dict[str, Any]] | None = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_title_values = candidate.get("title") or []
        candidate_title = str(candidate_title_values[0]) if candidate_title_values else ""
        title_score = _bibliographic_title_similarity(title, candidate_title)
        candidate_authors = _metadata_authors(candidate)
        candidate_surnames = {
            token
            for value in candidate_authors
            for token in re.findall(r"[A-Za-z0-9]+", value.lower())[-1:]
        }
        author_match = bool(expected_surname and expected_surname in candidate_surnames)
        candidate_year = _metadata_year(candidate)
        year_match = bool(
            isinstance(expected_year, int)
            and isinstance(candidate_year, int)
            and abs(expected_year - candidate_year) <= 1
        )
        # Exact-ish title match is sufficient; otherwise demand both an author
        # and a near year match.  This protects bibliography correctness.
        accepted = title_score >= 0.90 or (
            title_score >= 0.80 and author_match and year_match
        )
        if not accepted:
            continue
        score = title_score + (0.05 if author_match else 0.0) + (0.03 if year_match else 0.0)
        if selected is None or score > selected[0]:
            selected = (score, candidate)
    if selected is None:
        return {}
    return _crossref_row_metadata(selected[1], source="crossref_bibliographic_match")


def _merge_reference_metadata(
    paper_ids: list[str],
    *,
    local_records: dict[str, dict[str, Any]],
    kb_records: dict[str, dict[str, Any]],
    enrich_crossref: bool,
    max_crossref_requests: int,
    enrich_s2: bool = True,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    merged: dict[str, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []
    crossref_requests = 0
    # CorpusId citations are emitted by the S2 snippet route.  They have a
    # valid evidence identity but need graph metadata before publication.
    # Resolve them as one cached batch rather than issuing one request per
    # reference or degrading them to fake bibliography entries.
    s2_records = (
        _s2_metadata_for_ids(
            [
                paper_id
                for paper_id in paper_ids
                if str(paper_id).lower().startswith("corpusid:")
            ]
        )
        if enrich_s2
        else {}
    )
    for paper_id in paper_ids:
        record: dict[str, Any] = {}
        for source in (
            kb_records.get(paper_id, {}),
            local_records.get(paper_id, {}),
            s2_records.get(paper_id, {}),
        ):
            for key, value in source.items():
                if value not in (None, "", []):
                    record[key] = value
        doi_match = DOI_PATTERN.match(paper_id)
        if doi_match and not record.get("doi"):
            record["doi"] = doi_match.group(1)
        canonical_doi = _canonical_doi(record.get("doi"))
        if not canonical_doi and doi_match:
            canonical_doi = doi_match.group(1)
        if canonical_doi:
            record["doi"] = canonical_doi
        missing_before = [
            key
            for key in ("title", "authors", "year", "venue")
            if record.get(key) in (None, "", [])
        ]
        if (
            enrich_crossref
            and canonical_doi
            and missing_before
            and crossref_requests < max_crossref_requests
        ):
            crossref_requests += 1
            fetched = _crossref_metadata(canonical_doi)
            for key, value in fetched.items():
                if record.get(key) in (None, "", []) and value not in (
                    None,
                    "",
                    [],
                ):
                    record[key] = value
        missing_after_doi_lookup = [
            key
            for key in ("title", "authors", "year", "venue")
            if record.get(key) in (None, "", [])
        ]
        # Some valid S2 papers do not expose a DOI or venue in Graph metadata.
        # A carefully matched Crossref bibliographic lookup is preferable to a
        # fabricated reference entry, but it must never become a fuzzy metadata
        # substitution service.
        if (
            enrich_crossref
            and missing_after_doi_lookup
            and crossref_requests < max_crossref_requests
        ):
            crossref_requests += 1
            fetched = _crossref_metadata_by_bibliographic_record(record)
            for key, value in fetched.items():
                if record.get(key) in (None, "", []) and value not in (
                    None,
                    "",
                    [],
                ):
                    record[key] = value
        if not record.get("url"):
            s2_paper_id = str(record.get("s2_paper_id") or "").strip()
            corpus_id = str(record.get("corpus_id") or "").strip()
            if not corpus_id and str(paper_id).lower().startswith("corpusid:"):
                corpus_id = str(paper_id).split(":", 1)[1]
            if s2_paper_id:
                record["url"] = (
                    "https://www.semanticscholar.org/paper/" + s2_paper_id
                )
            elif corpus_id:
                record["url"] = (
                    "https://api.semanticscholar.org/graph/v1/paper/CorpusId%3A"
                    + urllib.parse.quote(corpus_id, safe="")
                )
        required_missing = [
            key
            for key in ("title", "authors", "year")
            if record.get(key) in (None, "", [])
        ]
        has_locator = bool(record.get("doi") or record.get("url"))
        record["reference_kind"] = (
            "article" if record.get("venue") else "misc"
        )
        stable_locator_pending = (
            record["reference_kind"] == "misc" and not has_locator
        )
        publication_complete = not required_missing and (
            bool(record.get("venue")) or has_locator
        )
        record["paper_id"] = paper_id
        merged[paper_id] = record
        missing_after = [
            key
            for key in ("title", "authors", "year", "venue")
            if record.get(key) in (None, "", [])
        ]
        audit.append(
            {
                "paper_id": paper_id,
                "doi": record.get("doi", ""),
                "metadata_source": record.get("metadata_source", ""),
                "missing_before_enrichment": missing_before,
                "missing_after_enrichment": missing_after,
                "metadata_complete": publication_complete,
                "reference_kind": record["reference_kind"],
                "stable_locator_present": has_locator,
                "stable_locator_pending": stable_locator_pending,
                "identity_incomplete": bool(required_missing),
            }
        )
    return merged, audit


def _recover_incomplete_final_references(
    ordered_ids: list[str],
    records: dict[str, dict[str, Any]],
    *,
    enrich_crossref: bool,
    max_crossref_requests: int = 20,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Last-resort Crossref recovery for title-only publication references.

    A reference is a candidate when it carries a title but is still missing
    authors *or* year after the main metadata merge pass.  The function issues
    one targeted bibliographic Crossref search per such reference, independent
    of the per-reference budget consumed in ``_merge_reference_metadata``, and
    fills in only genuinely missing fields without overwriting existing values.
    Nothing is fabricated: the similarity threshold inside
    ``_crossref_metadata_by_bibliographic_record`` rejects ambiguous matches.

    Returns the updated records mapping and a per-entry recovery audit list.
    """

    if not enrich_crossref:
        return dict(records), []
    updated = {pid: dict(rec) for pid, rec in records.items()}
    audit: list[dict[str, Any]] = []
    crossref_calls = 0
    for paper_id in ordered_ids:
        record = updated.get(paper_id, {})
        title = str(record.get("title") or "").strip()
        if not title:
            continue  # nothing to search on
        missing = [
            k for k in ("authors", "year")
            if record.get(k) in (None, "", [])
        ]
        if not missing:
            continue  # already has both authors and year
        if crossref_calls >= max_crossref_requests:
            audit.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "missing_before_recovery": missing,
                    "recovered_fields": [],
                    "crossref_matched": False,
                    "skipped_reason": "crossref_budget_exhausted",
                }
            )
            continue
        crossref_calls += 1
        fetched = _crossref_metadata_by_bibliographic_record(record)
        recovered: list[str] = []
        for key, value in fetched.items():
            if record.get(key) in (None, "", []) and value not in (
                None,
                "",
                [],
            ):
                updated[paper_id][key] = value
                recovered.append(key)
        audit.append(
            {
                "paper_id": paper_id,
                "title": title,
                "missing_before_recovery": missing,
                "recovered_fields": recovered,
                "crossref_matched": bool(fetched),
            }
        )
    return updated, audit


def _bibtex_escape(value: Any) -> str:
    text = _clean_unicode(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "%": r"\%",
        "&": r"\&",
        "#": r"\#",
        "_": r"\_",
        "$": r"\$",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _bibtex_escape_locator(value: Any) -> str:
    """Escape a DOI/URL without leaking ``\\_`` into ``\\doi`` output.

    The generated bibliography renders DOI and URL values through the
    ``url`` package.  Underscores are valid URL characters there; escaping
    them in the BibTeX field made some styles print the TeX control sequence
    itself (for example ``cleo\\_at``) instead of the actual DOI.
    """

    return _bibtex_escape(value).replace(r"\_", "_")


# Trailing affiliation markers: ASCII digits, Unicode superscript digits
# (U+00B9 ¹, U+00B2 ², U+00B3 ³, U+2070–U+2079 ⁰–⁹), and common symbols
# (* † ‡ § ‖) that publishers sometimes attach inline to author names.
_AFFILIATION_MARKER_RE = re.compile(
    r"[\s\d¹²³⁰-⁹*†‡§‖,]+$"
)


def _strip_affiliation_marker(name: str) -> str:
    """Remove trailing affiliation markers from a single author name string.

    ``"Xijun Gao1"`` → ``"Xijun Gao"``.
    ``"Smith†"`` → ``"Smith"``.
    Names without markers are returned unchanged.
    """
    return _AFFILIATION_MARKER_RE.sub("", name.strip()).strip()


def _normalize_author_list(authors: Any) -> list[str]:
    """Normalize the author field to a list of individual author name strings.

    Accepts a pre-structured list (returned as-is after stripping affiliation
    markers) or any of the common bibliographic string formats:

    - BibTeX ``and``-delimited: ``"A. Smith and B. Jones"``
    - Semicolon-delimited: ``"A. Smith; B. Jones"``
    - Comma-delimited Given-Surname list:
      ``"Xijun Gao, Wenxuan Gong, Jiaqi Li"``
    - Single-author BibTeX Last-First: ``"Smith, Alice B."``
    - Mixed: ``"Smith, A. and Jones, B."``

    The single-author ``"Surname, Given"`` form (exactly two comma-parts
    where the first part contains no spaces) is preserved so BibTeX renders
    it correctly.  All other comma-containing forms are treated as multiple
    authors in Given-Surname order.  Affiliation markers are stripped from
    each individual name.
    """
    if isinstance(authors, list):
        # Each list element may itself be a delimited string
        # (e.g. ["Gao1, Gong1, Li1"] from some metadata providers).
        # Normalise every element through the string path and flatten.
        result_list: list[str] = []
        for item in authors:
            item_str = str(item).strip()
            if not item_str:
                continue
            # Re-enter as a string to apply all delimiter logic.
            result_list.extend(_normalize_author_list(item_str))
        return result_list

    text = str(authors or "").strip()
    if not text:
        return []

    # 1. Split on BibTeX "and" delimiter (case-insensitive).
    and_parts: list[str] = re.split(r"\s+and\s+", text, flags=re.IGNORECASE)

    # 2. Within each "and"-part, split on semicolons.
    semi_parts: list[str] = []
    for part in and_parts:
        if ";" in part:
            semi_parts.extend(s.strip() for s in part.split(";") if s.strip())
        else:
            semi_parts.append(part.strip())

    # 3. Resolve commas within each remaining part.
    result: list[str] = []
    for part in semi_parts:
        if not part:
            continue
        if "," not in part:
            result.append(part)
            continue
        tokens = [t.strip() for t in part.split(",") if t.strip()]
        if len(tokens) == 2 and " " not in tokens[0]:
            # "Surname, Given" single-author BibTeX form — preserve the comma.
            result.append(part.strip())
        elif all(" " in t for t in tokens):
            # Every token is a full "Given Surname" name — comma-separated list.
            result.extend(tokens)
        elif any(" " in t for t in tokens):
            # Mixed — treat as comma-separated full names (best effort).
            result.extend(tokens)
        else:
            # All single-word tokens — ambiguous.  Apply an even-pair Last,
            # First heuristic when the count is even and every second token
            # starts with an uppercase letter (i.e. "Smith, Alice, Jones, Bob").
            if (
                len(tokens) >= 4
                and len(tokens) % 2 == 0
                and all(tokens[i + 1][:1].isupper() for i in range(0, len(tokens), 2))
            ):
                result.extend(
                    f"{tokens[i]}, {tokens[i + 1]}"
                    for i in range(0, len(tokens), 2)
                )
            else:
                # Fall back: preserve as-is (single "Surname, Given" or unknown).
                result.append(part.strip())

    # 4. Strip affiliation markers and drop empty names.
    cleaned = [_strip_affiliation_marker(name) for name in result]
    return [name for name in cleaned if name]


def _reference_bibtex(
    ordered_ids: list[str],
    key_by_id: dict[str, str],
    records: dict[str, dict[str, Any]],
) -> str:
    entries = []
    for paper_id in ordered_ids:
        record = records.get(paper_id, {})
        reference_kind = str(record.get("reference_kind") or "").lower()
        if reference_kind not in {"article", "misc"}:
            reference_kind = "article" if record.get("venue") else "misc"
        authors = record.get("authors") or []
        authors = _normalize_author_list(authors)
        author_text = " and ".join(
            _bibtex_escape(value) for value in authors if str(value).strip()
        )
        if author_text == "Authors not recovered in chapter snapshot":
            # Keep the missing-author fact in the audit, but give BibTeX a
            # unique corporate-author token instead of a prose sentence.
            # Uniqueness avoids natbib producing invalid suffixes after more
            # than 26 same-author/same-year fallback entries.
            digest = hashlib.sha1(paper_id.encode("utf-8")).hexdigest()[:8]
            author_text = "{{OptoMind source metadata pending " + digest + "}}"
        missing = [
            key
            for key, value in {
                "author": author_text,
                "title": record.get("title"),
                "year": record.get("year"),
            }.items()
            if value in (None, "", [])
        ]
        if reference_kind == "article" and not record.get("venue"):
            missing.append("journal")
        if missing:
            title_text = str(record.get("title") or "").strip()
            title_hint = (
                f"; title: {title_text[:120]!r}"
                if title_text
                else "; no title recovered"
            )
            raise ValueError(
                f"Publication reference has incomplete bibliographic metadata"
                f" for paper_id={paper_id!r}{title_hint}"
                f"; missing fields: {', '.join(missing)}."
                " To resolve: verify this reference appears in the knowledge"
                " base with full bibliographic data, or add it to a"
                " supplemental metadata file before re-running the renderer."
            )
        metadata_note = _bibtex_escape(record.get("metadata_note") or "")
        if reference_kind == "misc" and not (
            record.get("doi") or record.get("url")
        ):
            metadata_note = (
                (metadata_note + " " if metadata_note else "")
                + "Stable locator pending."
            )
        fields = {
            "author": author_text,
            "title": _bibtex_escape(record.get("title")),
            "year": str(record.get("year")),
            "volume": _bibtex_escape(record.get("volume") or ""),
            "number": _bibtex_escape(record.get("issue") or ""),
            "pages": _bibtex_escape(record.get("pages") or ""),
            "doi": _bibtex_escape_locator(record.get("doi") or ""),
            "url": _bibtex_escape_locator(
                record.get("url")
                or (
                    f"https://doi.org/{record.get('doi')}"
                    if record.get("doi")
                    else ""
                )
            ),
            "note": metadata_note,
        }
        if reference_kind == "article":
            fields["journal"] = _bibtex_escape(record.get("venue"))
        body = ",\n".join(
            f"  {name} = {{{value}}}"
            for name, value in fields.items()
            if value
        )
        entries.append(f"@{reference_kind}{{{key_by_id[paper_id]},\n{body}\n}}")
    return "\n\n".join(entries) + "\n"


def _identify_dropped_references(
    ordered_ids: list[str],
    records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Identify final references that cannot satisfy the BibTeX contract.

    Mirrors the missing-field check in ``_reference_bibtex`` exactly so that
    unrecoverable references are excluded before BibTeX generation rather than
    raising.  Only references that would cause ``_reference_bibtex`` to raise
    are returned; ``misc`` entries without a stable locator are NOT dropped
    (the existing stable-locator note covers them).

    The caller must exclude the returned ``paper_id`` values from
    ``ordered_ids``, ``key_by_id``, and ``records`` before calling
    ``_reference_bibtex``.  ``_reference_bibtex`` still raises for any record
    that reaches it with missing fields — this function is the pre-filter only.

    Returns a list of audit dicts, one per dropped reference.
    """
    dropped: list[dict[str, Any]] = []
    for paper_id in ordered_ids:
        record = records.get(paper_id, {})
        reference_kind = str(record.get("reference_kind") or "").lower()
        if reference_kind not in {"article", "misc"}:
            reference_kind = "article" if record.get("venue") else "misc"
        authors = record.get("authors") or []
        authors = _normalize_author_list(authors)
        author_text = " and ".join(
            _bibtex_escape(v) for v in authors if str(v).strip()
        )
        if author_text == "Authors not recovered in chapter snapshot":
            # _reference_bibtex replaces this with a stable corporate-author
            # token — treat it as present, not a missing field.
            digest = hashlib.sha1(paper_id.encode("utf-8")).hexdigest()[:8]
            author_text = "{{OptoMind source metadata pending " + digest + "}}"
        missing = [
            key
            for key, value in {
                "author": author_text,
                "title": record.get("title"),
                "year": record.get("year"),
            }.items()
            if value in (None, "", [])
        ]
        if reference_kind == "article" and not record.get("venue"):
            missing.append("journal")
        if missing:
            dropped.append(
                {
                    "paper_id": paper_id,
                    "title": str(record.get("title") or ""),
                    "missing_fields": missing,
                    "reason": (
                        "Reference cannot satisfy BibTeX contract after all"
                        f" recovery passes: missing {', '.join(missing)}"
                    ),
                }
            )
    return dropped


def _drop_citation_tokens(
    markdown: str,
    dropped_keys: set[str],
) -> str:
    """Remove ``[@key]`` citation tokens for dropped references from Markdown.

    Adjacent citation clusters such as ``[@good][@bad][@good2]`` are handled
    correctly: only the dropped tokens are removed while neighbouring valid
    citations remain intact.  Prose surrounding any citation group is
    unchanged.
    """
    if not dropped_keys:
        return markdown
    _cite = re.compile(r"\[@([^\]]+)\]")
    return _cite.sub(
        lambda m: "" if m.group(1) in dropped_keys else m.group(0),
        markdown,
    )


def _latex_escape(value: Any) -> str:
    text = _clean_unicode(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "%": r"\%",
        "&": r"\&",
        "#": r"\#",
        "_": r"\_",
        "$": r"\$",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _infer_title(blueprint: dict[str, Any]) -> str:
    context = blueprint.get("input_context", {})
    question = str(
        context.get("user_question")
        or context.get("problem_understanding")
        or ""
    ).strip()
    question = re.sub(
        r"^The question seeks (?:a |an )?(?:comprehensive )?"
        r"(?:literature-oriented )?(?:research framing|review) of\s+",
        "",
        question,
        flags=re.IGNORECASE,
    )
    question = re.split(r",\s+(?:examining|encompassing|while)\b", question)[0]
    question = question.rstrip(" .")
    if len(question) > 180:
        question = question[:177].rsplit(" ", 1)[0] + "..."
    if question:
        return question[0].upper() + question[1:] + ": A Critical Review"
    return "Critical Review Manuscript"


def _infer_abstract(blueprint: dict[str, Any]) -> str:
    thesis = _clean_unicode(blueprint.get("review_thesis") or "")
    argument = _clean_unicode(blueprint.get("full_review_argument") or "")
    abstract = " ".join(part.strip() for part in (thesis, argument) if part.strip())
    words = abstract.split()
    if len(words) > 260:
        abstract = " ".join(words[:260]).rstrip(" ,;:") + "."
    return abstract or (
        "This manuscript provides a critical synthesis of the literature, "
        "organizes the major mechanisms and design strategies, and identifies "
        "the principal research gaps and translational constraints."
    )


def _normalize_metadata(
    raw: dict[str, Any],
    *,
    blueprint: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    title = _clean_unicode(raw.get("title") or _infer_title(blueprint))
    abstract = _clean_unicode(raw.get("abstract") or _infer_abstract(blueprint))
    topic_identity = blueprint.get("topic_identity", {})
    keywords = raw.get("keywords") or (
        topic_identity.get("core_anchors")
        or topic_identity.get("anchor_phrases")
        or topic_identity.get("core_anchor_tokens")
        or []
    )
    if isinstance(keywords, str):
        keywords = [
            item.strip() for item in keywords.split(",") if item.strip()
        ]
    authors = raw.get("authors") or []
    if isinstance(authors, str):
        authors = [{"name": authors}]
    normalized_authors = []
    for row in authors:
        if isinstance(row, str):
            row = {"name": row}
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            continue
        normalized_authors.append(
            {
                "name": _clean_unicode(row.get("name")),
                "affiliation": _clean_unicode(row.get("affiliation") or ""),
                "email": str(row.get("email") or "").strip(),
                "orcid": str(row.get("orcid") or "").strip(),
            }
        )
    if not normalized_authors:
        normalized_authors = [
            {
                "name": "Author information pending",
                "affiliation": "",
                "email": "",
                "orcid": "",
            }
        ]
        warnings.append("author_metadata_pending")
    if title == "Critical Review Manuscript":
        warnings.append("title_metadata_pending")
    if not raw.get("abstract"):
        warnings.append("abstract_inferred_from_review_blueprint")
    if not raw.get("keywords"):
        warnings.append("keywords_inferred_from_topic_identity")
    normalized_keywords = []
    seen_keywords = set()
    for item in keywords:
        value = _clean_unicode(item).strip()
        key = value.casefold()
        if value and key not in seen_keywords:
            normalized_keywords.append(value)
            seen_keywords.add(key)
        if len(normalized_keywords) >= 8:
            break
    return {
        "title": title,
        "abstract": abstract,
        "keywords": normalized_keywords,
        "authors": normalized_authors,
        "date": _clean_unicode(raw.get("date") or ""),
        "draft_only": bool(raw.get("draft_only", False)),
        "acknowledgements": _clean_unicode(
            raw.get("acknowledgements") or ""
        ),
    }, warnings


def resolve_publication_metadata(
    *,
    content_package_path: Path,
    metadata_path: Optional[Path] = None,
) -> tuple[dict[str, Any], list[str]]:
    """Resolve the exact metadata the renderer will place in the manuscript."""

    package_path = Path(content_package_path).resolve()
    package = _read_json(package_path)
    if not package:
        raise ValueError(f"Invalid content package: {package_path}")
    blueprint_path = _resolve_artifact_path(
        package.get("artifacts", {}).get("review_blueprint"),
        package_path=package_path,
    )
    blueprint = _read_json(blueprint_path)
    completion_path = _resolve_artifact_path(
        package.get("artifacts", {}).get("article_completion_package"),
        package_path=package_path,
    )
    completion = _read_json(completion_path)
    supplied: dict[str, Any] = {}
    if metadata_path:
        supplied = _read_json(Path(metadata_path))
    else:
        auto_metadata_path = _resolve_artifact_path(
            package.get("publication_metadata_path"),
            package_path=package_path,
        )
        if auto_metadata_path is None:
            auto_metadata_path = _resolve_artifact_path(
                package.get("artifacts", {}).get("publication_metadata"),
                package_path=package_path,
            )
        supplied = _read_json(auto_metadata_path)
    if completion:
        supplied = dict(supplied)
        supplied.setdefault("title", completion.get("title", ""))
        supplied.setdefault("abstract", completion.get("abstract", ""))
    return _normalize_metadata(
        supplied,
        blueprint=blueprint,
    )


def _copy_verified_figures(
    visual_plan: dict[str, Any],
    *,
    output_dir: Path,
    base_dir: Optional[Path] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    rejected = []
    final_package_mode = isinstance(
        visual_plan.get("figures"),
        list,
    )
    source_items = (
        visual_plan.get("figures", [])
        if final_package_mode
        else visual_plan.get("placements", [])
    ) or []
    for index, item in enumerate(source_items, 1):
        if not isinstance(item, dict):
            continue
        source = Path(
            str(
                item.get("local_path")
                or item.get("local_image_path")
                or ""
            )
        )
        if base_dir is not None and not source.is_absolute():
            source = (base_dir / source).resolve()
        suffix = source.suffix.lower()
        reason = ""
        if final_package_mode:
            if str(item.get("review_decision") or "") not in {
                "human_approved",
                "timeout_accepted_for_draft",
                "system_approved_test_mode",
                "system_approved_test_mode_with_warnings",
            }:
                reason = "final_figure_review_state_not_accepted"
            elif str(item.get("render_status") or "") != "ready":
                reason = "final_figure_not_render_ready"
        elif str(item.get("status") or "") != "verified_existing":
            reason = "placement_not_verified"
        if not reason and not source.is_file():
            reason = "source_image_missing"
        elif not reason and suffix not in SAFE_FIGURE_SUFFIXES:
            reason = "unsupported_figure_format"
        if reason:
            rejected.append(
                {
                    "visual_chunk_id": item.get(
                        "visual_chunk_id",
                        item.get("figure_id", ""),
                    ),
                    "local_image_path": str(source),
                    "reason": reason,
                }
            )
            continue
        stem = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            str(
                item.get("figure_id")
                or item.get("visual_chunk_id")
                or f"figure_{index}"
            ),
        ).strip("_.")
        destination = figures_dir / f"{index:02d}_{stem}{suffix}"
        publication_asset = prepare_publication_figure(
            source,
            destination,
            item,
        )
        if not bool(
            publication_asset.get("publication_asset_eligible", True)
        ):
            rejected.append(
                {
                    "visual_chunk_id": item.get(
                        "visual_chunk_id",
                        item.get("figure_id", ""),
                    ),
                    "local_image_path": str(source),
                    "reason": str(
                        publication_asset.get(
                            "publication_asset_rejection_reason"
                        )
                        or "publication_asset_quality_rejected"
                    ),
                    "publication_asset_audit": publication_asset,
                }
            )
            continue
        copied.append(
            {
                **item,
                "source_path": str(source),
                "source_caption_en": str(item.get("caption_en") or item.get("caption_preview") or ""),
                "caption_en": str(publication_asset.get("caption") or ""),
                "latex_path": f"figures/{destination.name}",
                "figure_label": f"fig:visual_{index:02d}",
                "figure_number": index,
                "caption_preview": str(publication_asset.get("caption") or ""),
                "publication_asset_audit": publication_asset,
                "section_id": str(
                    item.get("section_id")
                    or (
                        (item.get("target_section_ids") or [""])[0]
                        if isinstance(
                            item.get("target_section_ids"),
                            list,
                        )
                        and item.get("target_section_ids")
                        else ""
                    )
                ),
            }
        )
    return copied, rejected


_INTERNAL_FIGURE_TRANSITION_PHRASES = (
    "visual guide",
    "the figure below supports",
    "supports the explanatory thread",
    "explanatory thread",
    "at this point in the review",
    "figure below",
    "placement",
    "argumentative purpose",
    "visual argument",
    "visual chunk",
    "source-derived visual",
    "internal study visual",
    "figure contract",
    "review harness",
)
_INTERNAL_PLANNING_PURPOSE_MARKERS = (
    "commander:",
    "commander ",
    "placement:",
    "placement ",
    "argument role",
    "argumentative role",
    "request:",
    "generation brief",
    "generation_brief",
    "work order",
    "work_order",
    "workflow",
    "review decision",
    "approval_required",
    "approval required",
    "reposition",
    "merge_candidate",
    "must shed",
    "visual work order",
    "source_route",
    "data_provenance",
)
_PURPOSE_PREFIX_PATTERN = re.compile(
    r"^(?:this\s+(?:visual|figure|table)\s+)?"
    r"(?:illustrates?|demonstrates?|shows?|presents?|depicts?|compares?|"
    r"summarizes?|reports?|provides?)\s+"
    r"(?P<conjunction>how|that|why)\s+",
    re.IGNORECASE,
)
_PURPOSE_SUBJECT_PATTERN = re.compile(
    r"^(?:this|the)\s+(?:visual|figure|table|diagram)\s+"
    r"(?:(?:quantitatively|qualitatively|directly|schematically|"
    r"conceptually|clearly|visually)\s+)*"
    r"(?:illustrates?|demonstrates?|shows?|presents?|depicts?|compares?|"
    r"summarizes?|reports?|provides?|traces?)\s+",
    re.IGNORECASE,
)
_PURPOSE_LEADING_VERB_PATTERN = re.compile(
    r"^(?:show|showing|demonstrate(?:s|d)?|illustrate(?:s|d)?|"
    r"clarif(?:y|ies|ying)|explain(?:s|ed|ing)?|provide(?:s|d)?|"
    r"present(?:s|ed|ing)?|summariz(?:e|es|ed|ing)|depict(?:s|ed|ing)?|"
    r"compare(?:s|d)?|trace(?:s|d)?|outlin(?:e|es|ed|ing)?|"
    r"highlight(?:s|ed|ing)?)\s+(?:that\s+|how\s+|why\s+)?",
    re.IGNORECASE,
)
_PURPOSE_TRAILING_CONDITIONAL_PATTERN = re.compile(
    r"\s*(?:only\s+if\b.*|if\s+[^.]*fits\.?|when\s+[^.]*fits\.?|"
    r"when\s+supported\s+by\b.*|where\s+[^.]*fits\.?|"
    r"leave\s+empty\s+if\b.*|without\s+invented\s+results\.?|"
    r"unless\s+[^.]*fits\.?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_PURPOSE_NOISE_PREFIX_PATTERN = re.compile(
    r"^(?:fig\.?\s*\d+|figure\s*\d+|table\s*\d+|[);:,;])",
    re.IGNORECASE,
)
_PURPOSE_IMPERATIVE_HELPER_PATTERN = re.compile(
    r"^help(?:\s+(?:readers?|the\s+reader))?\s+",
    re.IGNORECASE,
)


def _figure_is_table(figure: dict[str, Any]) -> bool:
    """Detect table-like visuals from the final or legacy figure contract."""

    contract = figure.get("figure_contract")
    if not isinstance(contract, dict):
        audit = figure.get("source_audit")
        if isinstance(audit, dict):
            contract = audit.get("figure_contract")
    if isinstance(contract, dict):
        if contract.get("is_table") is True:
            return True
        asset_kind = str(contract.get("asset_kind") or "").lower()
        figure_kind = str(contract.get("figure_kind") or "").lower()
        if asset_kind == "table" or figure_kind == "source_table":
            return True
    for key in ("figure_type", "asset_kind", "figure_kind"):
        if str(figure.get(key) or "").lower() in {"table", "source_table"}:
            return True
    return False


def _contains_internal_planning_purpose(text: str) -> bool:
    """Detect commander/placement/role metadata before it reaches prose."""

    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in _INTERNAL_PLANNING_PURPOSE_MARKERS)


def _contains_internal_figure_language(text: str) -> bool:
    """Detect both legacy workflow phrasing and planning markers."""

    lowered = str(text or "").casefold()
    if any(
        phrase in lowered
        for phrase in _INTERNAL_FIGURE_TRANSITION_PHRASES
    ):
        return True
    return _contains_internal_planning_purpose(text)


def _clean_figure_purpose(text: str) -> str:
    """Strip workflow phrasing and imperative scaffolding from a purpose."""

    cleaned = _clean_unicode(text)
    if _contains_internal_planning_purpose(cleaned):
        return ""
    if _PURPOSE_IMPERATIVE_HELPER_PATTERN.match(cleaned):
        return ""
    for phrase in _INTERNAL_FIGURE_TRANSITION_PHRASES:
        cleaned = re.sub(
            rf"\b{re.escape(phrase)}\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = " ".join(cleaned.split())
    prefix = _PURPOSE_PREFIX_PATTERN.match(cleaned)
    if prefix:
        cleaned = (
            prefix.group("conjunction").lower()
            + " "
            + cleaned[prefix.end() :]
        )
    else:
        cleaned = _PURPOSE_SUBJECT_PATTERN.sub("", cleaned).strip()
        cleaned = _PURPOSE_LEADING_VERB_PATTERN.sub("", cleaned).strip()
    cleaned = _PURPOSE_TRAILING_CONDITIONAL_PATTERN.sub("", cleaned).strip()
    if re.search(r"(?:\.\.\.|…)$", cleaned):
        cleaned = re.sub(r"(?:\.\.\.|…)+$", "", cleaned).strip()
        if re.search(
            r"\b(?:a|an|the|and|or|of|to|for|with|in|on|via|by|from|that|which)$",
            cleaned,
            flags=re.IGNORECASE,
        ):
            return ""
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
    if len(cleaned) < 4:
        return ""
    if _PURPOSE_NOISE_PREFIX_PATTERN.match(cleaned):
        return ""
    return cleaned


def _reader_safe_figure_caption(figure: dict[str, Any]) -> str:
    """Choose a reader-safe caption without exposing workflow metadata."""

    caption = _clean_unicode(
        figure.get("caption_en") or figure.get("caption_preview") or ""
    )
    if caption and not _contains_internal_figure_language(caption):
        return caption
    purpose = _clean_figure_purpose(
        figure.get("argumentative_purpose")
        or figure.get("purpose")
        or ""
    )
    if purpose:
        return purpose
    return "Verified visual evidence."


def _figure_transition_sentence(
    figure: dict[str, Any],
    *,
    language: str = "en",
) -> str:
    """Build a natural reader-facing transition for one inserted figure.

    The sentence references the figure by its LaTeX label so Pandoc/LaTeX can
    resolve the actual manuscript-order number.  It falls back to a neutral
    academic sentence when no useful purpose survives sanitization. Internal
    workflow language and command-style purposes are never exposed to the
    reader.
    """

    number = str(figure.get("figure_number") or "")
    figure_label = str(figure.get("figure_label") or "").strip()
    if not figure_label and number.isdigit():
        figure_label = f"fig:visual_{int(number):02d}"
    if not figure_label:
        figure_label = "fig:visual"
    is_chinese = str(language or "en").lower().startswith("zh")
    label = (
        f"图 \\ref{{{figure_label}}}"
        if is_chinese
        else f"Figure \\ref{{{figure_label}}}"
    )
    if is_chinese:
        return f"{label}展示了本节讨论的代表性视觉证据。"
    figure_id = str(
        figure.get("figure_id") or figure.get("visual_chunk_id") or ""
    ).strip().upper()
    if figure_id.startswith("FIG-SRC-"):
        return f"{label} provides source-derived visual context for this section."
    purpose = _clean_figure_purpose(
        figure.get("argumentative_purpose")
        or figure.get("purpose")
        or ""
    )
    if not purpose:
        return f"{label} presents representative evidence discussed in this section."
    lowered = purpose.lower()
    if lowered.startswith("how "):
        verb = "illustrates"
    elif lowered.startswith("that "):
        verb = "demonstrates"
    elif "by comparing" in lowered or "by comparison" in lowered:
        verb = "illustrates"
    elif "compar" in lowered:
        verb = "compares"
    elif "summar" in lowered or "overview" in lowered:
        verb = "summarizes"
    elif "demonstrat" in lowered:
        verb = "demonstrates"
    elif "report" in lowered or "data" in lowered:
        verb = "shows"
    elif "show" in lowered or "display" in lowered or "present" in lowered:
        verb = "shows"
    elif "evolv" in lowered or "time" in lowered or "trend" in lowered:
        verb = "traces"
    else:
        verb = "illustrates"
    sentence = f"{label} {verb} {purpose.rstrip('.')}."
    if len(sentence) > 260:
        # Keep a complete clause rather than emitting an AI-looking ellipsis
        # or leaving a dangling preposition at the hard boundary.  Prefer a
        # real punctuation boundary; otherwise trim at a word boundary and
        # remove any trailing function-word chain.
        prefix = f"{label} {verb} "
        body = purpose.rstrip(".")
        body_limit = max(40, 260 - len(prefix) - 1)
        bounded = body[:body_limit]
        punctuation_boundary = max(
            bounded.rfind(","),
            bounded.rfind(";"),
            bounded.rfind(":"),
        )
        if punctuation_boundary >= max(40, body_limit // 3):
            bounded = bounded[:punctuation_boundary]
        else:
            bounded = bounded.rsplit(" ", 1)[0]
        bounded = re.sub(
            r"(?:\s+(?:a|an|the|and|or|of|to|for|with|in|on|via|by|from|"
            r"that|which))+$",
            "",
            bounded,
            flags=re.IGNORECASE,
        ).rstrip(" ,;:")
        sentence = f"{prefix}{bounded}."
    return sentence


def _record_omitted_figure(
    omitted_figures: list[dict[str, Any]] | None,
    figure: dict[str, Any],
    *,
    section_id: str,
    reason: str,
) -> None:
    """Add a fail-safe placement record without changing Markdown."""

    if omitted_figures is None:
        return
    omitted_figures.append(
        {
            "visual_chunk_id": str(
                figure.get("visual_chunk_id")
                or figure.get("figure_id")
                or ""
            ),
            "figure_id": str(figure.get("figure_id") or ""),
            "section_id": section_id,
            "local_image_path": str(
                figure.get("source_path")
                or figure.get("latex_path")
                or ""
            ),
            "reason": reason,
        }
    )


def _inject_figures(
    markdown: str,
    *,
    figures: list[dict[str, Any]],
    blueprint: dict[str, Any],
    language: str = "en",
    omitted_figures: list[dict[str, Any]] | None = None,
) -> str:
    if not figures:
        return markdown
    title_by_section = {
        str(section.get("section_id") or ""): _clean_unicode(
            section.get("section_title") or section.get("title") or ""
        )
        for section in blueprint.get("sections", []) or []
        if isinstance(section, dict)
    }
    heading_rows = [
        (len(match.group(1)), _clean_unicode(match.group(2)).strip())
        for match in re.finditer(
            r"^(#{1,6})\s+(.+?)\s*$",
            markdown,
            re.MULTILINE,
        )
    ]
    if heading_rows:
        top_level = min(level for level, _ in heading_rows)
        top_headings = [
            title for level, title in heading_rows if level == top_level
        ]
    else:
        top_headings = []

    def structural_heading(title: str) -> bool:
        normalized = re.sub(r"\s+", " ", title).strip().lower()
        exact = {
            "abstract",
            "introduction",
            "challenges and future outlook",
            "future outlook",
            "conclusion",
            "conclusions",
            "references",
            "摘要",
            "引言",
            "挑战与未来展望",
            "未来展望",
            "结论",
            "参考文献",
        }
        return normalized in exact

    planned_sections = [
        section
        for section in blueprint.get("sections", []) or []
        if isinstance(section, dict) and section.get("section_id")
    ]
    body_headings = [
        title for title in top_headings if not structural_heading(title)
    ]
    fallback_title_by_section: dict[str, str] = {}
    if len(body_headings) >= len(planned_sections):
        fallback_title_by_section = {
            str(section.get("section_id")): body_headings[index]
            for index, section in enumerate(planned_sections)
        }
    for figure in figures:
        section_id = str(figure.get("section_id") or "")
        title = title_by_section.get(section_id, "")
        caption = _reader_safe_figure_caption(figure)
        transition = _figure_transition_sentence(figure, language=language)
        block = (
            "\n\n"
            f"{transition}\n\n"
            f"![{caption}]({figure['latex_path']})"
            f"{{#{figure['figure_label']} width=90%}}\n"
        )
        if not title:
            _record_omitted_figure(
                omitted_figures,
                figure,
                section_id=section_id,
                reason="unknown_section_id_no_blueprint_heading",
            )
            continue
        heading = re.compile(
            r"^(#{1,6})\s+" + re.escape(title) + r"\s*$",
            re.MULTILINE | re.IGNORECASE,
        )
        match = heading.search(markdown)
        if not match:
            rendered_title = fallback_title_by_section.get(section_id, "")
            if rendered_title:
                heading = re.compile(
                    r"^(#{1,6})\s+"
                    + re.escape(rendered_title)
                    + r"\s*$",
                    re.MULTILINE | re.IGNORECASE,
                )
                match = heading.search(markdown)
        if not match:
            _record_omitted_figure(
                omitted_figures,
                figure,
                section_id=section_id,
                reason="unknown_section_id_no_matching_rendered_heading",
            )
            continue
        paragraph_end = markdown.find("\n\n", match.end())
        if paragraph_end < 0:
            paragraph_end = match.end()
        next_end = markdown.find("\n\n", paragraph_end + 2)
        insertion = next_end if next_end >= 0 else paragraph_end
        markdown = markdown[:insertion] + block + markdown[insertion:]
    return markdown


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    effective_command = list(command)
    if (
        os.name == "nt"
        and effective_command
        and Path(effective_command[0]).suffix.lower() in {".cmd", ".bat"}
    ):
        effective_command = [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(effective_command),
        ]
    try:
        completed = subprocess.run(
            effective_command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": command,
            "effective_command": effective_command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "effective_command": effective_command,
            "returncode": -1,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
            "timed_out": True,
        }


def _find_binary(name: str) -> str:
    """Find a real executable, avoiding broken Windows wrapper scripts."""

    discovered = shutil.which(name)
    if discovered and Path(discovered).suffix.lower() == ".exe":
        return discovered
    candidates: list[Path] = []
    if discovered:
        wrapper = Path(discovered)
        try:
            dependency_root = wrapper.parents[2]
            candidates.append(
                dependency_root
                / "native"
                / "poppler"
                / "Library"
                / "bin"
                / f"{name}.exe"
            )
        except IndexError:
            pass
    if os.name == "nt":
        candidates.extend(
            sorted(
                Path("C:/texlive").glob(
                    f"*/bin/windows/{name}.exe"
                ),
                reverse=True,
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(discovered or "")


def _render_main_tex(
    *,
    metadata: dict[str, Any],
    body_tex: str,
    language: str = "en",
    tex_engine: str = "xelatex",
) -> str:
    author_parts = []
    for author in metadata["authors"]:
        rendered = _latex_escape(author["name"])
        details = []
        if author.get("affiliation"):
            details.append(_latex_escape(author["affiliation"]))
        if author.get("email"):
            details.append(r"\texttt{" + _latex_escape(author["email"]) + "}")
        if author.get("orcid"):
            details.append("ORCID: " + _latex_escape(author["orcid"]))
        if details:
            rendered += r"\thanks{" + "; ".join(details) + "}"
        author_parts.append(rendered)
    authors = r" \and ".join(author_parts)
    keywords = ", ".join(_latex_escape(value) for value in metadata["keywords"])
    is_chinese = str(language or "en").lower().startswith("zh")
    acknowledgements = ""
    if metadata.get("acknowledgements"):
        acknowledgements = (
            "\n\\section*{"
            + ("致谢" if is_chinese else "Acknowledgements")
            + "}\n"
            + _latex_escape(metadata["acknowledgements"])
            + "\n"
        )
    if is_chinese:
        preamble = r"""\documentclass[11pt]{article}
\usepackage[UTF8,fontset=fandol]{ctex}
\usepackage{microtype}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs,longtable}
\usepackage{array}
\usepackage{siunitx}
\usepackage[numbers,sort&compress]{natbib}
\usepackage[unicode,hidelinks]{hyperref}
\usepackage[capitalise,noabbrev]{cleveref}
\usepackage{xcolor}
\usepackage{url}
\graphicspath{{figures/}}
\setlength{\emergencystretch}{3em}
\setlength{\parindent}{2em}
\setlength{\parskip}{0.35em}
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}
\renewcommand{\abstractname}{摘要}
"""
    elif tex_engine == "xelatex":
        preamble = r"""\documentclass[11pt]{article}
\usepackage{fontspec}
\defaultfontfeatures{Ligatures=TeX}
\usepackage{microtype}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs,longtable}
\usepackage{array}
\usepackage{siunitx}
\usepackage[numbers,sort&compress]{natbib}
\usepackage[unicode,hidelinks]{hyperref}
\usepackage[capitalise,noabbrev]{cleveref}
\usepackage{xcolor}
\usepackage{url}
\graphicspath{{figures/}}
\setlength{\emergencystretch}{3em}
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}
"""
    else:
        preamble = r"""\documentclass[11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs,longtable}
\usepackage{array}
\usepackage{siunitx}
\usepackage[numbers,sort&compress]{natbib}
\usepackage[hidelinks]{hyperref}
\usepackage[capitalise,noabbrev]{cleveref}
\usepackage{xcolor}
\usepackage{url}
\graphicspath{{figures/}}
\setlength{\emergencystretch}{3em}
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}
"""
    return (
        preamble
        + r"""\hypersetup{
  pdftitle={"""
        + _latex_escape(metadata["title"])
        + r"""},
  pdfauthor={"""
        + _latex_escape(", ".join(a["name"] for a in metadata["authors"]))
        + r"""}
}

\title{"""
        + _latex_escape(metadata["title"])
        + r"""}
\author{"""
        + authors
        + r"""}
\date{"""
        + _latex_escape(metadata.get("date") or "")
        + r"""}

\begin{document}
\maketitle

\begin{abstract}
"""
        + _latex_escape(metadata["abstract"])
        + r"""
\end{abstract}
"""
        + (
            "\n\\noindent\\textbf{关键词：} "
            if is_chinese
            else "\n\\noindent\\textbf{Keywords:} "
        )
        + keywords
        + "\n\n"
        + body_tex
        + acknowledgements
        + r"""
\bibliographystyle{unsrtnat}
\bibliography{references}
\end{document}
"""
    )


def _pdf_validation(pdf_path: Path, *, expected_title: str) -> dict[str, Any]:
    result = {
        "exists": pdf_path.is_file(),
        "page_count": 0,
        "text_character_count": 0,
        "title_found": False,
        "reference_marker_leak": False,
        "mojibake_found": False,
        "html_tag_leak_found": False,
        "errors": [],
    }
    if not pdf_path.is_file():
        result["errors"].append("compiled_pdf_missing")
        return result
    try:
        import fitz

        document = fitz.open(pdf_path)
        result["page_count"] = document.page_count
        text = "\n".join(page.get_text() for page in document)
        document.close()
        result["text_character_count"] = len(text)
        title_words = {
            word.lower()
            for word in re.findall(r"[A-Za-z]{5,}", expected_title)
        }
        found_words = {
            word.lower() for word in re.findall(r"[A-Za-z]{5,}", text[:6000])
        }
        title_cjk = "".join(re.findall(r"[\u3400-\u9fff]", expected_title))
        found_cjk = "".join(re.findall(r"[\u3400-\u9fff]", text[:6000]))
        if title_cjk:
            title_probe = title_cjk[: min(10, len(title_cjk))]
            result["title_found"] = title_probe in found_cjk
        else:
            result["title_found"] = bool(
                title_words
                and len(title_words & found_words) >= min(3, len(title_words))
            )
        result["reference_marker_leak"] = "[REF:" in text
        result["mojibake_found"] = _contains_likely_mojibake(text)
        result["html_tag_leak_found"] = bool(
            re.search(r"</?[A-Za-z][^>]*>", text)
        )
        if result["page_count"] < 1:
            result["errors"].append("compiled_pdf_has_no_pages")
        if result["text_character_count"] < 500:
            result["errors"].append("compiled_pdf_has_too_little_text")
        if not result["title_found"]:
            result["errors"].append("title_not_found_in_compiled_pdf")
        if result["reference_marker_leak"]:
            result["errors"].append("raw_reference_marker_leaked_into_pdf")
        if result["mojibake_found"]:
            result["errors"].append("mojibake_detected_in_compiled_pdf")
        if result["html_tag_leak_found"]:
            result["errors"].append("html_tag_leaked_into_compiled_pdf")
    except Exception as exc:
        result["errors"].append(f"pdf_validation_error:{type(exc).__name__}:{exc}")
    return result


def _render_preview_pages(
    pdf_path: Path,
    *,
    output_dir: Path,
) -> list[str]:
    pdfinfo = _find_binary("pdfinfo")
    pdftoppm = _find_binary("pdftoppm")
    if not pdf_path.is_file() or not pdftoppm:
        return []
    page_count = 1
    if pdfinfo:
        info = _run_command(
            [pdfinfo, str(pdf_path)],
            cwd=output_dir,
            timeout_seconds=30,
        )
        match = re.search(r"^Pages:\s+(\d+)", info["stdout"], re.MULTILINE)
        if match:
            page_count = max(1, int(match.group(1)))
    pages = sorted({1, max(1, (page_count + 1) // 2), page_count})
    preview_dir = output_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for page in pages:
        prefix = preview_dir / f"page_{page:03d}"
        command = [
            pdftoppm,
            "-png",
            "-r",
            "120",
            "-f",
            str(page),
            "-l",
            str(page),
            "-singlefile",
            str(pdf_path),
            str(prefix),
        ]
        result = _run_command(command, cwd=output_dir, timeout_seconds=60)
        rendered = prefix.with_suffix(".png")
        if result["returncode"] == 0 and rendered.exists():
            outputs.append(str(rendered))
    return outputs


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_arxiv_archive(
    output_dir: Path,
    *,
    referenced_figures: list[dict[str, Any]],
) -> tuple[Path, list[dict[str, Any]]]:
    archive_path = output_dir / "arxiv-source.zip"
    relative_paths = [
        Path("main.tex"),
        Path("references.bib"),
        Path("main.bbl"),
        Path("00README.txt"),
    ]
    relative_paths.extend(
        Path(str(item["latex_path"])) for item in referenced_figures
    )
    files = []
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for relative in relative_paths:
            source = output_dir / relative
            if not source.is_file():
                continue
            archive.write(source, arcname=relative.as_posix())
            files.append(
                {
                    "path": relative.as_posix(),
                    "bytes": source.stat().st_size,
                    "sha256": _sha256(source),
                }
            )
    return archive_path, files


@dataclass
class LatexPublicationRenderer:
    content_package_path: Path
    output_dir: Path
    metadata_path: Optional[Path] = None
    source_markdown_path: Optional[Path] = None
    language: str = "en"
    document_type: str = "review"
    enrich_crossref: bool = True
    max_crossref_requests: int = 20
    compile_pdf: bool = True
    # F5: when compile_pdf is requested but the LaTeX toolchain is missing,
    # pdf_strict=True fast-fails (--require-pdf); the default degrades to
    # skipping the PDF while keeping .tex/.md/.bib artifacts.
    pdf_strict: bool = False
    render_previews: bool = True
    pandoc_path: Optional[Path] = None
    latexmk_path: Optional[Path] = None
    compile_timeout_seconds: float = 240.0
    pdf_skipped_reason: Optional[str] = field(default=None, init=False)
    _warnings: list[str] = field(default_factory=list, init=False)

    def build(self) -> dict[str, Any]:
        package_path = Path(self.content_package_path).resolve()
        package = _read_json(package_path)
        if not package:
            raise ValueError(f"Invalid content package: {package_path}")
        self.output_dir = Path(self.output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        run_dir = (
            _resolve_artifact_path(
                package.get("source_run_dir"), package_path=package_path
            )
            or package_path.parent
        )

        review_path = (
            Path(self.source_markdown_path).resolve()
            if self.source_markdown_path
            else _resolve_artifact_path(
                package.get("final_review_path"),
                package_path=package_path,
            )
        )
        visual_path = _resolve_artifact_path(
            package.get("final_visual_package_path")
            or package.get("visual_editorial_plan_path"),
            package_path=package_path,
        )
        kb_path = _resolve_artifact_path(
            package.get("base_kb_sqlite"),
            package_path=package_path,
        )
        blueprint_path = _resolve_artifact_path(
            package.get("artifacts", {}).get("review_blueprint"),
            package_path=package_path,
        )
        if not review_path or not review_path.is_file():
            raise ValueError("Content package has no readable final review")

        blueprint = _read_json(blueprint_path)
        visual_plan = _read_json(visual_path)
        metadata, metadata_warnings = resolve_publication_metadata(
            content_package_path=package_path,
            metadata_path=self.metadata_path,
        )
        self._warnings.extend(metadata_warnings)

        markdown, integrity_before_figures = _prepare_scientific_markdown_with_integrity(
            _strip_embedded_title_and_abstract(
                review_path.read_text(encoding="utf-8")
            ),
            preserve_verification_labels=(
                self.document_type == "research_plan"
            ),
        )
        reference_aliases = _load_reference_alias_map(
            package,
            package_path=package_path,
        )
        markdown, ordered_ids, key_by_id = _replace_reference_markers(
            markdown,
            canonical_id_by_alias=reference_aliases,
        )
        if REF_PATTERN.search(markdown):
            raise ValueError("Unresolved Review Harness reference marker remains")

        figures, rejected_figures = _copy_verified_figures(
            visual_plan,
            output_dir=self.output_dir,
            base_dir=visual_path.parent if visual_path else None,
        )
        figure_asset_audit = {
            "schema_version": "research_harness.figure_asset_audit.v1",
            "accepted": [
                _portable_audit_entry(
                    item.get("publication_asset_audit", {}),
                    output_dir=self.output_dir,
                    package_root=package_path.parent,
                )
                for item in figures
            ],
            "rejected": [
                _portable_rejected_entry(
                    row,
                    output_dir=self.output_dir,
                    package_root=package_path.parent,
                )
                for row in rejected_figures
            ],
            "policy": (
                "Source-derived images are publication copies with any "
                "embedded caption removed only when an explicit crop or a "
                "conservative caption-band detector supports it. The reader-"
                "facing caption is rendered outside the image."
            ),
        }
        omitted_figure_placements: list[dict[str, Any]] = []
        markdown = _inject_figures(
            markdown,
            figures=figures,
            blueprint=blueprint,
            language=self.language,
            omitted_figures=omitted_figure_placements,
        )
        figure_asset_audit["rejected"].extend(
            _portable_rejected_entry(
                row,
                output_dir=self.output_dir,
                package_root=package_path.parent,
            )
            for row in omitted_figure_placements
        )
        atomic_write_json(
            self.output_dir / "FIGURE_ASSET_AUDIT.json",
            figure_asset_audit,
        )
        # Captions and visual callouts are injected after the manuscript's
        # first normalization pass, so normalize the assembled source once
        # more before Pandoc sees it.
        markdown, integrity_after_figures = _prepare_scientific_markdown_with_integrity(
            markdown,
            preserve_verification_labels=(
                self.document_type == "research_plan"
            ),
        )
        if (
            integrity_after_figures.get("public_prose_audit", {}).get("status")
            != "passed"
        ):
            self._warnings.append("internal_workflow_language_detected")
        fragmented_formula_count = len(
            re.findall(r"\$[^$\n]+\$\s*\$[\^_]\{", markdown)
        )
        if fragmented_formula_count:
            self._warnings.append("fragmented_formula_spans_detected")
        unresolved_formula_hazards = list(
            integrity_after_figures.get("unresolved_formula_hazards", [])
        )
        if unresolved_formula_hazards:
            self._warnings.append("unresolved_formula_hazards_detected")
        atomic_write_json(
            self.output_dir / "PUBLICATION_INTEGRITY_AUDIT.json",
            {
                "schema_version": "research_harness.publication_integrity_audit.v1",
                "document_type": self.document_type,
                "before_figure_injection": integrity_before_figures,
                "after_figure_injection": integrity_after_figures,
                "fragmented_formula_span_count": fragmented_formula_count,
                "unresolved_formula_hazards": unresolved_formula_hazards,
            },
        )
        unicode_audit_source = "\n".join(
            [
                markdown,
                str(metadata.get("title") or ""),
                str(metadata.get("abstract") or ""),
                " ".join(metadata.get("keywords") or []),
            ]
        )
        unicode_preflight = _scientific_unicode_preflight(
            unicode_audit_source
        )
        if unicode_preflight["dangerous_count"]:
            self._warnings.append("dangerous_unicode_detected")
        manuscript_md = self.output_dir / "manuscript.normalized.md"
        _write_text(manuscript_md, markdown)

        local_records = _collect_source_metadata(run_dir)
        catalog_records = _collect_catalog_metadata(
            package,
            package_path=package_path,
        )
        for paper_id, row in catalog_records.items():
            prior = local_records.get(paper_id)
            if prior is None or _metadata_score(row) >= _metadata_score(prior):
                local_records[paper_id] = row
        bibliography_cache_path = (
            self.output_dir / "BIBLIOGRAPHY_METADATA.json"
        )
        cached_records = _read_json(bibliography_cache_path).get(
            "records",
            {},
        )
        if isinstance(cached_records, dict):
            for paper_id, row in cached_records.items():
                if not isinstance(row, dict):
                    continue
                prior = local_records.get(str(paper_id))
                if prior is None or _metadata_score(row) >= _metadata_score(
                    prior
                ):
                    local_records[str(paper_id)] = dict(row)
        kb_records = _collect_kb_metadata(kb_path, ordered_ids)
        references, reference_audit = _merge_reference_metadata(
            ordered_ids,
            local_records=local_records,
            kb_records=kb_records,
            enrich_crossref=self.enrich_crossref,
            max_crossref_requests=self.max_crossref_requests,
        )
        incomplete_references = [
            row["paper_id"]
            for row in reference_audit
            if row.get("identity_incomplete")
        ]
        locator_pending_ids = [
            row["paper_id"]
            for row in reference_audit
            if row.get("stable_locator_pending")
        ]
        if incomplete_references:
            self._warnings.append("incomplete_bibliographic_metadata")
        if locator_pending_ids:
            self._warnings.append("stable_locator_pending")

        # Targeted recovery pass for title-only references (title present but
        # authors or year still missing after the per-reference Crossref
        # budget).  This runs outside the earlier budget so every final
        # reference with a title gets one dedicated bibliographic search
        # attempt before the hard BibTeX gate fires.
        references, title_recovery_audit = _recover_incomplete_final_references(
            ordered_ids,
            references,
            enrich_crossref=self.enrich_crossref,
            max_crossref_requests=self.max_crossref_requests,
        )
        if any(row.get("recovered_fields") for row in title_recovery_audit):
            self._warnings.append("title_only_references_recovered_via_crossref")
        if title_recovery_audit:
            atomic_write_json(
                self.output_dir / "TITLE_RECOVERY_AUDIT.json",
                {
                    "schema_version": (
                        "research_harness.title_recovery_audit.v1"
                    ),
                    "recovery_attempts": len(title_recovery_audit),
                    "recovered_count": sum(
                        1
                        for row in title_recovery_audit
                        if row.get("recovered_fields")
                    ),
                    "entries": title_recovery_audit,
                },
            )

        # Fail-open: drop references that still cannot satisfy the BibTeX
        # contract after all local and online recovery passes.  Their citation
        # tokens are removed from the Markdown and an auditable JSON file is
        # written, but surrounding prose and neighbouring valid citations are
        # left intact.  The renderer still raises for any record that reaches
        # _reference_bibtex with missing fields — this block is the pre-filter.
        _original_marker_count = len(ordered_ids)
        dropped_entries: list[dict[str, Any]] = _identify_dropped_references(
            ordered_ids, references
        )
        if dropped_entries:
            _dropped_ids: set[str] = {
                e["paper_id"] for e in dropped_entries
            }
            _dropped_keys: set[str] = {
                key_by_id[pid]
                for pid in _dropped_ids
                if pid in key_by_id
            }
            markdown = _drop_citation_tokens(markdown, _dropped_keys)
            # Re-write the normalised Markdown so pandoc converts the
            # updated text (citation tokens already removed).
            _write_text(manuscript_md, markdown)
            ordered_ids = [
                pid for pid in ordered_ids if pid not in _dropped_ids
            ]
            for pid in _dropped_ids:
                key_by_id.pop(pid, None)
            references = {
                k: v for k, v in references.items() if k not in _dropped_ids
            }
            incomplete_references = [
                pid
                for pid in incomplete_references
                if pid not in _dropped_ids
            ]
            locator_pending_ids = [
                pid
                for pid in locator_pending_ids
                if pid not in _dropped_ids
            ]
            atomic_write_json(
                self.output_dir / "DROPPED_REFERENCE_AUDIT.json",
                {
                    "schema_version": (
                        "research_harness.dropped_reference_audit.v1"
                    ),
                    "original_marker_count": _original_marker_count,
                    "dropped_count": len(dropped_entries),
                    "entries": dropped_entries,
                },
            )
            self._warnings.append("unresolved_references_dropped_fail_open")

        atomic_write_json(
            bibliography_cache_path,
            {
                "schema_version": (
                    "research_harness.bibliography_metadata_cache.v1"
                ),
                "records": references,
                "updated_at": _now(),
            },
        )
        references_path = self.output_dir / "references.bib"
        _write_text(
            references_path,
            _reference_bibtex(ordered_ids, key_by_id, references),
        )
        atomic_write_json(
            self.output_dir / "REFERENCE_METADATA_AUDIT.json",
            {
                "schema_version": "research_harness.reference_metadata_audit.v1",
                "reference_count": len(ordered_ids),
                "incomplete_reference_count": len(incomplete_references),
                "references": reference_audit,
            },
        )

        pandoc = str(
            self.pandoc_path
            or _find_binary("pandoc")
            or ""
        )
        if not pandoc:
            raise RuntimeError(
                "Pandoc is required for Markdown-to-LaTeX conversion"
            )
        body_path = self.output_dir / "body.tex"
        pandoc_result = _run_command(
            [
                pandoc,
                str(manuscript_md.name),
                "--from=markdown+pipe_tables+strikeout+tex_math_dollars+link_attributes",
                "--to=latex",
                "--natbib",
                "--wrap=none",
                "--output",
                str(body_path.name),
            ],
            cwd=self.output_dir,
            timeout_seconds=120,
        )
        if pandoc_result["returncode"] != 0 or not body_path.exists():
            raise RuntimeError(
                "Pandoc conversion failed: "
                + str(pandoc_result["stderr"] or pandoc_result["stdout"])[-1000:]
            )
        body_tex = body_path.read_text(encoding="utf-8")
        main_path = self.output_dir / "main.tex"
        # Use a Unicode-native engine for both languages.  Scientific
        # normalization handles known notation semantically; XeLaTeX keeps an
        # unforeseen but valid Unicode character from crashing the entire
        # pressure-test run.
        tex_engine = "xelatex"
        _write_text(
            main_path,
            _render_main_tex(
                metadata=metadata,
                body_tex=body_tex,
                language=self.language,
                tex_engine=tex_engine,
            ),
        )
        _write_text(
            self.output_dir / "00README.txt",
            (
                "Main file: main.tex\n"
                f"TeX engine: {tex_engine}\n"
                "Bibliography: references.bib and pre-generated main.bbl\n"
                "The source archive contains only files required to compile "
                "the manuscript.\n"
            ),
        )

        latex_result: dict[str, Any] = {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
        pdf_path = self.output_dir / "main.pdf"
        if self.compile_pdf:
            latexmk = str(
                self.latexmk_path
                or _find_binary("latexmk")
                or ""
            )
            if not latexmk:
                if self.pdf_strict:
                    raise RuntimeError(
                        "--require-pdf is set but latexmk was not found on PATH; "
                        "install a TeX distribution providing xelatex+latexmk, "
                        "or drop --require-pdf to skip PDF compilation"
                    )
                # F5 auto-degrade: keep the run alive without a PDF and make
                # the skip explicit via report field + harness event stream.
                self.compile_pdf = False
                self.pdf_skipped_reason = "latexmk_not_found"
            else:
                latex_result = _run_command(
                    [
                        latexmk,
                        "-g",
                        "-xelatex" if tex_engine == "xelatex" else "-pdf",
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "-file-line-error",
                        "main.tex",
                    ],
                    cwd=self.output_dir,
                    timeout_seconds=self.compile_timeout_seconds,
                )
        log_text = ""
        log_path = self.output_dir / "main.log"
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        latex_warnings = LATEX_WARNING_PATTERN.findall(log_text)
        undefined_citations = sorted(
            set(
                re.findall(
                    r"Citation [`']([^`']+)[`'] .* undefined",
                    log_text,
                    re.IGNORECASE,
                )
            )
        )
        overfull_box_count = len(
            re.findall(r"Overfull \\[hv]box", log_text)
        )
        pdf_validation = _pdf_validation(
            pdf_path,
            expected_title=metadata["title"],
        )
        preview_paths = (
            _render_preview_pages(pdf_path, output_dir=self.output_dir)
            if self.render_previews
            else []
        )
        archive_path, archived_files = _create_arxiv_archive(
            self.output_dir,
            referenced_figures=figures,
        )

        compile_ok = bool(
            not self.compile_pdf
            or (
                latex_result.get("returncode") == 0
                and pdf_path.exists()
                and not pdf_validation["errors"]
                and not undefined_citations
                and not latex_warnings
            )
        )
        submission_blockers = []
        if not compile_ok:
            submission_blockers.append("latex_or_pdf_validation_failed")
        if incomplete_references:
            submission_blockers.append("incomplete_bibliographic_metadata")
        if locator_pending_ids:
            submission_blockers.append("stable_locator_pending")
        if "author_metadata_pending" in self._warnings:
            submission_blockers.append("author_metadata_pending")
        if metadata.get("draft_only"):
            submission_blockers.append("draft_test_metadata")
        if "internal_workflow_language_detected" in self._warnings:
            submission_blockers.append("internal_workflow_language_detected")
        if "unresolved_formula_hazards_detected" in self._warnings:
            submission_blockers.append("unresolved_formula_hazards_detected")
        if rejected_figures:
            self._warnings.append("one_or_more_visual_placements_rejected")
        status = (
            "submission_ready"
            if not submission_blockers
            else (
                "compiled_awaiting_metadata"
                if compile_ok
                and set(submission_blockers).issubset(
                    {
                        "incomplete_bibliographic_metadata",
                        "stable_locator_pending",
                        "author_metadata_pending",
                        "draft_test_metadata",
                    }
                )
                else "failed"
            )
        )
        report = {
            "schema_version": "research_harness.latex_build_report.v3",
            "status": status,
            "created_at": _now(),
            "content_package_path": str(package_path),
            "title": metadata["title"],
            "language": self.language,
            "document_type": self.document_type,
            "author_count": len(metadata["authors"]),
            "reference_count": len(ordered_ids),
            "incomplete_reference_ids": incomplete_references,
            "stable_locator_pending_ids": locator_pending_ids,
            "verified_figures_copied": len(figures),
            "rendered_figures_copied": len(figures),
            "body_visual_callouts_inserted": (
                len(figures) - len(omitted_figure_placements)
            ),
            "visual_input_contract": (
                "final_visual_package"
                if isinstance(visual_plan.get("figures"), list)
                else "legacy_visual_editorial_plan"
            ),
            "rejected_visual_placements": rejected_figures,
            "conceptual_visual_requests_pending": len(
                visual_plan.get("conceptual_figure_requests", []) or []
            ),
            "unfilled_visual_needs": len(
                visual_plan.get(
                    "unfilled_visual_opportunities",
                    visual_plan.get("unfilled_visual_needs", []),
                )
                or []
            ),
            "pandoc": {
                "path": pandoc,
                "returncode": pandoc_result["returncode"],
            },
            "unicode_preflight": {
                **unicode_preflight,
                "normalization_strategy": (
                    "class_based_scientific_unicode_to_tex"
                ),
                "compiler_strategy": "unicode_native_xelatex",
            },
            "latex": {
                "compiled": self.compile_pdf,
                "pdf_skipped_reason": self.pdf_skipped_reason,
                "message": (
                    "compile_pdf is False; no PDF was produced"
                    if not self.compile_pdf
                    else ""
                ),
                "returncode": latex_result.get("returncode"),
                "timed_out": latex_result.get("timed_out", False),
                "undefined_citations": undefined_citations,
                "warning_count": len(latex_warnings),
                "overfull_box_count": overfull_box_count,
            },
            "pdf_validation": pdf_validation,
            "preview_paths": preview_paths,
            "submission_blockers": submission_blockers,
            "warnings": sorted(set(self._warnings)),
            "artifacts": {
                "main_tex": str(main_path),
                "body_tex": str(body_path),
                "normalized_markdown": str(manuscript_md),
                "references_bib": str(references_path),
                "main_bbl": str(self.output_dir / "main.bbl"),
                "compiled_pdf": str(pdf_path) if pdf_path.exists() else "",
                "arxiv_source_zip": str(archive_path),
                "reference_metadata_audit": str(
                    self.output_dir / "REFERENCE_METADATA_AUDIT.json"
                ),
                "publication_integrity_audit": str(
                    self.output_dir / "PUBLICATION_INTEGRITY_AUDIT.json"
                ),
                "figure_asset_audit": str(
                    self.output_dir / "FIGURE_ASSET_AUDIT.json"
                ),
                "bibliography_metadata_cache": str(
                    bibliography_cache_path
                ),
                "dropped_reference_audit": str(
                    self.output_dir / "DROPPED_REFERENCE_AUDIT.json"
                ) if dropped_entries else "",
            },
            "arxiv_source_files": archived_files,
            "arxiv_policy": {
                "compiler": tex_engine,
                "source_root_contains_main_file": True,
                "bib_and_bbl_included": bool(
                    (self.output_dir / "references.bib").exists()
                    and (self.output_dir / "main.bbl").exists()
                ),
                "compiled_pdf_excluded_from_source_archive": True,
                "auxiliary_build_files_excluded": True,
                "only_review_accepted_final_figures_included": bool(
                    isinstance(visual_plan.get("figures"), list)
                ),
                "legacy_only_verified_existing_figures_included": bool(
                    not isinstance(visual_plan.get("figures"), list)
                ),
            },
        }
        atomic_write_json(
            self.output_dir / "LATEX_BUILD_REPORT.json",
            _portable_report_refs(
                report,
                output_dir=self.output_dir,
                package_root=package_path.parent,
            ),
        )
        atomic_write_json(
            self.output_dir / "ARXIV_MANIFEST.json",
            {
                "schema_version": "research_harness.arxiv_manifest.v1",
                "status": status,
                "archive_path": _portable_ref(
                    archive_path,
                    output_dir=self.output_dir,
                    package_root=package_path.parent,
                ),
                "files": archived_files,
                "submission_blockers": submission_blockers,
                "generated_at": _now(),
            },
        )
        return report


def build_latex_publication(
    *,
    content_package_path: Path,
    output_dir: Path,
    metadata_path: Optional[Path] = None,
    source_markdown_path: Optional[Path] = None,
    language: str = "en",
    document_type: str = "review",
    enrich_crossref: bool = True,
    compile_pdf: bool = True,
    pdf_strict: bool = False,
    render_previews: bool = True,
) -> dict[str, Any]:
    return LatexPublicationRenderer(
        content_package_path=Path(content_package_path),
        output_dir=Path(output_dir),
        metadata_path=Path(metadata_path) if metadata_path else None,
        source_markdown_path=(
            Path(source_markdown_path) if source_markdown_path else None
        ),
        language=language,
        document_type=document_type,
        enrich_crossref=enrich_crossref,
        compile_pdf=compile_pdf,
        pdf_strict=pdf_strict,
        render_previews=render_previews,
    ).build()
