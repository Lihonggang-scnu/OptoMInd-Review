"""Chapter-scoped, two-pass Qwen style governance.

The publication boundary uses this module after the manuscript text is fixed
and before LaTeX/PDF rendering.  Each selected chapter has exactly one
reviewer pass followed by one revision-author pass.  Chapters run in parallel,
but the two roles inside a chapter are deliberately sequential:

    Qwen 3.5 Plus reviewer -> structured edit memo -> Qwen 3.7 Flash author

The author receives the original chapter and the review memo, but the local
acceptance gate remains the source of truth.  It reconstructs the only edits
the memo authorized, then accepts the author's output only when it is exactly
that reconstruction.  Citation markers, measurements, formulas, hedges,
protected terminology, polarity cues, paragraph order, and unreviewed prose
are therefore protected even if the model ignores the prompt.

The reviewer is chapter-scoped rather than whole-manuscript scoped.  A compact
global opening/abbreviation inventory is supplied as context so that a chapter
can avoid repeating an opener or redefining a term already introduced in an
earlier chapter without exposing the other chapters' full text to that call.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import re
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from config.qwen_config import get_model_name
from llm.qwen_chat_client import call_qwen_chat

from .cost_ledger import estimate_call_cost_cny
from .review_content_evaluator import _WORD, paragraph_opener_distribution
from .style_hard_verifier import verify_rewrite


_PARA_SPLIT = re.compile(r"(\n\s*\n)")
_H2 = re.compile(r"(?m)^##\s+(?P<title>[^\n]+?)\s*$")
_SENTENCE_BOUNDARY = re.compile(
    r"(?<=[.!?])(?=\s+(?:[A-Z0-9\[\(\"“‘]|$))"
)
_ABBR_PAREN = re.compile(
    r"\((?P<abbr>[A-Z][A-Z0-9²³⁴⁵⁶⁷⁸⁹-]{1,14}s?)\)"
)
_WORD_WITH_DIGITS = re.compile(r"[A-Za-z][A-Za-z0-9²³⁴⁵⁶⁷⁸⁹-]*")
_NEGATION = re.compile(
    r"\b(?:not|no|without|never|cannot|can't|neither|nor|lack|lacks|"
    r"lacked|fails?|failed|unable|insufficient)\b",
    re.IGNORECASE,
)
_FACT_TOKEN = re.compile(
    r"\[REF:[^\]]+\]|[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|"
    r"\$[^$]+\$|\\\([^)]*\\\)|\\\[[^\]]*\\\]"
)
_FENCE = chr(96) * 3

_NON_BODY_TITLES = {"abstract", "introduction", "conclusion"}
_STYLE_OPENERS = {
    "while",
    "building",
    "overall",
    "however",
    "moreover",
    "furthermore",
    "additionally",
    "therefore",
    "consequently",
    "nevertheless",
    "thus",
    "meanwhile",
    "similarly",
    "conversely",
    "specifically",
    "notably",
    "beyond",
    "despite",
}
_SENTENCE_SCAFFOLDS: tuple[tuple[str, str], ...] = (
    ("while ", "while"),
    ("building on ", "building on"),
    ("building upon ", "building on"),
    ("having established ", "having established"),
    ("given that ", "given that"),
    ("to bridge ", "to bridge"),
    ("drawing on ", "drawing on"),
    ("drawing from ", "drawing on"),
    ("grounded in ", "grounded in"),
    ("starting from ", "starting from"),
    ("based on ", "based on"),
    ("informed by ", "informed by"),
    ("guided by ", "guided by"),
    ("on the basis of ", "on the basis of"),
    ("following ", "following"),
    ("beyond ", "beyond"),
)
_CLAIM_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "among",
    "because",
    "before",
    "being",
    "between",
    "could",
    "despite",
    "does",
    "during",
    "each",
    "from",
    "further",
    "have",
    "however",
    "into",
    "more",
    "most",
    "often",
    "only",
    "other",
    "rather",
    "remain",
    "should",
    "since",
    "some",
    "such",
    "than",
    "that",
    "their",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "using",
    "were",
    "which",
    "while",
    "with",
    "would",
}


_REVIEWER_SYSTEM = """
You are the style reviewer for one chapter of a serious English scientific
review article.  Do not rewrite the chapter.  Return only a JSON object with
this shape:

{"edits":[
  {"paragraph_index":1,"action":"replace_first_sentence",
   "original_text":"exact sentence", "replacement_text":"new sentence",
   "reason":"short reason","priority":"high|medium|low"},
  {"paragraph_index":2,"action":"delete_sentence",
   "original_text":"exact redundant sentence", "replacement_text":"",
   "reason":"short reason","priority":"high|medium|low"},
  {"paragraph_index":3,"action":"replace_abbreviation",
   "original_text":"full term (ABC)", "replacement_text":"ABC",
   "reason":"the term was already defined earlier","priority":"medium"}
]}

The chapter text is authoritative.  Every original_text must be copied from
it exactly (apart from harmless whitespace normalization).  If no safe edit is
needed, return {"edits":[]}.

Review priorities, in order:
1. Repair paragraph openings.  The first sentence should enter through the
   physical object, mechanism, result, limitation, or comparison that the
   paragraph actually develops.  Vary rhythm and syntax across neighboring
   paragraphs.  Do not mechanically replace one stock opener with another.
   Avoid repeated starts such as While, Building on/upon, Having established,
   To bridge, Given that, Overall, However, Moreover, and similar scaffolds.
   Aim for lucid, measured, vivid
   scholarly prose rather than decorative or promotional language.
2. Remove a sentence only when it is genuinely redundant and carries no
   citation, number, measurement, formula, hedge, or unique scientific claim.
   Never remove a sentence merely because it is long.
3. Normalize repeated terminology.  The first occurrence may define a term as
   “Full Name (ABC)”.  Once that definition exists, later occurrences should
   normally use “ABC” alone; do not write “Full Name (ABC)” again.  Do not
   invent an acronym, alter a legitimate acronym, or collapse two distinct
   concepts.  Use the supplied abbreviation inventory to identify safe
   repeated definitions.  This rule applies even when the repeated definition
   occurs in another paragraph of this chapter.

Hard limits:
- Do not alter scientific meaning, evidence strength, scope, causality, or
  the chapter's argument.
- Do not change any [REF:...] marker, number, unit, formula, or hedge.
- Do not propose edits outside the chapter or edits to headings.
- Prefer a small number of high-confidence edits over broad stylistic surgery.
- For a first-sentence edit, return the complete replacement sentence.
- For abbreviation normalization, replacement_text must be the existing
  abbreviation exactly.
""".strip()


_REVISER_SYSTEM = """
You are the revision author for one chapter of an English scientific review.
The reviewer memo is advisory input, but only its exact, locally validated
operations may be applied.  Return only this JSON object, with no preamble and
no Markdown fence:

