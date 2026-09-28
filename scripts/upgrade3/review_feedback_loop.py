"""Run one bounded arrangement-feedback -> owner -> arrangement -> writer loop.

The command is deliberately chapter-scoped.  It consumes a human or caller
supplied issue envelope, lets the existing affected-chapter owner revise the
authoritative packet, and then calls the existing arrangement and unit writer
entry points once each.  Preview is the default; ``--run`` is the only mode
that reads a key or contacts Qwen.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from optomind_research.runtime.upgrade3.module4.runtime import (
    GlobalBudgetLedger,
    QwenDirectClient,
    estimated_cost_cny,
)


DEFAULT_LEDGER = PROJECT_ROOT / "outputs/review_blueprint/20260922_phase1/budget.sqlite"
DEFAULT_KEY = PROJECT_ROOT / "api_keys/qwen-api-key.txt"
DEFAULT_TOKENIZER = planning.DEFAULT_TOKENIZER_PATH


def _json(path: Path, *, label: str) -> Any:
    if not path.is_file():
        raise ValueError(f"{label}_missing:{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"{label}_invalid_json:{path}") from exc


def _issues(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("issues") or value.get("arrangement_issues") or []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("feedback_issues_must_be_list")
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _budget_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        limit = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
        actual = db.execute("SELECT COALESCE(SUM(actual_cny),0) FROM reservations").fetchone()[0]
        held = db.execute(
            "SELECT COALESCE(SUM(amount_cny),0) FROM reservations "
            "WHERE status IN ('reserved','uncertain')"
        ).fetchone()[0]
    cap = float(limit[0]) if limit else None
    return {
        "limit_cny": cap,
        "actual_cny": float(actual or 0.0),
        "held_cny": float(held or 0.0),
        "available_cny": cap - float(actual or 0.0) - float(held or 0.0) if cap is not None else None,
    }


def _chapter_outline(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
    return [{
        "chapter_id": str(chapter.get("chapter_id") or ""),
        "title": str(chapter.get("title") or ""),
        "purpose": str(chapter.get("purpose") or ""),
    }]


class FeedbackLoop:
    """Concrete adapters for the three existing runtime entry points."""

    def __init__(self, args: argparse.Namespace, *, output_dir: Path, packet: Mapping[str, Any]):
        self.args = args
        self.output_dir = output_dir
        self.packet = packet
        self.counter = planning.qwen_local_token_counter(Path(args.tokenizer))
        self.ledger = GlobalBudgetLedger(path=Path(args.budget_ledger))
        self.budget_limit = args.budget_limit_cny
        if self.budget_limit is None:
            snapshot = _budget_snapshot(Path(args.budget_ledger)) or {}
            self.budget_limit = snapshot.get("limit_cny")
        self.pool_rows = []
        if args.pool:
            self.pool_rows = planning.load_planning_pool(Path(args.pool).resolve())
        self.plan = _json(Path(args.plan), label="plan") if args.plan else dict(packet)
        cfg = planning.ProgressivePlannerConfig(
            topic_id=str(packet.get("topic_id") or "feedback-loop"),
            pool_path=Path(args.pool).resolve() if args.pool else Path(args.packet).resolve(),
            plan_path=Path(args.plan).resolve() if args.plan else Path(args.packet).resolve(),
            output_dir=output_dir,
            shared_deep_read_budget=int(args.deep_read_budget),
            chapter_workers=1,
            reader_workers=1,
            planning_revision_enabled=True,
            local_material_index_path=(Path(args.local_index).resolve() if args.local_index else None),
        )
        if self.budget_limit is not None:
            self.supplement_runner = planning.make_planning_supplement_runner(
                cfg, key_file=Path(args.key_file), budget_ledger_path=Path(args.budget_ledger),
                budget_limit_cny=float(self.budget_limit),
                local_index_path=(Path(args.local_index).resolve() if args.local_index else None),
                allow_external=not args.no_external_tools,
            )
            self.directed_reader = planning.make_directed_reading_runner(
                cfg, key_file=Path(args.key_file), budget_ledger_path=Path(args.budget_ledger),
                budget_limit_cny=float(self.budget_limit), prior_readings=(),
            )
        else:
            self.supplement_runner = None
            self.directed_reader = None
        self.owner = planning.QwenProgressivePlanner(
            model=args.owner_model,
            key_file=Path(args.key_file),
            budget_ledger_path=Path(args.budget_ledger),
            budget_limit_cny=None,
            output_dir=output_dir / "owner",
            tokenizer_path=Path(args.tokenizer),
            timeout_seconds=args.timeout_seconds,
            thinking_budget=args.owner_thinking_budget,
            output_tokens=args.owner_output_tokens,
            chapter_model=args.owner_model,
        )

    def owner_planner(self, stage: str, payload: Mapping[str, Any]) -> Any:
        return self.owner(stage, payload)

    def feedback_action_runner(
        self,
        issues: Sequence[Mapping[str, Any]],
        packet: Mapping[str, Any],
        output_dir: Path,
    ) -> Mapping[str, Any]:
        """Route tool actions through the already configured local/tool adapters."""

        source_rows = [dict(item) for item in (packet.get("source_materials") or ()) if isinstance(item, Mapping)]
        source_rows.extend(dict(item) for item in (packet.get("candidate_materials") or ()) if isinstance(item, Mapping))
        requested_local_handles = {
            str(value)
            for issue in issues
            if planning._feedback_action(issue) == "local_backfill"
            for value in ((issue.get("source_handles") or []) if not isinstance(issue.get("source_handles"), str) else [issue.get("source_handles")])
            if str(value or "")
        }
        handles = {
            str(item.get("source_handle")) for item in source_rows
            if str(item.get("source_handle") or "") in requested_local_handles
        }
        local_rows = planning._refresh_local_material_snapshots([{"source_materials": source_rows}])[0].get("source_materials") or []
        local_updates = [dict(item) for item in local_rows if str(item.get("source_handle") or "") in handles]
        local_issues = [issue for issue in issues if planning._feedback_action(issue) == "local_backfill"]
        local_unmet = [
            dict(issue) for issue in local_issues
            if not requested_local_handles
            or any(str(value) not in handles for value in (
                (issue.get("source_handles") or [])
                if not isinstance(issue.get("source_handles"), str)
                else [issue.get("source_handles")]
            ))
        ]
        supplement_requests: list[dict[str, Any]] = []
        directed_requests: list[dict[str, Any]] = []
        owner_feedback: list[dict[str, Any]] = []
        for issue in issues:
            action = planning._feedback_action(issue)
            chapter_id = str((packet.get("chapter") or {}).get("chapter_id") or issue.get("chapter_id") or "")
            if action == "local_backfill":
                owner_feedback.append({
                    "action": "chapter_owner",
                    "problem": str(issue.get("problem") or "Review the locally backfilled A/B material."),
                    "source_handles": issue.get("source_handles") or [],
                    "issue_id": issue.get("issue_id") or "local-backfill",
                })
            elif action == "supplement":
                supplement_requests.append({
                    **dict(issue),
                    "gap_id": issue.get("gap_id") or issue.get("issue_id") or f"feedback-gap-{len(supplement_requests)+1}",
                    "gap_question": issue.get("gap_question") or issue.get("question") or issue.get("problem") or "Resolve the requested evidence gap.",
                    "chapter_ids": issue.get("chapter_ids") or ([chapter_id] if chapter_id else []),
                    "targeted_queries": issue.get("targeted_queries") or [],
                })
            elif action == "directed_read":
                questions = issue.get("questions") or issue.get("question") or issue.get("problem") or "Extract the requested source-based material."
                if isinstance(questions, str):
                    questions = [{"question": questions}]
                directed_requests.append({
                    **dict(issue),
                    "paper_id": issue.get("paper_id") or next((item.get("paper_id") for item in source_rows if item.get("source_handle") in (issue.get("source_handles") or [])), ""),
                    "chapter_ids": issue.get("chapter_ids") or ([chapter_id] if chapter_id else []),
                    "questions": questions,
                    "required_outputs": issue.get("required_outputs") or [],
                })
        tool_materials: list[dict[str, Any]] = []
        feedback_materials: list[dict[str, Any]] = []
        tool_statuses: list[str] = []
        if supplement_requests:
            if self.supplement_runner is None:
                tool_statuses.append("unavailable")
            else:
                raw = dict(self.supplement_runner(
                    supplement_requests,
                    phase="feedback_supplement",
                    output_dir=output_dir / "supplements",
                    pool_rows=self.pool_rows,
                    plan=self.plan,
                    source_handle_map={str(row.get("_source_handle")): str(row.get("_paper_id")) for row in self.pool_rows if isinstance(row, Mapping)},
                ))
                tool_statuses.append(str(raw.get("status") or "partial"))
                feedback_materials.extend(dict(item) for item in (raw.get("results") or []) if isinstance(item, Mapping))
                # Normalize raw source units into the same candidate-row shape
                # consumed by the existing pool merge.  New supplement papers
                # commonly carry only canonical identity and card_path; their
                # program handle is assigned by _refresh_source_handles.
                supplement_rows: list[dict[str, Any]] = []
                supplement_paper_ids: set[str] = set()
                pending = list(raw.get("results") or [])
                while pending:
                    result = pending.pop(0)
                    if not isinstance(result, Mapping):
                        continue
                    pending.extend(result.get("results") or [])
                    for source in result.get("source_units") or []:
                        if not isinstance(source, Mapping):
                            continue
                        identity = source.get("record_identity") if isinstance(source.get("record_identity"), Mapping) else {}
                        row = {**dict(identity), **dict(source)}
                        paper_id = str(
                            row.get("paper_id") or row.get("canonical_paper_id")
                            or identity.get("paper_id") or identity.get("canonical_paper_id") or ""
                        )
                        if not paper_id:
                            continue
                        row["paper_id"] = paper_id
                        row["supplement_gap_material"] = dict(result)
                        supplement_rows.append(row)
                        supplement_paper_ids.add(paper_id)
                if supplement_rows:
                    merged_pool = [dict(row) for row in self.pool_rows if isinstance(row, Mapping)]
                    planning._merge_supplement_pool_updates(
                        merged_pool, {"supplement_results": [{"candidate_rows": supplement_rows}]}
                    )
                    for row in merged_pool:
                        paper_id = str(row.get("_paper_id") or row.get("paper_id") or "")
                        if paper_id not in supplement_paper_ids:
                            continue
                        material = planning.build_local_material_payload(row)
                        if not material.get("source_handle"):
                            continue
                        material["chapter_ids"] = list(dict.fromkeys(
                            str(value) for request in supplement_requests
                            for value in (request.get("chapter_ids") or []) if str(value)
                        ))
                        if row.get("supplement_gap_material"):
                            material["supplement_gap_material"] = dict(row["supplement_gap_material"])
                        tool_materials.append(material)
        if directed_requests:
            if self.directed_reader is None:
                tool_statuses.append("unavailable")
            else:
                raw = dict(self.directed_reader(
                    directed_requests,
                    phase="feedback_directed_read",
                    output_dir=output_dir / "directed",
                    pool_by_id={str(row.get("_paper_id")): row for row in self.pool_rows if isinstance(row, Mapping)},
                    plan=self.plan,
                ))
                tool_statuses.append(str(raw.get("status") or "partial"))
                feedback_materials.extend(dict(item) for item in (raw.get("results") or []) if isinstance(item, Mapping))
                by_paper = {str(item.get("paper_id")): str(item.get("source_handle")) for item in source_rows if item.get("paper_id")}
                for result in raw.get("results") or []:
                    if not isinstance(result, Mapping):
                        continue
                    handle = str(result.get("source_handle") or by_paper.get(str(result.get("paper_id"))) or "")
                    material = result.get("material") if isinstance(result.get("material"), Mapping) else result.get("deep_read_material")
                    if handle and isinstance(material, Mapping):
                        tool_materials.append({
                            "source_handle": handle,
                            "paper_id": result.get("paper_id"),
                            "deep_read_material": dict(material),
                        })
        status = "fulfilled" if (
            not local_unmet
            and (not tool_statuses or all(item in {"complete", "completed", "fulfilled", "local_material_ready", "reused"} for item in tool_statuses))
        ) else "partial"
        unmet = [
            *local_unmet,
            *[
                dict(issue) for issue in issues
                if planning._feedback_action(issue) != "local_backfill" and status != "fulfilled"
            ],
        ]
        return {
            "status": status,
            "source_materials": local_updates + tool_materials,
            "tool_materials": tool_materials,
            "feedback_materials": feedback_materials,
            "owner_feedback": owner_feedback,
            "unmet_actions": unmet,
            "tool_statuses": tool_statuses,
        }

    def arrangement_runner(
        self,
        updated_packet: Mapping[str, Any],
        previous_arrangement: Mapping[str, Any],
        output_dir: Path,
    ) -> Mapping[str, Any]:
        packet_path = output_dir / "UPDATED_WRITER_PACKET.json"
        outline = _chapter_outline(updated_packet)
        chapter = updated_packet.get("chapter") if isinstance(updated_packet.get("chapter"), Mapping) else {}
        view = arranging.build_chapter_view(
            packet_path,
            shared_outline=outline,
            review_argument=str(chapter.get("purpose") or updated_packet.get("review_argument") or ""),
            id_map_path=output_dir / "ID_MAP.json",
        )
        payload = view.arrangement_payload(max_source_chars=self.args.max_source_chars)
        payload["planning_revision_mode"] = True
        arranging.write_view(view, output_dir / "ARRANGEMENT_INPUT.json")
        client = QwenDirectClient(
            model=self.args.arrangement_model,
            key_file=Path(self.args.key_file),
            max_retries=0,
            timeout_seconds=self.args.timeout_seconds,
            max_output_tokens=self.args.arrangement_output_tokens,
            thinking=True,
            thinking_budget=self.args.arrangement_thinking_budget,
            json_mode=False,
            budget_ledger=self.ledger,
            raw_response_dir=output_dir / "raw_responses" / "arrangement",
            prompt_token_counter=self.counter,
            prompt_token_multiplier=planning.TOKEN_MARGIN_MULTIPLIER,
            prompt_token_framing_margin=planning.TOKEN_FRAMING_MARGIN,
        )
        result = arranging.run_arrangement(
            view,
            client=client,
            model=self.args.arrangement_model,
            prompt=arranging.load_editor_prompt(planning_revision=True),
            view_payload=payload,
            call_id="planning-revision:wp4-feedback:arrangement",
            raw_response_dir=output_dir / "raw_responses" / "arrangement",
            planning_revision=True,
        )
        # The model supplies task placement; the program reattaches the local
        # material catalogue so the following writer can use the same packet.
        exported = dict(result)
        exported["source_catalog"] = arranging.build_source_catalog(view, result)
        exported["unit_id_remap"] = dict(updated_packet.get("unit_id_remap") or {})
        return exported

    def writer_runner(
        self,
        updated_packet: Mapping[str, Any],
        rebuilt_arrangement: Mapping[str, Any],
        output_dir: Path,
    ) -> Mapping[str, Any]:
        arrangement_path = output_dir / "_ARRANGEMENT_FOR_WRITER.json"
        arrangement_path.write_text(
            json.dumps(dict(rebuilt_arrangement), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        units = rebuilt_arrangement.get("units") or []
        known_ids = [str(item.get("unit_id") or "") for item in units if isinstance(item, Mapping) and str(item.get("unit_id") or "")]
        unit_id = self.args.unit
        if unit_id and unit_id not in known_ids:
            remap = rebuilt_arrangement.get("unit_id_remap") or updated_packet.get("unit_id_remap") or {}
            candidates = [new_id for new_id, old_ids in remap.items() if unit_id in (old_ids if isinstance(old_ids, list) else [old_ids])]
            if len(candidates) == 1 and candidates[0] in known_ids:
                unit_id = candidates[0]
            else:
                return {"status": "partial", "affected_units": known_ids, "requested_unit": self.args.unit}
        if not unit_id:
            if len(known_ids) == 1:
                unit_id = known_ids[0]
            else:
                return {"status": "partial", "affected_units": known_ids, "reason": "selected_unit_required_for_multiple_units"}
        if not unit_id:
            raise ValueError("writer_unit_missing")
        view = writing.build_unit_view(
            arrangement_path,
            unit_id,
            view_path=output_dir / "ARRANGEMENT_INPUT.json",
            max_material_chars_per_source=self.args.max_material_chars,
        )
        payload = writing.unit_payload(view, language=self.args.language)
        payload["planning_revision_mode"] = True
        prompt = writing.load_writer_prompt(planning_revision=True)
        messages = writing.unit_messages(
            view, prompt=prompt, language=self.args.language, payload=payload,
            planning_revision=True,
        )
        estimate = writing.estimate_unit_cost(
            messages, model=self.args.writer_model,
            output_tokens=self.args.writer_output_tokens,
            thinking_budget=self.args.writer_thinking_budget,
            token_counter=self.counter,
        )
        unit_dir = output_dir / "writer" / f"{view.chapter_id}_{view.unit_id}"
        writing.write_unit_input(view, messages, unit_dir, estimate=estimate, language=self.args.language)
        client = QwenDirectClient(
            model=self.args.writer_model,
            key_file=Path(self.args.key_file),
            max_retries=0,
            timeout_seconds=self.args.timeout_seconds,
            max_output_tokens=self.args.writer_output_tokens,
            thinking=bool(self.args.writer_thinking_budget),
            thinking_budget=self.args.writer_thinking_budget,
            json_mode=False,
            budget_ledger=self.ledger,
            raw_response_dir=unit_dir / "raw_responses",
            prompt_token_counter=self.counter,
            prompt_token_multiplier=planning.TOKEN_MARGIN_MULTIPLIER,
            prompt_token_framing_margin=planning.TOKEN_FRAMING_MARGIN,
        )
        result = writing.run_unit_writing(
            view,
            client=client,
            model=self.args.writer_model,
            prompt=prompt,
            language=self.args.language,
            payload=payload,
            raw_response_dir=unit_dir / "raw_responses",
            planning_revision=True,
        )
        return writing.write_unit_output(
            view,
            result["body_markdown"],
            unit_dir,
            model=result.get("model") or self.args.writer_model,
            language=self.args.language,
            mode="run",
            used_messages=result["messages"],
            estimate=estimate,
            usage=result.get("usage") or {},
            response_path=result.get("raw_response") or "",
            finish_reason=result.get("finish_reason") or "",
            complete=result.get("complete", True),
            partial_error=result.get("partial_error") or "",
            issues=result.get("issues") or [],
        )


def _preflight(args: argparse.Namespace, packet: Mapping[str, Any], arrangement: Mapping[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    chapter = packet.get("chapter") if isinstance(packet.get("chapter"), Mapping) else {}
    chapter_plan = packet.get("chapter_plan") if isinstance(packet.get("chapter_plan"), Mapping) else {}
    owner_payload = planning.build_arrangement_issue_revision_payload(
        chapter=chapter,
        chapter_plan=chapter_plan,
        source_materials=packet.get("source_materials") or [],
        arrangement={**dict(arrangement), "issues": issues},
        topic_id="feedback-loop",
        research_question=str(packet.get("research_question") or ""),
    )
    counter = planning.qwen_local_token_counter(Path(args.tokenizer))
    owner_messages = planning._messages_for("affected_chapter_revision", owner_payload)
    owner_prompt_tokens = int(counter(b"", owner_messages))
    owner_cost = estimated_cost_cny(
        {"prompt_tokens": int(owner_prompt_tokens * planning.TOKEN_MARGIN_MULTIPLIER) + planning.TOKEN_FRAMING_MARGIN,
         "completion_tokens": args.owner_output_tokens + args.owner_thinking_budget},
        model=args.owner_model,
        conservative=True,
    )
    packet_path = args.packet.resolve()
    view = arranging.build_chapter_view(
        packet_path,
        shared_outline=_chapter_outline(packet),
        review_argument=str(chapter.get("purpose") or ""),
        id_map_path=args.output_root.resolve() / "ID_MAP.json",
    )
    arrangement_payload = view.arrangement_payload(max_source_chars=args.max_source_chars)
    arrangement_messages = arranging.arrangement_messages(
        {**arrangement_payload, "planning_revision_mode": True},
        prompt=arranging.load_editor_prompt(planning_revision=True),
        planning_revision=True,
    )
    arrangement_estimate = arranging.estimate_arrangement_cost(
        arrangement_messages, model=args.arrangement_model,
        output_tokens=args.arrangement_output_tokens,
        thinking_budget=args.arrangement_thinking_budget,
    )
    unit_id = args.unit or (str((arrangement.get("units") or [{}])[0].get("unit_id") or ""))
    writer_estimate = None
    if unit_id:
        writer_view = writing.build_unit_view(
            args.arrangement.resolve(), unit_id,
            view_path=(args.arrangement.parent / "ARRANGEMENT_INPUT.json") if (args.arrangement.parent / "ARRANGEMENT_INPUT.json").is_file() else None,
            max_material_chars_per_source=args.max_material_chars,
        )
        writer_messages = writing.unit_messages(
            writer_view, prompt=writing.load_writer_prompt(planning_revision=True),
            language=args.language, planning_revision=True,
        )
        writer_estimate = writing.estimate_unit_cost(
            writer_messages, model=args.writer_model,
            output_tokens=args.writer_output_tokens,
            thinking_budget=args.writer_thinking_budget,
            token_counter=counter,
        )
    estimates = {"owner": {"estimated_cost_cny": owner_cost, "prompt_tokens": owner_prompt_tokens},
                 "arrangement": arrangement_estimate, "writer": writer_estimate}
    total = owner_cost + float(arrangement_estimate.get("estimated_cost_cny") or 0.0) + float((writer_estimate or {}).get("estimated_cost_cny") or 0.0)
    return {
        "status": "preflight_only",
        "network_calls": 0,
        "issues": issues,
        "estimates": estimates,
        "estimated_total_cny": total,
        "budget": _budget_snapshot(args.budget_ledger),
        "unit_id": unit_id,
        "notes": ["估算覆盖 owner、一次 arrangement、一次 writer；真实用量以 ledger 结算为准。"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One bounded owner revision and downstream rebuild loop")
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--arrangement", type=Path, required=True)
    parser.add_argument("--issues", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Perform the three real Qwen calls")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--budget-ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--budget-limit-cny", type=float, default=None,
                        help="Use the existing ledger cap; omit to read it from the ledger")
    parser.add_argument("--pool", type=Path, default=None,
                        help="Existing planning pool for directed/supplement feedback actions")
    parser.add_argument("--plan", type=Path, default=None,
                        help="Existing original plan for configured feedback runners")
    parser.add_argument("--local-index", type=Path, default=None,
                        help="Existing local material index for supplement triage")
    parser.add_argument("--deep-read-budget", type=int, default=0,
                        help="Existing remaining deep-read allowance for directed feedback")
    parser.add_argument("--no-external-tools", action="store_true",
                        help="Route through local/tool adapters without external retrieval")
    parser.add_argument("--max-live-cny", type=float, default=2.0)
    parser.add_argument("--tokenizer", type=Path, default=DEFAULT_TOKENIZER)
    parser.add_argument("--owner-model", default="qwen3.5-plus")
    parser.add_argument("--arrangement-model", default="qwen3.5-plus")
    parser.add_argument("--writer-model", default=writing.DEFAULT_MODEL)
    parser.add_argument("--owner-output-tokens", type=int, default=12000)
    parser.add_argument("--owner-thinking-budget", type=int, default=2048)
    parser.add_argument("--arrangement-output-tokens", type=int, default=5000)
    parser.add_argument("--arrangement-thinking-budget", type=int, default=1024)
    parser.add_argument("--writer-output-tokens", type=int, default=4000)
    parser.add_argument("--writer-thinking-budget", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-source-chars", type=int, default=1200)
    parser.add_argument("--max-material-chars", type=int, default=writing.DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE)
    parser.add_argument("--unit", default="")
    parser.add_argument("--language", default="zh")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.packet = args.packet.resolve()
        args.arrangement = args.arrangement.resolve()
        args.issues = args.issues.resolve()
        args.output_root = args.output_root.resolve()
        packet = _json(args.packet, label="packet")
        arrangement = _json(args.arrangement, label="arrangement")
        if not isinstance(packet, Mapping) or not isinstance(arrangement, Mapping):
            raise ValueError("packet_and_arrangement_must_be_objects")
        issues = _issues(_json(args.issues, label="issues"))
        estimate = _preflight(args, packet, arrangement, issues)
        args.output_root.mkdir(parents=True, exist_ok=True)
        (args.output_root / "FEEDBACK_ISSUES.json").write_text(
            json.dumps({"issues": issues}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not args.run:
            result = {**estimate, "output_root": str(args.output_root)}
        else:
            if estimate["estimated_total_cny"] > args.max_live_cny:
                raise ValueError(
                    f"preflight_estimate_above_live_cap:{estimate['estimated_total_cny']:.6f}>{args.max_live_cny:.6f}"
                )
            loop = FeedbackLoop(args, output_dir=args.output_root, packet=packet)
            fed_arrangement = dict(arrangement)
            fed_arrangement["issues"] = issues
            (args.output_root / "INPUT_ARRANGEMENT_WITH_FEEDBACK.json").write_text(
                json.dumps(fed_arrangement, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = planning.run_feedback_loop(
                packet_path=args.packet,
                arrangement_path=args.arrangement,
                arrangement=fed_arrangement,
                owner_planner=loop.owner_planner,
                arrangement_runner=loop.arrangement_runner,
                writer_runner=loop.writer_runner,
                output_dir=args.output_root,
                feedback_runner=loop.feedback_action_runner,
                resume=True,
                execution_context={
                    "owner_model": args.owner_model,
                    "arrangement_model": args.arrangement_model,
                    "writer_model": args.writer_model,
                    "owner_output_tokens": args.owner_output_tokens,
                    "arrangement_output_tokens": args.arrangement_output_tokens,
                    "writer_output_tokens": args.writer_output_tokens,
                    "owner_thinking_budget": args.owner_thinking_budget,
                    "arrangement_thinking_budget": args.arrangement_thinking_budget,
                    "writer_thinking_budget": args.writer_thinking_budget,
                    "language": args.language,
                    "unit": args.unit,
                    "planning_revision": True,
                },
            )
            result = {
                **result,
                "mode": "run",
                "output_root": str(args.output_root),
                "budget_before": estimate.get("budget"),
                "budget_after": _budget_snapshot(args.budget_ledger),
                "preflight": estimate,
                "manual_issue_input": True,
            }
        (args.output_root / "FEEDBACK_LOOP_RUN.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI writes a bounded diagnostic
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
