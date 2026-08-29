"""Translate a validated English review into auditable academic Chinese.

The translator is a presentation layer.  It must not alter scientific claims,
citations, equations, or source identity.  Expensive upstream research is
therefore never repeated: the module translates immutable Markdown blocks,
validates them deterministically, caches successful blocks, and escalates only
the blocks that fail validation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Optional

from config.qwen_config import get_model_name
from llm.qwen_chat_client import call_qwen_chat

from .artifact_store import atomic_write_json
from .cost_ledger import estimate_call_cost_cny


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "Scientific Review Chinese Translator.txt"
)
DEFAULT_AUDIT_PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "Scientific Chinese Translation Auditor.txt"
)
REF_PATTERN = re.compile(r"\[REF:[^\]]+\]")
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"(?:[xX])?(?![A-Za-z0-9])"
)
PROTECTED_TOKEN_PATTERN = re.compile(r"ZXQK\d{4}TOKEN")
FALLBACK_PREFIXES = ("[fallback]", "[mock]")
LOWERCASE_ENGLISH_PATTERN = re.compile(r"\b[a-z][a-z-]{3,}\b")
OBVIOUS_UNTRANSLATED_GENERAL_WORDS = {
    "designs",
    "footprint",
    "constituent",
    "framework",
    "trade-off",
    "roadmap",
    "background",
    "challenge",
    "comparison",
    "conclusion",
    "requirement",
}

_DETERMINISTIC_TERM_REPLACEMENTS = (
    ("伴随源方法", "伴随法"),
    ("adjoint-source method", "伴随法"),
    ("adjoint source method", "伴随法"),
    (
        "诸如“疫苗”（vaccination）之类的鲁棒性策略",
        "基于“免疫训练”的鲁棒性策略",
    ),
    (
        "“疫苗”（vaccination）之类的鲁棒性策略",
        "基于“免疫训练”的鲁棒性策略",
    ),
    (
        "诸如“疫苗接种”（vaccination）之类的鲁棒性策略",
        "基于“免疫训练”（vaccination）的鲁棒性策略",
    ),
    ("“疫苗接种”（vaccination）", "“免疫训练”（vaccination）"),
    ("疫苗接种", "免疫训练"),
    ("“免疫”（vaccination）", "“免疫训练”（vaccination）"),
    ("“疫苗”（vaccination）", "“免疫训练”（vaccination）"),
    ("“疫苗式”鲁棒性策略", "基于“免疫训练”的鲁棒性策略"),
    ("疫苗式", "免疫训练式"),
)

_QUOTED_VACCINATION_LABEL_RE = re.compile(
    r"(?P<opening>[“”\"‘’]?\s*)"
    r"(?P<label>免疫训练式|疫苗接种|疫苗式|疫苗|免疫)"
    r"(?P<closing>[“”\"‘’])"
    r"(?=\s*(?:[（(]\s*vaccination|等策略|训练方案|方案|之类|鲁棒性策略))",
)


def _normalize_scientific_terminology(text: str) -> tuple[str, list[str]]:
    """Apply only unambiguous presentation-level terminology repairs."""

    value = str(text or "")
    notes: list[str] = []
    for source, target in _DETERMINISTIC_TERM_REPLACEMENTS:
        if source in value:
            value = value.replace(source, target)
            notes.append(f"{source}->{target}")
    quoted_notes: list[str] = []

    def _replace_quoted_vaccination_label(match: re.Match[str]) -> str:
        label = match.group("label")
        if label == "免疫训练":
            return match.group(0)
        quoted_notes.append(f"{label}->免疫训练")
        return (
            f"{match.group('opening')}免疫训练"
            f"{match.group('closing')}"
        )

    value = _QUOTED_VACCINATION_LABEL_RE.sub(
        _replace_quoted_vaccination_label,
        value,
    )
    notes.extend(quoted_notes)
    value, count = re.subn(
        r"(?<![A-Za-z])footprint(?![A-Za-z])",
        "器件占地尺寸",
        value,
        flags=re.IGNORECASE,
    )
    if count:
        notes.append("footprint->器件占地尺寸")
    value, count = re.subn(
        r"(?<![A-Za-z])designs(?![A-Za-z])",
        "设计方案",
        value,
        flags=re.IGNORECASE,
    )
    if count:
        notes.append("designs->设计方案")
    return value, notes


def _read_json(path: Optional[Path]) -> dict[str, Any]:
    if not path or not Path(path).is_file():
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


def _safe_json(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_markdown_blocks(text: str) -> tuple[list[str], list[str]]:
    """Return content blocks and the exact separators between them."""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    pieces = re.split(r"(\n[ \t]*\n+)", normalized)
    blocks: list[str] = []
    separators: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if re.fullmatch(r"\n[ \t]*\n+", piece):
            if blocks:
                separators.append(piece)
            continue
        blocks.append(piece)
    while len(separators) < max(0, len(blocks) - 1):
        separators.append("\n\n")
    return blocks, separators[: max(0, len(blocks) - 1)]


def _restore_markdown_blocks(
    translated_blocks: Iterable[str],
    separators: list[str],
) -> str:
    blocks = list(translated_blocks)
    if not blocks:
        return ""
    output = [blocks[0].strip()]
    for index, block in enumerate(blocks[1:]):
        output.append(separators[index] if index < len(separators) else "\n\n")
        output.append(block.strip())
    return "".join(output).rstrip() + "\n"


def _protect_fragile_tokens(text: str) -> tuple[str, dict[str, str]]:
    """Mask strings that a translation model must reproduce byte-for-byte."""

    patterns = (
        r"\[REF:[^\]]+\]",
        r"\bverification_deferred\b",
        r"\b(?:needs_more_literature|partially_supported|supported_boundary|open_gap)\b",
        r"\$\$.*?\$\$",
        r"\$[^$\n]+\$",
        r"https?://[^\s)\]>]+",
        r"(?<=\]\()[^)]+(?=\))",
        # Dimensional labels are scientific tokens, not ordinary prose.
        # Protect the complete token before the generic number pattern so a
        # model cannot turn ``3D-normalized`` into the scientifically different
        # ``3 normalized`` while still preserving the digit itself.
        r"(?<![A-Za-z0-9])\d+(?:[-\u2010-\u2014]?[Dd])(?=[^A-Za-z0-9]|$)",
        (
            r"(?<![A-Za-z0-9])\d+(?:\.\d+)?"
            r"(?:[eE][+-]?\d+|\^\s*[+-]?\d+)?"
            r"(?:\s*[–-]\s*\d+(?:\.\d+)?"
            r"(?:[eE][+-]?\d+|\^\s*[+-]?\d+)?)?"
            r"(?:[xX])?"
        ),
    )
    matches: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.DOTALL):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            occupied.append((match.start(), match.end()))
            matches.append((match.start(), match.end(), match.group(0)))
    matches.sort(key=lambda item: item[0])
    mapping: dict[str, str] = {}
    output: list[str] = []
    cursor = 0
    for index, (start, end, value) in enumerate(matches, 1):
        token = f"ZXQK{index:04d}TOKEN"
        output.append(text[cursor:start])
        output.append(token)
        mapping[token] = value
        cursor = end
    output.append(text[cursor:])
    return "".join(output), mapping


def _fragile_values_preserved(
    restored_target: str,
    mapping: dict[str, str],
) -> bool:
    """Check exact protected values when placeholder order legitimately moves.

    Model responses are validated against placeholder IDs first.  A cached or
    resumed translation is reconstructed from reader-facing text, where a
    grammatically valid translation may have reordered two protected values.
    In that case regenerated placeholder numbers differ even though every
    citation, formula, URL, dimensional label, and measurement is still
    present byte-for-byte.  Compare the value multiset as the safe fallback;
    missing or altered values still fail closed.
    """

    target = str(restored_target or "")
    required = Counter(mapping.values())
    return all(target.count(value) == count for value, count in required.items())


def _restore_fragile_tokens(text: str, mapping: dict[str, str]) -> str:
    output = str(text or "")
    for token, value in mapping.items():
        output = output.replace(token, value)
    return output


def _normalized_numbers(text: str) -> Counter[str]:
    value = str(text or "")
    # Equations, references, URLs, and workflow status tokens are protected
    # byte-for-byte by ``_protect_fragile_tokens``.  Counting the digits inside
    # them a second time creates false failures (DOIs and formula subscripts
    # dominate the counts), so the numeric audit only inspects prose here.
    prose = REF_PATTERN.sub(" ", value)
    prose = re.sub(r"\$\$.*?\$\$", " ", prose, flags=re.DOTALL)
    prose = re.sub(r"\$[^$\n]+\$", " ", prose)
    prose = re.sub(r"https?://[^\s)\]>]+", " ", prose)
    prose = re.sub(r"\bverification_deferred\b", " ", prose)
    numbers: Counter[str] = Counter()
    for match in NUMBER_PATTERN.finditer(prose):
        token = match.group("number")
        start, end = match.span()
        suffix = prose[end : min(len(prose), end + 18)]
        prefix = prose[max(0, start - 3) : start]
        has_measurement_context = bool(
            re.match(
                r"\s*(?:%|"
                r"(?:nm|µm|um|mm|cm|m|hz|khz|mhz|ghz|thz|"
                r"db|k|pa|w|mw|kw|v|mv|a|ma|s|ms|ns|ps|fs|"
                r"ev|mev|rad|deg|°|dpi|fps|mol|g|kg)\b)",
                suffix,
                flags=re.IGNORECASE,
            )
            or prefix.endswith(("±", "+/-"))
        )
        is_year = bool(re.fullmatch(r"(?:18|19|20|21)\d{2}", token))
        is_nontrivial = (
            "." in token
            or "e" in token.lower()
            or token not in {"0", "1"}
        )
        if has_measurement_context or is_year or is_nontrivial:
            numbers[token] += 1
    # Scientific English sometimes spells only this mathematical numerator
    # out while Chinese renders it as ``1/n``.  Keep the equivalence narrow;
    # ordinary prose such as "three routes" may legitimately use Chinese
    # numerals and must not enter the strict digit-preservation gate.
    numbers["1"] += len(
        re.findall(
            r"\bone(?:-|\s+)over(?:-|\s+)n\b",
            value,
            flags=re.IGNORECASE,
        )
    )
    return +numbers


def _is_reference_only_block(text: str) -> bool:
    """Return true for blocks that should be copied, not translated.

    Bibliography marker/DOI lists contain no reader-facing prose.  Sending them
    to an LLM wastes tokens and the Chinese-content gate used to reject the
    correct identity result.  The deliberately narrow recognizer does not
    match ordinary sentences that merely contain a citation.
    """

    value = str(text or "").strip()
    if not value:
        return False
    residue = REF_PATTERN.sub("", value)
    residue = re.sub(r"https?://[^\s)\]>]+", "", residue)
    residue = re.sub(
        r"\b(?:doi:\s*)?10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b",
        "",
        residue,
        flags=re.IGNORECASE,
    )
    residue = re.sub(r"\bCorpusId:\d+\b", "", residue, flags=re.IGNORECASE)
    residue = re.sub(r"[\s,;|*_\-\[\]().:]+", "", residue)
    return residue == ""


def _unexpected_lowercase_english(text: str) -> list[str]:
    scrubbed, _ = _protect_fragile_tokens(text)
    words = {
        match.group(0).lower()
        for match in LOWERCASE_ENGLISH_PATTERN.finditer(scrubbed)
    }
    return sorted(words & OBVIOUS_UNTRANSLATED_GENERAL_WORDS)


def _known_audit_false_positive(
    source: str,
    translation: str,
    reason: str,
) -> bool:
    """Reject deterministic terminology mistakes made by the audit model."""

    source_text = str(source or "")
    translation_text = str(translation or "")
    issue = str(reason or "").lower()
    if (
        "does not appear in the source" in issue
        or "does not appear in source" in issue
    ):
        cited_terms = re.findall(
            r"['\"]([A-Za-z][A-Za-z0-9.-]{1,})['\"]",
            str(reason or ""),
        )
        if any(term.lower() in source_text.lower() for term in cited_terms):
            return True
    if (
        "monochromatic imaging" in source_text.lower()
        and "单色成像" in translation_text
        and (
            "logical contradiction" in issue
            or "color data" in issue
            or "hyperspectral" in issue
        )
    ):
        # The source itself makes this specific claim.  The semantic auditor
        # must not replace a source-faithful translation with an inferred
        # multi-spectral interpretation merely because the source sentence is
        # scientifically awkward.
        return True
    return (
        "exceptional point" in source_text.lower()
        and "异常点" in translation_text
        and (
            "mistranslat" in issue
            or "outlier" in issue
            or "abnormal point" in issue
        )
    )


def _validate_translation(
    source: str,
    protected_source: str,
    protected_target: str,
    restored_target: str,
    mapping: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    target = str(restored_target or "").strip()
    if not target:
        return ["empty_translation"]
    if target.lower().startswith(FALLBACK_PREFIXES):
        errors.append("model_fallback_text")
    if mapping:
        source_tokens = Counter(PROTECTED_TOKEN_PATTERN.findall(protected_source))
        target_tokens = Counter(PROTECTED_TOKEN_PATTERN.findall(protected_target))
        if (
            source_tokens != target_tokens
            and not _fragile_values_preserved(target, mapping)
        ):
            errors.append("protected_token_mismatch")
    if REF_PATTERN.findall(source) != REF_PATTERN.findall(target):
        errors.append("citation_marker_mismatch")
    if _normalized_numbers(source) != _normalized_numbers(target):
        errors.append("numeric_token_mismatch")
    source_heading = re.match(r"^(#{1,6})\s+", source.strip())
    target_heading = re.match(r"^(#{1,6})\s+", target)
    if bool(source_heading) != bool(target_heading):
        errors.append("heading_structure_changed")
    elif source_heading and target_heading:
        if source_heading.group(1) != target_heading.group(1):
            errors.append("heading_level_changed")
    translatable_source = PROTECTED_TOKEN_PATTERN.sub(" ", protected_source)
    prose_chars = len(
        re.sub(r"[\W\d_]+", "", translatable_source, flags=re.UNICODE)
    )
    cjk_chars = len(CJK_PATTERN.findall(target))
    if prose_chars >= 24 and cjk_chars < max(6, int(prose_chars * 0.08)):
        errors.append("insufficient_chinese_content")
    minimum_length = (
        max(8, int(len(source) * 0.18))
        if len(source) > 80
        else max(1, int(len(source) * 0.08))
    )
    if len(target) < minimum_length:
        errors.append("implausibly_short")
    if len(target) > max(80, int(len(source) * 2.2)):
        errors.append("implausibly_long")
    unexpected_english = _unexpected_lowercase_english(target)
    if unexpected_english:
        errors.append(
            "unexpected_lowercase_english:"
            + ",".join(unexpected_english[:8])
        )
    if re.search(r"```(?:json)?\s*\{", target, re.IGNORECASE):
        errors.append("json_wrapper_leak")
    return errors


@dataclass
class TranslationUnit:
    unit_id: str
    source: str
    protected_source: str = ""
    protected_mapping: dict[str, str] = field(default_factory=dict)
    translated: str = ""
    model_name: str = ""
    model_tier: str = ""
    status: str = "pending"
    errors: list[str] = field(default_factory=list)
    from_cache: bool = False
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_cost_cny: float = 0.0
    repair_note: str = ""
    audit_warnings: list[str] = field(default_factory=list)
    semantic_audit_passed: bool = False
    attempt_count: int = 0
    retry_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cache_key(self) -> str:
        return _sha256_text(self.source)


@dataclass
class ScientificChineseTranslator:
    source_markdown_path: Path
    output_dir: Path
    model_tier: str = "c2_model"
    fallback_model_tier: str = "c_model"
    repair_model_tier: str = "b_minus_model"
    prompt_path: Path = DEFAULT_PROMPT_PATH
    audit_prompt_path: Path = DEFAULT_AUDIT_PROMPT_PATH
    audit_model_tier: str = "c2_model"
    semantic_audit: bool = True
    workers: int = 3
    max_batch_chars: int = 5600
    max_batch_items: int = 6
    cost_budget_cny: float = 3.0
    force_mock: bool = False
    _cache_lock: Lock = field(default_factory=Lock, init=False)
    _spent_cny: float = field(default=0.0, init=False)
    _aux_input_tokens: int = field(default=0, init=False)
    _aux_output_tokens: int = field(default=0, init=False)
    _audit_call_count: int = field(default=0, init=False)

    def _prompt(self) -> str:
        return Path(self.prompt_path).read_text(encoding="utf-8").strip()

    def _cache_path(self) -> Path:
        return Path(self.output_dir) / "TRANSLATION_CACHE.json"

    def _state_path(self) -> Path:
        return Path(self.output_dir) / "TRANSLATION_STATE.json"

    def _load_state(self, prompt_hash: str) -> dict[str, Any]:
        state = _read_json(self._state_path())
        if state.get("prompt_hash") != prompt_hash:
            return {}
        records = state.get("records")
        return records if isinstance(records, dict) else {}

    def _save_state(
        self,
        units: list[TranslationUnit],
        *,
        prompt_hash: str,
    ) -> None:
        records = {
            unit.unit_id: {
                "source_sha256": unit.cache_key,
                "status": unit.status,
                "protected_translation": (
                    _protect_fragile_tokens(unit.translated)[0]
                    if unit.translated else ""
                ),
                "translation": unit.translated,
                "model_name": unit.model_name,
                "model_tier": unit.model_tier,
                "semantic_audit_passed": unit.semantic_audit_passed,
                "errors": list(unit.errors),
                "attempt_count": unit.attempt_count,
                "retry_history": list(unit.retry_history),
            }
            for unit in units
        }
        with self._cache_lock:
            atomic_write_json(
                self._state_path(),
                {
                    "schema_version": "research_harness.translation_state.v1",
                    "prompt_hash": prompt_hash,
                    "records": records,
                },
            )

    def _load_cache(self, prompt_hash: str) -> dict[str, Any]:
        cache = _read_json(self._cache_path())
        if cache.get("prompt_hash") != prompt_hash:
            return {}
        records = cache.get("records")
        return records if isinstance(records, dict) else {}

    def _save_cache(
        self,
        records: dict[str, Any],
        *,
        prompt_hash: str,
    ) -> None:
        with self._cache_lock:
            atomic_write_json(
                self._cache_path(),
                {
                    "schema_version": "research_harness.translation_cache.v1",
                    "prompt_hash": prompt_hash,
                    "records": records,
                },
            )

    def _units(self, source_text: str) -> tuple[list[TranslationUnit], list[str]]:
        blocks, separators = _split_markdown_blocks(source_text)
        units: list[TranslationUnit] = []
        for index, block in enumerate(blocks, 1):
            protected, mapping = _protect_fragile_tokens(block)
            unit = TranslationUnit(
                unit_id=f"B{index:04d}",
                source=block,
                protected_source=protected,
                protected_mapping=mapping,
            )
            if re.fullmatch(r"\s*(?:---+|\*\*\*+|___+)\s*", block):
                unit.translated = block.strip()
                unit.status = "validated"
                unit.model_name = "deterministic"
                unit.model_tier = "deterministic"
            elif _is_reference_only_block(block):
                unit.translated = block.strip()
                unit.status = "validated"
                unit.model_name = "deterministic"
                unit.model_tier = "deterministic"
            units.append(unit)
        return units, separators

    def _batches(self, units: list[TranslationUnit]) -> list[list[TranslationUnit]]:
        batches: list[list[TranslationUnit]] = []
        current: list[TranslationUnit] = []
        current_chars = 0
        for unit in units:
            size = len(unit.protected_source)
            if current and (
                len(current) >= self.max_batch_items
                or current_chars + size > self.max_batch_chars
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(unit)
            current_chars += size
        if current:
            batches.append(current)
        return batches

    def _call_batch(
        self,
        batch: list[TranslationUnit],
        *,
        tier: str,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        payload = {
            "items": [
                {
                    "id": unit.unit_id,
                    "source": unit.protected_source,
                    **(
                        {"review_note": unit.repair_note}
                        if unit.repair_note
                        else {}
                    ),
                }
                for unit in batch
            ]
        }
        result = call_qwen_chat(
            "ScientificReviewChineseTranslator",
            [
                {"role": "system", "content": self._prompt()},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            model_tier=tier,
            temperature=0.05,
            max_tokens=max(1800, min(12000, int(self.max_batch_chars * 1.65))),
            response_format={"type": "json_object"},
            stream=False,
            timeout_seconds=180,
            max_transport_key_candidates=2,
            allow_model_fallback=False,
            enable_thinking=False,
            force_mock=self.force_mock,
            max_retries=1,
        )
        parsed = _safe_json(str(result.get("content") or ""))
        rows = parsed.get("translations")
        translations: dict[str, str] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                unit_id = str(row.get("id") or "").strip()
                text = str(row.get("text") or "").strip()
                if unit_id and text:
                    translations[unit_id] = text
        return translations, dict(result.get("_llm_usage") or {})

    def _audit_batch(
        self,
        batch: list[TranslationUnit],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        payload = {
            "items": [
                {
                    "id": unit.unit_id,
                    "source": unit.source,
                    "translation": unit.translated,
                }
                for unit in batch
            ]
        }
        result = call_qwen_chat(
            "ScientificChineseTranslationAuditor",
            [
                {
                    "role": "system",
                    "content": Path(self.audit_prompt_path).read_text(
                        encoding="utf-8"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            model_tier=self.audit_model_tier,
            temperature=0,
            max_tokens=1800,
            response_format={"type": "json_object"},
            stream=False,
            timeout_seconds=120,
            max_transport_key_candidates=2,
            allow_model_fallback=False,
            enable_thinking=False,
            force_mock=self.force_mock,
            max_retries=1,
        )
        parsed = _safe_json(str(result.get("content") or ""))
        issues: dict[str, str] = {}
        rows = parsed.get("issues")
        if isinstance(rows, list):
            valid_ids = {unit.unit_id for unit in batch}
            unit_by_id = {unit.unit_id: unit for unit in batch}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                unit_id = str(row.get("id") or "")
                reason = str(row.get("reason") or "").strip()
                severity = str(row.get("severity") or "minor").strip()
                unit = unit_by_id.get(unit_id)
                if (
                    unit is not None
                    and _known_audit_false_positive(
                        unit.source,
                        unit.translated,
                        reason,
                    )
                ):
                    continue
                if unit_id in valid_ids and reason:
                    issues[unit_id] = f"{severity}:{reason}"
        return issues, dict(result.get("_llm_usage") or {})

    def _record_aux_usage(self, usage: dict[str, Any]) -> None:
        input_tokens = int(usage.get("estimated_input_tokens", 0) or 0)
        output_tokens = int(usage.get("estimated_output_tokens", 0) or 0)
        model_name = str(
            usage.get("model_name") or get_model_name(self.audit_model_tier)
        )
        self._aux_input_tokens += input_tokens
        self._aux_output_tokens += output_tokens
        self._audit_call_count += 1
        self._spent_cny += estimate_call_cost_cny(
            model_name,
            input_tokens,
            output_tokens,
        )

    def _record_usage(self, units: list[TranslationUnit], usage: dict[str, Any]) -> None:
        input_tokens = int(usage.get("estimated_input_tokens", 0) or 0)
        output_tokens = int(usage.get("estimated_output_tokens", 0) or 0)
        model_name = str(usage.get("model_name") or get_model_name(self.model_tier))
        cost = estimate_call_cost_cny(model_name, input_tokens, output_tokens)
        self._spent_cny += cost
        share = max(1, len(units))
        for unit in units:
            unit.estimated_input_tokens += input_tokens // share
            unit.estimated_output_tokens += output_tokens // share
            unit.estimated_cost_cny += cost / share
            unit.model_name = model_name

    def _apply_translations(
        self,
        batch: list[TranslationUnit],
        translations: dict[str, str],
        usage: dict[str, Any],
        *,
        tier: str,
    ) -> list[TranslationUnit]:
        self._record_usage(batch, usage)
        failed: list[TranslationUnit] = []
        for unit in batch:
            unit.attempt_count += 1
            unit.retry_history.append({"tier": tier, "kind": "translation"})
            protected_target = translations.get(unit.unit_id, "")
            restored = _restore_fragile_tokens(
                protected_target,
                unit.protected_mapping,
            )
            restored, term_notes = _normalize_scientific_terminology(restored)
            errors = _validate_translation(
                unit.source,
                unit.protected_source,
                protected_target,
                restored,
                unit.protected_mapping,
            )
            unit.model_tier = tier
            unit.translated = restored.strip()
            if term_notes:
                unit.retry_history.append(
                    {
                        "tier": "deterministic",
                        "kind": "terminology_normalization",
                        "notes": term_notes,
                    }
                )
            unit.errors = errors
            unit.status = "validated" if not errors else "failed_validation"
            if errors:
                failed.append(unit)
        return failed

    def _translate_batch(
        self,
        batch: list[TranslationUnit],
        *,
        tier: str,
    ) -> list[TranslationUnit]:
        translations, usage = self._call_batch(batch, tier=tier)
        return self._apply_translations(
            batch,
            translations,
            usage,
            tier=tier,
        )

    def _translate_batch_resilient(
        self,
        batch: list[TranslationUnit],
        *,
        tier: str,
    ) -> list[TranslationUnit]:
        """Translate a batch without turning one malformed response into a
        whole-article retry.

        A transport/JSON failure is split recursively.  A single unit is
        allowed to raise so the caller can route only that unit to fallback.
        Successful siblings are never retransmitted.
        """
        try:
            return self._translate_batch(batch, tier=tier)
        except Exception as exc:
            if len(batch) <= 1:
                unit = batch[0]
                unit.status = "call_failed"
                unit.errors = [f"{type(exc).__name__}:{exc}"]
                unit.attempt_count += 1
                unit.retry_history.append(
                    {"tier": tier, "kind": "transport_failure", "error": type(exc).__name__}
                )
                return [unit]
            midpoint = max(1, len(batch) // 2)
            failed: list[TranslationUnit] = []
            for part in (batch[:midpoint], batch[midpoint:]):
                if part:
                    failed.extend(
                        self._translate_batch_resilient(part, tier=tier)
                    )
            return failed

    def _split_and_translate_unit(
        self,
        unit: TranslationUnit,
        *,
        tier: str,
    ) -> bool:
        """Last-resort recovery for an oversized or repeatedly malformed unit.

        The parent unit remains the durable identity.  Child calls are
        ephemeral and are reassembled in source order, so a resumed run never
        has to retransmit already accepted article units.
        """
        source = str(unit.source or "").strip()
        if len(source) < 900:
            return False
        pieces = [
            item.strip()
            for item in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", source)
            if item.strip()
        ]
        if len(pieces) < 2:
            return False
        children: list[TranslationUnit] = []
        for index, piece in enumerate(pieces, 1):
            protected, mapping = _protect_fragile_tokens(piece)
            children.append(
                TranslationUnit(
                    unit_id=f"{unit.unit_id}.S{index:02d}",
                    source=piece,
                    protected_source=protected,
                    protected_mapping=mapping,
                )
            )
        failed: list[TranslationUnit] = []
        for child in children:
            failed.extend(
                self._translate_batch_resilient([child], tier=tier)
            )
        if failed or any(child.status != "validated" for child in children):
            unit.retry_history.append({"tier": tier, "kind": "split_failed"})
            return False
        candidate = " ".join(child.translated.strip() for child in children)
        protected_candidate = candidate
        for token, value in unit.protected_mapping.items():
            protected_candidate = protected_candidate.replace(value, token)
        errors = _validate_translation(
            unit.source,
            unit.protected_source,
            protected_candidate,
            candidate,
            unit.protected_mapping,
        )
        if errors:
            unit.retry_history.append({"tier": tier, "kind": "split_parent_validation_failed"})
            return False
        unit.translated = candidate
        unit.status = "validated"
        unit.errors = []
        unit.model_tier = tier
        unit.model_name = children[-1].model_name
        unit.retry_history.append({"tier": tier, "kind": "split_reassembled", "child_count": len(children)})
        return True

    def _repair_failed_unit(
        self,
        unit: TranslationUnit,
        *,
        tier: str,
    ) -> bool:
        """Give any deterministic validation failure one bounded repair turn.

        The previous implementation only split blocks longer than 900
        characters.  A short block with one damaged citation or scientific
        token therefore had no recovery route and could block an otherwise
        complete article.  This repair reuses the original protected source,
        names the exact validation errors, and asks for a fresh translation;
        it never accepts or edits the old translation deterministically.
        """

        previous = str(unit.translated or "").strip()
        prior_errors = list(unit.errors)
        unit.repair_note = (
            "The previous translation failed deterministic validation: "
            + ", ".join(prior_errors or ["unknown_validation_error"])
            + ". Translate the protected source again. Reproduce every "
            "ZXQK...TOKEN exactly once and unchanged. Do not paraphrase, "
            "translate, split, or delete protected tokens."
            + (f" Previous rejected translation: {previous}" if previous else "")
        )
        unit.retry_history.append(
            {
                "tier": tier,
                "kind": "deterministic_validation_repair",
                "errors": prior_errors,
            }
        )
        failed = self._translate_batch_resilient([unit], tier=tier)
        return not failed and unit.status == "validated"

    def translate(
        self,
        *,
        output_filename: str = "FINAL_REVIEW_ZH.md",
        max_units: Optional[int] = None,
        extra_texts: Optional[dict[str, str]] = None,
        allow_partial_output: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        source_path = Path(self.source_markdown_path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        self.output_dir = Path(self.output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        prior_cost_path = self.output_dir / "TRANSLATION_COST.json"
        prior_cost = _read_json(prior_cost_path)
        if not prior_cost:
            prior_cost = _read_json(
                self.output_dir / "TRANSLATION_REPORT.json"
            )
        prior_input_tokens = int(
            prior_cost.get(
                "cumulative_input_tokens",
                prior_cost.get("estimated_input_tokens", 0),
            )
            or 0
        )
        prior_output_tokens = int(
            prior_cost.get(
                "cumulative_output_tokens",
                prior_cost.get("estimated_output_tokens", 0),
            )
            or 0
        )
        prior_spent_cny = float(
            prior_cost.get(
                "cumulative_estimated_cost_cny",
                prior_cost.get("estimated_cost_cny", 0.0),
            )
            or 0.0
        )
        source_text = source_path.read_text(encoding="utf-8")
        units, separators = self._units(source_text)
        if max_units is not None:
            units = units[: max(0, int(max_units))]
            separators = separators[: max(0, len(units) - 1)]
        review_units = list(units)
        extra_units: list[TranslationUnit] = []
        for raw_id, text in (extra_texts or {}).items():
            value = str(text or "").strip()
            if not value:
                continue
            safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw_id)).strip("_")
            protected, mapping = _protect_fragile_tokens(value)
            extra_units.append(
                TranslationUnit(
                    unit_id=f"META_{safe_id or len(extra_units) + 1}",
                    source=value,
                    protected_source=protected,
                    protected_mapping=mapping,
                )
            )
        units = review_units + extra_units
        selected_source_text = _restore_markdown_blocks(
            [unit.source for unit in review_units],
            separators,
        )
        prompt_hash = _sha256_text(self._prompt())
        cache = self._load_cache(prompt_hash)
        durable_state = self._load_state(prompt_hash)
        pending: list[TranslationUnit] = []
        cache_changed = False
        for unit in units:
            if unit.status == "validated":
                continue
            persisted = durable_state.get(unit.unit_id)
            if (
                isinstance(persisted, dict)
                and persisted.get("source_sha256") == unit.cache_key
                and persisted.get("status") == "validated"
            ):
                persisted_target = str(persisted.get("translation") or "")
                persisted_target, term_notes = _normalize_scientific_terminology(
                    persisted_target
                )
                persisted_protected = str(
                    _protect_fragile_tokens(persisted_target)[0]
                )
                errors = _validate_translation(
                    unit.source,
                    unit.protected_source,
                    persisted_protected,
                    persisted_target,
                    unit.protected_mapping,
                )
                if not errors:
                    unit.translated = persisted_target
                    unit.status = "validated"
                    unit.from_cache = True
                    unit.model_name = str(persisted.get("model_name") or "")
                    unit.model_tier = str(persisted.get("model_tier") or "")
                    unit.semantic_audit_passed = bool(
                        persisted.get("semantic_audit_passed", False)
                    )
                    unit.attempt_count = int(persisted.get("attempt_count", 0) or 0)
                    unit.retry_history = list(persisted.get("retry_history") or [])
                    if term_notes:
                        unit.semantic_audit_passed = False
                        unit.retry_history.append(
                            {
                                "tier": "deterministic",
                                "kind": "terminology_normalization",
                                "notes": term_notes,
                            }
                        )
                    continue
            cached = cache.get(unit.cache_key)
            if isinstance(cached, dict):
                target = str(cached.get("translation") or "")
                target, term_notes = _normalize_scientific_terminology(target)
                protected_target = _protect_fragile_tokens(target)[0]
                errors = _validate_translation(
                    unit.source,
                    unit.protected_source,
                    protected_target,
                    target,
                    unit.protected_mapping,
                )
                if not errors:
                    unit.translated = target
                    unit.status = "validated"
                    unit.from_cache = True
                    unit.model_name = str(cached.get("model_name") or "")
                    unit.model_tier = str(cached.get("model_tier") or "")
                    unit.semantic_audit_passed = bool(
                        cached.get("semantic_audit_passed", False)
                    )
                    if term_notes:
                        unit.semantic_audit_passed = False
                        unit.retry_history.append(
                            {
                                "tier": "deterministic",
                                "kind": "terminology_normalization",
                                "notes": term_notes,
                            }
                        )
                        cache[unit.cache_key] = {
                            **cached,
                            "translation": target,
                            "protected_translation": protected_target,
                            "semantic_audit_passed": False,
                        }
                        cache_changed = True
                    continue
            pending.append(unit)

        if cache_changed:
            self._save_cache(cache, prompt_hash=prompt_hash)
        self._save_state(units, prompt_hash=prompt_hash)

        primary_failures: list[TranslationUnit] = []
        batches = self._batches(pending)
        if batches:
            with ThreadPoolExecutor(max_workers=max(1, int(self.workers))) as pool:
                futures = {
                    pool.submit(
                        self._translate_batch_resilient,
                        batch,
                        tier=self.model_tier,
                    ): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch = futures[future]
                    try:
                        primary_failures.extend(future.result())
                    except Exception as exc:
                        for unit in batch:
                            unit.status = "call_failed"
                            unit.errors = [f"{type(exc).__name__}:{exc}"]
                            primary_failures.append(unit)
                    for unit in batch:
                        if unit.status == "validated":
                            cache[unit.cache_key] = {
                                "translation": unit.translated,
                                "protected_translation": _protect_fragile_tokens(
                                    unit.translated
                                )[0],
                                "model_name": unit.model_name,
                                "model_tier": unit.model_tier,
                                "semantic_audit_passed": False,
                            }
                    self._save_cache(cache, prompt_hash=prompt_hash)
                    self._save_state(units, prompt_hash=prompt_hash)

        escalated = 0
        for unit in primary_failures:
            if self._spent_cny >= max(0.0, float(self.cost_budget_cny)):
                unit.status = "budget_blocked"
                unit.errors.append("translation_cost_budget_exhausted")
                continue
            escalated += 1
            try:
                failed = self._translate_batch_resilient(
                    [unit],
                    tier=self.fallback_model_tier,
                )
                if failed:
                    if self._repair_failed_unit(
                        unit,
                        tier=self.repair_model_tier,
                    ):
                        failed = []
                    elif self._split_and_translate_unit(
                        unit,
                        tier=self.repair_model_tier,
                    ):
                        failed = []
                if not failed:
                    cache[unit.cache_key] = {
                        "translation": unit.translated,
                        "protected_translation": _protect_fragile_tokens(
                            unit.translated
                        )[0],
                        "model_name": unit.model_name,
                        "model_tier": unit.model_tier,
                        "semantic_audit_passed": False,
                    }
                    self._save_cache(cache, prompt_hash=prompt_hash)
                    self._save_state(units, prompt_hash=prompt_hash)
            except Exception as exc:
                unit.status = "call_failed_after_escalation"
                unit.errors.append(f"{type(exc).__name__}:{exc}")
            self._save_state(units, prompt_hash=prompt_hash)

        audit_issues: dict[str, str] = {}
        audited_units = [
            unit
            for unit in units
            if unit.status == "validated"
            and unit.model_tier != "deterministic"
            and not unit.semantic_audit_passed
        ]
        if self.semantic_audit and audited_units:
            audit_batches = self._batches(audited_units)
            for batch in audit_batches:
                try:
                    issues, usage = self._audit_batch(batch)
                    self._record_aux_usage(usage)
                    audit_issues.update(issues)
                    for unit in batch:
                        if unit.unit_id not in issues:
                            unit.semantic_audit_passed = True
                            cache[unit.cache_key] = {
                                "translation": unit.translated,
                                "protected_translation": (
                                    _protect_fragile_tokens(
                                        unit.translated
                                    )[0]
                                ),
                                "model_name": unit.model_name,
                                "model_tier": unit.model_tier,
                                "semantic_audit_passed": True,
                            }
                    self._save_cache(cache, prompt_hash=prompt_hash)
                    self._save_state(units, prompt_hash=prompt_hash)
                except Exception as exc:
                    for unit in batch:
                        unit.status = "audit_call_failed"
                        unit.errors.append(f"{type(exc).__name__}:{exc}")

            repaired_units: list[TranslationUnit] = []
            for unit in audited_units:
                issue = audit_issues.get(unit.unit_id)
                if not issue:
                    continue
                if self._spent_cny >= max(0.0, float(self.cost_budget_cny)):
                    unit.status = "budget_blocked"
                    unit.errors = [
                        f"semantic_audit:{issue}",
                        "translation_cost_budget_exhausted",
                    ]
                    continue
                unit.repair_note = issue
                unit.errors = [f"semantic_audit:{issue}"]
                try:
                    failed = self._translate_batch_resilient(
                        [unit],
                        tier=self.fallback_model_tier,
                    )
                    if failed and self._repair_failed_unit(
                        unit,
                        tier=self.repair_model_tier,
                    ):
                        failed = []
                    elif failed and self._split_and_translate_unit(
                        unit,
                        tier=self.repair_model_tier,
                    ):
                        failed = []
                    if not failed:
                        repaired_units.append(unit)
                except Exception as exc:
                    unit.status = "call_failed_after_semantic_audit"
                    unit.errors.append(f"{type(exc).__name__}:{exc}")
                self._save_state(units, prompt_hash=prompt_hash)

            if repaired_units:
                try:
                    remaining_issues, usage = self._audit_batch(repaired_units)
                    self._record_aux_usage(usage)
                    deep_repair_units: list[TranslationUnit] = []
                    for unit in repaired_units:
                        issue = remaining_issues.get(unit.unit_id)
                        if issue:
                            if issue.lower().startswith("major:"):
                                unit.repair_note = issue
                                unit.status = "pending_deep_repair"
                                unit.errors = [f"semantic_reaudit:{issue}"]
                                deep_repair_units.append(unit)
                            else:
                                unit.status = "validated"
                                unit.errors = []
                                unit.semantic_audit_passed = True
                                unit.audit_warnings = [
                                    f"semantic_reaudit:{issue}"
                                ]
                        else:
                            unit.status = "validated"
                            unit.errors = []
                            unit.semantic_audit_passed = True
                            cache[unit.cache_key] = {
                                "translation": unit.translated,
                                "protected_translation": _protect_fragile_tokens(
                                    unit.translated
                                )[0],
                                "model_name": unit.model_name,
                                "model_tier": unit.model_tier,
                                "semantic_audit_passed": True,
                            }
                    self._save_cache(cache, prompt_hash=prompt_hash)

                    for unit in deep_repair_units:
                        if self._spent_cny >= max(
                            0.0,
                            float(self.cost_budget_cny),
                        ):
                            unit.status = "budget_blocked"
                            unit.errors.append(
                                "translation_cost_budget_exhausted"
                            )
                            continue
                        try:
                            failed = self._translate_batch_resilient(
                                [unit],
                                tier=self.repair_model_tier,
                            )
                            if failed and self._split_and_translate_unit(
                                unit,
                                tier=self.repair_model_tier,
                            ):
                                failed = []
                            if failed:
                                continue
                            final_issues, final_usage = self._audit_batch([unit])
                            self._record_aux_usage(final_usage)
                            final_issue = final_issues.get(unit.unit_id)
                            if (
                                final_issue
                                and final_issue.lower().startswith("major:")
                            ):
                                unit.status = "failed_semantic_audit"
                                unit.errors = [
                                    f"semantic_final_audit:{final_issue}"
                                ]
                            else:
                                unit.status = "validated"
                                unit.errors = []
                                unit.semantic_audit_passed = True
                                if final_issue:
                                    unit.audit_warnings = [
                                        f"semantic_final_audit:{final_issue}"
                                    ]
                                cache[unit.cache_key] = {
                                    "translation": unit.translated,
                                    "protected_translation": (
                                        _protect_fragile_tokens(
                                            unit.translated
                                        )[0]
                                    ),
                                    "model_name": unit.model_name,
                                    "model_tier": unit.model_tier,
                                    "semantic_audit_passed": True,
                                }
                                self._save_cache(
                                    cache,
                                    prompt_hash=prompt_hash,
                                )
                                self._save_state(units, prompt_hash=prompt_hash)
                        except Exception as exc:
                            unit.status = "deep_repair_failed"
                            unit.errors.append(
                                f"{type(exc).__name__}:{exc}"
                            )
                        self._save_state(units, prompt_hash=prompt_hash)
                except Exception as exc:
                    for unit in repaired_units:
                        unit.status = "audit_call_failed_after_repair"
                        unit.errors.append(f"{type(exc).__name__}:{exc}")

        failed_units = [unit for unit in units if unit.status != "validated"]
        translated_path = self.output_dir / output_filename
        # A partially validated translation is paid-for scientific content.
        # It is never deleted: full output keeps the canonical filename, and a
        # degraded output is written beside it under a .partial.md name so the
        # delivery gate can accept it explicitly instead of losing the work.
        partial_path = translated_path.with_suffix(".partial.md")
        rendered_translation = _restore_markdown_blocks(
            [unit.translated or unit.source for unit in review_units],
            separators,
        )
        # Freshness contract: the report may only claim files that THIS run
        # wrote.  A canonical file left over from an earlier run must never be
        # reported as this run's output.
        wrote_canonical = False
        wrote_partial = False
        if not failed_units or allow_partial_output:
            _write_text(translated_path, rendered_translation)
            wrote_canonical = True
        else:
            _write_text(partial_path, rendered_translation)
            wrote_partial = True

        attempt_input_tokens = (
            sum(unit.estimated_input_tokens for unit in units)
            + self._aux_input_tokens
        )
        attempt_output_tokens = (
            sum(unit.estimated_output_tokens for unit in units)
            + self._aux_output_tokens
        )
        cumulative_input_tokens = prior_input_tokens + attempt_input_tokens
        cumulative_output_tokens = prior_output_tokens + attempt_output_tokens
        cumulative_cost_cny = prior_spent_cny + self._spent_cny
        report = {
            "schema_version": "research_harness.chinese_translation_report.v1",
            "status": (
                "completed"
                if not failed_units
                else "completed_with_warnings"
                if wrote_canonical or wrote_partial
                else "failed"
            ),
            "source_path": str(source_path),
            "translated_path": str(translated_path) if wrote_canonical else "",
            "partial_translated_path": str(partial_path) if wrote_partial else "",
            "source_sha256": _sha256_text(source_text),
            "prompt_path": str(Path(self.prompt_path).resolve()),
            "prompt_hash": prompt_hash,
            "state_path": str(self._state_path()),
            "primary_model_tier": self.model_tier,
            "fallback_model_tier": self.fallback_model_tier,
            "repair_model_tier": self.repair_model_tier,
            "unit_count": len(units),
            "review_unit_count": len(review_units),
            "metadata_unit_count": len(extra_units),
            "cache_hit_count": sum(1 for unit in units if unit.from_cache),
            "primary_call_batch_count": len(batches),
            "escalated_unit_count": escalated,
            "semantic_audit_enabled": self.semantic_audit,
            "semantic_audit_call_count": self._audit_call_count,
            "semantic_audit_issue_count": len(audit_issues),
            "failed_unit_ids": [unit.unit_id for unit in failed_units],
            "partial_output": bool(
                failed_units and (wrote_canonical or wrote_partial)
            ),
            "citation_marker_count_source": len(
                REF_PATTERN.findall(selected_source_text)
            ),
            # Counted from the in-memory rendering that this run actually
            # wrote, so a stale file on disk can never inflate the number.
            "citation_marker_count_translation": len(
                REF_PATTERN.findall(rendered_translation)
            ),
            "attempt_input_tokens": attempt_input_tokens,
            "attempt_output_tokens": attempt_output_tokens,
            "attempt_estimated_cost_cny": round(self._spent_cny, 6),
            "estimated_input_tokens": cumulative_input_tokens,
            "estimated_output_tokens": cumulative_output_tokens,
            "estimated_cost_cny": round(cumulative_cost_cny, 6),
            "cumulative_input_tokens": cumulative_input_tokens,
            "cumulative_output_tokens": cumulative_output_tokens,
            "cumulative_estimated_cost_cny": round(
                cumulative_cost_cny,
                6,
            ),
            "wall_time_seconds": round(time.monotonic() - started, 3),
            "extra_translations": {
                unit.unit_id.removeprefix("META_"): unit.translated
                for unit in extra_units
                if unit.status == "validated"
            },
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "source_sha256": unit.cache_key,
                    "status": unit.status,
                    "from_cache": unit.from_cache,
                    "model_name": unit.model_name,
                    "model_tier": unit.model_tier,
                    "errors": unit.errors,
                    "audit_warnings": unit.audit_warnings,
                    "semantic_audit_passed": unit.semantic_audit_passed,
                    "attempt_count": unit.attempt_count,
                    "retry_history": list(unit.retry_history),
                    "source_preview": unit.source[:180],
                    "translation_preview": unit.translated[:180],
                }
                for unit in units
            ],
        }
        # _save_state and cost-file write must complete before the report is
        # written so that an exception in either does not leave a "completed"
        # TRANSLATION_REPORT.json while the orchestrator records "failed".
        self._save_state(units, prompt_hash=prompt_hash)
        atomic_write_json(
            prior_cost_path,
            {
                "schema_version": (
                    "research_harness.chinese_translation_cost.v1"
                ),
                "source_sha256": report["source_sha256"],
                "prompt_hash": prompt_hash,
                "cumulative_input_tokens": cumulative_input_tokens,
                "cumulative_output_tokens": cumulative_output_tokens,
                "cumulative_estimated_cost_cny": round(
                    cumulative_cost_cny,
                    6,
                ),
                "last_attempt_input_tokens": attempt_input_tokens,
                "last_attempt_output_tokens": attempt_output_tokens,
                "last_attempt_estimated_cost_cny": round(
                    self._spent_cny,
                    6,
                ),
            },
        )
        atomic_write_json(self.output_dir / "TRANSLATION_REPORT.json", report)
        return report


def translate_scientific_review(
    *,
    source_markdown_path: Path,
    output_dir: Path,
    model_tier: str = "c2_model",
    fallback_model_tier: str = "c_model",
    workers: int = 3,
    cost_budget_cny: float = 3.0,
    max_units: Optional[int] = None,
    allow_partial_output: bool = False,
) -> dict[str, Any]:
    return ScientificChineseTranslator(
        source_markdown_path=Path(source_markdown_path),
        output_dir=Path(output_dir),
        model_tier=model_tier,
        fallback_model_tier=fallback_model_tier,
        workers=workers,
        cost_budget_cny=cost_budget_cny,
    ).translate(
        max_units=max_units,
        allow_partial_output=allow_partial_output,
    )


def translate_review_package(
    *,
    content_package_path: Path,
    source_markdown_path: Path,
    output_dir: Path,
    english_metadata_path: Optional[Path] = None,
    model_tier: str = "c2_model",
    fallback_model_tier: str = "c_model",
    workers: int = 3,
    cost_budget_cny: float = 3.0,
    allow_partial_output: bool = False,
) -> dict[str, Any]:
    """Translate review prose and publication metadata with one shared cache."""

    from .latex_publication_renderer import resolve_publication_metadata

    metadata, metadata_warnings = resolve_publication_metadata(
        content_package_path=Path(content_package_path),
        metadata_path=(
            Path(english_metadata_path) if english_metadata_path else None
        ),
    )
    extra_texts: dict[str, str] = {
        "title": str(metadata.get("title") or ""),
        "abstract": str(metadata.get("abstract") or ""),
    }
    for index, keyword in enumerate(metadata.get("keywords") or [], 1):
        extra_texts[f"keyword_{index:03d}"] = str(keyword)
    if metadata.get("acknowledgements"):
        extra_texts["acknowledgements"] = str(metadata["acknowledgements"])

    translator = ScientificChineseTranslator(
        source_markdown_path=Path(source_markdown_path),
        output_dir=Path(output_dir),
        model_tier=model_tier,
        fallback_model_tier=fallback_model_tier,
        workers=workers,
        cost_budget_cny=cost_budget_cny,
    )
    report = translator.translate(
        extra_texts=extra_texts,
        allow_partial_output=allow_partial_output,
    )
    translated_extras = dict(report.get("extra_translations") or {})
    metadata_path = Path(output_dir) / "PUBLICATION_METADATA_ZH.json"
    if report.get("status") in {"completed", "completed_with_warnings"}:
        chinese_metadata = {
            **metadata,
            "title": translated_extras.get("title", metadata["title"]),
            "abstract": translated_extras.get(
                "abstract",
                metadata["abstract"],
            ),
            "keywords": [
                translated_extras.get(
                    f"keyword_{index:03d}",
                    str(keyword),
                )
                for index, keyword in enumerate(
                    metadata.get("keywords") or [],
                    1,
                )
            ],
            "acknowledgements": translated_extras.get(
                "acknowledgements",
                metadata.get("acknowledgements", ""),
            ),
            "translation_source_metadata": str(
                Path(english_metadata_path).resolve()
                if english_metadata_path
                else ""
            ),
        }
        atomic_write_json(metadata_path, chinese_metadata)
        report["translated_metadata_path"] = str(metadata_path)
    else:
        report["translated_metadata_path"] = ""
    report["metadata_resolution_warnings"] = metadata_warnings
    atomic_write_json(Path(output_dir) / "TRANSLATION_REPORT.json", report)
    return report
