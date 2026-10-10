"""Independent, network-free losslessness checks for the restored unit route."""
from copy import deepcopy
import pytest
from optomind_research.runtime.upgrade3 import legacy_unit_route as route


def book():
    task = {"paragraph_id": "p1", "point": "Explain", "development": "Condition matters", "source_uses": [{"source_handle": "P0901", "role": "case", "future_condition": {"dose": 3}}], "source_brief_details": [{"finding_conditions": {"setting": "low"}}], "future_scientific_field": {"verbatim": "preserve me"}}
    table = {"table_id": "t1", "purpose": "Compare", "columns": ["Setting", "Finding"], "future_table_limit": ["do not rank"], "row_tasks": [{"row_id": "r1", "content": "low", "condition": {"dose": 3}, "source_uses": [{"source_handle": "P0001", "future_role": "qualified"}]}]}
    sources = [{"source_handle": "P0001", "aliases": ["P0901"], "title": "Original", "paper_id": "same-paper", "study_summary_A": {"text": "Long material " * 3000, "supporting_studies": [{"source_handle": "P0002"}]}}, {"source_handle": "P0901", "title": "Complement", "paper_id": "same-paper", "study_summary_A": {"text": "Distinct complementary evidence"}}, {"source_handle": "P0002", "title": "Dependency", "review_planning_B": {"inference_limits": "independent study"}}, {"source_handle": "P0003", "title": "Other unit"}]
    unit = {"unit_id": "U1", "focus": "First", "paragraph_tasks": [task], "table_tasks": [table], "owner_unit_context": {"condition": "owner evidence"}, "unit_notes": "Keep exact", "source_handles": ["P0901"]}
    other = {"unit_id": "U2", "focus": "Second", "paragraph_tasks": [{"paragraph_id": "p2", "source_uses": [{"source_handle": "P0003"}]}], "table_tasks": [], "source_handles": ["P0003"]}
    return {"language": "en", "source_aliases": {"P0901": "P0001"}, "sources": sources, "chapters": [{"chapter_id": "Ch1", "language": "en", "chapter_frame": {"chapter_title": "Title", "chapter_argument": "Argument"}, "other_chapters": [], "sources": sources, "units": [unit, other], "chapter_tool_materials": [{"unit_key": "Ch1:U1", "source_handles": ["P0002"], "text": "tool evidence"}, {"unit_key": "Ch1:U2", "source_handles": ["P0003"], "text": "other tool"}]}]}


def test_original_tasks_conditions_and_owner_survive():
    data = book()
    before = deepcopy(data)
    view = route.build_unit_views(data)[0]
    payload = route.build_legacy_payload(view)
    original = data["chapters"][0]["units"][0]
    for field in ("paragraph_tasks", "table_tasks", "owner_unit_context", "unit_notes"):
        assert payload[field] == original[field]
    assert data == before
    assert len(payload["chapter_tool_materials"]) == 1
    assert payload["chapter_tool_materials"][0]["text"] == "tool evidence"


def test_alias_records_and_recursive_dependencies_survive_without_other_unit():
    data = book()
    payload = route.build_legacy_payload(route.build_unit_views(data)[0])
    sources = {item["source_handle"]: item for item in payload["sources"]}
    assert set(sources) == {"P0001", "P0002"}
    originals = sources["P0001"]["audit_complete_original_records"]
    assert originals == data["chapters"][0]["sources"][:2]
    assert "P0901" in sources["P0001"]["aliases"]
    assert sources["P0002"]["review_planning_B"] == data["chapters"][0]["sources"][2]["review_planning_B"]
    assert len(sources["P0001"]["study_summary_A"]["text"]) > 30000


def test_source_material_and_payload_copies_do_not_mutate_book():
    data = book()
    before = deepcopy(data)
    payload = route.build_legacy_payload(route.build_unit_views(data)[0])
    payload["paragraph_tasks"][0]["future_scientific_field"]["verbatim"] = "mutated"
    payload["sources"][0]["study_summary_A"]["text"] = "mutated"
    assert data == before


def test_unresolved_numeric_identity_is_not_guessed_by_old_writer():
    from optomind_research.runtime.upgrade3 import review_unit_writer as writer
    view = route.build_unit_views(book())[0]
    payload = route.build_legacy_payload(view)
    result = writer.run_unit_writing(view, client=lambda messages, **kwargs: {"content": "Result [1].", "finish_reason": "stop", "complete": True}, model="offline-synthetic", payload=payload, planning_revision=True)
    assert result["body_markdown"] == "Result [1]."
    assert result["unresolved_numeric_citations"] == ["[1]"]


