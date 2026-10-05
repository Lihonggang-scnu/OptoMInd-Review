"""Offline opt-in integration: actual edited BODY -> independent serial parts."""

import json
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import review_delivery as delivery


REPO = Path(__file__).resolve().parents[2]


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Publication imports urllib/ssl lazily. Load its stdlib socket subclass
    # before replacing the constructor, while retaining the no-network guard.
    import ssl  # noqa: F401

    def blocked(*args, **kwargs):
        raise AssertionError("offline serial parts must not use the network")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _config(tmp_path, **extra):
    return _write_json(tmp_path / "delivery.json", {
        "schema": delivery.DELIVERY_CONFIG_SCHEMA,
        "research_question": "Which differences are supported by this BODY?",
        **extra,
    })


def test_delivery_legacy_default_and_post_body_minimum_inputs(tmp_path):
    legacy = delivery.load_delivery_config(_config(tmp_path))
    assert legacy["front_back_mode"] == "legacy"
    assert legacy["post_body_context"] == {}
    _write_json(tmp_path / "parts.json", {"fixture": True})
    config = delivery.load_delivery_config(_config(tmp_path, front_back={
        "mode": "post_body", "fixture": "parts.json",
    }))
    assert config["front_back_mode"] == "post_body"
    assert config["front_back_fixture"] == tmp_path / "parts.json"
    assert config["post_body_context"] == {}


def test_post_body_context_and_material_paths_are_config_relative(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    outline = [{"chapter_id": "CH01", "title": "Mechanism"}]
    materials = [{"source_handle": "P0001", "text": "A bounded selected record"}]
    identities = {"P0001": {"title": "A source"}}
    _write_json(cfg / "outline.json", outline)
    _write_json(cfg / "materials.json", materials)
    _write_json(cfg / "identities.json", identities)
    _write_json(cfg / "context" / "context.json", {
        "shared_scope": "Only what BODY covers",
        "final_outline": {"path": "outline.json"},
        "material_records": {"path": "materials.json"},
        "source_identity_map": "identities.json",
    })
    other = tmp_path / "unrelated_cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    config = delivery.load_delivery_config(_config(cfg, front_back={"mode": "post_body"},
        post_body_context={"path": "context/context.json"}))
    assert config["post_body_context"] == {
        "shared_scope": "Only what BODY covers", "final_outline": outline,
        "material_records": materials, "source_identity_map": identities,
    }
    assert config["front_back_fixture"] is None
    assert config["front_back_recordings"] is None


@pytest.mark.parametrize("location", ["front_back", "top_level"])
def test_material_records_without_context_or_early_plan(tmp_path, location):
    records = [{"source_handle": "P0002", "text": "Selected background"}]
    _write_json(tmp_path / "materials.json", records)
    entry = {"mode": "post_body"}
    extra = {"front_back": entry}
    target = entry if location == "front_back" else extra
    target["material_records"] = {"path": "materials.json"}
    config = delivery.load_delivery_config(_config(tmp_path, **extra))
    assert config["post_body_context"] == {"material_records": records}


@pytest.mark.parametrize("extra, match", [
    ({"front_back": {"mode": "unknown"}}, "mode_unsupported"),
    ({"front_back": {"mode": "post_body"}, "post_body_context": []}, "not_object"),
    ({"front_back": {"mode": "post_body", "material_records": {}}}, "not_list"),
    ({"front_back": {"mode": "post_body"},
      "post_body_context": {"path": "missing.json"}}, "unreadable"),
])
def test_post_body_invalid_config_fails_loudly(tmp_path, extra, match):
    with pytest.raises(delivery.DeliveryConfigError, match=match):
        delivery.load_delivery_config(_config(tmp_path, **extra))


