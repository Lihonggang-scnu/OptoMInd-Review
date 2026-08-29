"""Narrative projection layer (F1).

Turns the raw harness artifacts of a run directory into human sentences
and small, bounded metrics -- the ONLY module allowed to translate
machine language for the UI. Two hard rules:

* Field whitelist: an event dict passes through whitelisted keys only;
  anything else is DROPPED outright (never truncated, never sent).
* Server-side truncation: any value whose UTF-8 form exceeds
  ``_MAX_VALUE_BYTES`` is cut to that budget plus a visible marker, and
  the line is flagged ``{"truncated": true, "raw_bytes": N}`` so a
  client can offer an "expand raw JSON" affordance.

All key names below mirror the real artifacts exactly as recorded in
the W0 baseline audit (前端助手工作日志.md, ticket 01_W0):
HARNESS_COST.json / S2_MATERIAL_FLOW_LEDGER.json /
S2_QUERY_TELEMETRY.json / S2_LITERATURE_GRAPH.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from optomind_ui.stage_registry import stage_label

_MAX_VALUE_BYTES = 2000
_LINES_CAP = 300  # newest N narrative lines kept in the projection
_S2_DIR = "s2_literature_intelligence"
# Below this, a whitelist drop is internal bookkeeping (flags, small ids)
# and flagging it would badge 41 of the reference run's 49 rows with
# noise like "8 字节". Only withholdings a reader might actually wonder
# about are surfaced; the real case is line 6 at 3,385,451 bytes.
_MIN_WITHHELD_BYTES = 4096

# Event keys allowed through to the UI. Everything else is dropped.
# (W0: the topic_scoped_kb stage_finished monster carries multi-MB
# "selection"/"evidence" payloads -- neither is whitelisted.)
_EVENT_KEY_WHITELIST = frozenset(
    {
        "timestamp",
        "event",
        "stage",
        "status",
        "run_id",
        "cost_cny",
        "estimated_cost_cny",
        "input_tokens",
        "output_tokens",
        "wall_time_seconds",
        "error",
        "reused",
        "retrieval_scope",
        "source_base_asset_role",
        "manifest_path",
    }
)

_EVENT_NAMES = {
    "stage_started": "阶段开始",
    "stage_finished": "阶段完成",
}

_RETRIEVAL_CATEGORIES = (
    "discovery_search",
    "snippet_search",
    "citations",
    "references",
    "recommendations",
    "multi_seed_recommendations",
    "batch_enrichment",
    "paper_get",
    "title_match",
)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_events(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "HARNESS_EVENTS.jsonl"
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _clip(value: Any) -> Tuple[str, bool, int]:
    """Return (display_value, was_truncated, raw_byte_length)."""

    text = _stringify(value)
    raw = text.encode("utf-8")
    size = len(raw)
    if size <= _MAX_VALUE_BYTES:
        return text, False, size
    head = raw[:_MAX_VALUE_BYTES].decode("utf-8", errors="ignore")
    return f"{head}…（已截断，原长 {size} 字节）", True, size


def _project_event(row: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, int, int, int]:
    """Whitelist-filter one event row; clip any oversized passing value.

    Returns ``(projected, truncated, raw_bytes, dropped_fields, dropped_bytes)``.

    The dropped counters carry SIZES ONLY -- never a byte of dropped
    content. Without them a whitelist drop is invisible: the reference
    run's line 6 is 3,386,117 bytes, of which ``selection`` (2,681,446)
    and ``evidence`` (703,877) are dropped, leaving a row that reports
    293 bytes and ``truncated: false``. The UI then has no way to say
    that a multi-MB payload was withheld on purpose.
    """

    projected: Dict[str, Any] = {}
    truncated = False
    total_raw_bytes = 0
    dropped_fields = 0
    dropped_bytes = 0
    for key in sorted(row):
        if key not in _EVENT_KEY_WHITELIST:
            dropped_fields += 1
            dropped_bytes += len(_stringify(row[key]).encode("utf-8"))
            continue  # drop, never send
        clipped, was_truncated, raw_bytes = _clip(row[key])
        projected[key] = clipped
        truncated = truncated or was_truncated
        total_raw_bytes += raw_bytes
    return projected, truncated, total_raw_bytes, dropped_fields, dropped_bytes


def _event_sentence(row: Dict[str, Any]) -> str:
    event = str(row.get("event") or "")
    label = stage_label(str(row.get("stage") or ""))
    if event == "stage_started":
        return f"开始：{label}" if label else "阶段开始"
    if event == "stage_finished":
        wall = row.get("wall_time_seconds")
        cost = row.get("cost_cny")
        status = str(row.get("status") or "")
        pieces = [f"完成：{label}" if label else "阶段完成"]
        if isinstance(wall, (int, float)):
            pieces.append(f"耗时 {wall:g}s")
        if isinstance(cost, (int, float)):
            pieces.append(f"¥{cost:.4f}")
        if status and status != "completed":
            pieces.append(f"状态 {status}")
        return " · ".join(pieces)
    name = _EVENT_NAMES.get(event, event or "事件")
    return f"{name}（{label}）" if label else name


def project_line(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project ONE event row exactly like the lines built by build().

    Public on purpose: the SSE stream (F2) pushes single rows as they
    land and MUST go through the same whitelist + truncation as the
    batch projection -- no raw line ever reaches the client.
    """

    projected, truncated, raw_bytes, dropped_fields, dropped_bytes = _project_event(row)
    event = str(row.get("event") or "")
    line: Dict[str, Any] = {
        "ts": str(row.get("timestamp") or row.get("ts") or ""),
        "raw_event": event,
        "event": _EVENT_NAMES.get(event, event or "事件"),
        "stage": str(row.get("stage") or ""),
        "stage_label": stage_label(str(row.get("stage") or "")),
        "text": _event_sentence(row),
        "truncated": truncated,
        "raw_bytes": raw_bytes,
        "data": projected,
    }
    if dropped_bytes >= _MIN_WITHHELD_BYTES:
        # Sizes only -- the withheld content itself never leaves the server.
        line["withheld_fields"] = dropped_fields
        line["withheld_bytes"] = dropped_bytes
    return line


