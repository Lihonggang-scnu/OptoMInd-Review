"""P2: M1 教学案例库 enrichment — 为每条 move 生成5个领域无关字段。

新字段：
  transferable_rule  — 与领域无关的一句话规则
  trigger_when       — 触发该规则的场景关键词
  bad_pattern_to_avoid — 该规则纠正的反模式
  downstream_hooks   — 受益的下游模块列表
  example_transformation — {"ordinary": ..., "top_review_style": ...}

设计约束：
  - 不使用 response_format（qwen3.x 不支持），用 prompt 强制 JSON 输出
  - 增量处理：已有 transferable_rule 的 move 跳过
  - LLM 失败时 fallback：transferable_rule = reuse_for_our_review_system[:300]
"""

from __future__ import annotations

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    PROJECT_ROOT / "outputs" / "review_example_memory"
    / "final_canonical" / "intellectual_moves_active_by_category.json"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "outputs" / "review_example_memory"
    / "final_canonical" / "intellectual_moves_enriched_by_category.json"
)

_VALID_HOOKS = {"blueprint", "M2a", "M2b", "M3", "M4"}

_SYSTEM_PROMPT = (
    "You are abstracting domain-specific writing patterns into domain-agnostic rules "
    "for academic review writers. Your output must be generic — it should apply equally "
    "to reviews in optics, biology, computer science, or any other field."
)

_USER_TEMPLATE = """\
Category: {category}
Move: {move}
Why it matters: {why_it_matters}
Reuse hint: {reuse_hint}
Possible overreach: {possible_overreach}

Generate eight fields. Return a single JSON object with no other text:
{{
  "transferable_rule": "A one-sentence rule any review writer can apply, with NO domain-specific terms",
  "trigger_when": "Conditions/signals that activate this rule (e.g. 'when the review topic involves competing constraints')",
  "bad_pattern_to_avoid": "The anti-pattern this move corrects, described generically",
  "downstream_hooks": ["blueprint", "M2a", "M2b", "M3", "M4"],
  "example_transformation": {{
    "ordinary": "Generic example of the bad writing pattern",
    "top_review_style": "How applying this rule transforms the writing"
  }},
  "example_reviews": ["Hypothetical review title 1 that applies this pattern", "Title 2"],
  "anti_example": "One-sentence description of a review that failed by ignoring this pattern",
  "difficulty_tier": "novice|intermediate|advanced"
}}
Guidelines:
- Only include relevant downstream_hooks (subset of blueprint/M2a/M2b/M3/M4)
- example_reviews: 2-3 plausible review paper titles (do not cite real papers, use representative titles)
- difficulty_tier: novice=basic organization, intermediate=synthesis/comparison, advanced=meta-analysis/theory-building"""

_CATEGORY_SYSTEM_PROMPT = (
    "You are a writing-patterns abstractor. Given a sample of writing moves from one "
    "rhetorical category, synthesize ONE set of domain-agnostic rules that captures the "
    "essential pattern of the entire category. Your output must apply equally to reviews "
    "in optics, biology, computer science, or any other field."
)

_CATEGORY_USER_TEMPLATE = """\
Category: {category} ({total_count} moves total)
Sample of {sample_count} representative moves:

{moves_block}

Synthesize ONE category-level rule set. Return a single JSON object with no other text:
{{
  "transferable_rule": "One sentence capturing what ALL moves in this category do, no domain terms",
  "trigger_when": "Conditions that activate any move in this category (2-3 sentences)",
  "bad_pattern_to_avoid": "The generic anti-pattern this entire category corrects",
  "downstream_hooks": ["blueprint", "M2a", "M2b", "M3", "M4"],
  "example_transformation": {{
    "ordinary": "Generic example of writing that needs this category fix",
    "top_review_style": "How applying this category rule transforms the writing"
  }},
  "example_reviews": ["Hypothetical review title applying this category pattern", "Title 2"],
  "anti_example": "One-sentence description of failure without this category pattern",
  "difficulty_tier": "novice|intermediate|advanced"
}}
Only include relevant downstream_hooks (subset of blueprint/M2a/M2b/M3/M4)."""