def _mock_downstream(tmp_path, monkeypatch, *, status="generated", mode="post_body"):
    """Mock expensive downstreams; keep the real delivery coordinator."""
    from optomind_research.runtime.upgrade3 import article_text_editor, delivery_citations
    from optomind_research.runtime.upgrade3 import manuscript_front_back
    assembly = tmp_path / "assembly"
    assembly.mkdir()
    source = assembly / "REVIEW_DRAFT_HANDLES.md"
    source.write_text("# Title\n\n## Mechanism\n\nOld BODY.\n", encoding="utf-8")
    edited = tmp_path / "edited.md"
    edited.write_text("# Title\n\n## Mechanism\n\nACTUAL EDITED BODY.\n", encoding="utf-8")
    final = tmp_path / "final.md"
    final.write_text("# Final title\n\nComplete manuscript.\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(article_text_editor, "run_text_edit_stage",
                        lambda **kwargs: {"status": "edited", "edited_draft": str(edited)})

    def serial(**kwargs):
        seen["serial"] = kwargs
        return {"stage": "front_back", "status": status, "final_manuscript": str(final)}

    fake_module = types.ModuleType("optomind_research.runtime.upgrade3.serial_manuscript_parts")
    fake_module.run_serial_parts = serial
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)

    def legacy(**kwargs):
        seen["legacy"] = kwargs
        return {"stage": "front_back", "status": status, "final_manuscript": str(final)}
    monkeypatch.setattr(manuscript_front_back, "run_front_back_stage", legacy)

    def figures(**kwargs):
        seen["figures"] = kwargs
        return {"status": "complete", "reader_draft": str(final),
                "references_path": str(tmp_path / "refs.json")}
    monkeypatch.setattr(delivery_citations, "run_figures_citations_stage", figures)

    def publication(**kwargs):
        seen["publication"] = kwargs
        return {"status": "complete"}
    monkeypatch.setattr(delivery, "run_publication_delivery", publication)
    context = {"shared_scope": "Bounded scope", "material_records": []}
    config = {"text_edit_fixture": "edits.json", "front_back_fixture": "parts.json",
              "front_back_mode": mode, "post_body_context": context,
              "research_question": "Question", "chapter_roles": []}
    return config, {"output_root": str(assembly),
                    "assembly": {"status": "complete", "problems_resolved": True}}, seen, edited, final


def test_post_body_dispatch_consumes_actual_edited_artifact_and_context(tmp_path, monkeypatch):
    config, assembly, seen, edited, final = _mock_downstream(tmp_path, monkeypatch)
    report = delivery.run_downstream_delivery(config=config, assembly_report=assembly,
                                             out_dir=tmp_path / "out")
    assert report["downstream_status"] == "complete"
    assert "legacy" not in seen
    assert seen["serial"]["draft_path"] == edited
    assert seen["serial"]["context"] == config["post_body_context"]
    assert seen["figures"]["final_draft_path"] == final
    assert "publication" in seen


@pytest.mark.parametrize("status", ["partial", "pending", "failed", "placement_conflict",
                                    "unsupported_input", "no_parts_generated"])
def test_every_post_body_failure_halts_before_citations_even_with_stale_final(
    tmp_path, monkeypatch, status,
):
    config, assembly, seen, _, _ = _mock_downstream(tmp_path, monkeypatch, status=status)
    report = delivery.run_downstream_delivery(config=config, assembly_report=assembly,
                                             out_dir=tmp_path / "out")
    assert report["halt_reasons"] == ["03_front_back"]
    assert report["downstream_status"] == "pending"
    assert "figures" not in seen and "publication" not in seen


@pytest.mark.parametrize("mode", ["legacy", None])
def test_legacy_dispatch_remains_unchanged_including_legacy_partial(tmp_path, monkeypatch, mode):
    config, assembly, seen, _, _ = _mock_downstream(
        tmp_path, monkeypatch, mode="legacy", status="partial")
    if mode is None:
        config.pop("front_back_mode")
    report = delivery.run_downstream_delivery(config=config, assembly_report=assembly,
                                             out_dir=tmp_path / "out")
    assert report["downstream_status"] == "complete"
    assert "serial" not in seen and "legacy" in seen
    assert "context" not in seen["legacy"]


@pytest.mark.parametrize("state", [
    None,
    {"status": "partial_check", "problems_resolved": True},
    {"status": "complete", "problems_resolved": False},
    {"status": "complete", "problems_resolved": True,
     "pending_problems": [{"code": "unresolved"}]},
])
def test_post_body_dispatch_cannot_bypass_current_assembly_gate(tmp_path, monkeypatch, state):
    config, assembly, seen, _, _ = _mock_downstream(tmp_path, monkeypatch)
    assembly["assembly"] = state
    report = delivery.run_downstream_delivery(config=config, assembly_report=assembly,
                                             out_dir=tmp_path / "out")
    assert report["halt_reasons"] == ["assembly_pending"]
    assert report["stages"] == {}
    assert seen == {}