def _current_stage(state: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    current = str(state.get("current_stage") or "")
    if current:
        return current
    for row in rows:
        if row.get("event") == "stage_started":
            current = str(row.get("stage") or current)
    return current


def _int_sum(mapping: Any) -> int:
    if not isinstance(mapping, dict):
        return 0
    return int(sum(v for v in mapping.values() if isinstance(v, (int, float))))


def _union_stage_keys(cost_stages: Any, state_stages: Any) -> List[str]:
    """Every stage key present in either ledger, cost-ledger order first.

    Summing the cost ledger alone under-reports total wall time: on the
    reference run ``packaging`` completed in 96.094 s but has no cost
    entry, so its time vanished from the displayed total.
    """

    keys: List[str] = []
    for source in (cost_stages, state_stages):
        if not isinstance(source, dict):
            continue
        for key in source:
            name = str(key)
            if name not in keys:
                keys.append(name)
    return keys


def _stage_wall_time(
    stage: str,
    cost_stages: Any,
    state_stages: Any,
) -> Optional[float]:
    """Wall time for one stage: cost ledger first, state ledger second.

    Neither ledger alone is complete, verified against the reference run:

    * ``query_planner`` has no ``HARNESS_STATE.json`` stage entry but does
      bill 20.86 s in the cost ledger.
    * ``packaging`` finished (96.094 s in the state ledger) yet never
      appears in the cost ledger at all.
    * ``authoring_revision`` records 0.0 in the state ledger against
      1202.0 in the cost ledger, so the cost ledger must win ties.

    Stages that genuinely never ran (``latex_publication_zh``, status
    ``disabled_translation_failed``) are absent from both and correctly
    yield ``None`` rather than a fabricated zero.
    """

    if not stage:
        return None
    for source in (cost_stages, state_stages):
        if not isinstance(source, dict):
            continue
        entry = source.get(stage)
        if not isinstance(entry, dict):
            continue
        wall = entry.get("wall_time_seconds")
        if isinstance(wall, (int, float)):
            return float(wall)
    return None


def build(run_dir: Path) -> Dict[str, Any]:
    """Project one run directory into the bounded narrative dict."""

    run_dir = Path(run_dir)
    cost = _read_json(run_dir / "HARNESS_COST.json")
    state = _read_json(run_dir / "HARNESS_STATE.json")
    s2_dir = run_dir / _S2_DIR
    ledger = _read_json(s2_dir / "S2_MATERIAL_FLOW_LEDGER.json")
    telemetry = _read_json(s2_dir / "S2_QUERY_TELEMETRY.json")
    graph = _read_json(s2_dir / "S2_LITERATURE_GRAPH.json")
    rows = _read_events(run_dir)  # read ONCE: the reference stream is 3.4 MB

    # ---- metrics: disk values only, no estimation ----
    papers = ledger.get("papers")
    ledger_summary = ledger.get("summary") or {}
    paper_count = (
        len(papers) if isinstance(papers, list)
        else int(ledger_summary.get("paper_count") or 0)
    )
    graph_summary = graph.get("summary") or {}
    query_runs = graph.get("query_runs")
    cost_stages = cost.get("stages") or {}
    state_stages = state.get("stages") or {}
    current_stage = _current_stage(state, rows)
    current_wall = _stage_wall_time(current_stage, cost_stages, state_stages)
    total_wall = sum(
        wall
        for wall in (
            _stage_wall_time(key, cost_stages, state_stages)
            for key in _union_stage_keys(cost_stages, state_stages)
        )
        if wall is not None
    )
    category_counts = telemetry.get("category_counts") or {}
    metrics: Dict[str, Any] = {
        "cost_cny": cost.get("cost_cny"),
        "global_cost_budget_cny": cost.get("global_cost_budget_cny"),
        "remaining_budget_cny": cost.get("remaining_budget_cny"),
        "model_call_count": cost.get("model_call_count"),
        "papers_ingested": paper_count,
        "admitted_paper_count": ledger_summary.get("admitted_paper_count"),
        "s2_body_paper_count": ledger_summary.get("s2_body_paper_count"),
        "oa_fulltext_paper_count": ledger_summary.get("oa_fulltext_paper_count"),
        "abstract_claim_paper_count": ledger_summary.get("abstract_claim_paper_count"),
        "total_query_count": telemetry.get("total_query_count"),
        "graph_query_count": telemetry.get("graph_query_count"),
        "cache_hit_total": _int_sum(telemetry.get("cache_hit_counts")),
        "failed_query_total": _int_sum(telemetry.get("failed_counts")),
        "literature_node_count": graph_summary.get("node_count"),
        "literature_edge_count": graph_summary.get("edge_count"),
        "query_runs": len(query_runs) if isinstance(query_runs, list) else query_runs,
        "current_stage_wall_time_seconds": current_wall,
        "total_wall_time_seconds": round(total_wall, 3) if total_wall else 0.0,
    }

    # ---- headline / detail ----
    top_category = ""
    top_count = 0
    for name in _RETRIEVAL_CATEGORIES:
        count = category_counts.get(name)
        if isinstance(count, (int, float)) and count > top_count:
            top_category, top_count = name, int(count)
    if current_stage == "s2_literature_intelligence" and top_category:
        headline = f"正在用 {top_category} 检索文献"
    elif current_stage:
        headline = f"当前阶段：{stage_label(current_stage)}"
    else:
        headline = "等待启动"
    details: List[str] = []
    tqc = metrics.get("total_query_count")
    if isinstance(tqc, int):
        details.append(f"已发起 {tqc} 次查询")
    if paper_count:
        details.append(f"已入库 {paper_count} 篇")
    cache_hits = metrics.get("cache_hit_total")
    if isinstance(cache_hits, int):
        details.append(f"命中缓存 {cache_hits} 次")
    detail = " · ".join(details)

    # ---- narrative log lines ----
    lines: List[Dict[str, Any]] = [
        project_line(row) for row in rows[-_LINES_CAP:]
    ]

    return {
        "headline": headline,
        "detail": detail,
        "metrics": metrics,
        "lines": lines,
    }