def _compact(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _parse_json(text: str) -> dict[str, Any]:
    """Extract first JSON object from LLM response text."""
    text = str(text or "").strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                val = json.loads(match.group(0))
                return val if isinstance(val, dict) else {}
            except Exception:
                pass
    return {}


def _fallback_fields(move: dict[str, Any]) -> dict[str, Any]:
    """Generate minimal new fields without LLM (used on failure)."""
    reuse = _compact(move.get("reuse_for_our_review_system", ""), 300)
    # Remove most domain-specific references by stripping named entities pattern
    generic = re.sub(r"\b[A-Z][a-z]*[A-Z][a-z]+\b", "this", reuse)
    return {
        "transferable_rule": generic or _compact(move.get("move", ""), 200),
        "trigger_when": "",
        "bad_pattern_to_avoid": _compact(move.get("possible_overreach", ""), 200),
        "downstream_hooks": ["blueprint"],
        "example_transformation": {"ordinary": "", "top_review_style": ""},
    }


def _normalize_enrichment(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise the LLM-generated enrichment fields."""
    hooks = raw.get("downstream_hooks")
    if not isinstance(hooks, list):
        hooks = []
    hooks = [h for h in hooks if str(h) in _VALID_HOOKS]
    if not hooks:
        hooks = ["blueprint"]

    xf = raw.get("example_transformation")
    if not isinstance(xf, dict):
        xf = {}

    return {
        "transferable_rule": _compact(raw.get("transferable_rule", ""), 500),
        "trigger_when": _compact(raw.get("trigger_when", ""), 400),
        "bad_pattern_to_avoid": _compact(raw.get("bad_pattern_to_avoid", ""), 400),
        "downstream_hooks": hooks,
        "example_transformation": {
            "ordinary": _compact(xf.get("ordinary", ""), 300),
            "top_review_style": _compact(xf.get("top_review_style", ""), 300),
        },
    }


def _build_category_prompt(category: str, moves: list[dict[str, Any]], sample_size: int = 25) -> str:
    sample = moves[:sample_size] if len(moves) <= sample_size else random.sample(moves, sample_size)
    lines = []
    for i, m in enumerate(sample, 1):
        lines.append(
            f"{i}. Move: {_compact(m.get('move', ''), 200)}\n"
            f"   Why it matters: {_compact(m.get('why_it_matters', ''), 150)}"
        )
    return _CATEGORY_USER_TEMPLATE.format(
        category=category,
        total_count=len(moves),
        sample_count=len(sample),
        moves_block="\n\n".join(lines),
    )


def _normalize_category_enrichment(raw: dict[str, Any]) -> dict[str, Any]:
    hooks = raw.get("downstream_hooks")
    if not isinstance(hooks, list):
        hooks = []
    hooks = [h for h in hooks if str(h) in _VALID_HOOKS]
    if not hooks:
        hooks = ["blueprint"]
    xf = raw.get("example_transformation")
    if not isinstance(xf, dict):
        xf = {}
    return {
        "transferable_rule": _compact(raw.get("transferable_rule", ""), 600),
        "trigger_when": _compact(raw.get("trigger_when", ""), 500),
        "bad_pattern_to_avoid": _compact(raw.get("bad_pattern_to_avoid", ""), 500),
        "downstream_hooks": hooks,
        "example_transformation": {
            "ordinary": _compact(xf.get("ordinary", ""), 400),
            "top_review_style": _compact(xf.get("top_review_style", ""), 400),
        },
    }


def _needs_enrichment(move: dict[str, Any]) -> bool:
    return not bool(str(move.get("transferable_rule", "")).strip())


def enrich_moves_library(
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    max_moves: int | None = None,
    model_tier: str = "standard_model",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enrich intellectual moves with transferable fields.

    Returns a report dict with counts and status.
    """
    from llm.qwen_chat_client import call_qwen_chat

    # ── 读取输入 ──────────────────────────────────────────────
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    library: dict[str, list[dict[str, Any]]] = raw if isinstance(raw, dict) else {}

    # ── 读取现有输出（增量 resume）────────────────────────────
    if output_path.exists():
        existing_raw = json.loads(output_path.read_text(encoding="utf-8"))
        enriched: dict[str, list[dict[str, Any]]] = (
            existing_raw if isinstance(existing_raw, dict) else {}
        )
    else:
        enriched = {}

    # ── 统计待处理数量 ────────────────────────────────────────
    total_pending = 0
    for cat, moves in library.items():
        existing_moves = {
            _compact(m.get("move", ""), 200): m
            for m in (enriched.get(cat) or [])
        }
        for m in moves:
            key = _compact(m.get("move", ""), 200)
            existing = existing_moves.get(key, {})
            if _needs_enrichment(existing) and _needs_enrichment(m):
                total_pending += 1

    report: dict[str, Any] = {
        "total_pending": total_pending,
        "processed": 0,
        "llm_success": 0,
        "fallback_used": 0,
        "skipped_already_done": 0,
        "dry_run": dry_run,
    }

    if dry_run:
        report["message"] = f"{total_pending} moves need enrichment (dry-run, no LLM calls made)"
        return report

    # ── 逐条处理 ──────────────────────────────────────────────
    processed_count = 0
    for cat, moves in library.items():
        enriched_list = list(enriched.get(cat) or [])
        existing_by_key = {_compact(m.get("move", ""), 200): i for i, m in enumerate(enriched_list)}

        for move in moves:
            if max_moves is not None and processed_count >= max_moves:
                break
            key = _compact(move.get("move", ""), 200)
            existing_idx = existing_by_key.get(key)
            existing = enriched_list[existing_idx] if existing_idx is not None else {}

            if not _needs_enrichment(existing):
                report["skipped_already_done"] += 1
                continue

            # LLM 调用
            user_msg = _USER_TEMPLATE.format(
                category=cat,
                move=_compact(move.get("move", ""), 400),
                why_it_matters=_compact(move.get("why_it_matters", ""), 300),
                reuse_hint=_compact(move.get("reuse_for_our_review_system", ""), 300),
                possible_overreach=_compact(move.get("possible_overreach", ""), 200),
            )
            try:
                result = call_qwen_chat(
                    "MoveEnricher",
                    [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    model_tier=model_tier,
                    temperature=0.1,
                    max_tokens=400,
                    force_mock=False,
                    max_retries=1,
                )
                content = str(result.get("content") or "")
                if content.startswith("[fallback]") or content.startswith("[mock]"):
                    raise ValueError(content[:60])
                parsed = _parse_json(content)
                if parsed and parsed.get("transferable_rule"):
                    new_fields = _normalize_enrichment(parsed)
                    report["llm_success"] += 1
                else:
                    raise ValueError("empty transferable_rule")
            except Exception:
                new_fields = _fallback_fields(move)
                report["fallback_used"] += 1

            # 合并到 enriched move
            enriched_move = {**move, **new_fields}
            if existing_idx is not None:
                enriched_list[existing_idx] = enriched_move
            else:
                enriched_list.append(enriched_move)
                existing_by_key[key] = len(enriched_list) - 1

            processed_count += 1
            report["processed"] += 1

            # 每处理5条保存一次（avoid data loss）
            if processed_count % 5 == 0:
                enriched[cat] = enriched_list
                _save(output_path, enriched)

        enriched[cat] = enriched_list
        if max_moves is not None and processed_count >= max_moves:
            break

    _save(output_path, enriched)
    report["output_path"] = str(output_path)
    return report


def enrich_by_category(
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    *,
    model_tier: str = "advanced_model",
    max_tokens: int = 1200,
    max_workers: int = 4,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enrich all moves using category-level LLM synthesis (11 calls total).

    Each category gets one LLM call. All moves in that category inherit the
    resulting transferable_rule, trigger_when, bad_pattern_to_avoid, etc.
    """
    from llm.qwen_chat_client import call_qwen_chat

    raw = json.loads(input_path.read_text(encoding="utf-8"))
    library: dict[str, list[dict[str, Any]]] = raw if isinstance(raw, dict) else {}

    if output_path.exists():
        existing_raw = json.loads(output_path.read_text(encoding="utf-8"))
        enriched: dict[str, list[dict[str, Any]]] = (
            existing_raw if isinstance(existing_raw, dict) else {}
        )
    else:
        enriched = {}

    pending_cats, done_cats = [], []
    for cat, moves in library.items():
        existing_list = enriched.get(cat) or []
        all_done = (
            len(existing_list) == len(moves)
            and bool(existing_list)
            and all(bool(str(m.get("transferable_rule", "")).strip()) for m in existing_list)
        )
        (done_cats if all_done else pending_cats).append(cat)

    report: dict[str, Any] = {
        "mode": "category",
        "total_categories": len(library),
        "pending_categories": len(pending_cats),
        "done_categories": len(done_cats),
        "llm_success": 0,
        "fallback_used": 0,
        "total_moves_enriched": 0,
        "dry_run": dry_run,
    }

    if dry_run:
        report["message"] = (
            f"{len(pending_cats)} categories need enrichment "
            f"({sum(len(library[c]) for c in pending_cats)} moves total)"
        )
        return report

    def _process_category(cat: str) -> tuple[str, dict[str, Any], str]:
        moves = library[cat]
        user_msg = _build_category_prompt(cat, moves)
        try:
            result = call_qwen_chat(
                "MoveEnricherCategory",
                [
                    {"role": "system", "content": _CATEGORY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                model_tier=model_tier,
                temperature=0.1,
                max_tokens=max_tokens,
                force_mock=False,
                max_retries=2,
            )
            content = str(result.get("content") or "")
            if content.startswith("[fallback]") or content.startswith("[mock]"):
                raise ValueError(content[:60])
            parsed = _parse_json(content)
            if parsed and parsed.get("transferable_rule"):
                return cat, _normalize_category_enrichment(parsed), "llm"
            raise ValueError("empty transferable_rule")
        except Exception as exc:
            fallback = _fallback_fields(moves[0]) if moves else {}
            return cat, fallback, f"fallback:{exc}"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process_category, cat): cat for cat in pending_cats}
        for future in as_completed(futures):
            cat, cat_fields, status = future.result()
            if status == "llm":
                report["llm_success"] += 1
            else:
                report["fallback_used"] += 1
            enriched[cat] = [{**m, **cat_fields} for m in library[cat]]
            report["total_moves_enriched"] += len(library[cat])
            _save(output_path, enriched)

    report["output_path"] = str(output_path)
    return report


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