def test_partial_resume_keeps_usable_text_without_second_call(tmp_path):
    calls = []
    def factory(stage, output, profile):
        def client(messages, **kwargs):
            calls.append(messages)
            return {"content": "Partial result [P0001].", "finish_reason": "length", "complete": False, "usage": {}}
        return client
    data = book()
    data["chapters"][0]["units"] = data["chapters"][0]["units"][:1]
    data["chapters"][0]["units"][0]["table_tasks"] = []
    first = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory, output_tokens=256, thinking_budget=0)
    assert len(calls) == 1
    assert first["missing_units"] == []
    second = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory, output_tokens=256, thinking_budget=0)
    assert len(calls) == 1
    assert second["missing_units"] == []
    assert second["complete_units"] == 0


def test_explicit_conflicting_alias_does_not_merge_sources():
    data = book()
    data["chapters"][0]["sources"][1]["paper_id"] = "different-paper"
    with pytest.raises(ValueError, match="source_alias_ambiguous"):
        route.build_unit_views(data)


def test_existing_unowned_budget_cannot_be_adopted(tmp_path):
    from scripts.upgrade3.legacy_unit_writer import parser, _dedicated_factory
    ledger = tmp_path / "old.sqlite"
    ledger.write_text("Earlier experiment must stay untouched")
    args = parser().parse_args(["--input", "unused.json", "--output-dir", str(tmp_path / "new"), "--ledger", str(ledger), "--run"])
    with pytest.raises(ValueError, match="legacy_existing_ledger_not_owned"):
        _dedicated_factory(args, book(), None)
    assert ledger.read_text() == "Earlier experiment must stay untouched"


def test_network_free_production_assembly_preserves_title_and_alias_identity(tmp_path, monkeypatch):
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("Synthetic integration test must never use network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    def factory(stage, output, profile):
        return lambda messages, **kwargs: {"content": "SYNTHETIC TEST ONLY: aliases [P0901] and [P0001], dependency [P0002].", "finish_reason": "stop", "complete": True, "usage": {}}
    # Exercise the production assembly boundary using a network-denied factory.
    # Text is explicitly synthetic; this test makes no quality claim.
    factory.execution_mode = "live"
    data = book()
    data["chapters"][0]["units"] = data["chapters"][0]["units"][:1]
    data["chapters"][0]["units"][0]["table_tasks"] = []
    result = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory, output_tokens=256, thinking_budget=0)
    assert result["assembly"]
    files = list((tmp_path / "assembled").glob("*.md"))
    rendered = "\n".join(p.read_text(encoding="utf-8") for p in files)
    assert "Title" in rendered
    assert "SYNTHETIC TEST ONLY" in rendered
    references = __import__("json").loads((tmp_path / "assembled" / "REFERENCES.json").read_text())
    if isinstance(references, dict):
        references = references.get("references", references)
    assert len(references) == 2


def test_failed_second_unit_keeps_original_partial_delivery_accounting(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("No network allowed"))
    calls = []
    def factory(stage, output, profile):
        def client(messages, **kwargs):
            calls.append(messages)
            if len(calls) == 2:
                raise RuntimeError("synthetic provider failure")
            return {"content": "SYNTHETIC PARTIAL TEST [P0001].", "finish_reason": "stop", "complete": True, "usage": {}}
        return client
    factory.execution_mode = "live"
    data = book()
    data["chapters"][0]["units"][0]["table_tasks"] = []
    result = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory, output_tokens=256, thinking_budget=0)
    assert result["original_units"] == 2
    assert result["complete_units"] == 1
    assert result["missing_units"] == ["Ch1:U2"]
    assert result["status"] != "complete"
    assert result["assembly"].get("original_units", result["assembly"].get("expected_units")) == 2


def test_failed_retry_keeps_previous_partial_and_later_cached_units(tmp_path):
    calls = []
    fail_retry = [False]
    def factory(stage, output, profile):
        def client(messages, **kwargs):
            calls.append(messages)
            if fail_retry[0]:
                raise RuntimeError("synthetic replacement failure")
            first = len(calls) == 1
            return {"content": "SYNTHETIC retained result [P0001].", "finish_reason": "length" if first else "stop", "complete": not first, "usage": {}}
        return client
    data = book()
    data["chapters"][0]["units"][0]["table_tasks"] = []
    first = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory, output_tokens=256, thinking_budget=0)
    assert len(calls) == 2
    assert first["missing_units"] == []
    fail_retry[0] = True
    second = route.run_legacy_units(data, output_dir=tmp_path, run=True, retry_failed=True, client_factory=factory, output_tokens=256, thinking_budget=0)
    assert len(calls) == 3
    assert second["missing_units"] == []
    assert second["complete_units"] == 1
