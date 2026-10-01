"""Lossless standalone application for the post-BODY serial-parts runner.

This module has no planner, provider, or legacy front/back dependencies. Its
Markdown contract is deliberately small: an optional leading H1 is article
metadata; unowned BODY chapters use H2 or deeper. Extra H1s, explicit chapter
identities on that leading H1, and malformed ownership markers fail closed.
Only generated, valid owned spans and that article-title line may be replaced.
An unowned article-level abstract/introduction/conclusion is a placement
conflict, never permission to replace or extend a BODY chapter.

``extract_body`` is also the byte-for-byte preservation projection: it removes
owned spans (including their terminating line break) and the optional title
line, without trimming or normalizing anything else. References stay in BODY.
New padding is inside owned spans, so the projection is identical before and
after application, including CRLF, blank lines, and a missing final newline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_PART_NAMES = {
    "abstract": frozenset(("abstract", "摘要")),
    "introduction": frozenset(("introduction", "引言", "绪论")),
    "conclusion": frozenset(("conclusion", "conclusions", "结论", "结语", "总结")),
}
_ROLE_NAMES = {
    **_PART_NAMES,
    "introduction": _PART_NAMES["introduction"] | {"opening", "introduction-like", "入口"},
    "conclusion": _PART_NAMES["conclusion"] | {"closing", "conclusion-like", "收束"},
}
_HEADINGS = {
    "zh": {"abstract": "摘要", "introduction": "引言", "conclusion": "结语"},
    "en": {"abstract": "Abstract", "introduction": "Introduction", "conclusion": "Conclusion"},
}
_REFERENCE_NAMES = frozenset(("references", "bibliography", "参考文献"))
_MARKER_LIKE = re.compile(r"<!--\s*manuscript-part\b", re.IGNORECASE)
_MARKER = re.compile(r"<!-- manuscript-part:(title|abstract|introduction|conclusion):(start|end) -->")
_INLINE_TOKEN = re.compile(r"`+|<!--|-->")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*(?:\r?\n)?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$")
_PREFIX = re.compile(
    r"^(?:(?:Chapter\s+|CH)([0-9]+)(?![A-Za-z0-9]|\.[0-9])"
    r"|第([0-9一二三四五六七八九十百千零〇两]+)章"
    r"|([0-9]+)(?![0-9]|\.[0-9])(?=[.、:：) \t]))[ \t]*[.、:：)\-]?[ \t]*",
    re.IGNORECASE,
)


class SerialPartsApplicationError(ValueError):
    """The manuscript cannot be changed without crossing an ownership boundary."""


@dataclass(frozen=True)
class _Span:
    part: str
    start: int
    end: int


@dataclass(frozen=True)
class _Heading:
    start: int
    end: int
    level: int
    title: str
    line: str


def _visible_lines(manuscript: str) -> list[tuple[int, str]]:
    """Return non-fenced, non-indented-code lines with original character offsets."""
    visible: list[tuple[int, str]] = []
    fence = ""
    offset = 0
    for line in manuscript.splitlines(keepends=True):
        match = _FENCE.fullmatch(line)
        if fence:
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                fence = ""
        elif match and not (match[1][0] == "`" and "`" in match[2]):
            fence = match[1]
        elif not line.startswith(("    ", "\t")):
            visible.append((offset, line))
        offset += len(line)
    if fence:
        raise SerialPartsApplicationError("unclosed_markdown_fence")
    return visible


def _owned_spans(manuscript: str, lines: Sequence[tuple[int, str]]) -> list[_Span]:
    spans: list[_Span] = []
    opened: tuple[str, int] | None = None
    seen: set[str] = set()
    inline_ticks = 0
    ignored_inline_marker = False
    ordinary_comment = False
    for offset, line in lines:
        if not line.strip():
            if inline_ticks and ignored_inline_marker:
                raise SerialPartsApplicationError("invalid_owned_part_markers:ambiguous_inline_code")
            inline_ticks = 0
            ignored_inline_marker = False
        for probe in _INLINE_TOKEN.finditer(line):
            if ordinary_comment:
                if probe[0] == "-->":
                    ordinary_comment = False
                elif _MARKER_LIKE.match(line, probe.start()):
                    raise SerialPartsApplicationError("invalid_owned_part_markers:inside_html_comment")
                continue
            if probe[0].startswith("`"):
                if inline_ticks == 0:
                    prefix = line[:probe.start()]
                    if (len(prefix) - len(prefix.rstrip("\\"))) % 2:
                        continue
                    inline_ticks = len(probe[0])
                elif inline_ticks == len(probe[0]):
                    inline_ticks = 0
                    ignored_inline_marker = False
                continue
            if inline_ticks:
                ignored_inline_marker |= bool(_MARKER_LIKE.match(line, probe.start()))
                continue
            if probe[0] == "-->":
                continue
            if not _MARKER_LIKE.match(line, probe.start()):
                ordinary_comment = True
                continue
            marker = _MARKER.match(line, probe.start())
            if marker is None:
                raise SerialPartsApplicationError("invalid_owned_part_markers:malformed")
            part, edge = marker.groups()
            if edge == "start":
                if opened is not None:
                    raise SerialPartsApplicationError("invalid_owned_part_markers:nested")
                if part in seen:
                    raise SerialPartsApplicationError(f"invalid_owned_part_markers:duplicate:{part}")
                opened = (part, offset + marker.start())
                seen.add(part)
            else:
                if opened is None or opened[0] != part:
                    raise SerialPartsApplicationError(f"invalid_owned_part_markers:unmatched:{part}")
                end = offset + marker.end()
                # A generated block owns its immediately following line break.
                if manuscript.startswith("\r\n", end):
                    end += 2
                elif manuscript.startswith("\n", end):
                    end += 1
                spans.append(_Span(part, opened[1], end))
                opened = None
    if inline_ticks and ignored_inline_marker:
        raise SerialPartsApplicationError("invalid_owned_part_markers:ambiguous_inline_code")
    if ordinary_comment:
        raise SerialPartsApplicationError("unclosed_html_comment")
    if opened is not None:
        raise SerialPartsApplicationError(f"invalid_owned_part_markers:unclosed:{opened[0]}")
    return spans


def _remove_spans(manuscript: str, spans: Sequence[_Span]) -> str:
    result: list[str] = []
    at = 0
    for span in sorted(spans, key=lambda value: value.start):
        result.append(manuscript[at:span.start])
        at = span.end
    result.append(manuscript[at:])
    return "".join(result)


def _bare_title(title: str) -> str:
    return _PREFIX.sub("", title, count=1).strip()


def _chapter_number(title: str) -> int | None:
    match = _PREFIX.match(title)
    if match is None:
        return None
    value = next(group for group in match.groups() if group is not None)
    if value.isascii() and value.isdecimal():
        return int(value)
    digits = {char: number for number, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    total = pending = 0
    for char in value:
        if char in digits:
            pending = digits[char]
        elif char in "十百千":
            total += (pending or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            pending = 0
        else:
            return None
    return total + pending


def _explicit_number(row: Mapping[str, Any]) -> int | None:
    chapter_id = str(row.get("chapter_id") or "").strip()
    match = re.fullmatch(r"(?:CH|C)?([0-9]+)", chapter_id, re.IGNORECASE)
    if match:
        return int(match[1])
    value = row.get("chapter_number")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) and int(value) > 0:
        return int(value)
    return None


def _identity_matches(row: Mapping[str, Any], headings: Sequence[_Heading]) -> list[_Heading]:
    """Resolve explicit ID, exact title, or explicit chapter number, never index."""
    chapter_id = str(row.get("chapter_id") or "").strip()
    title = str(row.get("title") or "").strip()
    candidates: set[int] = set()
    if chapter_id and not chapter_id.isdecimal():
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(chapter_id)}(?![A-Za-z0-9_]|\.[0-9])", re.IGNORECASE)
        candidates.update(heading.start for heading in headings if pattern.search(heading.title))
    if title:
        candidates.update(heading.start for heading in headings
                          if heading.title == title or _bare_title(heading.title) == title)
    number = _explicit_number(row)
    if number is not None:
        candidates.update(heading.start for heading in headings
                          if _chapter_number(heading.title) == number)
    return [heading for heading in headings if heading.start in candidates]


def _structure(
    manuscript: str, chapter_roles: Sequence[Mapping[str, Any]],
) -> tuple[list[_Span], list[_Heading], _Heading | None]:
    if not isinstance(manuscript, str):
        raise SerialPartsApplicationError("manuscript_must_be_text")
    lines = _visible_lines(manuscript)
    spans = _owned_spans(manuscript, lines)
    headings = []
    for offset, line in lines:
        if any(span.start <= offset < span.end for span in spans):
            continue
        match = _HEADING.fullmatch(line)
        if match:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2]).strip()
            headings.append(_Heading(offset, offset + len(line), len(match[1]), title, line.rstrip("\r\n")))
    h1s = [heading for heading in headings if heading.level == 1]
    if len(h1s) > 1:
        raise SerialPartsApplicationError("ambiguous_document_title:multiple_unowned_h1")
    title = h1s[0] if h1s else None
    if title:
        preceding = [span for span in spans if span.end <= title.start]
        if _remove_spans(manuscript[:title.start], preceding).strip():
            raise SerialPartsApplicationError("ambiguous_document_title:h1_not_leading")
        if any(span.part == "title" for span in spans):
            raise SerialPartsApplicationError("ambiguous_document_title:owned_and_unowned_h1")
        special_names = _REFERENCE_NAMES.union(*_PART_NAMES.values())
        if _bare_title(title.title).casefold() in special_names or _PREFIX.match(title.title):
            raise SerialPartsApplicationError("ambiguous_document_title:h1_is_chapter")
        if any(_identity_matches(row, [title]) for row in chapter_roles):
            raise SerialPartsApplicationError("ambiguous_document_title:h1_matches_chapter_role")
    return spans, headings, title


def preflight_placement(
    manuscript: str, chapter_roles: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return deterministic blocking conflicts and nonblocking ambiguity warnings.

    Only H1/H2 article locations outside code and owned spans are considered.
    Explicit IDs, exact titles and explicit chapter numbers can resolve roles;
    a row's list position or a technical title substring cannot establish one.
    """
    try:
        _spans, headings, title = _structure(manuscript, chapter_roles)
    except SerialPartsApplicationError as exc:
        return ([{"severity": "error", "code": str(exc).split(":", 1)[0],
                  "reason": str(exc), "part": "manuscript"}], [])
    article_headings = [heading for heading in headings if heading.level <= 2 and heading != title]
    conflicts: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    recorded: set[tuple[str, int]] = set()

    def conflict(part: str, heading: _Heading) -> None:
        if (part, heading.start) not in recorded:
            recorded.add((part, heading.start))
            conflicts.append({"severity": "error", "code": "placement_conflict", "part": part,
                              "requested_mode": "standalone", "existing_location": heading.line,
                              "reason": "existing_unowned_body_location"})

    for part, names in _PART_NAMES.items():
        for heading in article_headings:
            bare = _bare_title(heading.title).casefold()
            if bare in names:
                conflict(part, heading)
            elif any(re.match(re.escape(name) + r"(?:\s|[:：])", bare) for name in names):
                warnings.append({"severity": "warning", "code": "ambiguous_part_heading",
                                 "part": part, "heading": heading.line,
                                 "message": "Technical heading alone does not establish article responsibility; BODY preserved."})
        for row in chapter_roles:
            role = str(row.get("role") or "").strip().casefold()
            if role not in _ROLE_NAMES[part]:
                continue
            matches = _identity_matches(row, article_headings)
            if len(matches) == 1:
                conflict(part, matches[0])
            else:
                warnings.append({"severity": "warning", "code": "unresolved_part_role",
                                 "part": part, "chapter_id": str(row.get("chapter_id") or ""),
                                 "message": "Role does not resolve to one unowned article location; BODY preserved."})
    return conflicts, warnings


