"""Focused offline tests for the injected live front/back adapter."""

import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import manuscript_front_back as fb


def _context():
    return {
        "research_question": "When does a measured signal preserve useful bandwidth?",
        "review_argument": "The tradeoff is conditional on noise and measurement assumptions.",
        "shared_scope": {"include": ["noise", "bandwidth"]},
        "material_theme_inventory": ["measurement assumptions"],
        "source_identity_map": {
            "P0900": {"paper_id": "background", "title": "Calibration background"},
        },
        "material_records": [
            {"source_handle": "P0900", "text": "Calibration depends on operating conditions."},
        ],
        "manuscript_parts_plan": {
            "context": "A concise technical review for readers familiar with measurement.",
            **{
                part: {
                    "purpose": f"Explain the conditional {part} claim",
                    "focus": ["Compare suppression methods at equal bandwidth"],
                    "boundary": ["Substantive derivations remain in BODY"],
                    "placement": {"mode": "standalone", "anchor": "article front/back"},
                    "finalize_from": ["actual BODY", "shared_scope", "material_records"],
                }
                for part in ("abstract", "introduction", "conclusion")
            },
        },
    }


class _LiveClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response = dict(self.responses.pop(0))
        response.setdefault("call_id", kwargs.get("call_id"))
        response.setdefault("attempt", 1)
        return response


