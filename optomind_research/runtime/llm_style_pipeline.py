"""Three-role LLM style governance with deterministic hard acceptance.

Roles (P1-1):

  A style-critic    finds style issues; input is the whole chapter text plus
                    its measured paragraph-opener distribution -- never a
                    hardcoded banned-word table (measured data aims at the
                    real offenders; a fixed list aims at folklore).
  B style-rewriter  rewrites ONE paragraph per call, first sentence and
                    subject phrasing only.
  C style-reviewer  optional second opinion; default off because the hard
                    verifier already guards facts and a reviewer doubles cost.

Local code owns everything deterministic: fact acceptance lives in
style_hard_verifier, fallback keeps the original paragraph, and style items
never enter blocking_issues.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cost_ledger import estimate_call_cost_cny
from .review_content_evaluator import (
    _OPENER_WORD,
    _WORD,
    abstract_subject_hits,
    paragraph_opener_distribution,
)
from .style_hard_verifier import verify_rewrite

from llm.qwen_chat_client import call_qwen_chat

_PARA_SPLIT = re.compile(r"(\n\s*\n)")
_ALLOWED_ISSUE_TYPES = {
    "template_opener",
    "abstract_subject",
    "repetitive_summary",
    # P2 (round 3): the measured strawman_not_but_count in every quality
    # report showed this construction surviving to final text.  The critic
    # may now flag it so the rewriter can fix it under the same fact guard.
    "strawman_not_but",
}

_CRITIC_SYSTEM = (
    "你是学术综述的风格审稿人。只判断风格，绝不改写文本。\n"
    '输出严格 JSON：{"issues":[{"paragraph_index":整数,'
    '"issue_type":"template_opener|abstract_subject|repetitive_summary|strawman_not_but",'
    '"severity":"high|medium|low","evidence":"...","suggestion":"..."}]}\n'
    "规则：\n"
    "· 结合给出的段首词分布，找出同一开头词重复 ≥2 次的段落；\n"
    "· 找出主语抽象的段落（the field / this approach / these approaches /\n"
    "  the landscape / researchers have / the trade-off / this paradigm /\n"
    "  the community / is not merely 之类）；\n"
    "· 不要建议改动任何数字、引用、单位、公式；\n"
    "· 不确定的段落不要输出。"
)

_REWRITER_SYSTEM = (
    "你是学术综述的段落改写器。一次只改一段，且只改首句、主语措辞或被标记的 not-but 句式。\n"
    "五条硬规则：\n"
    "1. 只改首句、主语措辞，或把被标记的 not-but 句式改成直接陈述，其余句子尽量保持原样；\n"
    "2. 逐字保留所有 [REF:...] 标记、数字、单位、公式、限定词（may / might /"
    "approximately 等）；\n"
    "3. 不得新增任何数字或引用；\n"
    "4. 不得改变论断强度（不得把 may improve 写成 improves）；\n"
    "5. 长度保持在原文 ±30% 内。\n"
    "输出：改写后的段落纯文本，不要任何解释或代码块。"
)

_REVIEWER_SYSTEM = (
    "复核一次段落改写。输出严格 JSON：\n"
    '{"improved":bool,"faithful":bool,"note":"..."}\n'
    "improved：风格是否真的改善；faithful：事实是否无失真。"
)


def load_protected_terms(
    run_dir: Optional[Path] = None,
    *,
    ledger_path: Optional[Path] = None,
) -> List[str]:
    """Read the full-review terminology ledger when present; otherwise [].

    Inventing protected terms would make legitimate rewrites fail hard
    verification, so an unreadable ledger must yield an empty list.
    """

    ledger = Path(ledger_path) if ledger_path is not None else (
        Path(run_dir) / "authoring" / "full_review" / "TERMINOLOGY_LEDGER.json"
        if run_dir else None
    )
    if ledger is None:
        return []
    if not ledger.exists():
        return []
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except Exception:
        return []
    terms: List[str] = []

    def _walk(node: Any, *, mapping_keys: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                # Older full-review ledgers persist the canonical vocabulary as
                # ``{"terms": {"ODNN": "acronym (S05)"}}`` rather than as
                # records with a ``term`` field.  Those keys are still the
                # protected scientific terms; ignoring them makes the style
                # stage fail open before it can do any useful work.
                if (
                    mapping_keys
                    and key not in ("term", "canonical_term", "canonical")
                    and isinstance(key, str)
                    and key.strip()
                ):
                    terms.append(key)
                if key in ("term", "canonical_term", "canonical") and isinstance(
                    value, str
                ):
                    terms.append(value)
                else:
                    _walk(value, mapping_keys=key == "terms")
        elif isinstance(node, list):
            for item in node:
                _walk(item, mapping_keys=mapping_keys)

    _walk(data)
    return sorted({term.strip() for term in terms if term.strip()})


_FENCE = chr(96) * 3


def _safe_json(text: str) -> Dict[str, Any]:
    """Parse a JSON object out of an LLM reply, tolerating markdown fences."""

    cleaned = str(text or "").strip()
    if cleaned.startswith(_FENCE):
        cleaned = cleaned[len(_FENCE):].lstrip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        end = cleaned.rfind(_FENCE)
        if end != -1:
            cleaned = cleaned[:end]
    try:
        value = json.loads(cleaned.strip())
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    # Flash-tier replies intermittently carry a prose preamble or fences;
    # falling back to the outermost braces keeps those from silently
    # degrading into "no findings".
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(cleaned[start:end + 1])
            if isinstance(value, dict):
                return value
        except Exception:
            return {}
    return {}


def _is_long_block(block: str) -> bool:
    """Same qualification as review_content_evaluator._paragraphs."""

    return len(_WORD.findall(block)) >= 25 and not block.startswith("#")


def _call_llm(agent_name: str, system: str, user_payload: Any, *, json_mode: bool):
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": user_payload
            if isinstance(user_payload, str)
            else json.dumps(user_payload, ensure_ascii=False),
        },
    ]
    return call_qwen_chat(
        agent_name,
        messages,
        model_tier="c2_model",
        temperature=0.2,
        max_tokens=4000,
        response_format={"type": "json_object"} if json_mode else None,
        stream=False,
        timeout_seconds=180,
        allow_model_fallback=True,
        enable_thinking=False,
        force_mock=False,
        max_retries=1,
    )


def _spend_of(result: Dict[str, Any]) -> float:
    usage = (result or {}).get("_llm_usage") or {}
    return estimate_call_cost_cny(
        str(usage.get("model_name") or ""),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    )


def run_style_pipeline(
    review_text: str,
    *,
    enabled: bool = True,
    review_enabled: bool = False,
    cost_budget_cny: float = 0.05,
    max_rewrites: Optional[int] = None,
    protected_terms: Optional[List[str]] = None,
    candidate_issues: Optional[List[Dict[str, Any]]] = None,
    llm_call=None,
) -> Dict[str, Any]:
    """Rewrite flagged paragraphs one at a time under hard fact acceptance.

    Returns a STYLE_REWRITE_REPORT dict; review_text carries the new full
    text (byte-identical to the input when enabled is False).
    """

    do_call = llm_call or _call_llm
    long_paragraphs = [
        block.strip()
        for block in _PARA_SPLIT.split(review_text)
        if _is_long_block(block)
    ]
    distribution_before = paragraph_opener_distribution(long_paragraphs)
    report: Dict[str, Any] = {
        "schema_version": "optomind.style_rewrite_report.v1",
        "enabled": bool(enabled),
        "review_enabled": bool(review_enabled),
        "review_text": review_text,
        "paragraphs_examined": len(long_paragraphs),
        "issues_found": 0,
        "rewrites_attempted": 0,
        "rewrites_accepted": 0,
        "rewrites_rejected": 0,
        "rejections": [],
        "opener_distribution_before": distribution_before,
        "opener_distribution_after": distribution_before,
        "abstract_subject_hits": abstract_subject_hits(review_text),
        "estimated_cost_cny": 0.0,
        # 红线：风格问题永远不进 blocking_issues；这里按构造恒为空。
        "blocking_issues": [],
        "warnings": [],
        "budget_exhausted": False,
        "critic_error": "",
        "accepted_paragraphs": [],
    }
    if not enabled:
        return report

    terms = list(protected_terms or [])
    critic_result = do_call(
        "StyleCritic",
        _CRITIC_SYSTEM,
        {
            "chapter_text": review_text,
            "opener_distribution": distribution_before,
            "long_paragraph_count": len(long_paragraphs),
            "instructions": [
                "找出同一开头词重复 ≥2 次的段落",
                "找出主语抽象的段落",
                "找出 is/are not ... but ... 式的稻草人句式（strawman_not_but）",
                "不要建议改动任何数字、引用、单位、公式",
            ],
        },
        json_mode=True,
    )
    report["estimated_cost_cny"] += _spend_of(critic_result)
    parsed = _safe_json(str((critic_result or {}).get("content") or ""))
    if not parsed:
        report["critic_error"] = "critic_output_not_json"
    issues: List[Dict[str, Any]] = []

    def _admit(rows: Any) -> None:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            index = row.get("paragraph_index")
            if not isinstance(index, int) or not 0 <= index < len(long_paragraphs):
                continue
            if str(row.get("issue_type") or "") not in _ALLOWED_ISSUE_TYPES:
                continue
            issues.append(row)

    # Measured candidates lead, critic findings follow.  The critic is handed
    # the opener distribution but answers in free form, and on the real corpus
    # that meant 11 paragraphs flagged, 5 acted on, and ``While`` still sitting
    # at 21/75 afterwards.  The queue is what actually drives the share down;
    # the critic still runs because abstract_subject and strawman_not_but are
    # invisible to an opener histogram.  The rewrite loop below dedupes by
    # paragraph index, so overlap between the two costs nothing.
    _admit(candidate_issues)
    _admit(parsed.get("issues"))
    report["candidate_issues_supplied"] = len(list(candidate_issues or []))
    report["issues_found"] = len(issues)
    report["warnings"] = sorted({str(row["issue_type"]) for row in issues})

    replacements: list = []
    seen_indexes: set = set()
    for issue in issues:
        index = int(issue["paragraph_index"])
        if index in seen_indexes:
            continue
        if max_rewrites is not None and report["rewrites_attempted"] >= max_rewrites:
            break
        if report["estimated_cost_cny"] >= cost_budget_cny:
            # 超预算即停；已接受的改写保留（硬约束 4）。
            report["budget_exhausted"] = True
            break
        original = long_paragraphs[index]
        seen_indexes.add(index)
        report["rewrites_attempted"] += 1
        rewrite_result = do_call(
            "StyleRewriter",
            _REWRITER_SYSTEM,
            {
                "paragraph": original,
                "issues": [issue],
                "protected_terms": terms,
            },
            json_mode=False,
        )
        report["estimated_cost_cny"] += _spend_of(rewrite_result)
        candidate = str((rewrite_result or {}).get("content") or "").strip()
        if not candidate or candidate == original.strip():
            report["rewrites_rejected"] += 1
            report["rejections"].append(
                {"paragraph_index": index, "violations": [{"check": "no_change"}]}
            )
            continue
        verdict = verify_rewrite(
            original=original,
            rewritten=candidate,
            protected_terms=terms,
        )
        if not verdict["ok"]:
            # 硬验收失败 → 丢弃改写、保留原文、记录 violations（硬约束 2）。
            report["rewrites_rejected"] += 1
            report["rejections"].append(
                {"paragraph_index": index, "violations": verdict["violations"]}
            )
            continue
        if review_enabled:
            review_result = do_call(
                "StyleReviewer",
                _REVIEWER_SYSTEM,
                {"original": original, "rewritten": candidate},
                json_mode=True,
            )
            report["estimated_cost_cny"] += _spend_of(review_result)
            review = _safe_json(str((review_result or {}).get("content") or ""))
            if not (
                review.get("improved") is True and review.get("faithful") is True
            ):
                report["rewrites_rejected"] += 1
                report["rejections"].append(
                    {
                        "paragraph_index": index,
                        "violations": [{"check": "reviewer_rejected"}],
                    }
                )
                continue
        report["rewrites_accepted"] += 1
        report["accepted_paragraphs"].append(index)
        replacements.append([index, candidate])

    if replacements:
        segments = _PARA_SPLIT.split(review_text)
        cursor = -1
        replace_map = {int(index): cand for index, cand in replacements}
        for position, segment in enumerate(segments):
            if position % 2 == 1:
                continue  # separator slot -- kept byte-identical
            if _is_long_block(segment):
                cursor += 1
                if cursor in replace_map:
                    segments[position] = replace_map[cursor]
        text_out = "".join(segments)
        after_paragraphs = [
            block.strip()
            for block in _PARA_SPLIT.split(text_out)
            if _is_long_block(block)
        ]
        report["review_text"] = text_out
        report["opener_distribution_after"] = paragraph_opener_distribution(
            after_paragraphs
        )
        report["abstract_subject_hits"] = abstract_subject_hits(text_out)
    return report


# ---------------------------------------------------------------------------
# P1-1 (round 3): distribution-driven convergence driver.
#
# The historical one-off P11 script hard-selected the "first five flagged
# paragraphs whose opener sat in a locked six-word table"; that selection
# rule never entered this module and is now explicitly retired.  Selection is
# fully distribution-driven: every long paragraph whose current opener is
# over-represented (>=5 occurrences or >=5% share) joins the candidate queue,
# ordered by descending occurrence, and the pipeline iterates until the
# paragraph-opener max share converges (<=0.12) or the budget is spent.

_STYLE_MAX_SHARE_TARGET = 0.12
_STYLE_OVER_REPRESENTED_COUNT = 5
_STYLE_OVER_REPRESENTED_SHARE = 0.05
_STYLE_DEFAULT_BUDGET_CNY = 0.50
_STYLE_DEFAULT_MAX_REWRITES = 60

_TEMPLATE_OPENER_WORDS = {
    "while", "building", "overall", "however", "moreover", "furthermore",
    "additionally", "therefore", "consequently", "nevertheless", "thus",
    "meanwhile", "similarly", "conversely", "specifically", "notably",
}
_BUILDING_ON_PREFIX = "building on"
_WHILE_PREFIX = "while"

# The model stage is intentionally best-effort and budgeted.  These local
# rewrites are the deterministic publication guard: they only alter a
# connective or a generic subject, and they are still passed through the same
# fact-bearing token verifier before they can reach the manuscript.  Keeping
# this guard outside the paid loop means a model fixed point cannot ship an
# obviously repetitive final draft.
_STRAW_MAN_RE = re.compile(
    r"(?P<verb>\b(?:is|are|was|were|should be|can be))\s+not\s+"
    r"(?P<qualifier>(?:(?:merely|only|just|simply)\s+)?)"
    r"(?P<left>[^.;!?\n]{1,100}?)\s*,?\s+but(?:\s+also)?\s+",
    re.IGNORECASE,
)
_WHILE_LEADING_RE = re.compile(
    r"^While\s+(?P<lead>[^,.;!?\n]{3,240}),\s+"
    r"(?P<main>[^.!?\n]+[.!?])(?P<tail>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_BUILDING_LEADING_RE = re.compile(
    r"^Building(?:\s+on|\s+upon)\s+(?P<lead>[^,.;!?\n]{3,240}),\s+"
    r"(?P<main>[^.!?\n]+[.!?])(?P<tail>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_BUILDING_OPENER_VARIANTS = (
    "Drawing on",
    "Drawing from",
    "Grounded in",
    "Extending from",
    "Starting from",
    "Based on",
    "Informed by",
    "Guided by",
    "Following",
    "On the basis of",
    "With",
)
_ABSTRACT_SUBJECT_RE = re.compile(
    r"\b(?:the field|this approach|these approaches|the landscape|"
    r"researchers have|the trade-off|this paradigm|the community)\b",
    re.IGNORECASE,
)
_ABSTRACT_SUBJECT_REPLACEMENTS = {
    "the field": ("ODNN research",),
    "this approach": (
        "the resulting design",
        "the optical architecture",
        "that implementation",
        "the same strategy",
        "this design",
        "the proposed network",
        "the configuration",
    ),
    "these approaches": ("these designs",),
    "the landscape": ("the evidence base",),
    "researchers have": (
        "published studies have",
        "experimental reports have",
        "recent studies have",
        "multiple studies have",
    ),
    "the trade-off": ("the balance",),
    "this paradigm": ("this framework",),
    "the community": ("research groups",),
}


def _match_case(original: str, replacement: str) -> str:
    """Keep sentence-initial capitalization while replacing a generic phrase."""

    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _capitalize_first_letter(value: str) -> str:
    return re.sub(
        r"^(\s*[^A-Za-z]*)([A-Za-z])",
        lambda match: match.group(1) + match.group(2).upper(),
        value,
        count=1,
    )


def _lower_first_letter(value: str) -> str:
    # Keep acronyms such as ODNNs intact when a subordinate clause is moved
    # after "even though".
    if re.match(r"^[A-Z]{2,}[A-Z0-9]*\b", value):
        return value
    return re.sub(
        r"^([A-Z])",
        lambda match: match.group(1).lower(),
        value,
        count=1,
    )


def _rewrite_strawman_not_but(value: str) -> tuple[str, int]:
    """Turn the locked ``not merely X but Y`` pattern into ``X and Y``."""

    replacements = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal replacements
        left = str(match.group("left") or "").strip()
        if not left:
            return match.group(0)
        replacements += 1
        return f"{match.group('verb')} {left} and "

    return _STRAW_MAN_RE.sub(replace, value), replacements


def _rewrite_abstract_subjects(
    value: str,
    counters: dict[str, int],
) -> tuple[str, int]:
    replacements = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal replacements
        phrase = match.group(0).casefold()
        options = _ABSTRACT_SUBJECT_REPLACEMENTS.get(phrase)
        if not options:
            return match.group(0)
        offset = counters.get(phrase, 0)
        counters[phrase] = offset + 1
        replacements += 1
        return _match_case(
            match.group(0), options[offset % len(options)]
        )

    return _ABSTRACT_SUBJECT_RE.sub(replace, value), replacements


def _rewrite_while_opener(value: str) -> tuple[str, int]:
    """Move a concessive ``While A, B.`` opener to ``A; however, B.``."""

    def replace(match: re.Match[str]) -> str:
        lead = _capitalize_first_letter(
            str(match.group("lead") or "").strip()
        )
        main = str(match.group("main") or "").strip()
        if not lead or not main or main[-1] not in ".!?":
            return match.group(0)
        punctuation = main[-1]
        main_core = _lower_first_letter(main[:-1].rstrip())
        if lead.casefold().startswith("the "):
            return (
                f"Given that {_lower_first_letter(lead)}, however, "
                f"{main_core}{punctuation}{match.group('tail')}"
            )
        return (
            f"{lead}; however, {main_core}{punctuation}"
            f"{match.group('tail')}"
        )

    rewritten, count = _WHILE_LEADING_RE.subn(replace, value, count=1)
    return rewritten, count


def _rewrite_building_on_opener(
    value: str,
    occurrence: int,
) -> tuple[str, int, int]:
    """Diversify repeated ``Building on/upon A, B.`` openers.

    The lead clause is retained verbatim and only the discourse connective is
    varied.  This keeps the repair mechanical and lets the hard verifier
    reject it if a paragraph contains any protected scientific token that is
    accidentally changed.
    """

    def replace(match: re.Match[str]) -> str:
        variant = _BUILDING_OPENER_VARIANTS[occurrence % len(_BUILDING_OPENER_VARIANTS)]
        lead = str(match.group("lead") or "").strip()
        main = str(match.group("main") or "").strip()
        if not lead or not main:
            return match.group(0)
        tail = str(match.group("tail") or "")
        if variant == "With":
            return f"With {lead} as a foundation, {main}{tail}"
        return f"{variant} {lead}, {main}{tail}"

    rewritten, count = _BUILDING_LEADING_RE.subn(replace, value, count=1)
    return rewritten, count, occurrence + count


def style_opener_metrics(review_text: str) -> Dict[str, Any]:
    """Measured opener metrics used before/after reporting (P1-1)."""

    paragraphs = [
        block.strip()
        for block in _PARA_SPLIT.split(review_text or "")
        if _is_long_block(block)
    ]
    total = len(paragraphs)
    distribution = paragraph_opener_distribution(paragraphs)
    template_hits = sum(
        count
        for opener, count in distribution.items()
        if str(opener).lower() in _TEMPLATE_OPENER_WORDS
    )
    while_count = sum(
        1
        for paragraph in paragraphs
        if paragraph.lower().startswith(_WHILE_PREFIX)
    )
    building_count = sum(
        1
        for paragraph in paragraphs
        if paragraph.lower().startswith(_BUILDING_ON_PREFIX)
    )
    max_share = (
        round(max(distribution.values()) / total, 4) if total else 0.0
    )
    return {
        "paragraphs_total": total,
        "template_opener_share": (
            round(template_hits / total, 4) if total else 0.0
        ),
        "template_opener_hits": template_hits,
        "paragraph_opener_max_share": max_share,
        "paragraph_opener_top": (
            max(distribution.items(), key=lambda kv: kv[1])
            if distribution
            else None
        ),
        "while_paragraphs": while_count,
        "building_on_paragraphs": building_count,
        "opener_distribution": distribution,
    }


def _over_represented_openers(
    review_text: str,
) -> List[Dict[str, Any]]:
    """Distribution-driven candidate queue (no locked table, no first-N)."""

    paragraphs = [
        block.strip()
        for block in _PARA_SPLIT.split(review_text or "")
        if _is_long_block(block)
    ]
    total = len(paragraphs)
    if not total:
        return []
    counts = paragraph_opener_distribution(paragraphs)
    hot = sorted(
        (
            {"opener": opener, "count": count}
            for opener, count in counts.items()
            if count >= _STYLE_OVER_REPRESENTED_COUNT
            or (count / total) >= _STYLE_OVER_REPRESENTED_SHARE
        ),
        key=lambda row: (-row["count"], row["opener"]),
    )
    queue: List[Dict[str, Any]] = []
    for row in hot:
        for index, paragraph in enumerate(paragraphs):
            match = _OPENER_WORD.match(paragraph)
            if match and match.group(1) == row["opener"]:
                queue.append(
                    {
                        "paragraph_index": index,
                        "opener": row["opener"],
                        "count": row["count"],
                        "issue_type": "template_opener",
                        "severity": (
                            "high" if row["count"] >= _STYLE_OVER_REPRESENTED_COUNT else "medium"
                        ),
                        "evidence": (
                            f"opener {row['opener']!r} occurs "
                            f"{row['count']}/{total} long paragraphs"
                        ),
                        "suggestion": (
                            "rewrite the sentence opener away from the "
                            "over-represented word"
                        ),
                    }
                )
    return queue


def apply_deterministic_style_governance(
    review_text: str,
    *,
    enabled: bool = True,
    protected_terms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Apply the zero-cost style guard after model-based convergence.

    The paid rewriter is allowed to stop at a model fixed point.  Publication
    output still needs a deterministic last pass for defects that are both
    mechanical and safe to repair: repeated concessive ``While A, B.``
    paragraph openers, the measured ``not merely X but Y`` construction, and
    the small locked set of abstract-subject phrases.  Each changed paragraph
    is accepted only when :func:`verify_rewrite` preserves citations, numbers,
    units, formulas, hedges, and protected terminology.

    This function does not call a model and therefore does not consume the
    research or publication budget.  It is deliberately separate from
    :func:`run_style_convergence` so existing callers can still inspect the
    model-wave ratchet independently.
    """

    before_metrics = style_opener_metrics(review_text)
    before_abstract = abstract_subject_hits(review_text)
    before_strawman = len(_STRAW_MAN_RE.findall(review_text))
    report: Dict[str, Any] = {
        "schema_version": "optomind.deterministic_style_governance.v1",
        "enabled": bool(enabled),
        "review_text": review_text,
        "changed": False,
        "paragraphs_examined": 0,
        "rewrites_accepted": 0,
        "rewrites_rejected": 0,
        "accepted_paragraphs": [],
        "rewrite_kind_counts": {
            "while_opener": 0,
            "building_on_opener": 0,
            "strawman_not_but": 0,
            "abstract_subject": 0,
        },
        "rejections": [],
        "metrics_before": before_metrics,
        "metrics_after": before_metrics,
        "abstract_subject_hits_before": before_abstract,
        "abstract_subject_hits_after": before_abstract,
        "strawman_not_but_before": before_strawman,
        "strawman_not_but_after": before_strawman,
    }
    if not enabled:
        return report

    terms = list(protected_terms or [])
    subject_counters: dict[str, int] = {}
    building_occurrence = 0
    segments = _PARA_SPLIT.split(review_text or "")
    paragraph_index = -1
    for position, segment in enumerate(segments):
        if position % 2 == 1 or not _is_long_block(segment):
            continue
        paragraph_index += 1
        report["paragraphs_examined"] += 1
        original = segment.strip()
        candidate = original
        kinds: list[str] = []

        candidate, count = _rewrite_strawman_not_but(candidate)
        if count:
            kinds.extend(["strawman_not_but"] * count)
        candidate, count = _rewrite_abstract_subjects(
            candidate,
            subject_counters,
        )
        if count:
            kinds.extend(["abstract_subject"] * count)
        candidate, count, building_occurrence = _rewrite_building_on_opener(
            candidate,
            building_occurrence,
        )
        if count:
            kinds.extend(["building_on_opener"] * count)
        candidate, count = _rewrite_while_opener(candidate)
        if count:
            kinds.extend(["while_opener"] * count)

        if candidate == original:
            continue
        verdict = verify_rewrite(
            original=original,
            rewritten=candidate,
            protected_terms=terms,
        )
        if not verdict["ok"]:
            report["rewrites_rejected"] += 1
            report["rejections"].append(
                {
                    "paragraph_index": paragraph_index,
                    "violations": verdict["violations"],
                    "rewrite_kinds": sorted(set(kinds)),
                }
            )
            continue

        leading = segment[: len(segment) - len(segment.lstrip())]
        trailing = segment[len(segment.rstrip()) :]
        segments[position] = leading + candidate + trailing
        report["rewrites_accepted"] += 1
        report["accepted_paragraphs"].append(paragraph_index)
        report["changed"] = True
        for kind in set(kinds):
            report["rewrite_kind_counts"][kind] += kinds.count(kind)

    text_out = "".join(segments)
    report["review_text"] = text_out
    report["metrics_after"] = style_opener_metrics(text_out)
    report["abstract_subject_hits_after"] = abstract_subject_hits(text_out)
    report["strawman_not_but_after"] = len(_STRAW_MAN_RE.findall(text_out))
    return report


