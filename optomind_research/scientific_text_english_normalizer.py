"""Faithful English working copies for non-English scientific paragraphs."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Scientific Text English Normalizer.txt"
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def repair_likely_scientific_mojibake(text: str) -> str:
    """Repair isolated GB18030-decoded UTF-8 symbols in English text.

    Windows/proxy boundaries occasionally turn scientific symbols such as
    ``Delta`` and the middle dot into single CJK characters.  This repair is
    deliberately conservative: it only runs on overwhelmingly Latin text with
    a small number of CJK characters, and only replaces a character when a
    GB18030 -> UTF-8 round trip yields printable, non-CJK text.
    """
    value = str(text or "")
    cjk_count = len(_CJK.findall(value))
    latin_count = len(re.findall(r"[A-Za-z]", value))
    # Repair whole UTF-8-as-GBK/Windows-1252 sequences before attempting
    # isolated-character recovery. Restrict this to Latin-dominant scientific
    # text so genuine Chinese user input is never rewritten.
    if latin_count >= 40 and (
        cjk_count or "\ufffd" in value or "Ã" in value or "â€" in value
    ):
        try:
            from ftfy import fix_text

            fixed = fix_text(value)
            if len(_CJK.findall(fixed)) < cjk_count or "\ufffd" not in fixed:
                value = fixed
                cjk_count = len(_CJK.findall(value))
                latin_count = len(re.findall(r"[A-Za-z]", value))
        except Exception:
            pass
    if not cjk_count or cjk_count > 20 or latin_count < 40:
        return value
    # A short English sentence can contain several consecutive mojibake
    # characters for one em dash (for example ``鈥攕``).  A 3% ratio rejected
    # exactly these common cases. Genuine Chinese prose is already excluded by
    # the absolute CJK and Latin-count guards above, so a modestly wider ratio
    # remains conservative while repairing short scientific sentences.
    if cjk_count / max(1, len(value)) > 0.15:
        return value

    def repair_sequence(match: re.Match[str]) -> str:
        sequence = match.group(0)
        try:
            candidate = sequence.encode("gb18030").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return sequence
        if not candidate or contains_cjk(candidate) or "\ufffd" in candidate:
            return sequence
        if any(ord(item) < 32 and item not in "\n\r\t" for item in candidate):
            return sequence
        return candidate

    # Multi-character corruption such as ``鈥檚`` represents one UTF-8
    # punctuation sequence plus an adjacent ASCII letter. Repair it as a unit
    # before falling back to isolated characters.
    value = re.sub(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", repair_sequence, value)

    def repair_char(match: re.Match[str]) -> str:
        char = match.group(0)
        try:
            candidate = char.encode("gb18030").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return char
        if not candidate or contains_cjk(candidate) or "\ufffd" in candidate:
            return char
        if any(ord(item) < 32 and item not in "\n\r\t" for item in candidate):
            return char
        return candidate

    return _CJK.sub(repair_char, value)


@dataclass
class EnglishTextRecord:
    source_text: str
    text_en: str
    source_language: str
    translation_status: str
    translation_model: str = ""
    validation_errors: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_text": self.source_text,
            "text_en": self.text_en,
            "source_language": self.source_language,
            "translation_status": self.translation_status,
            "translation_model": self.translation_model,
            "validation_errors": list(self.validation_errors or []),
        }


def contains_cjk(text: str) -> bool:
    return bool(_CJK.search(str(text or "")))


def ensure_english_strings(
    values: list[str],
    *,
    model_tier: str = "standard_model",
    fallback_model_tier: str = "premium_model",
) -> list[str]:
    """Return English-only working strings, translating CJK only when needed.

    This is a boundary gate for model-to-model messages.  Original user input
    and source provenance remain untouched elsewhere.  A failed translation is
    quarantined as an empty value rather than leaking mixed-language text into
    retrieval, planning, or writing agents.
    """
    texts = [repair_likely_scientific_mojibake(str(value or "")) for value in values]
    if not any(contains_cjk(text) for text in texts):
        return texts
    try:
        records = ScientificTextEnglishNormalizer(
            model_tier=model_tier,
            fallback_model_tier=fallback_model_tier,
            batch_size=4,
            workers=2,
        ).normalize(texts)
        return [
            record.text_en
            if record.translation_status != "translation_failed_quarantined"
            else ""
            for record in records
        ]
    except Exception:
        return ["" if contains_cjk(text) else text for text in texts]


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(raw or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _validate_translation(source: str, translated: str) -> list[str]:
    errors: list[str] = []
    if not translated.strip():
        errors.append("empty_translation")
    if contains_cjk(translated):
        errors.append("non_english_cjk_remaining")
    # Protect scientific quantities, not every bare integer. Bare small
    # integers are often citation markers or affiliation indices and may be
    # legitimately reformatted by translation.
    numeric_pattern = re.compile(
        r"\d+\.\d+|\d{4}|\d+(?=\s*(?:%|nm|um|µm|mm|cm|mK|K|°C|C|W|mW|kW|Hz|kHz|MHz|GHz|THz|Pa|MPa|GPa|RIU|eV)\b)|\d+(?:\.\d+)?\s*[×x]\s*10\^?[-+]?\d+",
        re.I,
    )
    source_numbers = set(numeric_pattern.findall(source))
    translated_numbers = set(numeric_pattern.findall(translated))
    missing_numbers = sorted(source_numbers - translated_numbers)
    if missing_numbers:
        errors.append("missing_numbers:" + ",".join(missing_numbers[:12]))
    if source.strip() and len(translated.strip()) < max(30, int(len(source.strip()) * 0.18)):
        errors.append("suspiciously_short")
    return errors


class ScientificTextEnglishNormalizer:
    def __init__(
        self,
        *,
        prompt_path: Path = DEFAULT_PROMPT,
        model_tier: str = "standard_model",
        fallback_model_tier: str = "premium_model",
        batch_size: int = 4,
        workers: int = 4,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.prompt_path = Path(prompt_path)
        self.model_tier = model_tier
        self.fallback_model_tier = fallback_model_tier
        self.batch_size = max(1, int(batch_size))
        self.workers = max(1, int(workers))
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.last_audit: dict[str, Any] = {}

    def normalize(self, paragraphs: list[str]) -> list[EnglishTextRecord]:
        repaired = [repair_likely_scientific_mojibake(text) for text in paragraphs]
        records = [
            EnglishTextRecord(
                source_text=source,
                text_en=text if not contains_cjk(text) else "",
                source_language="english" if not contains_cjk(text) else "contains_cjk",
                translation_status="original_english" if not contains_cjk(text) else "pending",
            )
            for source, text in zip(paragraphs, repaired)
        ]
        pending = [idx for idx, rec in enumerate(records) if rec.translation_status == "pending"]
        batches = [pending[i : i + self.batch_size] for i in range(0, len(pending), self.batch_size)]
        prompt = (
            self.prompt_path.read_text(encoding="utf-8")
            if batches
            else ""
        )

        def translate_batch(
            indices: list[int],
            model_tier: str,
            pass_name: str,
        ) -> tuple[dict[int, str], dict[str, Any]]:
            refs = {f"P{position:02d}": idx for position, idx in enumerate(indices, start=1)}
            payload = {
                "paragraphs": [
                    {"ref": ref, "source_text": records[idx].source_text}
                    for ref, idx in refs.items()
                ]
            }
            call_kwargs = {
                "model_tier": model_tier,
                "temperature": 0,
                "max_tokens": 4200,
                "response_format": {"type": "json_object"},
                "force_mock": False,
                "max_retries": 0,
                "timeout_seconds": self.timeout_seconds,
                "max_transport_key_candidates": 1,
                "max_key_candidates": 1,
                "allow_model_fallback": False,
                "enable_thinking": False,
            }
            try:
                result = call_qwen_chat(
                    "ScientificTextEnglishNormalizerAgent",
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    **call_kwargs,
                )
            except Exception as exc:
                # One transport/model failure must only quarantine the items
                # in this batch; it must never abort the whole paper.
                usage = {
                    "module": "ScientificTextEnglishNormalizerAgent",
                    "agent_name": "ScientificTextEnglishNormalizerAgent",
                    "model_tier": model_tier,
                    "model_name": str(model_tier),
                    "task_type": "scientific_text_english_normalization",
                    "mock_llm": False,
                    "success": False,
                    "failure": True,
                    "error_type": type(exc).__name__,
                    "fallback_used": True,
                    "estimated_input_tokens": 0,
                    "estimated_output_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "token_usage_source": "provider_call_failed",
                    "reason": "provider_call_exception",
                    "pass": pass_name,
                    "paragraph_count": len(indices),
                    "bounded_translation_call": True,
                    "timeout_seconds": self.timeout_seconds,
                    "allow_model_fallback": False,
                    "enable_thinking": False,
                    "max_retries": 0,
                    "transport_key_candidates": 1,
                    "key_candidates": 1,
                }
                return {}, usage
            parsed = _safe_json(str(result.get("content") or ""))
            rows = parsed.get("translations") if isinstance(parsed.get("translations"), list) else []
            out: dict[int, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                idx = refs.get(str(row.get("ref") or ""))
                if idx is not None:
                    out[idx] = str(row.get("text_en") or "").strip()
            usage = dict(result.get("_llm_usage") or {})
            usage.update(
                {
                    "pass": pass_name,
                    "paragraph_count": len(indices),
                    "bounded_translation_call": True,
                    "timeout_seconds": self.timeout_seconds,
                    "allow_model_fallback": False,
                    "enable_thinking": False,
                    "max_retries": 0,
                    "transport_key_candidates": 1,
                    "key_candidates": 1,
                }
            )
            if str(result.get("content") or "").startswith("[fallback]"):
                usage["provider_fallback_returned"] = True
            return out, usage

        usages: list[dict[str, Any]] = []
        primary_calls = len(batches)
        if batches:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(batches))) as pool:
                first_results = list(
                    pool.map(
                        lambda batch: translate_batch(batch, self.model_tier, "primary"),
                        batches,
                    )
                )
            for translated, usage in first_results:
                usages.append(usage)
                for idx, text_en in translated.items():
                    errors = _validate_translation(records[idx].source_text, text_en)
                    if not errors:
                        records[idx].text_en = text_en
                        records[idx].translation_status = "translated"
                        records[idx].translation_model = str(usage.get("model_name") or self.model_tier)
                    else:
                        records[idx].validation_errors = errors

        # Escalate only failed paragraphs, still in bounded batches. There is
        # exactly one escalation pass; anything left after it is quarantined so
        # one paper can never drive an unbounded per-paragraph retry chain.
        failed = [idx for idx in pending if records[idx].translation_status == "pending"]
        escalation_batches = [
            failed[i : i + self.batch_size]
            for i in range(0, len(failed), self.batch_size)
        ]
        escalation_calls = len(escalation_batches)
        if escalation_batches:
            with ThreadPoolExecutor(
                max_workers=min(self.workers, len(escalation_batches))
            ) as pool:
                escalation_results = list(
                    pool.map(
                        lambda batch: translate_batch(
                            batch,
                            self.fallback_model_tier,
                            "escalation",
                        ),
                        escalation_batches,
                    )
                )
            for translated, usage in escalation_results:
                usages.append(usage)
                for idx, text_en in translated.items():
                    errors = _validate_translation(records[idx].source_text, text_en)
                    records[idx].validation_errors = errors
                    if not errors:
                        records[idx].text_en = text_en
                        records[idx].translation_status = "translated_after_escalation"
                        records[idx].translation_model = str(
                            usage.get("model_name") or self.fallback_model_tier
                        )
                    else:
                        records[idx].translation_status = "translation_failed_quarantined"

        # Fail-open quarantine: a missing row, malformed payload, provider
        # exception, or still-invalid translation must become an empty English
        # value instead of an untranslated CJK paragraph or a silent pending
        # record.
        for idx in failed:
            if records[idx].translation_status != "pending":
                continue
            errors = list(records[idx].validation_errors or [])
            if "unresolved_after_bounded_passes" not in errors:
                errors.append("unresolved_after_bounded_passes")
            records[idx].validation_errors = errors
            records[idx].translation_status = "translation_failed_quarantined"

        self.last_audit = {
            "total": len(records),
            "translation_required": len(pending),
            "translated": sum(1 for r in records if r.translation_status.startswith("translated")),
            "quarantined": sum(1 for r in records if r.translation_status == "translation_failed_quarantined"),
            "provider_call_count": primary_calls + escalation_calls,
            "primary_batch_calls": primary_calls,
            "escalation_batch_calls": escalation_calls,
            "batch_size": self.batch_size,
            "max_translation_passes": 2,
            "timeout_seconds": self.timeout_seconds,
            "bounded_translation": True,
            "usage": usages,
        }
        return records