def extract_body(manuscript: str, chapter_roles: Sequence[Mapping[str, Any]] = ()) -> str:
    """Project original BODY losslessly; remove only valid owned spans and title."""
    spans, _headings, title = _structure(manuscript, chapter_roles)
    if title:
        spans = [*spans, _Span("document_title", title.start, title.end)]
    return _remove_spans(manuscript, spans)


def body_preservation_projection(
    manuscript: str, chapter_roles: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Public exact-preservation check, identical to the model's BODY extraction."""
    return extract_body(manuscript, chapter_roles)


def extract_owned_parts(manuscript: str) -> dict[str, str]:
    """Read owned inner Markdown for publication metadata, validating all markers.

    This does not infer ownership from an unmarked heading. Values retain their
    heading and content but omit the marker comments and surrounding padding.
    """
    spans, _headings, _title = _structure(manuscript, ())
    result = {}
    for span in spans:
        start_marker = f"<!-- manuscript-part:{span.part}:start -->"
        end_marker = f"<!-- manuscript-part:{span.part}:end -->"
        end = manuscript.rfind(end_marker, span.start, span.end)
        result[span.part] = manuscript[span.start + len(start_marker):end].strip()
    return result


def _block(part: str, content: str, newline: str) -> str:
    # A start marker may directly follow the final BODY character when the
    # source has no final newline. Padding still belongs entirely to this span.
    return (f"<!-- manuscript-part:{part}:start -->{newline}{newline}"
            f"{content}{newline}{newline}<!-- manuscript-part:{part}:end -->{newline}")


def _part_text(parts: Mapping[str, Any], part: str) -> str:
    value = parts.get(part)
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise SerialPartsApplicationError(f"part_must_be_text:{part}")
    value = value.strip()
    if _MARKER_LIKE.search(value):
        raise SerialPartsApplicationError(f"part_contains_ownership_marker:{part}")
    if part == "title" and ("\n" in value or "\r" in value):
        raise SerialPartsApplicationError("title_must_be_single_line")
    return value


def apply_serial_parts(
    manuscript: str, parts: Mapping[str, Any],
    chapter_roles: Sequence[Mapping[str, Any]] = (),
    *, language: str = "zh",
) -> tuple[str, list[dict[str, str]]]:
    """Apply standalone parts atomically; never replace an unowned BODY span.

    Unprovided parts remain unchanged. Keywords, when provided, belong to the
    provided abstract. Callers should preflight before generation; this second
    preflight prevents bypassing safety through direct application.
    """
    conflicts, warnings = preflight_placement(manuscript, chapter_roles)
    if conflicts:
        raise SerialPartsApplicationError(conflicts[0]["code"] + ":" + conflicts[0]["reason"])
    if not isinstance(parts, Mapping):
        raise SerialPartsApplicationError("parts_must_be_object")
    language_family = str(language).lower().replace("_", "-").split("-", 1)[0]
    if language_family not in _HEADINGS:
        raise SerialPartsApplicationError("unsupported_application_language:" + str(language))
    supplied = {part: _part_text(parts, part) for part in ("title", *_PART_NAMES)}
    keywords = parts.get("keywords") or []
    if not isinstance(keywords, (list, tuple)) or any(not isinstance(value, str) for value in keywords):
        raise SerialPartsApplicationError("keywords_must_be_list_of_text")
    if keywords and not supplied["abstract"]:
        raise SerialPartsApplicationError("keywords_require_supplied_abstract")
    if any(_MARKER_LIKE.search(value) or "\n" in value or "\r" in value for value in keywords):
        raise SerialPartsApplicationError("invalid_keyword_text")
    original_body = extract_body(manuscript, chapter_roles)
    newline = "\r\n" if "\r\n" in manuscript else "\n"
    result = manuscript
    log: list[dict[str, str]] = list(warnings)
    for part, value in supplied.items():
        if not value:
            continue
        if part == "title":
            content = "# " + value
        else:
            content = "## " + _HEADINGS[language_family][part] + newline + newline + value
            if part == "abstract" and keywords:
                label, separator = ("**Keywords:** ", "; ") if language_family == "en" else ("**关键词：** ", "；")
                content += newline + newline + label + separator.join(value.strip() for value in keywords if value.strip())
        block = _block(part, content, newline)
        spans, headings, title = _structure(result, chapter_roles)
        existing = next((span for span in spans if span.part == part), None)
        if existing:
            start, end = existing.start, existing.end
            position = "owned_part_replaced"
        elif part == "title":
            start, end = (title.start, title.end) if title else (0, 0)
            position = "document_title_replaced" if title else "owned_title_inserted"
        elif part == "conclusion":
            references = [heading for heading in headings
                          if heading.level <= 2 and _bare_title(heading.title).casefold() in _REFERENCE_NAMES]
            start = end = references[0].start if references else len(result)
            position = "standalone_before_references" if references else "standalone_at_end"
        else:
            abstract = next((span for span in spans if span.part == "abstract"), None)
            owned_title = next((span for span in spans if span.part == "title"), None)
            if part == "introduction" and abstract:
                start = end = abstract.end
            elif owned_title:
                start = end = owned_title.end
            else:
                start = end = title.end if title else 0
            position = "standalone_front"
        result = result[:start] + block + result[end:]
        log.append({"part": part, "position": position})
    if extract_body(result, chapter_roles) != original_body:
        raise SerialPartsApplicationError("body_preservation_check_failed")
    return result, log