def run_style_convergence(
    review_text: str,
    *,
    enabled: bool = True,
    cost_budget_cny: float = _STYLE_DEFAULT_BUDGET_CNY,
    max_rewrites: int = _STYLE_DEFAULT_MAX_REWRITES,
    target_max_share: float = _STYLE_MAX_SHARE_TARGET,
    protected_terms: Optional[List[str]] = None,
    llm_call=None,
) -> Dict[str, Any]:
    """Iterate run_style_pipeline to convergence under one budget envelope.

    Each wave re-measures the opener distribution and rebuilds the candidate
    queue from scratch -- no locked table, no first-N truncation.  Stops when
    the max paragraph-opener share reaches ``target_max_share``, when the
    rewrite cap or the cost budget is exhausted, or when a wave accepts
    nothing new (fixed point).
    """

    metrics_before = style_opener_metrics(review_text)
    report: Dict[str, Any] = {
        "schema_version": "optomind.style_convergence_report.v2",
        "enabled": bool(enabled),
        "metrics_before": metrics_before,
        "waves": [],
        "rewrites_attempted": 0,
        "rewrites_accepted": 0,
        "estimated_cost_cny": 0.0,
        "budget_exhausted": False,
        "converged": False,
        "stop_reason": "",
    }
    text = review_text
    if not enabled:
        report["stop_reason"] = "disabled"
        report["metrics_after"] = metrics_before
        return report

    remaining_budget = float(cost_budget_cny)
    remaining_rewrites = int(max_rewrites)
    stalled = 0
    # Ratchet.  A wave can make the manuscript measurably worse: rewriting
    # every ``While`` into the same ``Although`` collapses template_share but
    # drives max_share from 0.28 to 0.85, and the orchestrator would ship that
    # text because rewrites were accepted.  Waves are scored on the sum of the
    # two shares -- low is good -- and only a wave that beats the best score so
    # far becomes the candidate output.  The sum, not a lexicographic pair, so
    # a large template_share win still survives a negligible max_share loss.
    def _score(metrics: Dict[str, Any]) -> float:
        return float(metrics["paragraph_opener_max_share"]) + float(
            metrics.get("template_opener_share", 0.0) or 0.0
        )

    best_text = review_text
    best_metrics = metrics_before
    best_score = _score(metrics_before)
    best_wave = 0
    discarded_waves: List[int] = []
    for wave_index in range(1, 9):
        current_metrics = style_opener_metrics(text)
        if (
            current_metrics["paragraph_opener_max_share"]
            <= target_max_share
        ):
            report["converged"] = True
            report["stop_reason"] = (
                f"max_share<= {target_max_share}"
            )
            break
        if remaining_budget <= 0.0 or remaining_rewrites <= 0:
            report["budget_exhausted"] = True
            report["stop_reason"] = "budget_or_rewrites_exhausted"
            break
        issues = _over_represented_openers(text)
        if not issues:
            report["stop_reason"] = "no_over_represented_openers"
            break
        wave_budget = min(remaining_budget, 0.15)
        wave = run_style_pipeline(
            text,
            enabled=True,
            review_enabled=False,
            cost_budget_cny=wave_budget,
            max_rewrites=remaining_rewrites,
            protected_terms=protected_terms,
            # The measured queue drives selection; the critic inside still
            # contributes the issue types a histogram cannot see.  Recomputed
            # every wave against the current text, so a paragraph already
            # rewritten off a hot opener drops out on its own.
            candidate_issues=issues,
            llm_call=llm_call,
        )
        new_text = str(wave.get("review_text") or text)
        new_metrics = style_opener_metrics(new_text)
        report["waves"].append(
            {
                "wave": wave_index,
                "issues_found": wave.get("issues_found", 0),
                "candidates_supplied": wave.get("candidate_issues_supplied", 0),
                "attempted": wave.get("rewrites_attempted", 0),
                "accepted": wave.get("rewrites_accepted", 0),
                "cost_cny": round(
                    float(wave.get("estimated_cost_cny", 0.0)), 4
                ),
                "max_share_after": new_metrics["paragraph_opener_max_share"],
                "template_share_after": new_metrics.get(
                    "template_opener_share", 0.0
                ),
            }
        )
        report["rewrites_attempted"] += int(
            wave.get("rewrites_attempted", 0)
        )
        report["rewrites_accepted"] += int(
            wave.get("rewrites_accepted", 0)
        )
        report["estimated_cost_cny"] += float(
            wave.get("estimated_cost_cny", 0.0)
        )
        remaining_budget -= float(wave.get("estimated_cost_cny", 0.0))
        remaining_rewrites -= int(wave.get("rewrites_attempted", 0))
        if new_text == text:
            report["stop_reason"] = "fixed_point_no_acceptance"
            break
        wave_score = _score(new_metrics)
        if wave_score < best_score - 1e-9:
            best_score, best_text, best_metrics = wave_score, new_text, new_metrics
            best_wave = wave_index
        else:
            discarded_waves.append(wave_index)
        # A wave can rewrite dozens of paragraphs and move neither share when
        # the rewriter relocates the concentration instead of diversifying it
        # -- every ``While`` becoming the same ``Although``.  Both shares are
        # checked because a useful wave may flatten openers #2..#5 and leave
        # the top one alone.  Two flat waves in a row means more waves will not
        # help, and each one spends real calls on the full chapter.
        improved = (
            new_metrics["paragraph_opener_max_share"]
            < current_metrics["paragraph_opener_max_share"] - 1e-9
            or float(new_metrics.get("template_opener_share", 0.0))
            < float(current_metrics.get("template_opener_share", 0.0)) - 1e-9
        )
        stalled = 0 if improved else stalled + 1
        text = new_text
        if stalled >= 2:
            report["stop_reason"] = "no_share_improvement"
            break
    else:
        report["stop_reason"] = "wave_cap_reached"
    report["best_wave"] = best_wave
    report["regressed_waves_discarded"] = discarded_waves
    report["metrics_after"] = best_metrics
    report["review_text"] = best_text
    # Report convergence against the text actually returned, not against the
    # last text tried -- a discarded regression must never be able to claim it.
    report["converged"] = bool(
        best_metrics["paragraph_opener_max_share"] <= target_max_share
    )
    return report
