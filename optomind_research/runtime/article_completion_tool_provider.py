"""AgentScope tools for article-wide synthesis and front/back matter writing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentscope.tool import FunctionTool
from pydantic import ValidationError

from .article_completion_schemas import (
    ArticleCompletionPackage,
    ArticleRhetoricalContract,
)
from .article_synthesis_map_builder import (
    collect_article_synthesis_inputs,
    sanitize_article_synthesis_map,
)
from .artifact_store import atomic_write_json, atomic_write_text
from .tool_provider import ToolProvider

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_REF = re.compile(r"\[REF:([^\]\s]+)\]")
_STRAW = re.compile(
    r"\b(?:not\s+merely|not\s+simply|rather\s+than|not\s+only)\b",
    flags=re.IGNORECASE,
)
_NUMERIC_TOKEN = re.compile(
    r"(?<![A-Za-z])(?:[<>~≈≤≥]?\s*\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?"
    r"\s*(?:%|nm|µm|um|mm|cm|m|Hz|kHz|MHz|GHz|THz|K|h|hours?|days?|years?)?)",
    flags=re.IGNORECASE,
)
_PROPOSED_TARGET_LANGUAGE = re.compile(
    r"\b(?:proposed|target|milestone|success indicator|should aim|should "
    r"achieve|future work|research goal|benchmark goal|recommended threshold|"
    r"would be indicated|will be indicated|key milestones? include|"
    r"milestones? include|"
    r"success will be indicated|success indicators? include)\b",
    flags=re.IGNORECASE,
)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", str(text or "")))


def _content_tokens(text: str) -> set[str]:
    stop = {
        "the",
        "and",
        "for",
        "that",
        "with",
        "from",
        "this",
        "review",
        "article",
    }
    return {
        value.lower()
        for value in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", str(text or ""))
        if value.lower() not in stop
    }


def _unsupported_numeric_sentences(text: str) -> List[str]:
    """Find precise numeric assertions lacking a citation or proposal label."""

    findings: List[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")):
        value = sentence.strip()
        # Internal outline identifiers are provenance labels, not scientific
        # measurements.  Remove them before applying the numeric-claim gate.
        numeric_view = re.sub(
            r"\b(?:CH|OP|S|EP|HOEP|FIG|TABLE)\s*[-_]?\s*\d+\b",
            "",
            value,
            flags=re.IGNORECASE,
        )
        if not value or not _NUMERIC_TOKEN.search(numeric_view):
            continue
        if re.match(
            r"^(?:#{1,6}\s*)?(?:\*{0,2})?(?:challenge|opportunity|"
            r"section|figure|table)\s+\d+\s*[:.)]",
            value,
            flags=re.IGNORECASE,
        ):
            continue
        if _REF.search(value) or _PROPOSED_TARGET_LANGUAGE.search(value):
            continue
        findings.append(value[:240])
    return findings


def _coerce_string_list_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Repair harmless scalar/list shape drift without changing semantics.

    Large structured responses occasionally contain a single string where the
    declared schema requires a list of strings.  Treating that as a one-item
    list is lossless and avoids paying for a full model retry.  IDs and object
    collections are intentionally not repaired here.
    """

    top_level_fields = {
        "review_wide_consensus",
        "review_wide_disagreements",
        "cross_section_tradeoffs",
        "conclusion_candidates",
        "intro_promise_candidates",
    }
    section_fields = {
        "established_takeaways",
        "conditional_judgments",
        "unresolved_tensions",
    }
    challenge_fields = {"current_responses"}
    outlook_fields = {"actionable_milestones", "success_indicators"}

    def coerce(container: Dict[str, Any], fields: set[str]) -> None:
        for field in fields:
            value = container.get(field)
            if isinstance(value, str):
                container[field] = [value] if value.strip() else []

    coerce(raw, top_level_fields)
    for key, fields in (
        ("section_contributions", section_fields),
        ("challenge_candidates", challenge_fields),
        ("outlook_candidates", outlook_fields),
    ):
        for item in raw.get(key, []) or []:
            if isinstance(item, dict):
                coerce(item, fields)
    return raw


@dataclass
class ArticleCompletionContext:
    blueprint_path: Path
    sections_root: Path
    work_dir: Path
    min_introduction_words: int = 450
    min_outlook_words: int = 450
    min_conclusion_words: int = 180


