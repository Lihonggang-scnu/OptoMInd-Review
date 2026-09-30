"""Offline contract tests: owned parts never erase substantive BODY by title."""
import json
import socket
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import manuscript_front_back as fb
from optomind_research.runtime.upgrade3 import review_delivery as delivery


def context(mode="standalone"):
    return {
        "research_question": "When does noise suppression preserve sensor bandwidth?",
        "review_argument": "A conditional bandwidth tradeoff, not a universal best method",
        "shared_scope": {"include": ["noise and bandwidth"]},
        "material_theme_inventory": ["measurement assumptions"],
        "source_identity_map": {"P0900": {"paper_id": "background", "title": "Calibration background", "card_path": "not-read.json"}},
        "material_records": [{"source_handle": "P0900", "text": "Calibration depends on operating conditions."}],
        "manuscript_parts_plan": {"context": "Tutorial for technically trained readers", **{
            part: {"purpose": f"{part} clarifies the conditional bandwidth claim",
                   "focus": ["Noise suppression must be compared at equal bandwidth"],
                   "boundary": ["Derivation belongs to CH01, regardless of its Introduction heading"],
                   "placement": {"mode": mode if part == "conclusion" else "standalone", "anchor": "Before references"},
                   "finalize_from": ["actual BODY", "shared_scope"]}
            for part in ("abstract", "introduction", "conclusion")}},
    }


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def fixture():
    return {"conclusion": {"conclusion": "Closing with conditions"},
            "introduction": {"introduction": "Opening cites [P0900]."},
            "abstract": {"title": "Conditional sensing", "abstract": "Conditional summary", "keywords": ["sensing"]}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_contract_messages_supply_real_context_and_serial_parts(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("# Review\n\n## Physical principles\n\nMathematical tutorial E=mc².\n\n## Methods\n\nNew grounded synthesis.\n\n## References\n", encoding="utf-8")
    report = fb.run_front_back_stage(draft_path=body, research_question="q", chapter_roles=[],
        out_dir=tmp_path / "out", parts_fixture_path=write(tmp_path / "parts.json", fixture()), planning_context=context())
    assert report["status"] == "generated"
    text = Path(report["final_manuscript"]).read_text()
    assert "## Physical principles\n\nMathematical tutorial E=mc²." in text
    assert "## Methods\n\nNew grounded synthesis." in text
    assert text.index("Closing with conditions") < text.index("## References")
    intro = json.loads((tmp_path / "out/messages/front_back_introduction_messages.json").read_text())[1]["content"]
    assert "Closing with conditions" in intro
    assert "conditional bandwidth" in intro and "Calibration depends" in intro
    assert "not-read.json" in intro and "未必已读取" in intro
    abstract = json.loads((tmp_path / "out/messages/front_back_abstract_messages.json").read_text())[1]["content"]
    assert "Opening cites" in abstract and "Closing with conditions" in abstract
    again, _ = fb.apply_front_back(text, {**fixture()["abstract"], **fixture()["introduction"], **fixture()["conclusion"]}, planning_context=context())
    assert text == again


@pytest.mark.parametrize("mode", ["embedded", "distributed"])
def test_unsupported_placement_preserved_without_generation(tmp_path, mode):
    body = tmp_path / "body.md"
    body.write_text("# Title\n\n## Outlook\n\nDetailed agenda.")
    report = fb.run_front_back_stage(draft_path=body, research_question="q", chapter_roles=[],
        out_dir=tmp_path / "out", planning_context=context(mode), recordings={})
    assert report["status"] == "unsupported_placement"
    assert report["planning_context"]["manuscript_parts_plan"]["conclusion"]["placement"]["mode"] == mode
    assert report["generated"] == [] and report["final_manuscript"] == ""
    assert not (tmp_path / "out/messages").exists()


def test_config_relative_context_and_material_input(tmp_path):
    write(tmp_path / "plan.json", context())
    write(tmp_path / "material.json", [{"text": "Explicit replacement material"}])
    cfg = write(tmp_path / "config.json", {"schema": delivery.DELIVERY_CONFIG_SCHEMA,
        "planning_context": {"path": "plan.json"}, "material_records": {"path": "material.json"}})
    loaded = delivery.load_delivery_config(cfg)
    assert loaded["planning_context"]["material_records"][0]["text"] == "Explicit replacement material"
    write(cfg, {"schema": delivery.DELIVERY_CONFIG_SCHEMA, "planning_context": {}})
    with pytest.raises(delivery.DeliveryConfigError):
        delivery.load_delivery_config(cfg)


def test_new_contract_downstream_stops_unsupported_and_passes_full_identity_pool(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import article_text_editor, delivery_citations
    assembled = tmp_path / "assembled"
    assembled.mkdir()
    body = assembled / "REVIEW_DRAFT_HANDLES.md"
    body.write_text("# Title\n\n## BODY\n\nEvidence.")
    monkeypatch.setattr(article_text_editor, "run_text_edit_stage", lambda **kw: {"status": "no_change", "edited_draft": str(body)})
    cfg = {"text_edit_fixture": "ignored", "front_back_fixture": write(tmp_path / "parts.json", fixture()),
           "planning_context": context("embedded")}
    blocked = delivery.run_downstream_delivery(config=cfg, assembly_report={"output_root": str(assembled)}, out_dir=tmp_path / "out")
    assert blocked["downstream_status"] == "pending"
    assert "04_figures_citations" not in blocked["stages"]
    seen = {}
    def citations(**kw):
        seen.update(kw)
        return {"status": "pending", "reader_draft": ""}
    monkeypatch.setattr(delivery_citations, "run_figures_citations_stage", citations)
    cfg["planning_context"] = context()
    delivery.run_downstream_delivery(config=cfg, assembly_report={"output_root": str(assembled)}, out_dir=tmp_path / "out2")
    assert seen["identity_catalogs"][-1]["entries"][0]["source_handle"] == "P0900"


def test_material_inputs_are_bounded_not_silently_truncated():
    c = context()
    c["material_records"] = [{"text": "x" * fb.MATERIAL_RECORD_CHAR_LIMIT}]
    with pytest.raises(fb.FrontBackError, match="bounded"):
        fb.normalize_planning_context(c)


def test_explicit_new_packet_locator_resolves_or_refuses_legacy(tmp_path):
    write(tmp_path / "plan.json", context())
    packet = {"manuscript_parts_contract_version": "optomind.manuscript_parts_plan.v1",
              "planning_result_path": "plan.json"}
    assert delivery._input_planning_context(packet, tmp_path) == context()
    packet["planning_result_path"] = "missing.json"
    with pytest.raises(delivery.DeliveryConfigError, match="resolvable"):
        delivery._input_planning_context(packet, tmp_path)
    assert delivery._input_planning_context({"planning_result_path": "old-missing.json"}, tmp_path) is None


def test_manifest_assembly_and_report_preserve_contract(tmp_path):
    arr = write(tmp_path / "arr.json", {"chapter_id": "CH01", "title": "Introduction",
        "units": [{"unit_id": "CH01_U01", "focus": "Mathematical teaching"}]})
    result = write(tmp_path / "units/UNIT_RESULT.json", {"chapter_id": "CH01", "unit_id": "CH01_U01",
        "body_markdown": "Deep mathematics remains.", "complete": True})
    write(tmp_path / "BATCH_JOBS.json", [{"chapter_id": "CH01", "unit_id": "CH01_U01",
        "arrangement": str(arr), "output": str(result.parent)}])
    manifest = write(tmp_path / "manifest.json", {"review_title": "Tutorial", "chapters": [
        {"chapter_id": "CH01", "arrangement_path": str(arr)}], "planning_context": context()})
    report = delivery.run_history_delivery(manifest_path=manifest, batch_root=tmp_path, out_dir=tmp_path / "out")
    assert report["planning_context"] == context()
    assert report["assembly"]["manuscript_parts_plan"] == context()["manuscript_parts_plan"]
    copied = json.loads((tmp_path / "out/CONTRACT_MANIFEST.json").read_text())
    assert copied["planning_context"] == context()


def test_publication_metadata_excludes_owned_markers():
    text, _ = fb.apply_front_back("# Original\n\n## BODY\n\nEvidence.",
        {**fixture()["abstract"], **fixture()["introduction"], **fixture()["conclusion"]}, planning_context=context())
    metadata = delivery._extract_front_matter(text)
    assert metadata["abstract"] == "Conditional summary"
    assert metadata["keywords"] == ["sensing"]


@pytest.mark.parametrize("marker", [{"status": "partial"}, {"manuscript_parts_plan_frozen": False},
                                     {"manuscript_parts_plan_revision": "v1"}])
def test_nonfinal_planning_contract_refused(marker):
    with pytest.raises(fb.FrontBackError, match="planning_context"):
        fb.normalize_planning_context({**context(), **marker})


def test_historical_body_and_new_pool_identity_conflict_refused(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import article_text_editor
    assembled = tmp_path / "assembled"
    assembled.mkdir()
    body = assembled / "REVIEW_DRAFT_HANDLES.md"
    body.write_text("# Title\n\n## BODY\n\nEvidence [P0900].")
    monkeypatch.setattr(article_text_editor, "run_text_edit_stage", lambda **kw: {"status": "no_change", "edited_draft": str(body)})
    cfg = {"text_edit_fixture": "ignored", "front_back_fixture": write(tmp_path / "parts.json", fixture()),
           "planning_context": context(), "identity_catalogs": [{"entries": [
               {"source_handle": "P0900", "paper_id": "a-different-paper"}]}]}
    with pytest.raises(delivery.DeliveryConfigError, match="planning_identity_conflict"):
        delivery.run_downstream_delivery(config=cfg, assembly_report={"output_root": str(assembled)}, out_dir=tmp_path / "out")
    assert not (tmp_path / "out/05_publication").exists()


def test_historical_unowned_abstract_blocks_new_standalone_application(tmp_path):
    body = tmp_path / "body.md"
    original = "# Old title\n\n## Abstract\n\nHistorical summary.\n\n**关键词：** stale\n\n## BODY\n\nMathematics."
    body.write_text(original)
    report = fb.run_front_back_stage(draft_path=body, research_question="q", chapter_roles=[],
        out_dir=tmp_path / "out", parts_fixture_path=write(tmp_path / "parts.json", fixture()), planning_context=context())
    assert report["status"] == "placement_conflict"
    assert report["generated"] == [] and report["final_manuscript"] == ""
    assert body.read_text() == original
    assert not (tmp_path / "out/messages").exists()



def test_structured_owned_abstract_keeps_internal_headings():
    parts = {"title": "Review", "abstract": "### Background\nProblem.\n\n### Synthesis\nConditional conclusion.", "keywords": ["science"]}
    text, _ = fb.apply_front_back("# Old\n\n## BODY\nEvidence.", parts, planning_context=context())
    assert delivery._extract_front_matter(text)["abstract"] == parts["abstract"]


def test_identity_conflict_matches_first_declaration_resolution(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import article_text_editor
    assembled = tmp_path / "assembled"
    assembled.mkdir()
    body = assembled / "REVIEW_DRAFT_HANDLES.md"
    body.write_text("# Title\n\n## BODY\n\nEvidence [P0900].")
    monkeypatch.setattr(article_text_editor, "run_text_edit_stage", lambda **kw: {"status": "no_change", "edited_draft": str(body)})
    cfg = {"text_edit_fixture": "ignored", "front_back_fixture": write(tmp_path / "parts.json", fixture()),
           "planning_context": context(), "identity_catalogs": [
               {"references": [{"source_handle": "P0900", "paper_id": "first-wrong-paper"}],
                "entries": [{"source_handle": "P0900", "paper_id": "background"}]},
               {"entries": [{"source_handle": "P0900", "paper_id": "background"}]}]}
    with pytest.raises(delivery.DeliveryConfigError, match="planning_identity_conflict"):
        delivery.run_downstream_delivery(config=cfg, assembly_report={"output_root": str(assembled)}, out_dir=tmp_path / "out")


def test_placement_conflict_stops_before_citations_and_publication(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import article_text_editor, delivery_citations
    assembled = tmp_path / "assembled"
    assembled.mkdir()
    body = assembled / "REVIEW_DRAFT_HANDLES.md"
    original = b"# Title\r\n\r\n## Introduction\r\n\r\nDeep mathematics must remain.\r\n"
    body.write_bytes(original)
    monkeypatch.setattr(article_text_editor, "run_text_edit_stage",
                        lambda **kw: {"status": "no_change", "edited_draft": str(body)})
    def forbidden(**kwargs):
        pytest.fail("placement conflict must block stages 04 and 05")
    monkeypatch.setattr(delivery_citations, "run_figures_citations_stage", forbidden)
    monkeypatch.setattr(delivery, "run_publication_delivery", forbidden)
    stale = tmp_path / "out/03_front_back/MANUSCRIPT_FINAL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("Stale result must not be consumed")
    cfg = {"text_edit_fixture": "ignored", "front_back_fixture": tmp_path / "must-not-read.json",
           "planning_context": context()}
    report = delivery.run_downstream_delivery(config=cfg,
        assembly_report={"output_root": str(assembled)}, out_dir=tmp_path / "out")
    assert report["downstream_status"] == "pending"
    assert report["halt_reasons"] == ["03_front_back"]
    assert report["stages"]["03_front_back"]["status"] == "placement_conflict"
    assert report["stages"]["03_front_back"]["final_manuscript"] == ""
    assert "04_figures_citations" not in report["stages"]
    assert "05_publication" not in report["stages"]
    assert body.read_bytes() == original
    assert stale.read_text() == "Stale result must not be consumed"


def test_legacy_numbered_sequence_intro_fallback_uses_correct_role_index():
    """No context/IDs/titles: the bounded legacy fallback selects chapter two."""
    original = (
        "# Review\n\n"
        "## 第1章 Physical principles\n\nFirst BODY stays intact.\n\n"
        "## 第2章 Reader framework\n\nSecond BODY contains the derivation.\n\n"
        "## 第3章 Methods\n\nThird BODY stays intact.\n"
    )
    roles = [{"role": "physical principles"}, {"role": "introduction"}, {"role": "methods"}]
    introduction = "New legacy reader entrance."
    applied, log = fb.apply_front_back(original, {"introduction": introduction}, roles)
    opening = (
        "<!-- generated-introduction-start -->\n\n" + introduction +
        "\n\n<!-- generated-introduction-end -->\n\n"
    )
    assert log == [{"part": "introduction",
                    "position": "body_introduction_chapter_opening_inserted",
                    "chapter_heading": "## 第2章 Reader framework"}]
    assert applied.index("## 第2章 Reader framework") < applied.index(opening)
    assert applied.index(opening) < applied.index("Second BODY contains the derivation.")
    assert applied.replace(opening, "", 1) == original