def test_post_body_missing_source_is_pending_without_fallback(tmp_path, monkeypatch):
    config, assembly, seen, _, _ = _mock_downstream(tmp_path, monkeypatch)
    config.pop("front_back_fixture")
    report = delivery.run_downstream_delivery(config=config, assembly_report=assembly,
                                             out_dir=tmp_path / "out")
    assert report["halt_reasons"] == ["03_front_back"]
    assert "serial" not in seen and "legacy" not in seen and "figures" not in seen


def test_standalone_cli_help_and_explicit_offline_source():
    cli = REPO / "scripts/upgrade3/manuscript_parts.py"
    help_result = subprocess.run([sys.executable, str(cli), "--help"], text=True,
                                 capture_output=True, check=False)
    assert help_result.returncode == 0
    assert "--research-question" in help_result.stdout
    missing_source = subprocess.run([
        sys.executable, str(cli), "--draft", "BODY.md", "--output", "out",
        "--research-question", "q",
    ], text=True, capture_output=True, check=False)
    assert missing_source.returncode == 2
    assert "--fixture --recordings" in missing_source.stderr


def _parts_fixture(*, introduction="An introduction grounded in the BODY."):
    part = {"purpose": "Explain the reader task", "focus": ["The actual supported finding"],
            "boundary": ["Do not enlarge the evidence"],
            "placement": {"mode": "standalone", "anchor": "manuscript_start"},
            "finalize_from": ["Actual BODY chapter CH01"]}
    return {"fixture": True, "note": "Labeled manual fixture, not live-model evidence",
            "conception": {"manuscript_parts_plan": {
                "context": "A bounded review for researchers", "abstract": part,
                "introduction": part, "conclusion": part}},
            "conclusion": {"conclusion": "A synthesis preserving the BODY's stated conditions."},
            "introduction": {"introduction": introduction},
            "abstract": {"title": "A bounded review", "abstract": "A summary of the actual supported finding.",
                         "keywords": ["mechanism", "boundary"]}}


