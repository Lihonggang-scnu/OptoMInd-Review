"""Static replay exporter (F6 change 4).

Reads ONE run directory (READ-ONLY) and writes a self-contained bundle:
index.html (+404.html), assets/, replay-data.json, .nojekyll.
Timeline goes through optomind_ui.narrator.project_line (whitelist +
2000-byte truncation), so multi-MB event lines can never leak.
Sanitisation masks absolute paths, machine/user names, key shapes,
api_keys mentions and publisher PDF filenames. Size policy: merge
downsample to <=2.5 MB target; hard cap 5 MB -> exit 3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from optomind_ui.narrator import project_line  # noqa: E402

TARGET_BYTES = int(2.5 * 1024 * 1024)
HARD_CAP_BYTES = 5 * 1024 * 1024

_MILESTONE_EVENTS = {
    "run_started", "run_resumed", "run_finished", "run_error",
    "stage_started", "stage_finished", "pdf_skipped",
}

_P_SKKEY = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")
_P_APIDIR = re.compile(r"api_keys", re.IGNORECASE)
_P_USERS = re.compile(r"(?i)\bc:\\users\\?[^\s\"']{0,80}")
_P_DRIVE = re.compile(r"\b[A-Za-z]:\\(?:[^\s\"']+\\)*")
_P_DOIPDF = re.compile(r"\b10\.\d{4,9}/\S+?\.pdf\b", re.IGNORECASE)
_ANY_PDF = re.compile(r"\b(?!main\.pdf\b)[a-z0-9][A-Za-z0-9_.-]{3,}\.pdf\b")

_PATTERNS: List[Tuple[Any, str]] = [
    (_P_SKKEY, "[密钥已隐藏]"), (_P_APIDIR, "[密钥目录]"), (_P_USERS, "[本地路径]"),
    (_P_DRIVE, "[本地路径]"), (_P_DOIPDF, "[文献PDF]"),
]


def _machine_names() -> List[str]:
    names = {os.environ.get("COMPUTERNAME", ""), os.environ.get("USERNAME", "")}
    return [name for name in names if name and len(name) >= 3]


def sanitize_text(value: str) -> str:
    text = value
    for name in _machine_names():
        text = text.replace(name, "[本机]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _ANY_PDF.sub("[PDF文件名]", text)


def sanitize_obj(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_obj(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_obj(item) for key, item in value.items()}
    return value


def _parse_ts(raw: str) -> float:
    try:
        moment = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return moment.timestamp()
    except Exception:
        return 0.0


def _looks_like_model_stage(stage: str) -> bool:
    lowered = stage.lower()
    hints = ("plan", "review", "translate", "narrat", "editorial", "intent", "visual")
    return any(hint in lowered for hint in hints)


def load_timeline(run_dir: Path):
    events_path = run_dir / "HARNESS_EVENTS.jsonl"
    rows: List[Dict[str, Any]] = []
    with open(events_path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except Exception:
                continue
    rows.sort(key=lambda row: str(row.get("timestamp") or ""))

    timeline: List[Dict[str, Any]] = []
    metrics: List[Dict[str, Any]] = [
        {"t": 0.0, "cost_cny": 0.0, "model_calls": 0, "stages_done": 0}
    ]
    t0: float | None = None
    cost_cny = 0.0
    calls = 0
    stages_done = 0
    for row in rows:
        projected = project_line(row)
        entry = {
            "ts": projected["ts"],
            "event": projected["raw_event"],
            "stage": projected["stage"],
            "stage_label": projected["stage_label"],
            "text": projected["text"],
        }
        if projected.get("truncated"):
            entry["truncated"] = True
        # A whitelist DROP is otherwise invisible in the bundle: the
        # reference run's line 6 is 3,386,117 bytes, but `selection` and
        # `evidence` are dropped rather than clipped, so the row reports
        # 293 bytes with truncated=false. Carry the sizes (never the
        # content) so the replay can say a payload was withheld on purpose.
        withheld_bytes = projected.get("withheld_bytes")
        if isinstance(withheld_bytes, int) and withheld_bytes > 0:
            entry["withheld_fields"] = projected.get("withheld_fields")
            entry["withheld_bytes"] = withheld_bytes
        timeline.append(entry)

        stamp = _parse_ts(projected["ts"])
        if t0 is None:
            t0 = stamp
        seconds = max(0.0, stamp - (t0 or 0.0))
        event_name = str(row.get("event") or "")
        if event_name == "stage_finished":
            stages_done += 1
            delta = row.get("cost_cny")
            if isinstance(delta, (int, float)):
                cost_cny += float(delta)
            if _looks_like_model_stage(str(row.get("stage") or "")):
                calls += 1
            metrics.append({
                "t": round(seconds, 3),
                "cost_cny": round(cost_cny, 4),
                "model_calls": calls,
                "stages_done": stages_done,
            })

    final_cost: Dict[str, Any] = {}
    cost_file = run_dir / "HARNESS_COST.json"
    if cost_file.is_file():
        try:
            final_cost = json.loads(cost_file.read_text(encoding="utf-8"))
        except Exception:
            final_cost = {}
    duration = metrics[-1]["t"] if metrics else 0.0
    return timeline, metrics, {"final_cost": final_cost, "duration_seconds": duration}


def merge_adjacent(timeline: List[Dict[str, Any]], keep_every: int):
    merged: List[Dict[str, Any]] = []
    merged_count = 0
    bucket = -1
    for index, line in enumerate(timeline):
        is_milestone = line["event"] in _MILESTONE_EVENTS
        prev = merged[-1] if merged else None
        same_bucket = index // keep_every == bucket
        if (
            not is_milestone
            and same_bucket
            and prev is not None
            and prev.get("stage") == line.get("stage")
            and prev.get("event") == line.get("event")
        ):
            merged_count += 1
            continue
        if not is_milestone:
            bucket = index // keep_every
        merged.append(dict(line))
    return merged, merged_count


def _read_json_object(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _first_json_object(run_dir: Path, *relative_paths: str) -> Dict[str, Any]:
    for relative in relative_paths:
        value = _read_json_object(run_dir / relative)
        if value:
            return value
    return {}


def _bounded_text(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return sanitize_text(text[:limit])


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _metric_snapshot(metrics: Any) -> Dict[str, Any]:
    if not isinstance(metrics, dict):
        return {}
    fields = (
        "paragraphs_total",
        "first_sentence_count",
        "template_opener_share",
        "first_sentence_template_share",
        "first_sentence_scaffold_share",
        "first_sentence_scaffold_max_share",
        "paragraph_opener_max_share",
        "first_sentence_prefix_max_share",
        "while_paragraphs",
        "building_on_paragraphs",
        "abstract_subject_hits_before",
        "abstract_subject_hits_after",
        "strawman_not_but_before",
        "strawman_not_but_after",
    )
    result: Dict[str, Any] = {}
    for field in fields:
        if field in metrics and isinstance(metrics[field], (int, float)):
            result[field] = round(float(metrics[field]), 6)
    return result


def _style_snapshot(run_dir: Path) -> Dict[str, Any]:
    legacy = _first_json_object(
        run_dir,
        "publication_mainline/STYLE_CONVERGENCE_REPORT.json",
        "publication_mainline/style/STYLE_CONVERGENCE_REPORT.json",
    )
    chapter = _first_json_object(
        run_dir,
        "publication_mainline/CHAPTER_STYLE_GOVERNANCE_REPORT.json",
    )

    chapter_rows: List[Dict[str, Any]] = []
    raw_chapters = chapter.get("chapters")
    if isinstance(raw_chapters, list):
        for row in raw_chapters[:12]:
            if not isinstance(row, dict):
                continue
            accepted = row.get("accepted_edits")
            rejected = row.get("rejected_edits")
            chapter_rows.append(
                {
                    "section_id": _bounded_text(row.get("section_id"), 40),
                    "title": _bounded_text(row.get("chapter_title"), 150),
                    "status": _bounded_text(row.get("status"), 40),
                    "changed": bool(row.get("changed")),
                    "reviewer_attempted": bool(row.get("reviewer_attempted")),
                    "reviser_attempted": bool(row.get("reviser_attempted")),
                    "reviewer_edits_found": int(row.get("reviewer_edits_found") or 0),
                    "accepted_edits": len(accepted) if isinstance(accepted, list) else int(accepted or 0),
                    "rejected_edits": len(rejected) if isinstance(rejected, list) else int(rejected or 0),
                    "author_rejected": bool(row.get("author_rejected")),
                    "reviewer_model": _bounded_text(row.get("reviewer_model"), 60),
                    "reviser_model": _bounded_text(row.get("reviser_model"), 60),
                }
            )

    abbreviation_rows: List[Dict[str, Any]] = []
    raw_abbreviations = chapter.get("abbreviation_inventory")
    if isinstance(raw_abbreviations, list):
        for row in raw_abbreviations[:16]:
            if not isinstance(row, dict):
                continue
            abbreviation_rows.append(
                {
                    "abbreviation": _bounded_text(row.get("abbreviation"), 40),
                    "full_form": _bounded_text(row.get("full_form"), 120),
                    "full_form_mention_count": int(row.get("full_form_mention_count") or 0),
                    "repeat_count_after_first_definition": int(
                        row.get("repeat_count_after_first_definition") or 0
                    ),
                }
            )

    return {
        "legacy": {
            "enabled": bool(legacy.get("enabled")),
            "rewrites_attempted": int(legacy.get("rewrites_attempted") or 0),
            "rewrites_accepted": int(legacy.get("rewrites_accepted") or 0),
            "converged": bool(legacy.get("converged")),
            "stop_reason": _bounded_text(legacy.get("stop_reason"), 120),
            "metrics_before": _metric_snapshot(legacy.get("metrics_before")),
            "metrics_after": _metric_snapshot(legacy.get("metrics_after")),
        },
        "chapter": {
            "enabled": bool(chapter.get("enabled")),
            "chapters_attempted": int(chapter.get("chapters_attempted") or 0),
            "chapters_accepted": int(chapter.get("chapters_accepted") or 0),
            "chapters_changed": int(chapter.get("chapters_changed") or 0),
            "reviewer_calls": int(chapter.get("reviewer_calls") or 0),
            "reviser_calls": int(chapter.get("reviser_calls") or 0),
            "estimated_cost_cny": round(_finite_number(chapter.get("estimated_cost_cny")), 6),
            "promotion_eligible": bool(chapter.get("promotion_eligible")),
            "promotion_reason": _bounded_text(chapter.get("promotion_reason"), 180),
            "improved": bool(chapter.get("improved")),
            "style_score_before": round(_finite_number(chapter.get("style_score_before")), 6),
            "style_score_after": round(_finite_number(chapter.get("style_score_after")), 6),
            "metrics_before": _metric_snapshot(chapter.get("metrics_before")),
            "metrics_after": _metric_snapshot(chapter.get("metrics_after")),
            "chapters": chapter_rows,
            "abbreviation_inventory": abbreviation_rows,
        },
    }


def _visual_events_snapshot(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "visual_editor" / "final" / "VISUAL_EVENTS.jsonl"
    if not path.is_file():
        return []
    keep_events = {
        "visual_contract_created",
        "conceptual_visual_unresolved",
        "conceptual_visual_generation_overflow",
        "structured_diagram_fallback_skipped_by_budget",
        "structured_diagram_cache_hit",
        "visual_human_review_policy_applied",
        "final_visual_package_validated",
    }
    result: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or row.get("event") not in keep_events:
            continue
        item: Dict[str, Any] = {
            "event": _bounded_text(row.get("event"), 80),
        }
        for key in (
            "section_id",
            "figure_id",
            "visual_plan_id",
            "figure_kind",
            "reason",
            "status",
        ):
            if row.get(key) not in (None, ""):
                item[key] = _bounded_text(row.get(key), 160)
        result.append(item)
    return result[-32:]


def _visual_snapshot(run_dir: Path) -> Dict[str, Any]:
    package = _read_json_object(
        run_dir / "visual_editor" / "final" / "FINAL_VISUAL_PACKAGE.json"
    )
    cost = _read_json_object(
        run_dir / "visual_editor" / "final" / "VISUAL_COST_REPORT.json"
    )
    plan = _first_json_object(
        run_dir,
        "visual_editor/final/VISUAL_EDITORIAL_PLAN.json",
        "visual_editor/VISUAL_EDITORIAL_PLAN.json",
    )
    contract = package.get("article_visual_contract")
    contract = contract if isinstance(contract, dict) else {}
    ingestion = package.get("visual_plan_ingestion")
    ingestion = ingestion if isinstance(ingestion, dict) else {}

    request_rows: List[Dict[str, Any]] = []
    raw_requests = plan.get("conceptual_figure_requests")
    if not isinstance(raw_requests, list):
        raw_requests = contract.get("slots") or []
    if isinstance(raw_requests, list):
        for row in raw_requests[:8]:
            if not isinstance(row, dict):
                continue
            request_rows.append(
                {
                    "section_id": _bounded_text(row.get("section_id"), 40),
                    "figure_kind": _bounded_text(row.get("figure_kind"), 60),
                    "priority": _bounded_text(row.get("priority"), 30),
                    "status": _bounded_text(
                        row.get("generation_status")
                        or row.get("status")
                        or "pending_generation",
                        80,
                    ),
                    "estimated_cost_cny": round(
                        _finite_number(row.get("estimated_cost_cny")),
                        6,
                    ),
                }
            )

    validation = package.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    return {
        "plan": {
            "placement_count": int(
                ingestion.get("placement_count")
                or len(package.get("figures") or [])
                or 0
            ),
            "conceptual_request_count": int(
                ingestion.get("generation_request_count")
                or len(request_rows)
                or 0
            ),
            "unfilled_need_count": int(
                ingestion.get("unfilled_need_count")
                or len(plan.get("unfilled_visual_needs") or [])
                or 0
            ),
            "requests": request_rows,
        },
        "package": {
            "slot_count": int(contract.get("slot_count") or 0),
            "figure_contract_count": int(contract.get("figure_contract_count") or 0),
            "figure_count": len(package.get("figures") or []) if isinstance(package.get("figures"), list) else 0,
            "unfilled_visual_opportunities": int(
                len(package.get("unfilled_visual_opportunities") or [])
                if isinstance(package.get("unfilled_visual_opportunities"), list)
                else 0
            ),
            "validation_status": _bounded_text(validation.get("status"), 80),
            "warnings": [
                _bounded_text(item, 180)
                for item in (validation.get("warnings") or [])[:12]
            ],
        },
        "cost": {
            "budget_policy": _bounded_text(
                cost.get("budget_policy"),
                60,
            ),
            "global_remaining_cny": round(
                _finite_number(cost.get("global_remaining_cny")),
                6,
            ),
            # Keep the legacy field for older replay consumers.  In a
            # global-only package it is deliberately null/zero rather than
            # relabeling the shared balance as a visual-stage budget.
            "budget_cny": (
                round(_finite_number(cost.get("budget_cny")), 6)
                if cost.get("budget_cny") is not None
                else None
            ),
            "estimated_cost_cny": round(_finite_number(cost.get("estimated_cost_cny")), 6),
            "vision_calls": int(cost.get("vision_calls") or 0),
            "diagram_spec_calls": int(cost.get("diagram_spec_calls") or 0),
            "image_generation_calls": int(cost.get("image_generation_calls") or 0),
            "cache_hits": int(cost.get("cache_hits") or 0),
            "cache_hit_rate": round(_finite_number(cost.get("cache_hit_rate")), 6),
            "reserved_generation_cost_cny": round(
                _finite_number(cost.get("reserved_generation_cost_cny")),
                6,
            ),
            "cache_namespace": _bounded_text(
                package.get("cache_namespace")
                or cost.get("cache_namespace"),
                80,
            ),
        },
        "events": _visual_events_snapshot(run_dir),
    }


def _replay_snapshot(
    run_dir: Path,
    final_cost: Dict[str, Any],
) -> Dict[str, Any]:
    question = _first_json_object(
        run_dir,
        "query_planner/ORIGINAL_USER_QUESTION.json",
        "ORIGINAL_USER_QUESTION.json",
    )
    stages: List[Dict[str, Any]] = []
    raw_stages = final_cost.get("stages")
    if isinstance(raw_stages, dict):
        for stage_name, row in raw_stages.items():
            if not isinstance(row, dict):
                continue
            stages.append(
                {
                    "stage": _bounded_text(stage_name, 80),
                    "status": _bounded_text(row.get("status"), 50),
                    "cost_cny": round(
                        _finite_number(
                            row.get("cost_cny")
                            or row.get("estimated_cost_cny")
                        ),
                        6,
                    ),
                    "wall_time_seconds": round(
                        _finite_number(row.get("wall_time_seconds")),
                        3,
                    ),
                    "model_call_count": int(row.get("model_call_count") or 0),
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                }
            )
    stages.sort(key=lambda row: row["stage"])

    english_pdf = run_dir / "publication" / "latex" / "main.pdf"
    artifacts = [
        {
            "kind": "english_publication",
            "label": "英文 PDF",
            "available": english_pdf.is_file(),
            "size_bytes": int(english_pdf.stat().st_size) if english_pdf.is_file() else 0,
            "embedded_in_static_bundle": False,
        }
    ]
    return {
        "run": {
            "status": _bounded_text(final_cost.get("status"), 60),
            "current_stage": _bounded_text(final_cost.get("current_stage"), 80),
            "completed_stage": _bounded_text(final_cost.get("completed_stage"), 80),
            "error_count": int(final_cost.get("error_count") or 0),
            "question": _bounded_text(question.get("user_question"), 360),
            "cost_cny": round(_finite_number(final_cost.get("cost_cny")), 6),
            "budget_cny": round(
                _finite_number(final_cost.get("global_cost_budget_cny")),
                6,
            ),
            "remaining_budget_cny": round(
                _finite_number(final_cost.get("remaining_budget_cny")),
                6,
            ),
        },
        "stages": stages,
        "style": _style_snapshot(run_dir),
        "visual": _visual_snapshot(run_dir),
        "artifacts": artifacts,
    }


def copy_player(dist_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dist_dir / "index.html", out_dir / "index.html")
    assets_dst = out_dir / "assets"
    if assets_dst.exists():
        shutil.rmtree(assets_dst)
    shutil.copytree(dist_dir / "assets", assets_dst)
    index_text = (out_dir / "index.html").read_text(encoding="utf-8")
    index_text = index_text.replace('"/assets/', '"./assets/')
    marker = 'data-optomind-mode="static-replay"'
    if marker not in index_text:
        index_text = re.sub(
            r"<html(\s|>)",
            r'<html data-optomind-mode="static-replay"\1',
            index_text,
            count=1,
        )
    (out_dir / "index.html").write_text(index_text, encoding="utf-8")
    # GitHub Pages and some static viewers use 404.html as the SPA fallback.
    # Keep it byte-for-byte compatible with the replay entrypoint, including
    # relative asset URLs and the static-replay marker.
    (out_dir / "404.html").write_text(index_text, encoding="utf-8")


def export(run_dir: Path, out_dir: Path) -> int:
    if not (run_dir / "HARNESS_EVENTS.jsonl").is_file():
        print("[export] missing HARNESS_EVENTS.jsonl under run-dir", file=sys.stderr)
        return 2
    timeline, metrics, extras = load_timeline(run_dir)
    data: Dict[str, Any] = {
        "meta": {
            "name": run_dir.name,
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "duration_seconds": extras["duration_seconds"],
            "event_count": len(timeline),
            "merged_events": 0,
            "source_note": "由真实运行导出；已脱敏：路径/机器名/密钥/出版商 PDF 文件名",
        },
        "timeline": sanitize_obj(timeline),
        "metrics": metrics,
        "snapshot": _replay_snapshot(run_dir, extras["final_cost"]),
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    merged_total = 0
    keep_every = 1
    passes = 0
    while len(payload.encode("utf-8")) > TARGET_BYTES and passes < 14:
        keep_every += 1
        merged, count = merge_adjacent(data["timeline"], keep_every)
        data["timeline"] = merged
        data["meta"]["merged_events"] = count
        merged_total = count
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        passes += 1
    size = len(payload.encode("utf-8"))
    if size > HARD_CAP_BYTES:
        print("[export] replay-data.json %dB exceeds hard cap %dB" % (size, HARD_CAP_BYTES), file=sys.stderr)
        return 3
    copy_player(PROJECT_ROOT / "optomind_ui" / "static" / "dist", out_dir)
    (out_dir / "replay-data.json").write_text(payload, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="ascii")
    print("[export] wrote %s" % out_dir)
    print("[export] replay-data.json=%dB (%.1f KiB) events=%d merged=%d"
          % (size, size / 1024, data["meta"]["event_count"], merged_total))
    print("[export] final_cost=%s budget=%s"
          % (extras["final_cost"].get("cost_cny"), extras["final_cost"].get("global_cost_budget_cny")))
    return 0


_COLLECTION_PRESENTATION: Dict[str, Dict[str, str]] = {
    "rhr_optical_diffractive_neural_networks_20260828_v2b": {
        "label": "光学衍射神经网络",
        "short_label": "光学衍射神经网络",
        "topic": "光学衍射神经网络：从物理传播到智能推理",
        "accent": "cyan",
    },
    "rhr_metasurface_holography_20260828_v1": {
        "label": "超表面全息",
        "short_label": "超表面全息",
        "topic": "超表面全息：从逆向设计、制造到动态显示与成像应用",
        "accent": "violet",
    },
    "rhr_photonic_computing_20260829_v1": {
        "label": "规模化光子计算",
        "short_label": "规模化光子计算",
        "topic": "写一篇关于规模化光子计算的文献综述，阐述从可编程集成光子芯片到 AI 加速与光互连",
        "accent": "amber",
    },
}


def export_collection(run_dirs: List[Path], out_dir: Path) -> int:
    """Export several historical runs into one static observatory bundle."""
    if not run_dirs:
        print("[export] collection has no run directories", file=sys.stderr)
        return 2
    for run_dir in run_dirs:
        if not (run_dir / "HARNESS_EVENTS.jsonl").is_file():
            print("[export] missing HARNESS_EVENTS.jsonl under %s" % run_dir, file=sys.stderr)
            return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    copy_player(PROJECT_ROOT / "optomind_ui" / "static" / "dist", out_dir)
    (out_dir / ".nojekyll").write_text("", encoding="ascii")
    runs_out = out_dir / "runs"
    runs_out.mkdir(parents=True, exist_ok=True)
    staging_root = out_dir / ".staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)

    entries: List[Dict[str, Any]] = []
    try:
        for run_dir in run_dirs:
            stage_out = staging_root / run_dir.name
            code = export(run_dir, stage_out)
            if code != 0:
                return code
            data_path = stage_out / "replay-data.json"
            data = json.loads(data_path.read_text(encoding="utf-8"))
            target_dir = runs_out / run_dir.name
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(data_path, target_dir / "replay-data.json")

            presentation = _COLLECTION_PRESENTATION.get(run_dir.name, {})
            snapshot = data.get("snapshot") if isinstance(data, dict) else {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            run = snapshot.get("run") if isinstance(snapshot, dict) else {}
            run = run if isinstance(run, dict) else {}
            entries.append(
                {
                    "id": run_dir.name,
                    "label": presentation.get("label", "历史研究运行"),
                    "short_label": presentation.get("short_label", "历史研究运行"),
                    "topic": presentation.get("topic") or run.get("question") or "历史研究运行",
                    "data_path": "runs/%s/replay-data.json" % run_dir.name,
                    "accent": presentation.get("accent", "cyan"),
                    "status": run.get("status", ""),
                    "cost_cny": run.get("cost_cny", 0),
                }
            )
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)

    manifest = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "runs": entries,
    }
    (out_dir / "replay-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print("[export] wrote collection %s (%d runs)" % (out_dir, len(entries)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export one or more runs into a static replay bundle")
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run_dirs = [path.resolve() for path in args.run_dir]
    if len(run_dirs) == 1:
        return export(run_dirs[0], args.out.resolve())
    return export_collection(run_dirs, args.out.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