{"revised_paragraphs":[
  {"paragraph_index":3,"text":"the complete revised paragraph"}
]}

Return exactly one revised paragraph record for every paragraph listed in the
validated edit envelope, and no record for any other paragraph.  If the
validated edit envelope is empty, return {"revised_paragraphs":[]}.

Apply only these operations inside each listed paragraph:
- replace_first_sentence: replace that exact first sentence with the exact
  replacement_text;
- delete_sentence: delete that exact sentence only;
- replace_abbreviation: replace that exact full-term occurrence with the exact
  abbreviation in replacement_text.

Everything else in a returned paragraph must remain exactly unchanged:
citations, numbers, units, formulas, hedges, terminology, polarity, and all
unmentioned sentences.  Do not improve the memo, add transitions, or make any
independent edit.  Copy the original paragraph from the supplied chapter and
make only the listed replacement(s).  The local gate will reject any paragraph
that differs from the deterministic edit envelope.
""".strip()


@dataclass(frozen=True)
class ChapterSpan:
    """A selected H2 chapter and its offsets in the complete manuscript."""

    section_id: str
    title: str
    heading_start: int
    body_start: int
    body_end: int
    body_text: str


@dataclass(frozen=True)
class ParagraphRecord:
    """A prose block with both normalized text and raw-body coordinates."""

    index: int
    slot: int
    text: str
    content_start: int
    content_end: int


@dataclass(frozen=True)
class AbbreviationEntry:
    """One full-form/acronym identity discovered in the manuscript."""

    full_form: str
    abbreviation: str
    first_definition_start: int
    first_definition_end: int
    mentions: tuple[tuple[int, int, bool], ...]


class _BudgetGate:
    """Reserve a conservative amount before concurrent model calls."""

    def __init__(self, limit_cny: float) -> None:
        self.limit_cny = max(0.0, float(limit_cny))
        self._reserved = 0.0
        self._actual = 0.0
        self._lock = threading.Lock()

    @property
    def actual(self) -> float:
        with self._lock:
            return self._actual

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return (
                self.limit_cny <= 0.0
                or self._reserved >= self.limit_cny
                or self._actual >= self.limit_cny
            )

    def reserve(self, model_name: str, input_tokens: int, output_tokens: int) -> float | None:
        estimate = estimate_call_cost_cny(
            model_name,
            max(1, int(input_tokens)),
            max(1, int(output_tokens)),
        )
        # Provider accounting can use a higher input bracket than the rough
        # character estimate.  A modest margin prevents six parallel workers
        # from overshooting the caller's hard envelope at the same instant.
        reservation = max(0.0001, estimate * 1.25)
        with self._lock:
            if self._reserved + reservation > self.limit_cny + 1e-12:
                return None
            self._reserved += reservation
        return reservation

    def settle(self, reservation: float | None, actual: float) -> None:
        with self._lock:
            if reservation is not None:
                self._reserved = max(0.0, self._reserved - reservation)
            self._actual += max(0.0, float(actual))


def _safe_json(text: str) -> Dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith(_FENCE):
        cleaned = cleaned[len(_FENCE):].lstrip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        end = cleaned.rfind(_FENCE)
        if end >= 0:
            cleaned = cleaned[:end]
    try:
        value = json.loads(cleaned.strip())
        return value if isinstance(value, dict) else {}
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(cleaned[start:end + 1])
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _normalize_inline(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_for_compare(value: str) -> str:
    """Compare model output while ignoring only whitespace layout changes."""

    return re.sub(r"\s+", " ", str(value or "").strip())


def _title_key(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip()).casefold()


def split_manuscript_chapters(
    manuscript_text: str,
    *,
    chapter_titles: Optional[Sequence[str]] = None,
    chapter_ids: Optional[Mapping[str, str]] = None,
    include_non_body_sections: bool = False,
) -> List[ChapterSpan]:
    """Split H2 blocks without touching headings or non-selected sections.

    Production passes the six S01-S06 titles from the mainline manifest.  A
    standalone caller with no explicit list gets all H2 blocks except
    Abstract/Introduction/Conclusion by default; it can opt into those blocks
    explicitly with ``include_non_body_sections``.
    """

    text = str(manuscript_text or "")
    matches = list(_H2.finditer(text))
    wanted = {_title_key(item) for item in (chapter_titles or []) if str(item).strip()}
    ids = {
        _title_key(key): str(value)
        for key, value in dict(chapter_ids or {}).items()
        if str(key).strip()
    }
    chapters: List[ChapterSpan] = []
    for position, match in enumerate(matches):
        title = str(match.group("title") or "").strip()
        key = _title_key(title)
        if wanted:
            selected = key in wanted
        else:
            selected = include_non_body_sections or key not in _NON_BODY_TITLES
        if not selected:
            continue
        body_start = match.end()
        body_end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        chapters.append(
            ChapterSpan(
                section_id=ids.get(key, f"CH{len(chapters) + 1:02d}"),
                title=title,
                heading_start=match.start(),
                body_start=body_start,
                body_end=body_end,
                body_text=text[body_start:body_end],
            )
        )
    return chapters


def _paragraph_records(body_text: str) -> List[ParagraphRecord]:
    segments = _PARA_SPLIT.split(str(body_text or ""))
    records: List[ParagraphRecord] = []
    cursor = 0
    index = 0
    for slot, segment in enumerate(segments):
        start = cursor
        end = cursor + len(segment)
        cursor = end
        if slot % 2 == 1:
            continue
        stripped = segment.strip()
        if not stripped or stripped.startswith("#"):
            continue
        left = len(segment) - len(segment.lstrip())
        right = len(segment.rstrip())
        records.append(
            ParagraphRecord(
                index=index,
                slot=slot,
                text=stripped,
                content_start=start + left,
                content_end=start + right,
            )
        )
        index += 1
    return records


def _sentence_spans(value: str) -> List[tuple[int, int]]:
    text = str(value or "")
    spans: List[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        end = match.start()
        if text[start:end].strip():
            spans.append((start, end))
        start = match.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _sentence_texts(value: str) -> List[str]:
    return [str(value)[start:end].strip() for start, end in _sentence_spans(value)]


def _first_sentence(value: str) -> str:
    sentences = _sentence_texts(value)
    return sentences[0] if sentences else str(value or "").strip()


def _find_exact_span(value: str, target: str) -> tuple[int, int] | None:
    text = str(value or "")
    needle = str(target or "").strip()
    if not needle:
        return None
    direct = text.find(needle)
    if direct >= 0:
        return direct, direct + len(needle)
    # Reviewers sometimes collapse a line wrap while copying a sentence.  The
    # token sequence must still match; punctuation and case remain exact.
    tokens = re.findall(r"\S+", needle)
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    match = re.search(pattern, text)
    return (match.start(), match.end()) if match else None


def _strip_plural(acronym: str) -> str:
    value = re.sub(r"[^A-Z0-9²³⁴⁵⁶⁷⁸⁹-]", "", str(acronym or ""))
    if value.endswith("S") and len(value) > 2:
        value = value[:-1]
    return value


def _initials(words: Sequence[str]) -> str:
    return "".join(str(word).strip()[:1].upper() for word in words if str(word).strip())


def _infer_full_form(text: str, opening_parenthesis: int, abbreviation: str) -> tuple[str, int] | None:
    """Infer the shortest useful preceding noun phrase for an acronym."""

    window_start = max(0, opening_parenthesis - 240)
    before = text[window_start:opening_parenthesis]
    # Do not let a paragraph heading or the preceding sentence leak into the
    # noun phrase.  This matters for a definition such as ``Diffractive Deep
    # Neural Networks (D2NNs)`` when the source paragraph begins immediately
    # after a sentence ending in the previous block.
    boundaries = list(re.finditer(r"\n\s*\n|[.!?]", before))
    if boundaries:
        boundary = boundaries[-1]
        window_start += boundary.end()
        before = before[boundary.end():]
    word_matches = list(_WORD_WITH_DIGITS.finditer(before))
    if not word_matches:
        return None
    core = _strip_plural(abbreviation)
    candidates: list[tuple[int, int, int]] = []
    max_words = min(10, len(word_matches))
    for count in range(1, max_words + 1):
        selected = word_matches[-count:]
        words = [item.group(0) for item in selected]
        if _initials(words) == core:
            candidates.append((count, selected[0].start(), selected[-1].end()))
    if candidates:
        _count, start, end = min(candidates, key=lambda row: row[0])
        return before[start:end].strip(), window_start + start
    # Hyphenated names such as Hardware-in-the-Loop (HIL) do not always have
    # acronym initials that match literally.  Keep a short suffix as a
    # conservative inventory entry; the reviewer still must copy the exact
    # phrase from the chapter before it can be applied.
    selected = word_matches[-min(5, len(word_matches)):]
    phrase = before[selected[0].start():selected[-1].end()].strip()
    phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
    return (phrase, window_start + selected[0].start()) if phrase else None


def _collect_abbreviation_entries(
    manuscript_text: str,
    chapters: Sequence[ChapterSpan],
) -> List[AbbreviationEntry]:
    text = str(manuscript_text or "")
    raw: dict[tuple[str, str], dict[str, Any]] = {}
    for match in _ABBR_PAREN.finditer(text):
        abbreviation = str(match.group("abbr") or "").strip()
        inferred = _infer_full_form(text, match.start(), abbreviation)
        if not inferred:
            continue
        full_form, full_start = inferred
        full_form = re.sub(r"\s+", " ", full_form).strip(" ,;:")
        if len(full_form.split()) < 1:
            continue
        key = (_normalize_inline(full_form).casefold(), abbreviation.casefold())
        item = raw.setdefault(
            key,
            {
                "full_form": full_form,
                "abbreviation": abbreviation,
                "first_definition_start": match.start(),
                "first_definition_end": match.end(),
            },
        )
        if match.start() < item["first_definition_start"]:
            item["first_definition_start"] = match.start()
            item["first_definition_end"] = match.end()

    entries: List[AbbreviationEntry] = []
    for item in raw.values():
        full_form = str(item["full_form"])
        abbreviation = str(item["abbreviation"])
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(full_form)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        mentions: list[tuple[int, int, bool]] = []
        for occurrence in pattern.finditer(text):
            after = text[occurrence.end():]
            has_parenthetical = bool(
                re.match(rf"\s*\({re.escape(abbreviation)}\)", after, re.IGNORECASE)
            )
            mentions.append((occurrence.start(), occurrence.end(), has_parenthetical))
        if len(mentions) < 2:
            continue
        entries.append(
            AbbreviationEntry(
                full_form=full_form,
                abbreviation=abbreviation,
                first_definition_start=int(item["first_definition_start"]),
                first_definition_end=int(item["first_definition_end"]),
                mentions=tuple(mentions),
            )
        )
    return sorted(
        entries,
        key=lambda entry: (entry.first_definition_start, entry.abbreviation.casefold()),
    )


def _chapter_for_offset(
    offset: int,
    chapters: Sequence[ChapterSpan],
) -> tuple[str, str] | None:
    for chapter in chapters:
        if chapter.heading_start <= offset < chapter.body_end:
            return chapter.section_id, chapter.title
    return None


def _abbreviation_inventory(
    entries: Sequence[AbbreviationEntry],
    chapters: Sequence[ChapterSpan],
) -> List[Dict[str, Any]]:
    inventory: List[Dict[str, Any]] = []
    for entry in entries:
        occurrence_rows = []
        for start, end, has_parenthetical in entry.mentions:
            location = _chapter_for_offset(start, chapters)
            occurrence_rows.append(
                {
                    "section_id": location[0] if location else "",
                    "chapter_title": location[1] if location else "",
                    "offset": start,
                    "has_parenthetical": has_parenthetical,
                }
            )
        inventory.append(
            {
                "full_form": entry.full_form,
                "abbreviation": entry.abbreviation,
                "first_definition_section_id": (
                    _chapter_for_offset(entry.first_definition_start, chapters) or ("", "")
                )[0],
                "full_form_mention_count": len(entry.mentions),
                "repeat_count_after_first_definition": max(0, len(entry.mentions) - 1),
                "occurrences": occurrence_rows,
            }
        )
    return inventory


def _all_paragraph_context(
    manuscript_text: str,
    chapters: Sequence[ChapterSpan],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for chapter in chapters:
        for paragraph in _paragraph_records(chapter.body_text):
            first = _first_sentence(paragraph.text)
            words = _WORD.findall(first)
            rows.append(
                {
                    "section_id": chapter.section_id,
                    "paragraph_index": paragraph.index + 1,
                    "opening": " ".join(words[:8]),
                    "first_sentence": first[:180],
                }
            )
    return rows


def _prefix_counter(sentences: Iterable[str], width: int = 6) -> Counter[str]:
    counter: Counter[str] = Counter()
    for sentence in sentences:
        tokens = [token.casefold() for token in _WORD.findall(sentence)]
        if tokens:
            counter[" ".join(tokens[:width])] += 1
    return counter


def _style_metrics(
    manuscript_text: str,
    chapters: Sequence[ChapterSpan] | None = None,
) -> Dict[str, Any]:
    selected = list(chapters or split_manuscript_chapters(manuscript_text))
    paragraphs: list[tuple[str, str, int]] = []
    for chapter in selected:
        for paragraph in _paragraph_records(chapter.body_text):
            paragraphs.append((chapter.section_id, paragraph.text, paragraph.index + 1))
    first_sentences = [_first_sentence(item[1]) for item in paragraphs]
    opener_counts = paragraph_opener_distribution([item[1] for item in paragraphs])
    opener_template_hits = sum(
        count for opener, count in opener_counts.items()
        if str(opener).casefold() in _STYLE_OPENERS
    )
    prefixes = _prefix_counter(first_sentences)
    scaffold_counts: Counter[str] = Counter()
    for sentence in first_sentences:
        lowered = sentence.casefold().lstrip()
        for prefix, label in _SENTENCE_SCAFFOLDS:
            if lowered.startswith(prefix):
                scaffold_counts[label] += 1
                break
    repeated_prefix_paragraphs = sum(
        count for count in prefixes.values() if count >= 2
    )
    first_sentence_template_hits = sum(
        1
        for sentence in first_sentences
        if (_WORD.findall(sentence) or [""])[0].casefold() in _STYLE_OPENERS
    )
    total = len(paragraphs)
    return {
        "paragraphs_total": total,
        "first_sentence_count": len(first_sentences),
        "opener_distribution": dict(opener_counts),
        "template_opener_share": round(opener_template_hits / total, 4) if total else 0.0,
        "first_sentence_template_share": round(
            first_sentence_template_hits / total, 4
        ) if total else 0.0,
        "first_sentence_scaffold_distribution": dict(scaffold_counts),
        "first_sentence_scaffold_share": round(
            sum(scaffold_counts.values()) / total, 4
        ) if total else 0.0,
        "first_sentence_scaffold_max_share": round(
            max(scaffold_counts.values()) / total, 4
        ) if scaffold_counts and total else 0.0,
        "paragraph_opener_max_share": round(
            max(opener_counts.values()) / total, 4
        ) if opener_counts and total else 0.0,
        "first_sentence_prefix_distribution": dict(prefixes),
        "repeated_first_sentence_prefix_paragraphs": repeated_prefix_paragraphs,
        "repeated_first_sentence_prefix_share": round(
            repeated_prefix_paragraphs / total, 4
        ) if total else 0.0,
        "first_sentence_prefix_max_share": round(
            max(prefixes.values()) / total, 4
        ) if prefixes and total else 0.0,
        "while_paragraphs": sum(
            1 for sentence in first_sentences if sentence.casefold().startswith("while ")
        ),
        "building_on_paragraphs": sum(
            1
            for sentence in first_sentences
            if sentence.casefold().startswith(("building on ", "building upon "))
        ),
    }


def _style_score(metrics: Mapping[str, Any]) -> float:
    """Lower is better; keep the score explainable and deterministic."""

    return round(
        0.25 * float(metrics.get("first_sentence_template_share", 0.0) or 0.0)
        + 0.25 * float(metrics.get("first_sentence_scaffold_share", 0.0) or 0.0)
        + 0.20 * float(metrics.get("paragraph_opener_max_share", 0.0) or 0.0)
        + 0.15 * float(metrics.get("first_sentence_prefix_max_share", 0.0) or 0.0)
        + 0.15 * float(metrics.get("repeated_first_sentence_prefix_share", 0.0) or 0.0),
        6,
    )


def _usage_cost(result: Mapping[str, Any], model_tier: str) -> tuple[float, int, int, str]:
    usage = dict(result.get("_llm_usage") or {})
    model = str(usage.get("model_name") or get_model_name(model_tier) or model_tier)
    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("estimated_input_tokens")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("estimated_output_tokens")
        or 0
    )
    return (
        estimate_call_cost_cny(model, input_tokens, output_tokens),
        input_tokens,
        output_tokens,
        model,
    )


def _call_model(
    agent_name: str,
    system: str,
    payload: Mapping[str, Any],
    *,
    model_tier: str,
    json_mode: bool,
    max_tokens: int,
    llm_call: Callable[..., Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
    if llm_call is not None:
        try:
            return llm_call(
                agent_name,
                system,
                payload,
                json_mode=json_mode,
                model_tier=model_tier,
            )
        except TypeError as exc:
            # Existing test doubles use the older four-argument convention.
            # Retry the adapter call only for that signature mismatch; model
            # failures from the actual client are not hidden here.
            if "model_tier" not in str(exc):
                raise
            return llm_call(agent_name, system, payload, json_mode=json_mode)
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]
    return call_qwen_chat(
        agent_name,
        messages,
        model_tier=model_tier,
        temperature=0.25 if json_mode else 0.2,
        max_tokens=max_tokens,
        response_format={"type": "json_object"} if json_mode else None,
        stream=False,
        timeout_seconds=240,
        allow_model_fallback=False,
        enable_thinking=False,
        force_mock=False,
        max_retries=1,
    )


def _claim_anchor_tokens(sentence: str) -> Counter[str]:
    return Counter(
        token.casefold()
        for token in _WORD.findall(sentence)
        if len(token) >= 5
        and token.casefold() not in _CLAIM_STOPWORDS
        and token.casefold() not in _STYLE_OPENERS
    )


def _anchor_retention(original: str, rewritten: str) -> float:
    before = _claim_anchor_tokens(original)
    after = _claim_anchor_tokens(rewritten)
    total = sum(before.values())
    if not total:
        return 1.0
    kept = sum(min(count, after.get(token, 0)) for token, count in before.items())
    return round(kept / total, 4)


def _negation_counts(text: str) -> Counter[str]:
    return Counter(match.group(0).casefold() for match in _NEGATION.finditer(text))


def _is_redundant_deletion(
    paragraph: str,
    target_start: int,
    target_end: int,
) -> bool:
    spans = _sentence_spans(paragraph)
    target = paragraph[target_start:target_end]
    target_tokens = set(token.casefold() for token in _WORD.findall(target) if len(token) >= 4)
    if not target_tokens:
        return False
    for start, end in spans:
        if start == target_start and end == target_end:
            continue
        other_tokens = set(
            token.casefold() for token in _WORD.findall(paragraph[start:end]) if len(token) >= 4
        )
        if not other_tokens:
            continue
        overlap = len(target_tokens & other_tokens) / max(1, len(target_tokens))
        if overlap >= 0.65:
            return True
    return False


def _remove_span(text: str, start: int, end: int) -> str:
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    if before and after:
        return before + " " + after
    return before + after


def _entry_for_abbreviation_edit(
    original_text: str,
    replacement_text: str,
    entries: Sequence[AbbreviationEntry],
) -> AbbreviationEntry | None:
    original = _normalize_inline(original_text)
    replacement = _normalize_inline(replacement_text)
    for entry in entries:
        if replacement.casefold() != entry.abbreviation.casefold():
            continue
        full = _normalize_inline(entry.full_form)
        if original.casefold() == full.casefold():
            return entry
        if original.casefold() == f"{full} ({entry.abbreviation})".casefold():
            return entry
    return None


def _build_expected_chapter(
    chapter: ChapterSpan,
    raw_edits: Sequence[Mapping[str, Any]],
    *,
    manuscript_text: str,
    abbreviation_entries: Sequence[AbbreviationEntry],
    protected_terms: Sequence[str],
) -> Dict[str, Any]:
    records = _paragraph_records(chapter.body_text)
    by_index = {record.index: record for record in records}
    accepted_edits: list[Dict[str, Any]] = []
    rejected_edits: list[Dict[str, Any]] = []
    spans_by_paragraph: dict[int, list[tuple[int, int, str, str, Dict[str, Any]]]] = {}
    alias_map: dict[str, str] = {}

    for raw in raw_edits:
        if not isinstance(raw, Mapping):
            continue
        try:
            index_one_based = int(raw.get("paragraph_index"))
        except (TypeError, ValueError):
            rejected_edits.append({"edit": dict(raw), "reason": "invalid_paragraph_index"})
            continue
        index = index_one_based - 1
        paragraph = by_index.get(index)
        action = str(raw.get("action") or raw.get("issue_type") or "").strip().casefold()
        if action in {"template_opener", "first_sentence", "replace_first_sentence"}:
            action = "replace_first_sentence"
        elif action in {"abbreviation_repeat", "abbreviation_consistency", "replace_abbreviation"}:
            action = "replace_abbreviation"
        elif action in {"delete", "delete_sentence"}:
            action = "delete_sentence"
        original_text = str(
            raw.get("original_text")
            or raw.get("original_sentence")
            or raw.get("original")
            or raw.get("target")
            or ""
        ).strip()
        replacement_text = str(
            raw.get("replacement_text")
            or raw.get("replacement")
            or raw.get("new_text")
            or ""
        ).strip()
        if paragraph is None:
            rejected_edits.append({"edit": dict(raw), "reason": "paragraph_not_found"})
            continue
        if not original_text:
            rejected_edits.append({"edit": dict(raw), "reason": "empty_original_text"})
            continue
        span: tuple[int, int] | None = None
        if action == "replace_first_sentence":
            first = _first_sentence(paragraph.text)
            if _normalize_inline(first) != _normalize_inline(original_text):
                rejected_edits.append({"edit": dict(raw), "reason": "not_exact_first_sentence"})
                continue
            span = _find_exact_span(paragraph.text, first)
            if not replacement_text:
                rejected_edits.append({"edit": dict(raw), "reason": "empty_replacement"})
                continue
            anchor_retention = _anchor_retention(first, replacement_text)
            if anchor_retention < 0.55:
                rejected_edits.append(
                    {
                        "edit": dict(raw),
                        "reason": "argument_anchor_loss",
                        "anchor_retention": anchor_retention,
                    }
                )
                continue
            if _negation_counts(first) != _negation_counts(replacement_text):
                rejected_edits.append({"edit": dict(raw), "reason": "argument_polarity_changed"})
                continue
        elif action == "delete_sentence":
            span = _find_exact_span(paragraph.text, original_text)
            if span is None or _normalize_inline(paragraph.text[span[0]:span[1]]) != _normalize_inline(original_text):
                rejected_edits.append({"edit": dict(raw), "reason": "sentence_not_found"})
                continue
            if len(_sentence_spans(paragraph.text)) <= 1:
                rejected_edits.append({"edit": dict(raw), "reason": "cannot_delete_only_sentence"})
                continue
            if _FACT_TOKEN.search(original_text) or _NEGATION.search(original_text):
                rejected_edits.append({"edit": dict(raw), "reason": "fact_bearing_sentence"})
                continue
            if not _is_redundant_deletion(paragraph.text, span[0], span[1]):
                rejected_edits.append({"edit": dict(raw), "reason": "deletion_not_redundant"})
                continue
            replacement_text = ""
        elif action == "replace_abbreviation":
            entry = _entry_for_abbreviation_edit(
                original_text,
                replacement_text,
                abbreviation_entries,
            )
            if entry is None:
                rejected_edits.append({"edit": dict(raw), "reason": "unrecognized_abbreviation_alias"})
                continue
            span = _find_exact_span(paragraph.text, original_text)
            if span is None:
                rejected_edits.append({"edit": dict(raw), "reason": "full_form_not_found"})
                continue
            absolute_start = chapter.body_start + paragraph.content_start + span[0]
            if absolute_start <= entry.first_definition_end:
                rejected_edits.append({"edit": dict(raw), "reason": "first_definition_is_protected"})
                continue
            # If the same full form appears more than once in one paragraph,
            # require an explicit occurrence number instead of guessing.
            occurrences = [
                match.start()
                for match in re.finditer(
                    re.escape(original_text), paragraph.text, flags=re.IGNORECASE
                )
            ]
            if len(occurrences) > 1:
                try:
                    occurrence = int(raw.get("occurrence")) - 1
                except (TypeError, ValueError):
                    rejected_edits.append({"edit": dict(raw), "reason": "ambiguous_full_form_occurrence"})
                    continue
                if occurrence < 0 or occurrence >= len(occurrences) or span[0] != occurrences[occurrence]:
                    rejected_edits.append({"edit": dict(raw), "reason": "wrong_full_form_occurrence"})
                    continue
            alias_map[entry.full_form] = entry.abbreviation
        else:
            rejected_edits.append({"edit": dict(raw), "reason": "unsupported_action"})
            continue
        if span is None:
            rejected_edits.append({"edit": dict(raw), "reason": "target_span_not_found"})
            continue
        bucket = spans_by_paragraph.setdefault(index, [])
        if any(start < span[1] and span[0] < end for start, end, *_ in bucket):
            rejected_edits.append({"edit": dict(raw), "reason": "overlapping_edits"})
            continue
        edit_record = {
            "paragraph_index": index_one_based,
            "action": action,
            "original_text": original_text,
            "replacement_text": replacement_text,
            "reason": str(raw.get("reason") or raw.get("suggestion") or ""),
        }
        bucket.append((span[0], span[1], replacement_text, action, edit_record))
        accepted_edits.append(edit_record)

    expected_blocks: dict[int, str] = {}
    block_verifications: list[Dict[str, Any]] = []
    for index, edits in spans_by_paragraph.items():
        paragraph = by_index[index]
        candidate = paragraph.text
        for start, end, replacement, action, _record in sorted(edits, key=lambda row: row[0], reverse=True):
            if action == "delete_sentence":
                candidate = _remove_span(candidate, start, end)
            else:
                candidate = candidate[:start] + replacement + candidate[end:]
        verdict = verify_rewrite(
            original=paragraph.text,
            rewritten=candidate,
            protected_terms=list(protected_terms),
            protected_term_aliases=alias_map,
        )
        if verdict["ok"]:
            expected_blocks[index] = candidate
            block_verifications.append(
                {
                    "paragraph_index": index + 1,
                    "ok": True,
                    "anchor_retention": _anchor_retention(
                        _first_sentence(paragraph.text),
                        _first_sentence(candidate),
                    ),
                    "verdict": verdict,
                }
            )
        else:
            # Any unsafe operation in a paragraph invalidates the whole set of
            # operations for that paragraph, not just the offending sentence.
            expected_blocks.pop(index, None)
            rejected_edits.extend(
                {
                    "edit": record,
                    "reason": "hard_verification_failed",
                    "violations": verdict["violations"],
                }
                for _start, _end, _replacement, _action, record in edits
            )

    segments = _PARA_SPLIT.split(chapter.body_text)
    for index, candidate in expected_blocks.items():
        record = by_index[index]
        raw_segment = segments[record.slot]
        leading = raw_segment[: len(raw_segment) - len(raw_segment.lstrip())]
        trailing = raw_segment[len(raw_segment.rstrip()):]
        segments[record.slot] = leading + candidate + trailing
    expected_body = "".join(segments)
    valid_indexes = set(expected_blocks)
    accepted_edits = [
        edit
        for edit in accepted_edits
        if int(edit.get("paragraph_index", 0)) - 1 in valid_indexes
    ]
    valid_alias_map: dict[str, str] = {}
    for edit in accepted_edits:
        if edit.get("action") != "replace_abbreviation":
            continue
        entry = _entry_for_abbreviation_edit(
            str(edit.get("original_text") or ""),
            str(edit.get("replacement_text") or ""),
            abbreviation_entries,
        )
        if entry is not None:
            valid_alias_map[entry.full_form] = entry.abbreviation
    return {
        "expected_body": expected_body,
        "expected_blocks": expected_blocks,
        "accepted_edits": accepted_edits,
        "rejected_edits": rejected_edits,
        "block_verifications": block_verifications,
        "alias_map": valid_alias_map,
    }


def _clean_author_body(value: str, title: str) -> str:
    text = str(value or "").strip()
    if text.startswith(_FENCE):
        text = text[len(_FENCE):].lstrip()
        end = text.rfind(_FENCE)
        if end >= 0:
            text = text[:end].strip()
    # Be tolerant if the author echoes the chapter heading, but never tolerate
    # a prose preamble or a different heading.
    lines = text.splitlines()
    if lines and re.match(r"^##\s+", lines[0]):
        if _title_key(re.sub(r"^##\s+", "", lines[0])) != _title_key(title):
            return ""
        text = "\n".join(lines[1:]).strip()
    return text


def _review_edits(parsed: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows = parsed.get("edits")
    if rows is None:
        rows = parsed.get("issues")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _author_revision_body(
    content: str,
    *,
    chapter_title: str,
    chapter_body: str,
    expected: Mapping[str, Any],
) -> tuple[str | None, str, list[int]]:
    """Validate the author's compact paragraph response.

    The preferred response is a JSON list containing only the paragraphs that
    have passed the local edit envelope.  A full-body plain-text response is
    retained as a compatibility path for older adapters and test doubles, but
    it is held to the same exact normalized comparison.
    """

    parsed = _safe_json(content)
    if "revised_paragraphs" in parsed:
        rows = parsed.get("revised_paragraphs")
        if not isinstance(rows, list):
            return None, "author_revision_envelope_malformed", []
        expected_blocks = {
            int(index) + 1: str(text)
            for index, text in dict(expected.get("expected_blocks") or {}).items()
        }
        seen: set[int] = set()
        extra_indexes: list[int] = []
        for row in rows:
            if not isinstance(row, Mapping):
                return None, "author_revision_envelope_malformed", []
            try:
                index = int(row.get("paragraph_index"))
            except (TypeError, ValueError):
                return None, "author_revision_envelope_invalid_paragraph_index", []
            if index in seen:
                return None, "author_revision_envelope_duplicate_paragraph", []
            if index not in expected_blocks:
                # The model sometimes echoes a rejected suggestion.  It is
                # never copied into the manuscript; record it for audit and
                # continue validating the requested paragraphs.
                extra_indexes.append(index)
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                return None, "author_revision_envelope_empty_paragraph", []
            if _normalize_for_compare(text) != _normalize_for_compare(expected_blocks[index]):
                return None, "author_paragraph_exceeded_edit_envelope", []
            seen.add(index)
        if seen != set(expected_blocks):
            return None, "author_revision_envelope_missing_paragraph", extra_indexes
        return str(expected.get("expected_body") or chapter_body), "", extra_indexes

    # Compatibility fallback: the old adapter returned a complete body.  It
    # remains fail-closed against any unlisted change.
    author_body = _clean_author_body(content, chapter_title)
    if not author_body:
        return None, "empty_or_invalid_author_body", []
    expected_body = str(expected.get("expected_body") or chapter_body)
    if _normalize_for_compare(author_body) != _normalize_for_compare(expected_body):
        return None, "author_output_exceeded_edit_envelope", []
    return expected_body, "", []


def _chapter_worker(
    chapter: ChapterSpan,
    *,
    manuscript_text: str,
    global_context: Mapping[str, Any],
    abbreviation_entries: Sequence[AbbreviationEntry],
    protected_terms: Sequence[str],
    reviewer_model_tier: str,
    reviser_model_tier: str,
    budget: _BudgetGate,
    llm_call: Callable[..., Mapping[str, Any]] | None,
) -> Dict[str, Any]:
    body = chapter.body_text.strip()
    records = _paragraph_records(chapter.body_text)
    chapter_payload = {
        "section_id": chapter.section_id,
        "chapter_title": chapter.title,
        "paragraph_count": len(records),
        "paragraphs": [
            {"paragraph_index": record.index + 1, "text": record.text}
            for record in records
        ],
        "chapter_text": body,
        "global_style_context": global_context,
        "abbreviation_inventory": _abbreviation_inventory(
            abbreviation_entries,
            split_manuscript_chapters(manuscript_text, include_non_body_sections=True),
        ),
    }
    result: Dict[str, Any] = {
        "section_id": chapter.section_id,
        "chapter_title": chapter.title,
        "status": "pending",
        "reviewer_attempted": False,
        "reviser_attempted": False,
        "reviewer_edits_found": 0,
        "accepted_edits": [],
        "rejected_edits": [],
        "author_rejected": False,
        "changed": False,
        "body_before_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_after_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "estimated_cost_cny": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reviewer_model": "",
        "reviser_model": "",
        "review_error": "",
        "revision_error": "",
        "author_extra_paragraphs_discarded": [],
        "review_text": body,
    }

    reviewer_input_tokens = max(1, len(json.dumps(chapter_payload, ensure_ascii=False)) // 4)
    reviewer_model = get_model_name(reviewer_model_tier)
    reviewer_reservation = budget.reserve(reviewer_model, reviewer_input_tokens, 6000)
    if reviewer_reservation is None:
        result["status"] = "budget_skipped_fail_open"
        result["review_error"] = "budget_reservation_failed_reviewer"
        return result
    result["reviewer_attempted"] = True
    try:
        reviewer_result = _call_model(
            "ChapterStyleReviewer",
            _REVIEWER_SYSTEM,
            chapter_payload,
            model_tier=reviewer_model_tier,
            json_mode=True,
            max_tokens=6000,
            llm_call=llm_call,
        )
        cost, input_tokens, output_tokens, actual_model = _usage_cost(
            reviewer_result,
            reviewer_model_tier,
        )
        budget.settle(reviewer_reservation, cost)
        result["estimated_cost_cny"] += cost
        result["input_tokens"] += input_tokens
        result["output_tokens"] += output_tokens
        result["reviewer_model"] = actual_model
        parsed = _safe_json(str(reviewer_result.get("content") or ""))
        raw_edits = _review_edits(parsed)
        result["reviewer_edits_found"] = len(raw_edits)
    except Exception as exc:
        budget.settle(reviewer_reservation, 0.0)
        result["review_error"] = f"{type(exc).__name__}:{exc}"
        result["status"] = "review_failed_fail_open"
        return result

    expected = _build_expected_chapter(
        chapter,
        raw_edits,
        manuscript_text=manuscript_text,
        abbreviation_entries=abbreviation_entries,
        protected_terms=protected_terms,
    )
    result["accepted_edits"] = expected["accepted_edits"]
    result["rejected_edits"] = expected["rejected_edits"]

    reviser_payload = {
        "section_id": chapter.section_id,
        "chapter_title": chapter.title,
        "original_chapter_text": body,
        # Only pass locally validated edits to the author.  A reviewer may
        # produce a useful-looking suggestion that the fact/argument guards
        # reject; exposing that suggestion to the author would make the
        # author contract internally inconsistent and commonly causes a
        # perfectly safe revision to be rejected as out-of-envelope.
        "reviewer_memo": {"edits": expected["accepted_edits"]},
        "validated_edit_envelope": expected["accepted_edits"],
        "instructions": [
            "Return only the complete revised chapter body.",
            "If the memo is empty, return the original body exactly.",
            "Apply only the validated edit envelope; do not invent edits.",
        ],
    }
    reviser_input_tokens = max(1, len(json.dumps(reviser_payload, ensure_ascii=False)) // 4)
    reviser_model = get_model_name(reviser_model_tier)
    reviser_reservation = budget.reserve(reviser_model, reviser_input_tokens, 12000)
    if reviser_reservation is None:
        result["status"] = "budget_skipped_fail_open"
        result["revision_error"] = "budget_reservation_failed_reviser"
        return result
    result["reviser_attempted"] = True
    try:
        reviser_result = _call_model(
            "ChapterStyleReviser",
            _REVISER_SYSTEM,
            reviser_payload,
            model_tier=reviser_model_tier,
            json_mode=True,
            max_tokens=12000,
            llm_call=llm_call,
        )
        cost, input_tokens, output_tokens, actual_model = _usage_cost(
            reviser_result,
            reviser_model_tier,
        )
        budget.settle(reviser_reservation, cost)
        result["estimated_cost_cny"] += cost
        result["input_tokens"] += input_tokens
        result["output_tokens"] += output_tokens
        result["reviser_model"] = actual_model
        author_body, author_error, extra_indexes = _author_revision_body(
            str(reviser_result.get("content") or ""),
            chapter_title=chapter.title,
            chapter_body=body,
            expected=expected,
        )
        result["author_extra_paragraphs_discarded"] = extra_indexes
        if not author_body:
            result["author_rejected"] = True
            result["revision_error"] = author_error or "author_revision_rejected"
            result["status"] = "author_rejected_fail_open"
            return result
        # Re-run the chapter-wide hard verifier after the author response.  The
        # exact envelope check above already protects sentence scope; this
        # second pass protects against an accidental model-wide omission.
        final_verdict = verify_rewrite(
            original=body,
            rewritten=author_body,
            protected_terms=list(protected_terms),
            protected_term_aliases=expected["alias_map"],
        )
        if not final_verdict["ok"]:
            result["author_rejected"] = True
            result["revision_error"] = "chapter_hard_verification_failed"
            result["rejected_edits"].append(
                {"reason": "chapter_hard_verification_failed", "violations": final_verdict["violations"]}
            )
            result["status"] = "author_rejected_fail_open"
            return result
        # Keep the canonical local reconstruction rather than the model's
        # whitespace/layout serialization.  The model still has to return a
        # matching full body, but accepted output cannot introduce harmless
        # paragraph-layout drift into the manuscript.
        result["review_text"] = expected["expected_body"]
        result["changed"] = _normalize_for_compare(expected["expected_body"]) != _normalize_for_compare(body)
        result["body_after_sha256"] = hashlib.sha256(
            expected["expected_body"].encode("utf-8")
        ).hexdigest()
        result["status"] = "accepted" if result["changed"] else "unchanged"
        return result
    except Exception as exc:
        budget.settle(reviser_reservation, 0.0)
        result["revision_error"] = f"{type(exc).__name__}:{exc}"
        result["status"] = "revision_failed_fail_open"
        return result


def run_chapter_style_governance(
    manuscript_text: str,
    *,
    enabled: bool = True,
    chapter_titles: Optional[Sequence[str]] = None,
    chapter_ids: Optional[Mapping[str, str]] = None,
    include_non_body_sections: bool = False,
    reviewer_model_tier: str = "c_model",
    reviser_model_tier: str = "c2_model",
    workers: int = 6,
    cost_budget_cny: float = 0.75,
    protected_terms: Optional[Sequence[str]] = None,
    llm_call: Callable[..., Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Run chapter-parallel reviewer/author governance and fail open safely."""

    source = str(manuscript_text or "")
    chapters = split_manuscript_chapters(
        source,
        chapter_titles=chapter_titles,
        chapter_ids=chapter_ids,
        include_non_body_sections=include_non_body_sections,
    )
    entries = _collect_abbreviation_entries(source, chapters)
    metrics_before = _style_metrics(source, chapters)
    report: Dict[str, Any] = {
        "schema_version": "optomind.chapter_style_governance.v1",
        "enabled": bool(enabled),
        "reviewer_model_tier": reviewer_model_tier,
        "reviser_model_tier": reviser_model_tier,
        "configured_workers": max(1, int(workers)),
        "cost_budget_cny": max(0.0, float(cost_budget_cny)),
        "chapter_count": len(chapters),
        "chapters_attempted": 0,
        "chapters_accepted": 0,
        "chapters_changed": 0,
        "reviewer_calls": 0,
        "reviser_calls": 0,
        "estimated_cost_cny": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "budget_exhausted": False,
        "changed": False,
        "metrics_before": metrics_before,
        "metrics_after": metrics_before,
        "style_score_before": _style_score(metrics_before),
        "style_score_after": _style_score(metrics_before),
        "abbreviation_inventory": _abbreviation_inventory(entries, chapters),
        "chapters": [],
        "global_guard": {"ok": True, "violations": []},
        "improved": False,
        "promotion_eligible": False,
        "promotion_reason": "",
        "review_text": source,
    }
    if not enabled:
        report["promotion_reason"] = "disabled"
        return report
    if not chapters:
        report["promotion_reason"] = "no_selected_chapters"
        return report

    all_paragraphs = _all_paragraph_context(source, chapters)
    global_context = {
        "article_selected_chapter_count": len(chapters),
        "opener_distribution": dict(
            paragraph_opener_distribution([row["first_sentence"] for row in all_paragraphs])
        ),
        "opening_inventory": all_paragraphs,
        "policy": (
            "Use the chapter text for edits.  The inventory is only a compact "
            "cross-chapter style signal, not permission to edit another chapter."
        ),
    }
    budget = _BudgetGate(cost_budget_cny)
    worker_count = max(1, min(int(workers), len(chapters)))
    results: dict[int, Dict[str, Any]] = {}
    if worker_count == 1:
        for index, chapter in enumerate(chapters):
            results[index] = _chapter_worker(
                chapter,
                manuscript_text=source,
                global_context=global_context,
                abbreviation_entries=entries,
                protected_terms=list(protected_terms or []),
                reviewer_model_tier=reviewer_model_tier,
                reviser_model_tier=reviser_model_tier,
                budget=budget,
                llm_call=llm_call,
            )
    else:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="chapter-style",
        ) as pool:
            futures = {
                pool.submit(
                    _chapter_worker,
                    chapter,
                    manuscript_text=source,
                    global_context=global_context,
                    abbreviation_entries=entries,
                    protected_terms=list(protected_terms or []),
                    reviewer_model_tier=reviewer_model_tier,
                    reviser_model_tier=reviser_model_tier,
                    budget=budget,
                    llm_call=llm_call,
                ): index
                for index, chapter in enumerate(chapters)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    # The worker itself is fail-open; this outer boundary is a
                    # final containment guard for unexpected implementation
                    # errors in one chapter.
                    chapter = chapters[index]
                    results[index] = {
                        "section_id": chapter.section_id,
                        "chapter_title": chapter.title,
                        "status": "worker_failed_fail_open",
                        "reviewer_attempted": False,
                        "reviser_attempted": False,
                        "reviewer_edits_found": 0,
                        "accepted_edits": [],
                        "rejected_edits": [],
                        "author_rejected": False,
                        "changed": False,
                        "body_before_sha256": hashlib.sha256(chapter.body_text.encode("utf-8")).hexdigest(),
                        "body_after_sha256": hashlib.sha256(chapter.body_text.encode("utf-8")).hexdigest(),
                        "estimated_cost_cny": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reviewer_model": "",
                        "reviser_model": "",
                        "review_error": f"{type(exc).__name__}:{exc}",
                        "revision_error": "",
                        "review_text": chapter.body_text.strip(),
                    }

    ordered = [results[index] for index in range(len(chapters))]
    report["chapters"] = [
        {
            key: value
            for key, value in row.items()
            if key != "review_text"
        }
        for row in ordered
    ]
    report["chapters_attempted"] = sum(
        1 for row in ordered if row.get("reviewer_attempted") or row.get("reviser_attempted")
    )
    report["chapters_accepted"] = sum(1 for row in ordered if row.get("status") == "accepted")
    report["chapters_changed"] = sum(1 for row in ordered if row.get("changed"))
    report["reviewer_calls"] = sum(1 for row in ordered if row.get("reviewer_attempted"))
    report["reviser_calls"] = sum(1 for row in ordered if row.get("reviser_attempted"))
    report["estimated_cost_cny"] = round(
        sum(float(row.get("estimated_cost_cny", 0.0) or 0.0) for row in ordered),
        6,
    )
    report["input_tokens"] = sum(int(row.get("input_tokens", 0) or 0) for row in ordered)
    report["output_tokens"] = sum(int(row.get("output_tokens", 0) or 0) for row in ordered)
    report["budget_exhausted"] = budget.exhausted or any(
        "budget_" in str(row.get("review_error") or row.get("revision_error") or "")
        for row in ordered
    )

    # Apply chapter outputs from right to left, preserving all unselected
    # front/back matter and the original heading spelling.
    candidate = source
    changed_spans: list[tuple[int, int, str]] = []
    for chapter, row in zip(chapters, ordered):
        if row.get("status") != "accepted" or not row.get("changed"):
            continue
        body_out = str(row.get("review_text") or "").strip()
        raw = chapter.body_text
        leading = raw[: len(raw) - len(raw.lstrip())]
        trailing = raw[len(raw.rstrip()):]
        changed_spans.append((chapter.body_start, chapter.body_end, leading + body_out + trailing))
    for start, end, replacement in sorted(changed_spans, reverse=True):
        candidate = candidate[:start] + replacement + candidate[end:]

    aliases = {
        entry.full_form: entry.abbreviation
        for entry in entries
        if any(
            edit.get("action") == "replace_abbreviation"
            and _normalize_inline(edit.get("replacement_text")) == _normalize_inline(entry.abbreviation)
            for row in ordered
            for edit in row.get("accepted_edits", [])
        )
    }
    global_verdict = verify_rewrite(
        original=source,
        rewritten=candidate,
        protected_terms=list(protected_terms or []),
        protected_term_aliases=aliases,
    )
    report["global_guard"] = {
        "ok": bool(global_verdict["ok"]),
        "violations": global_verdict["violations"],
        "alias_map": aliases,
    }
    if not global_verdict["ok"]:
        report["review_text"] = source
        report["promotion_reason"] = "global_hard_guard_rejected"
        report["metrics_after"] = metrics_before
        report["style_score_after"] = report["style_score_before"]
        return report

    after_chapters = split_manuscript_chapters(
        candidate,
        chapter_titles=chapter_titles,
        chapter_ids=chapter_ids,
        include_non_body_sections=include_non_body_sections,
    )
    metrics_after = _style_metrics(candidate, after_chapters)
    report["review_text"] = candidate
    report["changed"] = candidate != source
    report["metrics_after"] = metrics_after
    report["style_score_after"] = _style_score(metrics_after)
    report["improved"] = bool(
        report["changed"]
        and report["chapters_changed"] > 0
        and report["style_score_after"] < report["style_score_before"] - 0.005
    )
    if report["improved"]:
        report["promotion_eligible"] = True
        report["promotion_reason"] = "global_style_score_improved_with_hard_guard_pass"
    elif report["changed"]:
        report["promotion_reason"] = "changed_without_measurable_global_style_improvement"
    else:
        report["promotion_reason"] = "no_accepted_chapter_change"
    return report


__all__ = [
    "ChapterSpan",
    "run_chapter_style_governance",
    "split_manuscript_chapters",
]