def test_actual_standalone_cli_runs_without_context_or_early_plan(tmp_path):
    body = tmp_path / "BODY.md"
    source = "# Provisional title\n\n## Mechanism\n\nThe actual substantive BODY establishes bounded findings.\n"
    body.write_text(source, encoding="utf-8")
    fixture = _write_json(tmp_path / "parts.json", _parts_fixture())
    result = subprocess.run([
        sys.executable, str(REPO / "scripts/upgrade3/manuscript_parts.py"),
        "--draft", str(body), "--research-question", "Which finding does BODY support?",
        "--fixture", str(fixture), "--output", str(tmp_path / "result"),
    ], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["status"] == "generated"
    assert report["generated"] == ["conception", "conclusion", "introduction", "abstract"]
    assert report["model_calls"] == report["external_requests"] == 0
    assert body.read_text(encoding="utf-8") == source
    assert (tmp_path / "result" / "MANUSCRIPT_PARTS_PLAN.json").is_file()
    assert Path(report["final_manuscript"]).is_file()


def _actual_delivery(tmp_path, monkeypatch, *, conflict=False):
    from optomind_research.runtime.upgrade3 import article_text_editor
    assembly = tmp_path / "assembled"
    assembly.mkdir()
    draft = assembly / "REVIEW_DRAFT_HANDLES.md"
    draft.write_text("# Review\n\n## Mechanism\n\nThe actual BODY compares mechanisms under stated conditions [P0001].\n",
                     encoding="utf-8")
    monkeypatch.setattr(article_text_editor, "run_text_edit_stage",
                        lambda **kw: {"status": "edited", "edited_draft": str(draft)})
    handle = "P0001" if conflict else "P0002"
    fixture = _write_json(tmp_path / "parts.json", _parts_fixture(
        introduction=f"Selected background explains the review's scope [{handle}]."))
    context = {"material_records": [{"source_handle": handle, "text": "Selected supported background",
                                    "paper_identity": {"paper_id": "background-paper", "title": "Background"}}]}
    catalogs = [{"namespace": "body", "entries": [
        {"source_handle": "P0001", "paper_id": "body-paper", "title": "BODY source"}]}]
    config = {"text_edit_fixture": "offline-edit.json", "front_back_fixture": fixture,
              "front_back_mode": "post_body", "research_question": "Which differences are supported?",
              "post_body_context": context, "identity_catalogs": catalogs}
    return delivery.run_downstream_delivery(config=config,
        assembly_report={"output_root": str(assembly),
                         "assembly": {"status": "complete", "problems_resolved": True}},
        out_dir=tmp_path / "delivery")


def test_actual_delivery_resolves_new_background_identity_and_publishes_owned_metadata(tmp_path, monkeypatch):
    report = _actual_delivery(tmp_path, monkeypatch)
    assert report["downstream_status"] == "complete", report
    stages = report["stages"]
    assert stages["03_front_back"]["status"] == "generated"
    assert set(stages["03_front_back"]["source_identity_map"]) == {"P0001", "P0002"}
    assert stages["04_figures_citations"]["status"] == "complete"
    refs = json.loads(Path(stages["04_figures_citations"]["references_path"]).read_text())
    assert {row["paper_id"] for row in refs["references"]} == {"body-paper", "background-paper"}
    metadata = stages["05_publication"]["publication_metadata"]
    assert metadata["title"] == "A bounded review"
    assert metadata["abstract"] == "A summary of the actual supported finding."
    assert metadata["keywords"] == ["mechanism", "boundary"]
    assert "manuscript-part" not in metadata["abstract"]


def test_catalog_material_identity_conflict_halts_before_generation_and_publication(tmp_path, monkeypatch):
    report = _actual_delivery(tmp_path, monkeypatch, conflict=True)
    assert report["halt_reasons"] == ["03_front_back"]
    stage = report["stages"]["03_front_back"]
    assert stage["status"] == "failed" and stage["final_manuscript"] is None
    assert "identity_conflict:" in stage["error"] and "P0001:paper_id" in stage["error"]
    assert "04_figures_citations" not in report["stages"]
    assert not (tmp_path / "delivery/03_front_back/MANUSCRIPT_FINAL.md").exists()


def test_identity_catalog_namespaces_stay_separate_but_primary_alias_conflicts_fail():
    catalogs = [
        {"namespace": "a", "entries": [{"source_handle": "P0001", "paper_id": "a"}]},
        {"namespace": "b", "entries": [{"source_handle": "P0001", "paper_id": "b"}]},
    ]
    context = delivery._post_body_identity_context({}, catalogs)
    assert context["source_identity_map"]["P0001"]["paper_id"] == "a"
    assert context["source_identity_map"]["b::P0001"]["paper_id"] == "b"
    from optomind_research.runtime.upgrade3.serial_manuscript_parts import SerialPartsError
    with pytest.raises(SerialPartsError, match="identity_conflict"):
        delivery._post_body_identity_context({"source_identity_map": {
            "P0001": {"paper_id": "conflicting"}}}, catalogs)


def test_owned_english_metadata_excludes_markers_and_body_abstract_heading():
    text = """<!-- manuscript-part:title:start -->
# Generated title
<!-- manuscript-part:title:end -->
<!-- manuscript-part:abstract:start -->
## Abstract

Generated abstract.

**Keywords:** mechanism; boundary
<!-- manuscript-part:abstract:end -->

## Abstract

This is a substantive BODY section, not publication metadata.
"""
    assert delivery._extract_front_matter(text) == {
        "title": "Generated title", "abstract": "Generated abstract.",
        "keywords": ["mechanism", "boundary"],
    }


@pytest.mark.parametrize("status", ["pending", "failed", "partial"])
def test_post_body_incomplete_citation_stage_never_publishes_existing_reader(tmp_path, monkeypatch, status):
    from optomind_research.runtime.upgrade3 import delivery_citations
    config, assembly, seen, _, final = _mock_downstream(tmp_path, monkeypatch)
    monkeypatch.setattr(delivery_citations, "run_figures_citations_stage", lambda **kw: {
        "status": status, "reader_draft": str(final), "references_path": "refs.json",
        "unknown_identity_handles": ["P9999"],
    })
    report = delivery.run_downstream_delivery(config=config, assembly_report=assembly,
                                             out_dir=tmp_path / "out")
    assert report["halt_reasons"] == ["04_figures_citations"]
    assert "05_publication" not in report["stages"] and "publication" not in seen


def test_standalone_context_roles_and_explicit_question_override(tmp_path):
    draft = tmp_path / "body.md"
    draft.write_text("# Working title\n\n## Mechanism\n\nActual substantive mechanism explanation.\n", encoding="utf-8")
    context = _write_json(tmp_path / "context.json", {
        "research_question": "Old question", "chapter_roles": [{"title": "Mechanism", "role": "explanation"}],
        "material_records": {"path": "materials.json"},
    })
    _write_json(tmp_path / "materials.json", [])
    fixture = _write_json(tmp_path / "fixture.json", _parts_fixture())
    result = subprocess.run([
        sys.executable, str(REPO / "scripts/upgrade3/manuscript_parts.py"),
        "--draft", str(draft), "--context", str(context), "--research-question", "New question",
        "--fixture", str(fixture), "--output", str(tmp_path / "out"),
    ], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads((tmp_path / "out/INPUT.json").read_text())
    assert payload["context"]["research_question"] == "New question"
    assert payload["context"]["chapter_roles"] == [{"title": "Mechanism", "role": "explanation"}]


def test_explicit_bare_catalog_does_not_alias_primary_namespace():
    catalogs = [
        {"namespace": "body", "entries": [{"source_handle": "P0001", "paper_id": "primary"}]},
        {"namespace": "", "entries": [{"source_handle": "P0001", "paper_id": "explicit-bare"}]},
    ]
    context = delivery._post_body_identity_context({"source_identity_map": {
        "P0001": {"paper_id": "explicit-bare"}, "body::P0001": {"paper_id": "primary"},
    }}, catalogs)
    assert context["source_identity_map"]["P0001"]["paper_id"] == "explicit-bare"
    assert context["source_identity_map"]["body::P0001"]["paper_id"] == "primary"


def test_namespaced_title_only_identity_collision_fails_closed():
    from optomind_research.runtime.upgrade3.manuscript_front_back import FrontBackError
    identities = {"one::P0001": {"title": "Paper one"}, "two::P0001": {"title": "Paper two"}}
    with pytest.raises(FrontBackError, match="namespace_collision_requires_doi_or_paper_id"):
        delivery._post_body_identity_context({"source_identity_map": identities}, [])


def test_post_body_catalog_populates_handle_only_keys_without_mutating_inputs():
    from optomind_research.runtime.upgrade3.delivery_citations import build_delivery_citation_map
    catalogs = [{"namespace": "body", "entries": [
        {"source_handle": "P0001", "title": "First"},
        {"source_handle": "P0002", "title": "Second"},
    ]}]
    merged = delivery._post_body_delivery_catalogs(catalogs, {})
    citation_map = build_delivery_citation_map(
        final_handle_draft="The BODY [P0001] compared with [P0002].", identity_catalogs=merged)
    assert citation_map["reference_count"] == 2
    assert "handles" not in catalogs[0]["entries"][0]


def test_late_identity_bridge_failure_is_a_halted_report(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.manuscript_front_back import FrontBackError
    config, assembly, seen, _, _ = _mock_downstream(tmp_path, monkeypatch)
    def collision(*args, **kwargs):
        raise FrontBackError("post_body_ambiguous_identity:P0001")
    monkeypatch.setattr(delivery, "_post_body_delivery_catalogs", collision)
    report = delivery.run_downstream_delivery(config=config, assembly_report=assembly,
                                             out_dir=tmp_path / "out")
    assert report["halt_reasons"] == ["04_figures_citations"]
    assert report["stages"]["04_figures_citations"]["status"] == "failed"
    assert "figures" not in seen and "publication" not in seen


def test_context_question_can_supply_delivery_question(tmp_path):
    config_path = _write_json(tmp_path / "delivery.json", {
        "schema": delivery.DELIVERY_CONFIG_SCHEMA,
        "front_back": {"mode": "post_body"},
        "post_body_context": {"research_question": "Context-only question"},
    })
    assert delivery.load_delivery_config(config_path)["research_question"] == "Context-only question"


@pytest.mark.parametrize("example", [
    "Inline example `<!-- manuscript-part:abstract:start -->` stays.",
    "```html\n<!-- manuscript-part:abstract:start -->\n```",
])
def test_literal_marker_examples_do_not_replace_legacy_front_matter(example):
    text = "# Article\n\n## Abstract\nOriginal abstract.\n\n## Method\n" + example + "\n"
    assert delivery._extract_front_matter(text) == {
        "title": "Article", "abstract": "Original abstract.", "keywords": [],
    }