def _response(payload, *, finish_reason="stop", complete=True, attempt=1):
    return {
        "content": json.dumps(payload, ensure_ascii=False),
        "complete": complete,
        "finish_reason": finish_reason,
        "attempt": attempt,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _draft(tmp_path, text=None):
    path = tmp_path / "body.md"
    path.write_text(
        text or "# Review\n\n## BODY\n\nDeep mathematical BODY remains.\n\n## References\n",
        encoding="utf-8",
    )
    return path


def test_live_adapter_uses_serial_contract_parser_and_application(tmp_path):
    client = _LiveClient([
        _response({"conclusion": "Conditional closing with [P0900]."}),
        _response({"introduction": "Opening consistent with the closing."}),
        _response({
            "title": "Conditional bandwidth review",
            "abstract": "A bounded summary.",
            "keywords": ["bandwidth"],
        }),
    ])

    report = fb.run_front_back_stage(
        draft_path=_draft(tmp_path),
        research_question="q",
        chapter_roles=[],
        out_dir=tmp_path / "out",
        planning_context=_context(),
        client=client,
        model="qwen3.7-flash",
        max_output_tokens=256,
    )

    assert report["status"] == "generated"
    assert report["generated"] == list(fb.STAGE_ORDER)
    assert report["model_calls"] == 3
    assert report["successful_model_calls"] == 3
    assert report["model_attempts"] == 3
    assert report["external_requests"] == 3
    assert [row["stage"] for row in report["model_call_records"]] == list(fb.STAGE_ORDER)
    assert all("raw_response" not in row for row in report["model_call_records"])
    assert [call[1]["call_id"] for call in client.calls] == [f"front_back:{stage}" for stage in fb.STAGE_ORDER]

    intro_messages = json.loads(
        (tmp_path / "out/messages/front_back_introduction_messages.json").read_text(encoding="utf-8")
    )
    abstract_messages = json.loads(
        (tmp_path / "out/messages/front_back_abstract_messages.json").read_text(encoding="utf-8")
    )
    assert "Conditional closing" in intro_messages[1]["content"]
    assert "Opening consistent" in abstract_messages[1]["content"]
    assert "Conditional closing" in abstract_messages[1]["content"]
    assert "Calibration depends" in abstract_messages[1]["content"]

    final = Path(report["final_manuscript"]).read_text(encoding="utf-8")
    assert "Deep mathematical BODY remains." in final
    assert final.index("Conditional closing") < final.index("## References")
    assert "<!-- manuscript-part:introduction:start -->" in final


def test_live_incomplete_response_stops_serial_downstream(tmp_path):
    client = _LiveClient([
        _response({"conclusion": "Valid closing."}),
        _response({"introduction": "Truncated opening."}, finish_reason="length", complete=False, attempt=2),
        _response({"title": "Must not run", "abstract": "Must not run", "keywords": ["blocked"]}),
    ])

    report = fb.run_front_back_stage(
        draft_path=_draft(tmp_path),
        research_question="q",
        chapter_roles=[],
        out_dir=tmp_path / "out",
        planning_context=_context(),
        client=client,
    )

    assert report["status"] == "partial"
    assert report["generated"] == ["conclusion"]
    assert report["missing_stages"] == ["introduction"]
    assert report["model_calls"] == 2
    assert report["successful_model_calls"] == 1
    assert report["model_attempts"] == 3
    assert report["failures"] == [{
        "stage": "introduction",
        "error": "front_back_incomplete:introduction:finish_reason='length'",
    }]
    assert len(client.calls) == 2
    assert not (tmp_path / "out/messages/front_back_abstract_messages.json").exists()
    assert "Must not run" not in (tmp_path / "out/MANUSCRIPT_FINAL.md").read_text(encoding="utf-8")


def test_live_client_and_offline_sources_are_explicitly_mutually_exclusive(tmp_path):
    with pytest.raises(fb.FrontBackError, match="live_client_fixture_or_recordings_conflict"):
        fb.run_front_back_stage(
            draft_path=_draft(tmp_path),
            research_question="q",
            chapter_roles=[],
            out_dir=tmp_path / "out",
            parts_fixture_path=tmp_path / "parts.json",
            client=_LiveClient([]),
        )
    with pytest.raises(fb.FrontBackError, match="live_client_fixture_or_recordings_conflict"):
        fb.run_front_back_stage(
            draft_path=_draft(tmp_path),
            research_question="q",
            chapter_roles=[],
            out_dir=tmp_path / "out-recordings",
            recordings={},
            client=_LiveClient([]),
        )


def test_live_prechecks_block_placement_conflict_without_a_call(tmp_path):
    client = _LiveClient([])
    report = fb.run_front_back_stage(
        draft_path=_draft(tmp_path, "# Review\n\n## Introduction\n\nDeep BODY.\n"),
        research_question="q",
        chapter_roles=[{"chapter_id": "CH01", "title": "Introduction", "role": "introduction"}],
        out_dir=tmp_path / "out",
        planning_context=_context(),
        client=client,
    )

    assert report["status"] == "placement_conflict"
    assert report["model_calls"] == 0
    assert report["model_attempts"] == 0
    assert client.calls == []
    assert not (tmp_path / "out/messages").exists()


def test_fixture_route_remains_offline_and_reports_zero_calls(tmp_path):
    fixture = tmp_path / "parts.json"
    fixture.write_text(json.dumps({
        "conclusion": {"conclusion": "Fixture closing"},
        "introduction": {"introduction": "Fixture opening"},
        "abstract": {"title": "Fixture", "abstract": "Fixture summary", "keywords": ["fixture"]},
    }), encoding="utf-8")

    report = fb.run_front_back_stage(
        draft_path=_draft(tmp_path),
        research_question="q",
        chapter_roles=[],
        out_dir=tmp_path / "out",
        planning_context=_context(),
        parts_fixture_path=fixture,
    )

    assert report["status"] == "generated"
    assert report["model_calls"] == 0
    assert report["successful_model_calls"] == 0
    assert report["model_attempts"] == 0
    assert report["model_call_records"] == []
    assert report["external_requests"] == 0


def test_offline_unexpected_exception_is_not_swallowed(tmp_path, monkeypatch):
    fixture = tmp_path / "parts.json"
    fixture.write_text(json.dumps({"conclusion": {"conclusion": "Fixture closing"}}), encoding="utf-8")

    def explode(*_args, **_kwargs):
        raise RuntimeError("unexpected parser bug")

    monkeypatch.setattr(fb, "parse_front_back_response", explode)
    with pytest.raises(RuntimeError, match="unexpected parser bug"):
        fb.run_front_back_stage(
            draft_path=_draft(tmp_path),
            research_question="q",
            chapter_roles=[],
            out_dir=tmp_path / "out",
            parts_fixture_path=fixture,
        )