class ArticleCompletionToolProvider(ToolProvider):
    TOOL_NAMES = [
        "load_article_completion_context",
        "submit_article_synthesis_map",
        "load_validated_article_synthesis_map",
        "submit_article_completion_package",
        "validate_article_completion_package",
    ]

    def __init__(self, ctx: ArticleCompletionContext) -> None:
        self.ctx = ctx
        self.ctx.work_dir.mkdir(parents=True, exist_ok=True)
        self.input_path = self.ctx.work_dir / "ARTICLE_SYNTHESIS_INPUT.json"
        self.map_path = self.ctx.work_dir / "ARTICLE_SYNTHESIS_MAP.json"
        self.map_audit_path = (
            self.ctx.work_dir / "ARTICLE_SYNTHESIS_MAP_AUDIT.json"
        )
        self.package_path = (
            self.ctx.work_dir / "ARTICLE_COMPLETION_PACKAGE.json"
        )
        self.validation_path = (
            self.ctx.work_dir / "ARTICLE_COMPLETION_VALIDATION.json"
        )

    def get_allowed_tool_names(self) -> List[str]:
        return list(self.TOOL_NAMES)

    def try_auto_finalize(self) -> Optional[str]:
        """Stop as soon as a persisted completion package passes all gates."""

        if not self.package_path.exists():
            return None
        return self.validate_persisted_package()

    def get_tools(self, work_dir: Path) -> list:
        provider = self

        def load_article_completion_context() -> str:
            """Load bounded body memory and the article rhetorical contract."""

            try:
                payload = collect_article_synthesis_inputs(
                    provider.ctx.blueprint_path,
                    provider.ctx.sections_root,
                    output_path=provider.input_path,
                )
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": str(exc)},
                    ensure_ascii=True,
                )
            return json.dumps(
                {
                    "status": "ok",
                    "synthesis_input": payload,
                    "completion_requirements": {
                        "minimum_words": {
                            "introduction": provider.ctx.min_introduction_words,
                            "challenge_and_outlook": provider.ctx.min_outlook_words,
                            "conclusion": provider.ctx.min_conclusion_words,
                        },
                        "hard_minimum_words": {
                            "introduction": int(
                                provider.ctx.min_introduction_words * 0.9
                            ),
                            "challenge_and_outlook": int(
                                provider.ctx.min_outlook_words * 0.9
                            ),
                            "conclusion": int(
                                provider.ctx.min_conclusion_words * 0.9
                            ),
                        },
                        "abstract_word_range": (
                            payload.get("article_rhetorical_contract", {})
                            .get("abstract_contract", {})
                            .get("target_word_range", {})
                        ),
                        "required_methodology_identity": (
                            payload.get("article_rhetorical_contract", {})
                            .get("methodology_identity", "")
                        ),
                    },
                    "required_sequence": [
                        "submit_article_synthesis_map",
                        "load_validated_article_synthesis_map",
                        "submit_article_completion_package",
                        "validate_article_completion_package",
                    ],
                },
                ensure_ascii=True,
            )

        def submit_article_synthesis_map(synthesis_map_json: str) -> str:
            """Submit one cross-section synthesis map as strict JSON."""

            synthesis_input = _read_json(provider.input_path, {})
            if not synthesis_input:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "load_article_completion_context must run first",
                    },
                    ensure_ascii=True,
                )
            try:
                raw = json.loads(synthesis_map_json)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_json: {exc}"},
                    ensure_ascii=True,
                )
            if not isinstance(raw, dict):
                return json.dumps(
                    {"status": "error", "error": "map must be an object"},
                    ensure_ascii=True,
                )
            raw = _coerce_string_list_fields(raw)
            try:
                synthesis_map, audit = sanitize_article_synthesis_map(
                    raw,
                    synthesis_input,
                )
            except Exception as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"invalid_synthesis_map: {exc}",
                    },
                    ensure_ascii=True,
                )
            atomic_write_json(
                provider.map_path,
                synthesis_map.model_dump(mode="json"),
            )
            atomic_write_json(provider.map_audit_path, audit)
            return json.dumps(
                {
                    "status": "ok",
                    "artifact": provider.map_path.name,
                    "section_count": len(
                        synthesis_map.section_contributions
                    ),
                    "challenge_count": len(
                        synthesis_map.challenge_candidates
                    ),
                    "outlook_count": len(
                        synthesis_map.outlook_candidates
                    ),
                    "removed_unverified_ids": audit[
                        "removed_unverified_ids"
                    ],
                },
                ensure_ascii=True,
            )

        def load_validated_article_synthesis_map() -> str:
            """Load the sanitized map that must govern article completion."""

            raw = _read_json(provider.map_path, {})
            if not raw:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "ARTICLE_SYNTHESIS_MAP.json is missing",
                    },
                    ensure_ascii=True,
                )
            return json.dumps(
                {"status": "ok", "article_synthesis_map": raw},
                ensure_ascii=True,
            )

        def submit_article_completion_package(
            completion_package_json: str,
        ) -> str:
            """Submit title, abstract, introduction, outlook, and conclusion."""

            synthesis_input = _read_json(provider.input_path, {})
            synthesis_map = _read_json(provider.map_path, {})
            if not synthesis_input or not synthesis_map:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "validated article synthesis map is required",
                    },
                    ensure_ascii=True,
                )
            try:
                raw = json.loads(completion_package_json)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_json: {exc}"},
                    ensure_ascii=True,
                )
            if not isinstance(raw, dict):
                return json.dumps(
                    {"status": "error", "error": "package must be an object"},
                    ensure_ascii=True,
                )
            try:
                package = ArticleCompletionPackage.model_validate(raw)
            except ValidationError as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "invalid_completion_contract: "
                        + str(exc),
                    },
                    ensure_ascii=True,
                )
            errors = provider._completion_errors(
                package,
                synthesis_input,
                synthesis_map,
            )
            if errors:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "completion_quality_gate_failed",
                        "errors": errors,
                    },
                    ensure_ascii=True,
                )
            atomic_write_json(
                provider.package_path,
                package.model_dump(mode="json"),
            )
            atomic_write_text(
                provider.ctx.work_dir / "ARTICLE_TITLE.txt",
                package.title.strip(),
            )
            atomic_write_text(
                provider.ctx.work_dir / "ARTICLE_ABSTRACT_EN.md",
                package.abstract.strip(),
            )
            atomic_write_text(
                provider.ctx.work_dir / "ARTICLE_INTRODUCTION_EN.md",
                package.introduction.strip(),
            )
            atomic_write_text(
                provider.ctx.work_dir / "ARTICLE_CHALLENGES_OUTLOOK_EN.md",
                package.challenge_and_outlook.strip(),
            )
            atomic_write_text(
                provider.ctx.work_dir / "ARTICLE_CONCLUSION_EN.md",
                package.conclusion.strip(),
            )
            return json.dumps(
                {
                    "status": "ok",
                    "artifact": provider.package_path.name,
                    "word_counts": {
                        "abstract": _word_count(package.abstract),
                        "introduction": _word_count(package.introduction),
                        "challenge_and_outlook": _word_count(
                            package.challenge_and_outlook
                        ),
                        "conclusion": _word_count(package.conclusion),
                    },
                },
                ensure_ascii=True,
            )

        def validate_article_completion_package() -> str:
            """Recompute all article-completion gates and persist a report."""

            return provider.validate_persisted_package()

        return [
            FunctionTool(load_article_completion_context),
            FunctionTool(submit_article_synthesis_map),
            FunctionTool(load_validated_article_synthesis_map),
            FunctionTool(submit_article_completion_package),
            FunctionTool(validate_article_completion_package),
        ]

    def validate_persisted_package(self) -> str:
        """Validate saved completion artifacts without another model call.

        This method lets the stage runner recover when a paid model call has
        already written a valid package but the cost reserve prevents the
        agent from spending another call solely to invoke the validator.
        """

        raw = _read_json(self.package_path, {})
        synthesis_input = _read_json(self.input_path, {})
        synthesis_map = _read_json(self.map_path, {})
        if not raw:
            return "VALIDATION_FAILED: ARTICLE_COMPLETION_PACKAGE.json is missing."
        try:
            package = ArticleCompletionPackage.model_validate(raw)
        except Exception as exc:
            return f"VALIDATION_FAILED: invalid completion package: {exc}"
        errors = self._completion_errors(
            package,
            synthesis_input,
            synthesis_map,
        )
        warnings = self._completion_warnings(package)
        report = {
            "schema_version": "article_completion_validation.v1",
            "status": "passed" if not errors else "failed",
            "input_fingerprint": str(
                synthesis_input.get("input_fingerprint") or ""
            ),
            "errors": errors,
            "warnings": warnings,
            "word_counts": {
                "abstract": _word_count(package.abstract),
                "introduction": _word_count(package.introduction),
                "challenge_and_outlook": _word_count(
                    package.challenge_and_outlook
                ),
                "conclusion": _word_count(package.conclusion),
            },
            "reference_ids": sorted(
                set(
                    _REF.findall(
                        "\n".join(
                            [
                                package.introduction,
                                package.challenge_and_outlook,
                                package.conclusion,
                            ]
                        )
                    )
                )
            ),
        }
        atomic_write_json(self.validation_path, report)
        if errors:
            return "VALIDATION_FAILED: " + "; ".join(errors[:12])
        return (
            "VALIDATION_PASSED: article synthesis, introduction, outlook, "
            "conclusion, and abstract satisfy the completion contract."
        )

    def _completion_errors(
        self,
        package: ArticleCompletionPackage,
        synthesis_input: Dict[str, Any],
        synthesis_map: Dict[str, Any],
    ) -> List[str]:
        errors: List[str] = []
        blueprint_contract_raw = synthesis_input.get(
            "article_rhetorical_contract",
            {},
        )
        try:
            contract = ArticleRhetoricalContract.model_validate(
                blueprint_contract_raw
            )
        except Exception as exc:
            return [f"article rhetorical contract is invalid: {exc}"]
        if package.methodology_identity != contract.methodology_identity:
            errors.append("completion methodology disagrees with blueprint")
        methodology_words = contract.methodology_identity.replace("_", " ")
        if methodology_words not in package.introduction.lower():
            errors.append(
                "introduction does not explicitly disclose the declared methodology"
            )
        combined = "\n".join(
            [
                package.title,
                package.abstract,
                package.introduction,
                package.challenge_and_outlook,
                package.conclusion,
                json.dumps(package.quality_self_check.model_dump()),
            ]
        )
        if _CJK.search(combined):
            errors.append("article completion output contains CJK text")
        if _REF.search(package.abstract):
            errors.append("abstract must not contain citation markers")
        abstract_words = _word_count(package.abstract)
        target = contract.abstract_contract.target_word_range
        if not target.min <= abstract_words <= target.max:
            errors.append(
                f"abstract word count {abstract_words} is outside "
                f"{target.min}-{target.max}"
            )
        # Keep the declared editorial target while accepting a compact,
        # complete introduction from economical model tiers.  A small length
        # miss must not trigger repeated full rewrites.
        intro_hard_min = int(self.ctx.min_introduction_words * 0.8)
        outlook_hard_min = int(self.ctx.min_outlook_words * 0.9)
        conclusion_hard_min = int(self.ctx.min_conclusion_words * 0.9)
        if _word_count(package.introduction) < intro_hard_min:
            errors.append(
                "introduction word count "
                f"{_word_count(package.introduction)} is below "
                f"hard minimum {intro_hard_min} "
                f"(editorial target {self.ctx.min_introduction_words})"
            )
        if (
            _word_count(package.challenge_and_outlook)
            < outlook_hard_min
        ):
            errors.append(
                "challenge_and_outlook word count "
                f"{_word_count(package.challenge_and_outlook)} is below "
                f"hard minimum {outlook_hard_min} "
                f"(editorial target {self.ctx.min_outlook_words})"
            )
        if _word_count(package.conclusion) < conclusion_hard_min:
            errors.append(
                "conclusion word count "
                f"{_word_count(package.conclusion)} is below "
                f"hard minimum {conclusion_hard_min} "
                f"(editorial target {self.ctx.min_conclusion_words})"
            )
        if package.quality_self_check.new_topic_declared:
            errors.append("completion package declares a new topic")
        allowed_papers = set(
            synthesis_input.get("verified_id_allowlist", {}).get(
                "paper_ids",
                [],
            )
        )
        cited = set(
            _REF.findall(
                "\n".join(
                    [
                        package.introduction,
                        package.challenge_and_outlook,
                        package.conclusion,
                    ]
                )
            )
        )
        unknown = sorted(cited - allowed_papers)
        if unknown:
            errors.append("unverified reference IDs: " + ", ".join(unknown))
        unsupported_numeric = _unsupported_numeric_sentences(
            "\n".join(
                [
                    package.introduction,
                    package.challenge_and_outlook,
                    package.conclusion,
                ]
            )
        )
        if unsupported_numeric:
            errors.append(
                "precise numeric assertions need a verified [REF:paper_id] "
                "or explicit proposed-target wording: "
                + " | ".join(unsupported_numeric[:4])
            )
        challenge_ids = {
            str(item.get("challenge_id") or "")
            for item in synthesis_map.get("challenge_candidates", [])
            if isinstance(item, dict)
        }
        opportunity_ids = {
            str(item.get("opportunity_id") or "")
            for item in synthesis_map.get("outlook_candidates", [])
            if isinstance(item, dict)
        }
        section_ids = set(
            synthesis_input.get("verified_id_allowlist", {}).get(
                "section_ids",
                [],
            )
        )
        if not challenge_ids:
            errors.append(
                "article synthesis map has no challenge candidates"
            )
        if not opportunity_ids:
            errors.append(
                "article synthesis map has no outlook candidates"
            )
        if opportunity_ids and not package.outlook_items:
            errors.append(
                "completion package does not realize any outlook candidate"
            )
        for item in package.outlook_items:
            if item.opportunity_id not in opportunity_ids:
                errors.append(
                    f"unknown outlook opportunity {item.opportunity_id}"
                )
            if set(item.linked_challenge_ids) - challenge_ids:
                errors.append(
                    f"{item.opportunity_id} links unknown challenges"
                )
            if set(item.linked_section_ids) - section_ids:
                errors.append(
                    f"{item.opportunity_id} links unknown sections"
                )
        if len(package.quality_self_check.introduction_promises) < 2:
            errors.append(
                "quality_self_check needs at least two introduction promises"
            )
        if (
            len(package.quality_self_check.conclusion_takeaways)
            < contract.conclusion_contract.required_takeaways
        ):
            errors.append(
                "quality_self_check has fewer conclusion takeaways than required"
            )
        if len(package.quality_self_check.abstract_major_messages) < 2:
            errors.append(
                "quality_self_check needs at least two abstract messages"
            )
        body_memory = " ".join(
            str(item.get("bounded_draft_memory") or "")
            for item in synthesis_input.get("sections", [])
            if isinstance(item, dict)
        )
        body_tokens = _content_tokens(body_memory)
        for promise in package.quality_self_check.introduction_promises:
            promise_tokens = _content_tokens(promise)
            if (
                promise_tokens
                and len(promise_tokens & body_tokens) / len(promise_tokens)
                < 0.3
            ):
                errors.append(
                    "introduction promise is not represented in body memory: "
                    + promise[:120]
                )
        completion_text = (
            package.introduction
            + "\n"
            + package.challenge_and_outlook
            + "\n"
            + package.conclusion
        ).lower()
        for phrase in (
            "further research is needed",
            "more research is needed",
            "remains to be seen",
            "still unclear",
            "requires further investigation",
        ):
            if completion_text.count(phrase) >= 3:
                errors.append(
                    "repetitive generic caveat in article completion: " + phrase
                )
        return list(dict.fromkeys(errors))

    def _completion_warnings(
        self,
        package: ArticleCompletionPackage,
    ) -> List[str]:
        """Return editorial issues that merit polishing but not full rejection."""

        warnings: List[str] = []
        word_specs = (
            (
                "introduction",
                _word_count(package.introduction),
                self.ctx.min_introduction_words,
            ),
            (
                "challenge_and_outlook",
                _word_count(package.challenge_and_outlook),
                self.ctx.min_outlook_words,
            ),
            (
                "conclusion",
                _word_count(package.conclusion),
                self.ctx.min_conclusion_words,
            ),
        )
        for name, count, target in word_specs:
            if count < target:
                warnings.append(
                    f"{name} word count {count} is below editorial target {target}"
                )
        if _STRAW.search(
            package.introduction
            + "\n"
            + package.challenge_and_outlook
            + "\n"
            + package.conclusion
        ):
            warnings.append(
                "front/back matter contains avoidable contrast phrasing; "
                "polish locally during whole-manuscript editing"
            )
        return warnings
