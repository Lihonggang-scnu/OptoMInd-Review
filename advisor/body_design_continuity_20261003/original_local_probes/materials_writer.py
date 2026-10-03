import json
import sys
from pathlib import Path

ROOT = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny\worktree")
sys.path.insert(0, str(ROOT))

from optomind_research.runtime.upgrade3.progressive_review_plan import (
    _attach_case_groups,
    _classify_owner_response,
    _resolve_owner_source_materials,
)
from optomind_research.runtime.upgrade3.review_unit_writer import (
    UnitWritingView,
    run_unit_completion,
    write_unit_output,
)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.response


def owner_plan(extra=None):
    unit = {"unit_id": "U1", "source_handles": ["P0001"], "paragraph_briefs": []}
    if extra:
        unit.update(extra)
    return {"units": [unit]}


def owner_response(plan):
    return {"status": "updated", "updated_plan": plan}


def run_r5():
    identity_only = [{"source_handle": "P0001", "paper_id": "paper-1", "title": "Identity only"}]
    closed, report = _resolve_owner_source_materials(
        source_materials=identity_only,
        chapter_plan=owner_plan(),
        pool_rows=[],
    )
    missing_card_closed, missing_card_report = _resolve_owner_source_materials(
        source_materials=identity_only,
        chapter_plan=owner_plan(),
        pool_rows=[{
            "_source_handle": "P0001", "_paper_id": "paper-1",
            "title": "Identity only", "card_path": "missing-card.json",
        }],
    )
    _, _, _, identity_errors = _classify_owner_response(
        owner_plan(), owner_response(owner_plan()), closed,
    )

    nested_plan = owner_plan({"paragraph_briefs": [{"source_handles": ["P0999"]}]})
    nested_closed, nested_report = _resolve_owner_source_materials(
        source_materials=identity_only,
        chapter_plan=nested_plan,
        pool_rows=[],
    )
    _, _, _, nested_errors = _classify_owner_response(
        owner_plan(), owner_response(nested_plan), nested_closed,
    )

    return {
        "identity_only": {
            "closed_rows": closed,
            "resolution_report": report,
            "validator_errors": identity_errors,
            "owner_updated_accepted": not identity_errors,
        },
        "identity_only_missing_card_candidate": {
            "closed_rows": missing_card_closed,
            "resolution_report": missing_card_report,
        },
        "nested_missing": {
            "closed_rows": nested_closed,
            "resolution_report": nested_report,
            "validator_errors": nested_errors,
            "owner_updated_accepted": not nested_errors,
        },
    }


def run_n2():
    card_dir = Path(r"F:\OptoMind-Review-2\outputs\body_repair_review_verification_20261003\materials_writer")
    card_path = card_dir / "conflicting-current-card.json"
    card_path.write_text(json.dumps({
        "general_understanding": {"key_findings": "NEW A"},
        "review_planning": {"planning_summary": "NEW B"},
    }, ensure_ascii=False), encoding="utf-8")
    current = [{
        "_source_handle": "P0001", "_paper_id": "paper-new",
        "title": "New title", "card_path": str(card_path),
    }]
    closed, report = _resolve_owner_source_materials(
        source_materials=[{
            "source_handle": "P0001", "paper_id": "paper-old",
            "title": "Old title", "doi": "10.old",
        }],
        chapter_plan=owner_plan(),
        pool_rows=current,
    )
    return {"rows": closed, "resolution_report": report}


def run_n3():
    records = [{
        "chapter": {"chapter_id": "CH03", "source_ids": [], "source_handles": []},
        "chapter_plan": {"units": [{"unit_id": "U1"}]},
        "source_materials": [],
    }]
    response = {
        "additions": [{
            "unit_key": "CH03:1",
            "studies": [{"source_handle": "P0999", "proposed_use": "case use"}],
        }],
    }
    candidates = [{
        "_source_handle": "P0999", "_paper_id": "paper-999",
        "title": "Identity only candidate", "doi": "10.9999",
    }]
    appended = _attach_case_groups(records, response, candidate_rows=candidates)
    unit = appended[0]["chapter_plan"]["units"][0]
    return {
        "supporting_studies": unit.get("supporting_studies"),
        "source_materials": appended[0].get("source_materials"),
        "chapter_source_ids": appended[0]["chapter"].get("source_ids"),
        "material_fields": {
            key: appended[0]["source_materials"][0].get(key)
            for key in ("study_summary_A", "review_planning_B", "deep_read_material", "paper_id", "title")
        } if appended[0].get("source_materials") else {},
    }


def view_for_completion():
    return UnitWritingView(
        chapter_id="CH03", unit_id="U1", focus="focus", unit_index=1,
        unit_count=1, sibling_units=[], chapter_frame={}, other_chapters=[],
        paragraph_tasks=[{"paragraph_id": "P1", "point": "paragraph"}],
        table_tasks=[
            {"table_id": "T1", "columns": ["a", "b"], "row_tasks": []},
            {"table_id": "T2", "columns": ["a", "b"], "row_tasks": []},
        ],
        materials=[
            {"source_handle": "P0001", "study_summary_A": {"x": "y"}},
        ],
    )


def run_n4():
    cases = {
        "fenced_table": "```markdown\n| a | b |\n|---|---|\n| x | y |\n```",
        "two_tasks_one_table": "| a | b |\n|---|---|\n| x | y |",
        "unknown_citation": "A new paragraph cites [P9999].",
    }
    out = {}
    for name, body in cases.items():
        response = {"body_markdown": body, "status": "appended", "complete": True}
        task_ids = ["P1"] if name == "unknown_citation" else ["T1", "T2"]
        result = run_unit_completion(
            view_for_completion(), existing_body="ORIGINAL", task_ids=task_ids,
            client=FakeClient(response), simulated=False,
        )
        out[name] = {
            key: result.get(key)
            for key in ("pending", "body_markdown", "completion_fragment", "table_check", "issues", "source_handles")
        }
    ordinary = write_unit_output(
        view_for_completion(), "A paragraph cites [P9999].",
        Path(r"F:\OptoMind-Review-2\outputs\body_repair_review_verification_20261003\materials_writer\ordinary_writer"),
        model="fake", language="zh", mode="real", used_messages=[], estimate={},
    )
    out["ordinary_writer_unknown_reference"] = {
        "unknown_citations": ordinary.get("unknown_citations"),
        "citation_problems": ordinary.get("citation_problems"),
    }
    return out


if __name__ == "__main__":
    print(json.dumps({"r5": run_r5(), "n2": run_n2(), "n3": run_n3(), "n4": run_n4()}, ensure_ascii=False, indent=2))
