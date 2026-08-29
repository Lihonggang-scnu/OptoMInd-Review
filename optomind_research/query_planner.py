"""Query Planner agent with schema repair and human-in-the-loop handoff.

Pipeline:

1. qwen3.7-max/premium_model generates the plan from the system prompt.
2. Python validates strict JSON shape and required fields.
3. If validation fails, qwen3.6-flash/standard_model repairs format only.
4. The UI exposes the validated JSON for human editing and confirmation.

The agent does not retrieve literature and does not answer the research question.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUERY_PLANNER_PROMPT_PATH = PROJECT_ROOT / "prompts" / "Query Planner.txt"
LEGACY_QUERY_PLANNER_PROMPT_PATH = Path.home() / "Desktop" / "Query Planner.txt"

EXPECTED_SCHEMA: dict[str, Any] = {
    "input": {"user_query": "English normalization of the original user question"},
    "output": {
        "problem_understanding": "English professional reformulation of the research question",
        "scope_definition": {
            "main_scope": "English one-sentence scope definition",
            "scope_items": ["English research focus 1", "English research focus 2"],
        },
        "keyword_decomposition": {
            "keywords": ["English scholarly search phrase", "English synonym or adjacent concept"],
        },
        "extra_notes": "",
    },
}


@dataclass
class QueryPlannerInput:
    user_query: str = ""


@dataclass
class ScopeDefinition:
    main_scope: str = ""
    scope_items: list[str] = field(default_factory=list)


@dataclass
class KeywordDecomposition:
    keywords: list[str] = field(default_factory=list)


@dataclass
class QueryPlannerOutput:
    problem_understanding: str = ""
    scope_definition: ScopeDefinition = field(default_factory=ScopeDefinition)
    keyword_decomposition: KeywordDecomposition = field(default_factory=KeywordDecomposition)
    extra_notes: str = ""


@dataclass
class QueryPlannerResponse:
    input: QueryPlannerInput
    output: QueryPlannerOutput

    @classmethod
    def model_validate(cls, value: Any, *, user_query: str = "") -> "QueryPlannerResponse":
        if not isinstance(value, dict):
            raise TypeError("QueryPlannerResponse requires a dict")
        input_data = value.get("input") or {}
        output_data = value.get("output") or {}
        scope_data = output_data.get("scope_definition") if isinstance(output_data, dict) else {}
        keyword_data = output_data.get("keyword_decomposition") if isinstance(output_data, dict) else {}
        if not isinstance(input_data, dict):
            input_data = {}
        if not isinstance(output_data, dict):
            output_data = {}
        if not isinstance(scope_data, dict):
            scope_data = {}
        if not isinstance(keyword_data, dict):
            keyword_data = {}
        return cls(
            input=QueryPlannerInput(
                user_query=str(
                    input_data.get("user_query", "")
                    or output_data.get("problem_understanding", "")
                    or user_query
                    or ""
                ).strip()
            ),
            output=QueryPlannerOutput(
                problem_understanding=str(output_data.get("problem_understanding", "") or "").strip(),
                scope_definition=ScopeDefinition(
                    main_scope=str(scope_data.get("main_scope", "") or "").strip(),
                    scope_items=_clean_list(scope_data.get("scope_items", []), max_items=24),
                ),
                keyword_decomposition=KeywordDecomposition(
                    keywords=_clean_list(keyword_data.get("keywords", []), max_items=40),
                ),
                extra_notes=str(output_data.get("extra_notes", "") or "").strip(),
            ),
        )

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FormatValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_contract: str = "QueryPlannerResponse.v1"

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def _clean_list(values: Any, *, max_items: int = 20) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[，,、;\n]+", values)
    if not isinstance(values, list):
        values = [values]
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip(" \t\r\n-•;；,，、")
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


class QueryPlannerAgent:
    """First-stage planning agent backed by qwen3.7-max via ``premium_model``."""

    agent_name = "QueryPlannerAgent"

    def __init__(
        self,
        *,
        prompt_path: str | Path | None = None,
        model_tier: str = "premium_model",
        repair_model_tier: str = "standard_model",
        real_llm: bool = True,
        temperature: float = 0.1,
        max_tokens: int = 2200,
    ) -> None:
        env_prompt = os.environ.get("OPTOMIND_QUERY_PLANNER_PROMPT", "").strip()
        configured_path = Path(prompt_path or env_prompt or DEFAULT_QUERY_PLANNER_PROMPT_PATH)
        if configured_path.exists():
            self.prompt_path = configured_path
        elif LEGACY_QUERY_PLANNER_PROMPT_PATH.exists():
            self.prompt_path = LEGACY_QUERY_PLANNER_PROMPT_PATH
        else:
            self.prompt_path = configured_path
        self.model_tier = model_tier
        self.repair_model_tier = repair_model_tier
        self.real_llm = real_llm
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.last_raw_response: str = ""
        self.last_usage: dict[str, Any] = {}
        self.last_repair_raw_response: str = ""
        self.last_repair_usage: dict[str, Any] = {}

    def plan(self, user_query: str) -> QueryPlannerResponse:
        """Return only the final validated planner response."""

        package = self.plan_for_human_review(user_query)
        return QueryPlannerResponse.model_validate(package["result"], user_query=user_query)

    def plan_dict(self, user_query: str) -> dict[str, Any]:
        """Return only the compact planner JSON for downstream agents."""

        return self.plan(user_query).model_dump()

    def plan_review_dict(self, user_query: str) -> dict[str, Any]:
        """Return the full generation/repair/validation package for UI review."""

        return self.plan_for_human_review(user_query)

    def plan_for_human_review(self, user_query: str) -> dict[str, Any]:
        question = str(user_query or "").strip()
        if not question:
            raise ValueError("user_query is required")

        if not self.real_llm:
            fallback = self._fallback_payload(question)
            validation = self.validate_payload(fallback, raw_text=json.dumps(fallback, ensure_ascii=False), user_query=question)
            return self._review_package(
                status="mock_or_fallback",
                question=question,
                result=fallback,
                final_validation=validation,
                primary_validation=validation,
                repair_attempted=False,
                repair_validation=None,
                note="Mock/fallback output generated for workflow checking; it does not represent real model quality.",
            )

        primary_raw = self._call_primary_model(question)
        primary_payload, primary_parse_error, primary_extra_text = self._parse_raw_json(primary_raw)
        primary_validation = self.validate_payload(
            primary_payload,
            raw_text=primary_raw,
            user_query=question,
            parse_error=primary_parse_error,
            extra_text=primary_extra_text,
        )
        salvaged = self._salvage_optional_extra_notes(
            primary_payload,
            validation=primary_validation,
            user_query=question,
        )
        if salvaged is not None:
            result, final_validation = salvaged
            return self._review_package(
                status="primary_valid_optional_notes_dropped",
                question=question,
                result=result,
                final_validation=final_validation,
                primary_validation=primary_validation,
                repair_attempted=False,
                repair_validation=None,
                note=(
                    "Primary plan was scientifically valid; invalid optional "
                    "extra_notes were discarded after the bounded format check."
                ),
            )
        if primary_payload is not None and primary_validation.ok:
            result = self._normalize_payload(primary_payload, question)
            final_validation = self.validate_payload(
                result,
                raw_text=json.dumps(result, ensure_ascii=False),
                user_query=question,
            )
            return self._review_package(
                status="primary_valid",
                question=question,
                result=result,
                final_validation=final_validation,
                primary_validation=primary_validation,
                repair_attempted=False,
                repair_validation=None,
                note="qwen3.7-max output passed schema and English checks; human confirmation is still required for content intent.",
            )

        repair_raw = self._call_format_repair_model(
            user_query=question,
            raw_output=primary_raw,
            validation=primary_validation,
        )
        repair_payload, repair_parse_error, repair_extra_text = self._parse_raw_json(repair_raw)
        repair_validation = self.validate_payload(
            repair_payload,
            raw_text=repair_raw,
            user_query=question,
            parse_error=repair_parse_error,
            extra_text=repair_extra_text,
        )
        salvaged = self._salvage_optional_extra_notes(
            repair_payload,
            validation=repair_validation,
            user_query=question,
        )
        if salvaged is not None:
            result, final_validation = salvaged
            return self._review_package(
                status="repaired_optional_notes_dropped",
                question=question,
                result=result,
                final_validation=final_validation,
                primary_validation=primary_validation,
                repair_attempted=True,
                repair_validation=repair_validation,
                note=(
                    "Format repair produced a valid core plan; invalid optional "
                    "extra_notes were discarded before human confirmation."
                ),
            )
        if repair_payload is not None and repair_validation.ok:
            result = self._normalize_payload(repair_payload, question)
            final_validation = self.validate_payload(
                result,
                raw_text=json.dumps(result, ensure_ascii=False),
                user_query=question,
            )
            return self._review_package(
                status="repaired_by_format_model",
                question=question,
                result=result,
                final_validation=final_validation,
                primary_validation=primary_validation,
                repair_attempted=True,
                repair_validation=repair_validation,
                note="qwen3.7-max output failed validation and qwen3.6-flash repaired structure only; human confirmation is still required.",
            )

        fallback = self._fallback_payload(question)
        final_validation = self.validate_payload(
            fallback,
            raw_text=json.dumps(fallback, ensure_ascii=False),
            user_query=question,
        )
        return self._review_package(
            status="deterministic_fallback_after_repair_failed",
            question=question,
            result=fallback,
            final_validation=final_validation,
            primary_validation=primary_validation,
            repair_attempted=True,
            repair_validation=repair_validation,
            note="Both primary generation and format repair failed; deterministic fallback JSON was generated and must be reviewed by a human.",
        )

    def _salvage_optional_extra_notes(
        self,
        payload: Any,
        *,
        validation: FormatValidationReport,
        user_query: str,
    ) -> tuple[dict[str, Any], FormatValidationReport] | None:
        """Drop only an invalid optional ``extra_notes`` field.

        The planner's scientific core is the normalized question, scope, and
        keyword set. ``extra_notes`` is advisory and never enters retrieval.
        If that field alone violates the English contract, discard it instead
        of replacing an otherwise useful plan with an unrelated fallback.
        """

        if payload is None or validation.ok or not validation.errors:
            return None
        if not all(
            str(error).startswith("$.output.extra_notes")
            for error in validation.errors
        ):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("output"), dict):
            return None
        candidate = json.loads(json.dumps(payload, ensure_ascii=False))
        candidate["output"]["extra_notes"] = ""
        try:
            normalized = self._normalize_payload(candidate, user_query)
        except (TypeError, ValueError, KeyError):
            return None
        final_validation = self.validate_payload(
            normalized,
            raw_text=json.dumps(normalized, ensure_ascii=False),
            user_query=user_query,
        )
        if not final_validation.ok:
            return None
        return normalized, final_validation

    def validate_user_payload(self, payload_or_text: Any, *, user_query: str = "") -> dict[str, Any]:
        payload: Any = payload_or_text
        parse_error = ""
        extra_text = False
        if isinstance(payload_or_text, str):
            payload, parse_error, extra_text = self._parse_raw_json(payload_or_text)
        validation = self.validate_payload(
            payload,
            raw_text=payload_or_text if isinstance(payload_or_text, str) else json.dumps(payload_or_text, ensure_ascii=False),
            user_query=user_query,
            parse_error=parse_error,
            extra_text=extra_text,
        )
        normalized = self._normalize_payload(payload, user_query) if payload is not None and validation.ok else None
        return {
            "ok": validation.ok,
            "validation": validation.model_dump(),
            "normalized": normalized,
        }

    def validate_payload(
        self,
        payload: Any,
        *,
        raw_text: str = "",
        user_query: str = "",
        parse_error: str = "",
        extra_text: bool = False,
    ) -> FormatValidationReport:
        errors: list[str] = []
        warnings: list[str] = []

        if parse_error:
            errors.append(f"JSON parsing failed: {parse_error}")
        if extra_text:
            errors.append("Output contains text, code fences, or explanatory prefixes/suffixes outside the JSON object.")
        if payload is None:
            errors.append("No valid JSON object was available for validation.")
            return FormatValidationReport(ok=False, errors=errors, warnings=warnings)
        if not isinstance(payload, dict):
            errors.append("Top-level payload must be a JSON object.")
            return FormatValidationReport(ok=False, errors=errors, warnings=warnings)

        self._check_keys(payload, {"input", "output"}, "$", errors)
        input_data = payload.get("input")
        output_data = payload.get("output")
        if not isinstance(input_data, dict):
            errors.append("$.input must be an object.")
            input_data = {}
        if not isinstance(output_data, dict):
            errors.append("$.output must be an object.")
            output_data = {}

        self._check_keys(input_data, {"user_query"}, "$.input", errors)
        user_query_value = input_data.get("user_query")
        if not isinstance(user_query_value, str) or not user_query_value.strip():
            errors.append("$.input.user_query must be a non-empty string.")
        else:
            self._check_english_text(user_query_value, "$.input.user_query", errors)

        self._check_keys(
            output_data,
            {"problem_understanding", "scope_definition", "keyword_decomposition", "extra_notes"},
            "$.output",
            errors,
        )
        if not isinstance(output_data.get("problem_understanding"), str) or not output_data.get("problem_understanding", "").strip():
            errors.append("$.output.problem_understanding must be a non-empty English string.")
        else:
            self._check_english_text(output_data.get("problem_understanding"), "$.output.problem_understanding", errors)

        scope = output_data.get("scope_definition")
        if not isinstance(scope, dict):
            errors.append("$.output.scope_definition must be an object.")
            scope = {}
        self._check_keys(scope, {"main_scope", "scope_items"}, "$.output.scope_definition", errors)
        if not isinstance(scope.get("main_scope"), str) or not scope.get("main_scope", "").strip():
            errors.append("$.output.scope_definition.main_scope must be a non-empty English string.")
        else:
            self._check_english_text(scope.get("main_scope"), "$.output.scope_definition.main_scope", errors)
        self._check_string_list(scope.get("scope_items"), "$.output.scope_definition.scope_items", errors)
        for index, item in enumerate(scope.get("scope_items") if isinstance(scope.get("scope_items"), list) else []):
            if isinstance(item, str):
                self._check_english_text(item, f"$.output.scope_definition.scope_items[{index}]", errors)

        keywords = output_data.get("keyword_decomposition")
        if not isinstance(keywords, dict):
            errors.append("$.output.keyword_decomposition must be an object.")
            keywords = {}
        self._check_keys(keywords, {"keywords"}, "$.output.keyword_decomposition", errors)
        self._check_string_list(keywords.get("keywords"), "$.output.keyword_decomposition.keywords", errors)
        for idx, keyword in enumerate(keywords.get("keywords") if isinstance(keywords.get("keywords"), list) else [], 1):
            if isinstance(keyword, str):
                if re.search(r"[\u4e00-\u9fff]", keyword) or not keyword.isascii() or not re.search(r"[A-Za-z]", keyword):
                    errors.append(
                        f"$.output.keyword_decomposition.keywords[{idx}] must be an English ASCII search phrase and must not contain Chinese, mojibake, or non-English text."
                    )

        if not isinstance(output_data.get("extra_notes", ""), str):
            errors.append("$.output.extra_notes must be a string; use an empty string when there are no notes.")
        else:
            self._check_english_text(output_data.get("extra_notes", ""), "$.output.extra_notes", errors, allow_empty=True)

        if raw_text and len(raw_text) > 20000:
            warnings.append("Raw output is unusually long; check whether extra explanation or body text leaked into the JSON response.")
        return FormatValidationReport(ok=not errors, errors=errors, warnings=warnings)

    @staticmethod
    def _check_keys(payload: dict[str, Any], expected: set[str], path: str, errors: list[str]) -> None:
        keys = set(payload.keys())
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        if missing:
            errors.append(f"{path} is missing required fields: {', '.join(missing)}.")
        if extra:
            errors.append(f"{path} contains unexpected fields: {', '.join(extra)}.")

    @staticmethod
    def _check_string_list(value: Any, path: str, errors: list[str]) -> None:
        if not isinstance(value, list):
            errors.append(f"{path} must be an array.")
            return
        if not value:
            errors.append(f"{path} must not be an empty array.")
            return
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                errors.append(f"{path}[{index}] must be a non-empty string.")

    @staticmethod
    def _check_english_text(value: Any, path: str, errors: list[str], *, allow_empty: bool = False) -> None:
        text = str(value or "")
        if not text.strip() and allow_empty:
            return
        if re.search(r"[\u4e00-\u9fff]", text):
            errors.append(f"{path} must be written in English and must not contain Chinese characters.")
        if text.strip() and not re.search(r"[A-Za-z]", text):
            errors.append(f"{path} must contain English alphabetic text.")

    def _call_primary_model(self, question: str) -> str:
        prompt = self._load_system_prompt()
        system = self._compose_primary_system_prompt(prompt)
        user = (
            "Run Query Planner for the following user research question.\n"
            "Return exactly one valid JSON object. Do not output Markdown, code fences, or explanatory text.\n"
            "Every JSON string, including input.user_query, must be in English. Translate the user question faithfully before placing it in input.user_query.\n\n"
            f"User question: {question}"
        )
        result = call_qwen_chat(
            self.agent_name,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model_tier=self.model_tier,
            max_retries=1,
            temperature=self.temperature,
            force_mock=False,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        self.last_raw_response = str(result.get("content") or "")
        self.last_usage = dict(result.get("_llm_usage") or {})
        return self.last_raw_response

    def _call_format_repair_model(
        self,
        *,
        user_query: str,
        raw_output: str,
        validation: FormatValidationReport,
    ) -> str:
        system = (
            "You are a JSON format repair agent. You do not reinterpret the scientific intent.\n"
            "Your only job is to repair the higher-level model output into a strict JSON object matching the target schema.\n"
            "Return JSON only. Do not add Markdown, code fences, or explanation.\n"
            "All generated fields under output must be in English. If the original output accidentally contains Chinese in generated fields, translate it into concise English.\n"
            "If a required field is missing, use a short English placeholder such as 'Needs human input'. Do not invent papers, DOIs, numbers, or conclusions."
        )
        user = json.dumps(
            {
                "user_query": user_query,
                "target_schema": EXPECTED_SCHEMA,
                "validation_errors": validation.errors,
                "validation_warnings": validation.warnings,
                "bad_output": raw_output,
                "rules": [
                    "Top-level keys must be input and output only.",
                    "output keys must be problem_understanding, scope_definition, keyword_decomposition, and extra_notes only.",
                    "scope_items and keywords must be non-empty arrays of strings.",
                    "Every JSON field, including input.user_query, must be English.",
                    "keywords must be English search phrases. Translate any Chinese keywords into concise English search phrases.",
                    "extra_notes must be a string; use an empty string when there are no notes.",
                    "Preserve the higher-level model content where possible, but fix structure, field names, arrays, JSON syntax, and accidental non-English generated text.",
                ],
            },
            ensure_ascii=False,
        )
        result = call_qwen_chat(
            "QueryPlannerFormatRepairAgent",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model_tier=self.repair_model_tier,
            max_retries=1,
            temperature=0,
            force_mock=False,
            max_tokens=1800,
            response_format={"type": "json_object"},
        )
        self.last_repair_raw_response = str(result.get("content") or "")
        self.last_repair_usage = dict(result.get("_llm_usage") or {})
        return self.last_repair_raw_response

    def _load_system_prompt(self) -> str:
        try:
            return self.prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            return (
                "You are Query Planner. Convert one vague research question into a searchable, verifiable, and writable research plan. "
                "Return only problem understanding, scope definition, and keyword decomposition in the required JSON schema. "
                "All generated fields must be English."
            )

    @staticmethod
    def _compose_primary_system_prompt(system_prompt_from_file: str) -> str:
        return (
            system_prompt_from_file.strip()
            + "\n\n"
            "MANDATORY OUTPUT CONTRACT\n"
            "The agent name is Query Planner.\n"
            "You may only do three tasks: problem understanding, scope definition, and keyword decomposition.\n"
            "Do not answer the scientific question. Do not write a literature review. Do not generate conclusions, device recipes, layer numbers, or thicknesses.\n"
            "All generated fields under output must be written in English only. Do not use Chinese in problem_understanding, scope_definition, keywords, or extra_notes.\n"
            "Translate the original question faithfully and place the English normalization in input.user_query. The raw question is preserved outside this JSON.\n"
            "If extra notes are needed, put them in output.extra_notes in English. If no notes are needed, use an empty string. Do not write anything outside JSON.\n"
            "Return exactly this JSON schema and do not add extra fields:\n"
            + json.dumps(EXPECTED_SCHEMA, ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _extract_json_candidate(text: str) -> tuple[str, bool]:
        stripped = str(text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
        if fenced:
            candidate = fenced.group(1).strip()
            return candidate, True
        first_obj = stripped.find("{")
        last_obj = stripped.rfind("}")
        if first_obj >= 0 and last_obj >= first_obj:
            candidate = stripped[first_obj : last_obj + 1].strip()
            return candidate, candidate != stripped
        return stripped, False

    @classmethod
    def _parse_raw_json(cls, text: str) -> tuple[Any | None, str, bool]:
        candidate, extra_text = cls._extract_json_candidate(text)
        if not candidate:
            return None, "empty output", extra_text
        try:
            payload = json.loads(candidate)
            return payload, "", extra_text
        except Exception as exc:
            return None, type(exc).__name__ + ": " + str(exc), extra_text

    def _normalize_payload(self, payload: Any, user_query: str) -> dict[str, Any]:
        response = QueryPlannerResponse.model_validate(payload, user_query=user_query)
        return response.model_dump()

    def _review_package(
        self,
        *,
        status: str,
        question: str,
        result: dict[str, Any],
        final_validation: FormatValidationReport,
        primary_validation: FormatValidationReport,
        repair_attempted: bool,
        repair_validation: FormatValidationReport | None,
        note: str,
    ) -> dict[str, Any]:
        return {
            "agent": self.agent_name,
            "status": status,
            "question": question,
            "needs_human_confirmation": True,
            "result": result,
            "final_validation": final_validation.model_dump(),
            "primary_validation": primary_validation.model_dump(),
            "repair": {
                "attempted": repair_attempted,
                "model_tier": self.repair_model_tier,
                "raw_response": self.last_repair_raw_response if repair_attempted else "",
                "validation": repair_validation.model_dump() if repair_validation else None,
            },
            "primary": {
                "model_tier": self.model_tier,
                "raw_response": self.last_raw_response,
                "usage": self.last_usage,
            },
            "repair_usage": self.last_repair_usage,
            "human_review": {
                "required": True,
                "instruction": "Review whether the English problem understanding, scope definition, and keyword decomposition match the real intent; edit the JSON if needed before confirmation.",
                "next_stage_after_confirmation": "retrieval_planning",
            },
            "note": note,
        }

    def _fallback_payload(self, question: str) -> dict[str, Any]:
        normalized_question = (
            question
            if question.isascii() and re.search(r"[A-Za-z]", question)
            else "Research question requiring English normalization during human review"
        )
        return {
            "input": {"user_query": normalized_question},
            "output": {
                "problem_understanding": (
                    "Reformulate the user's research question into an English scholarly search target, identifying the scientific object, "
                    "application context, core mechanisms, evaluation criteria, and likely evidence types."
                ),
                "scope_definition": {
                    "main_scope": (
                        "Focus on definitions, research background, key scientific mechanisms, mainstream technical routes, representative literature types, "
                        "evaluation metrics, known controversies, and research gaps; do not generate final designs or unsupported performance claims."
                    ),
                    "scope_items": [
                        "Concept definitions and research background",
                        "Key scientific mechanisms",
                        "Mainstream materials, structures, or methodological routes",
                        "Evaluation metrics and experimental or simulation conditions",
                        "Representative literature types and search entry points",
                        "Controversies, limitations, and follow-up evidence needs",
                    ],
                },
                "keyword_decomposition": {
                    "keywords": self._fallback_keywords(question),
                },
                "extra_notes": "",
            },
        }

    @staticmethod
    def _fallback_keywords(question: str) -> list[str]:
        compact = re.sub(r"\s+", " ", question).strip()
        base = [compact] if compact and compact.isascii() and re.search(r"[A-Za-z]", compact) else []
        optical_terms = [
            "optical thin film",
            "multilayer optical coating",
            "functional thin film",
            "spectral selective film",
            "optical coating",
            "multilayer thin film",
            "thin film optics",
            "TMM simulation",
            "materials and structure",
            "performance metrics",
            "review",
            "representative studies",
        ]
        return _clean_list(base + optical_terms, max_items=18)


__all__ = [
    "DEFAULT_QUERY_PLANNER_PROMPT_PATH",
    "EXPECTED_SCHEMA",
    "FormatValidationReport",
    "KeywordDecomposition",
    "QueryPlannerAgent",
    "QueryPlannerInput",
    "QueryPlannerOutput",
    "QueryPlannerResponse",
    "ScopeDefinition",
]
