"""Offline automatic crop transcription, immutable cache and consumer checks."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import copy
import threading
import time
from pathlib import Path
from dataclasses import replace

import pytest

from test_docling_native_bridge import _fixture
from optomind_research.runtime.upgrade3.docling_adapter import DoclingCliAdapter
from optomind_research.runtime.upgrade3.document_snapshot import validate_snapshot
from optomind_research.runtime.upgrade3.material_acquisition import AcquisitionConfig, MaterialAcquirer
from optomind_research.runtime.upgrade3.paper_reading_card import build_card_input, build_card_messages
from optomind_research.runtime.upgrade3.practical_materials import load_practical_material, build_practical_reader_messages
from optomind_research.runtime.upgrade3.directed_reading import build_prompt_source_packet
from optomind_research.runtime.upgrade3.regional_recognition import RegionalRecognitionConfig, RegionalRecognizer, normalize_latex, vision_token_counter


FORMULA = {"latex": r"x=\\frac{a}{\\sqrt{b}}", "label": "(4)", "uncertain_regions": []}
TABLE = {"caption": "Table 1. Length (μm) and signed bound.", "columns": ["Length (μm)", "Bound"],
         "rows": [["−1.5", "(2.0–3.0)"], ["+2", "a\nb"]], "footnotes": ["At 500 nm; ±0.1 μm."], "uncertain_regions": []}


class Factory:
    execution_mode = "recording"
    def __init__(self, fail=None): self.calls, self.fail = [], fail
    def __call__(self, role, stage_dir, profile):
        def call(messages, **kwargs):
            kind = "table" if "table faithfully" in messages[-1]["content"][-1]["text"] else "formula"
            self.calls.append((kind, messages, profile, stage_dir))
            if self.fail and self.fail[0] == kind: raise self.fail[1]
            return {"content": json.dumps(TABLE if kind == "table" else FORMULA, ensure_ascii=False),
                    "complete": True, "finish_reason": "stop", "execution_mode": "recording", "returned_model": profile["model"],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 60}, "effective_request": profile,
                    "raw_response": "fixture raw provider bytes", "raw_response_sha256": "fixture-raw-sha", "request_id": "fixture-" + kind}
        return call


def _setup(tmp_path, **kwargs):
    data, document, run, doc_config = _fixture(tmp_path)
    config = RegionalRecognitionConfig(cache_root=tmp_path / "recognition", **kwargs)
    return data, document, run, doc_config, config


def test_automatic_fresh_runner_exact_crops_cache_and_actual_consumers(tmp_path):
    data, document, run, saved, config = _setup(tmp_path)
    parser_calls = []
    def local_runner(command, **kwargs):
        from pathlib import Path
        parser_calls.append(command)
        output = Path(command[2])
        (output / "document.json").write_text(json.dumps(document))
        (output / "run.json").write_text(json.dumps(run))
    factory = Factory()
    doc_config = replace(saved, document_path="", run_metadata_path="", command=("configured-native-runner", "{input}", "{output}"))
    adapter = DoclingCliAdapter(doc_config, runner=local_runner, recognizer=RegionalRecognizer(config, provider_factory=factory))
    specs = adapter.prepare(data)
    assert specs == adapter.prepare(data) and len(parser_calls) == 1 and len(factory.calls) == 2
    layout = json.loads(specs[0]["data"])
    assert layout["coverage"]["complete_page_coverage"]
    assert layout["recognition_run"]["figure_interpretation"] == "disabled"
    table, formula = layout["regions"]
    assert table["recognition_status"] == formula["recognition_status"] == "recognized"
    assert table["cells"][0]["text"] == "Length (μm)"  # candidates with cells are recognized too
    assert formula["raw_source"] == FORMULA["latex"] and formula["text"] == r"x=\frac{a}{\sqrt{b}}"
    assert not any(gap.get("region_id") == formula["region_id"] for gap in layout["known_gaps"])
    originals = [spec for spec in specs if spec["role"] == "pdf_layout_before_recognition"]
    assert len(originals) == 1 and json.loads(originals[0]["data"])["regions"][0]["cells"][0]["text"] == "Header"
    crops = {spec["source_document_id"]: spec for spec in specs if spec["role"] == "pdf_region_crop"}
    for kind, messages, _, _ in factory.calls:
        region = table if kind == "table" else formula
        ids = [region["crop_source_document_id"]] + [child["crop_source_document_id"] for field in ("captions", "footnotes") for child in region.get(field, [])]
        images = messages[-1]["content"][:-1]
        assert len(images) == len(ids)
        assert [base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) for part in images] == [crops[sid]["data"] for sid in ids]
        assert all(part["max_pixels"] == config.max_pixels for part in images)
        assert "Do not" in messages[-1]["content"][-1]["text"]
    providers = {key: None for key in ("grobid", "openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv")}
    providers["docling"] = adapter
    acquirer = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(cache_root=tmp_path / "acquisition", docling_enabled=True), providers=providers)
    result = acquirer.acquire_local({"canonical_paper_id": "automatic-transcription"}, pdf_bytes=data)
    snapshot = result.snapshot
    assert snapshot is not None and validate_snapshot(snapshot.root)["valid"]
    assert len(factory.calls) == 2
    asset = next(asset for asset in snapshot.assets if asset.get("parser") == "docling" and asset["type"] == "table")
    assert asset["recognition_status"] == "recognized" and asset["provided_to_multimodal"] is True
    assert asset["recognition_provenance"]["usage"]["prompt_tokens"] == 100
    card = build_card_input(snapshot, {"question_en": "How does the design perform?", "facets": []})
    for profile, concise in (("standard", False), ("economy", False), ("economy", True)):
        messages = build_card_messages(card, input_profile=profile, concise_output=concise)
        user = messages[-1]["content"]
        for text in ("Length (μm)", "−1.5", "(2.0–3.0)", "±0.1 μm", "recognition_status=recognized", "normalized_latex:", "offline body source"):
            assert text in user
    practical = load_practical_material(snapshot.root)
    practical_messages = build_practical_reader_messages(title="Automatic", questions=[], required_outputs=[], material=practical)
    assert "Length (μm)" in practical_messages[-1]["content"]
    legacy, _ = build_prompt_source_packet(snapshot)
    legacy_table = next(table for table in legacy["tables_by_rows"] if table["table_asset_id"] == asset["asset_id"])
    assert legacy_table["parser_asset"]["recognition_status"] == "recognized"
    assert legacy_table["caption"][0]["text"] == TABLE["caption"]
    assert legacy_table["footnotes"][0]["text"] == TABLE["footnotes"][0]
    assert legacy_table["rows"][1]["cells"][0]["text"] == "−1.5"
    before_calls = len(factory.calls)
    xml = b"<article><body><p>Native XML priority.</p></body></article>"
    assert acquirer.acquire_local({"canonical_paper_id": "native-priority"}, xml_bytes=xml, pdf_bytes=data).snapshot
    assert len(factory.calls) == before_calls
    if os.environ.get("OPTOMIND_REGIONAL_ACCEPTANCE_OUTPUT"):
        evidence = Path(os.environ["OPTOMIND_REGIONAL_ACCEPTANCE_OUTPUT"])
        evidence.mkdir(parents=True, exist_ok=True)
        for profile, concise in (("standard", False), ("economy", False), ("economy", True)):
            (evidence / f"card-{profile}-{concise}.json").write_text(json.dumps(build_card_messages(card, input_profile=profile, concise_output=concise), ensure_ascii=False, indent=2), encoding="utf-8")
        (evidence / "practical-messages.json").write_text(json.dumps(practical_messages, ensure_ascii=False, indent=2), encoding="utf-8")
        (evidence / "legacy-packet.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
        (evidence / "ACCEPTANCE.json").write_text(json.dumps({"snapshot_path": str(snapshot.root), "snapshot_id": snapshot.snapshot_id,
            "source_sha256": hashlib.sha256(data).hexdigest(), "parser_calls": len(parser_calls), "recognition_calls": len(factory.calls),
            "paid_calls": 0, "exact_crop_bytes_caption_footnote": True, "actual_consumer_messages_checked": True}, indent=2), encoding="utf-8")


def test_changed_source_or_model_invalidates_regional_cache(tmp_path):
    data, _, _, saved, config = _setup(tmp_path)
    specs = DoclingCliAdapter(saved).prepare(data)
    factory = Factory()
    first = RegionalRecognizer(config, provider_factory=factory)
    first.prepare(specs); first.prepare(specs)
    assert len(factory.calls) == 2
    RegionalRecognizer(replace(config, model="qwen3.5-flash"), provider_factory=factory).prepare(specs)
    assert len(factory.calls) == 4
    modified = [dict(spec) for spec in specs]
    layout = json.loads(modified[0]["data"])
    layout["source"]["sha256"] = "changed-pdf"
    modified[0]["data"] = json.dumps(layout).encode()
    for spec in modified:
        if spec["role"] == "pdf_region_crop":
            spec["provenance"] = {**spec["provenance"], "crop": {**spec["provenance"]["crop"], "pdf_sha256": "changed-pdf"}}
    first.prepare(modified)
    assert len(factory.calls) == 6


@pytest.mark.parametrize("filename", ["RESPONSE.json", "RESULT.json"])
def test_corrupt_recognition_cache_fails_closed_without_new_call(tmp_path, filename):
    data, _, _, saved, config = _setup(tmp_path)
    specs = DoclingCliAdapter(saved).prepare(data)
    factory = Factory(); recognizer = RegionalRecognizer(config, provider_factory=factory)
    recognizer.prepare(specs)
    next(config.cache_root.rglob(filename)).write_text("corrupt")
    with pytest.raises(ValueError, match="cache_content_mismatch"): recognizer.prepare(specs)
    assert len(factory.calls) == 2


def test_scheduler_ceiling_and_cache_location_changes_reuse_success(tmp_path):
    import shutil
    data, _, _, saved, config = _setup(tmp_path, max_regions=30, scope="ceiling=1")
    specs = DoclingCliAdapter(saved).prepare(data)
    factory = Factory()
    RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    moved = tmp_path / "moved-cache"
    shutil.copytree(config.cache_root, moved)
    changed = replace(config, cache_root=moved, max_regions=50, max_attempts_per_region=2, deadline_seconds=900, timeout_seconds=100, scope="ceiling=2")
    second = RegionalRecognizer(changed, provider_factory=factory).prepare(specs)
    assert len(factory.calls) == 2
    assert all(region["recognition_status"] == "recognized" for region in json.loads(second[0]["data"])["regions"])
    assert json.loads(second[0]["data"])["recognition_run"]["policy"]["scope"] == "ceiling=2"


def test_json_mode_is_request_bound_and_legacy_default_cache_replays(tmp_path, monkeypatch):
    data, _, _, saved, config = _setup(tmp_path)
    specs = DoclingCliAdapter(saved).prepare(data)
    fingerprint = RegionalRecognitionConfig.fingerprint
    def legacy(self):
        result = fingerprint(self)
        result.pop("json_mode")
        return result | {"implementation_sha256": "prior-json-mode-config"}
    factory = Factory()
    with monkeypatch.context() as old:
        old.setattr(RegionalRecognitionConfig, "fingerprint", legacy)
        RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    assert len(factory.calls) == 2 and config.json_mode is True
    RegionalRecognizer(replace(config, json_mode=False), provider_factory=factory).prepare(specs)
    assert len(factory.calls) == 4 and factory.calls[-1][2]["json_mode"] is False


@pytest.mark.parametrize("value", ["false", 0, None])
def test_json_mode_config_requires_boolean(tmp_path, value):
    with pytest.raises(ValueError, match="requires_boolean"):
        RegionalRecognitionConfig(cache_root=tmp_path, json_mode=value)


@pytest.mark.parametrize("model", ["qwen-vl-ocr-2025-11-20", "qwen3.5-ocr"])
@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```"])
def test_ocr_accepts_only_complete_json_or_complete_json_fence(tmp_path, model, wrapper):
    config = RegionalRecognitionConfig(cache_root=tmp_path, model=model, json_mode=False, table_output_tokens=4096)
    request = {"binding": {"region": {}}, "profile": config.profile("formula")}
    response = {"content": wrapper.format(json.dumps(FORMULA)), "complete": True, "finish_reason": "stop"}
    result = RegionalRecognizer(config)._response_result({}, response, request, "formula")
    assert result["content"] == FORMULA and result["status"] == "recognized"
    assert ("response_content_parse" in result) == wrapper.startswith("```")


@pytest.mark.parametrize("wrapper", ["Explanation\n```json\n{}\n```", "```json\n{}\n```\nExplanation", "```\n{}\n```", "```json\n{}", "```json\n```json\n{}\n```\n```"])
def test_ocr_does_not_extract_json_from_arbitrary_text(tmp_path, wrapper):
    config = RegionalRecognitionConfig(cache_root=tmp_path, model="qwen3.5-ocr", json_mode=False, table_output_tokens=4096)
    request = {"binding": {"region": {}}, "profile": config.profile("formula")}
    response = {"content": wrapper.format(json.dumps(FORMULA)), "complete": True, "finish_reason": "stop"}
    with pytest.raises(ValueError): RegionalRecognizer(config)._response_result({}, response, request, "formula")


def test_parser_revision_freely_recovers_optional_formula_metadata_and_publishes_receipts(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import regional_recognition as module
    data, _, _, saved, config = _setup(tmp_path)
    fingerprint, validate = RegionalRecognitionConfig.fingerprint, module._validate_content
    class OmitsFormulaMetadata(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                response = call(messages, **kwargs)
                if "formula" in messages[-1]["content"][-1]["text"]:
                    response["content"] = json.dumps({"latex": FORMULA["latex"]})
                return response
            return wrapped
    def old_validate(kind, content):
        if kind == "formula" and "uncertain_regions" not in content:
            raise ValueError("recognition_schema_invalid")
        return validate(kind, content)
    factory = OmitsFormulaMetadata()
    with monkeypatch.context() as old:
        old.setattr(RegionalRecognitionConfig, "fingerprint", lambda self: fingerprint(self) | {"implementation_sha256": "prior-parser"})
        old.setattr(module, "_validate_content", old_validate)
        first = DoclingCliAdapter(saved, recognizer=RegionalRecognizer(config, provider_factory=factory)).prepare(data)
    assert [region["recognition_status"] for region in json.loads(first[0]["data"])["regions"]] == ["recognized", "error"]
    originals = {path: path.read_bytes() for path in config.cache_root.rglob("*.json")}
    def forbidden(*args): pytest.fail("parser-only recovery must not call a provider")
    adapter = DoclingCliAdapter(saved, recognizer=RegionalRecognizer(config, provider_factory=forbidden))
    recovered = adapter.prepare(data)
    assert recovered == adapter.prepare(data)
    table, formula = json.loads(recovered[0]["data"])["regions"]
    assert table["recognition_status"] == "recognized"  # prior successful crop also replays
    assert table["recognition"]["cache_replay"]["paid_calls"] == 0
    assert formula["recognition_status"] == "uncertain"
    assert formula["recognition_content"] == {"latex": FORMULA["latex"], "label": "", "uncertain_regions": ["uncertainty_not_reported"]}
    assert formula["recognition"]["reparse"]["original_reason"] == "ValueError:recognition_schema_invalid"
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    request = json.loads(next(path for path in originals if path.name == "REQUEST.json").read_bytes())
    binding = request["binding"] | {"config": config.fingerprint()}
    replay = adapter.recognizer
    kind = request["binding"]["region"]["type"]
    assert replay._same_request(request, binding, request["messages"], kind)
    assert not replay._same_request(request, binding, [{"role": "user", "content": "changed prompt"}], kind)
    changed = binding | {"config": binding["config"] | {"max_pixels": config.max_pixels + 1}}
    assert not replay._same_request(request, changed, request["messages"], kind)
    reparsed = list(config.cache_root.rglob("REPARSED_*.json"))
    assert len(reparsed) == 1
    providers = {key: None for key in ("grobid", "openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv")}
    providers["docling"] = adapter
    snapshot = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(cache_root=tmp_path / "acquisition", docling_enabled=True), providers=providers).acquire_local({"canonical_paper_id": "optional-formula-metadata"}, pdf_bytes=data).snapshot
    assert snapshot is not None and validate_snapshot(snapshot.root)["valid"]
    published = {path.read_bytes() for path in (snapshot.root / "sources").iterdir() if path.is_file()}
    assert reparsed[0].read_bytes() in published
    assert all(raw in published for raw in originals.values())
    material = load_practical_material(snapshot.root)["structured_material"]
    assert "uncertainty_not_reported" in material and "saved_complete_response_no_provider_call" in material
    assert len(factory.calls) == 2


def test_parser_revision_bounded_retry_keeps_per_attempt_binding_valid(tmp_path, monkeypatch):
    data, _, _, saved, config = _setup(tmp_path)
    specs = DoclingCliAdapter(saved).prepare(data)
    fingerprint = RegionalRecognitionConfig.fingerprint
    class InvalidFormula(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                response = call(messages, **kwargs)
                if "formula" in messages[-1]["content"][-1]["text"]: response["content"] = "invalid JSON"
                return response
            return wrapped
    factory = InvalidFormula()
    with monkeypatch.context() as old:
        old.setattr(RegionalRecognitionConfig, "fingerprint", lambda self: fingerprint(self) | {"implementation_sha256": "prior-parser"})
        RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    original = {path: path.read_bytes() for path in config.cache_root.rglob("*.json")}
    second_factory = Factory()
    resumed = RegionalRecognizer(replace(config, max_attempts_per_region=2), provider_factory=second_factory)
    result = resumed.prepare(specs)
    assert result == resumed.prepare(specs) and len(second_factory.calls) == 1
    assert all(region["recognition_status"] == "recognized" for region in json.loads(result[0]["data"])["regions"])
    assert all(path.read_bytes() == raw for path, raw in original.items())


@pytest.mark.parametrize("content", [{}, {"latex": ""}, {"latex": " "}, {"latex": None}, {"latex": 42},
                                      {"latex": "x", "label": None}, {"latex": "x", "uncertain_regions": None}])
def test_optional_formula_metadata_does_not_relax_invalid_required_values(content):
    from optomind_research.runtime.upgrade3.regional_recognition import _validate_content
    with pytest.raises(ValueError): _validate_content("formula", content)


def test_optional_formula_metadata_does_not_relax_tables():
    from optomind_research.runtime.upgrade3.regional_recognition import _validate_content
    for content in ({key: value for key, value in TABLE.items() if key != "uncertain_regions"}, TABLE | {"rows": [["wrong shape"]]}):
        with pytest.raises(ValueError): _validate_content("table", content)
    assert _validate_content("formula", {"latex": "x", "uncertain_regions": []})["label"] == ""


def test_rectangular_table_first_row_replays_freely_as_uncertain_columns(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import regional_recognition as module
    data, _, _, saved, config = _setup(tmp_path)
    specs = DoclingCliAdapter(saved).prepare(data)
    raw_table = TABLE | {"columns": [], "rows": [TABLE["columns"], *TABLE["rows"]]}
    class FirstRowHeaders(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                response = call(messages, **kwargs)
                if "table faithfully" in messages[-1]["content"][-1]["text"]: response["content"] = json.dumps(raw_table)
                return response
            return wrapped
    validate = module._validate_content
    def old_validate(kind, content):
        if kind == "table" and content.get("columns") == []: raise ValueError("recognition_table_empty")
        return validate(kind, content)
    factory = FirstRowHeaders()
    with monkeypatch.context() as old:
        old.setattr(module, "_validate_content", old_validate)
        RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    originals = {path: path.read_bytes() for path in config.cache_root.rglob("*.json")}
    def forbidden(*args): pytest.fail("saved table promotion must not call a provider")
    replay = RegionalRecognizer(config, provider_factory=forbidden)
    recovered = replay.prepare(specs)
    assert recovered == replay.prepare(specs)
    table = json.loads(recovered[0]["data"])["regions"][0]
    assert table["recognition_status"] == "uncertain"
    assert table["recognition_content"]["columns"] == raw_table["rows"][0]
    assert table["recognition_content"]["rows"] == raw_table["rows"][1:]
    assert table["recognition_content"]["uncertain_regions"] == ["first_row_promoted_to_columns"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert len(list(config.cache_root.rglob("REPARSED_*.json"))) == 1 and len(factory.calls) == 2


@pytest.mark.parametrize("rows", [[["only", "header"]], [["", ""], ["a", "b"]],
                                 [["A", "B"], ["short"]], [["A", "B"], [42, "b"]], [[], []]])
def test_empty_table_columns_do_not_relax_unstructured_rows(rows):
    from optomind_research.runtime.upgrade3.regional_recognition import _validate_content
    with pytest.raises(ValueError): _validate_content("table", TABLE | {"columns": [], "rows": rows})


@pytest.mark.parametrize("raw,warning", [("a\rb", True), ("a\tb", True), ("a\bb", True),
                                         ("a\fb", True), ("a\vb", True), ("a\nb", False)])
def test_decoded_latex_controls_warn_without_changing_raw_text(raw, warning):
    from optomind_research.runtime.upgrade3.regional_recognition import _validate_content
    result = _validate_content("formula", FORMULA | {"latex": raw})
    assert result["latex"] == raw and bool(result["uncertain_regions"]) == warning
    if warning: assert result["uncertain_regions"][0]["kind"] == "decoded_latex_control_characters"


@pytest.mark.parametrize("raw", ["\\left(1\right)", "first\r\nsecond"])
def test_cached_formula_controls_publish_exact_reading_bytes_and_detect_tamper(tmp_path, monkeypatch, raw):
    from optomind_research.runtime.upgrade3 import regional_recognition as module
    from optomind_research.runtime.upgrade3.document_snapshot import SnapshotError
    data, _, _, saved, config = _setup(tmp_path)
    class ControlFormula(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                response = call(messages, **kwargs)
                if "formula" in messages[-1]["content"][-1]["text"]: response["content"] = json.dumps(FORMULA | {"latex": raw})
                return response
            return wrapped
    validate = module._validate_content
    def old_validate(kind, content):
        result = validate(kind, content)
        if kind == "formula": result["uncertain_regions"] = []
        return result
    factory = ControlFormula()
    with monkeypatch.context() as old:
        old.setattr(module, "_validate_content", old_validate)
        first = DoclingCliAdapter(saved, recognizer=RegionalRecognizer(config, provider_factory=factory)).prepare(data)
    assert json.loads(first[0]["data"])["regions"][1]["recognition_status"] == "recognized"
    originals = {path: path.read_bytes() for path in config.cache_root.rglob("*.json")}
    def forbidden(*args): pytest.fail("warning update must replay saved response freely")
    providers = {key: None for key in ("grobid", "openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv")}
    providers["docling"] = DoclingCliAdapter(saved, recognizer=RegionalRecognizer(config, provider_factory=forbidden))
    snapshot = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(cache_root=tmp_path / "acquisition", docling_enabled=True), providers=providers).acquire_local({"canonical_paper_id": "formula-controls"}, pdf_bytes=data).snapshot
    assert snapshot is not None and validate_snapshot(snapshot.root)["valid"]
    formula = next(asset for asset in snapshot.assets if asset.get("parser") and asset["type"] == "formula")
    assert formula["raw_source"] == raw and formula["recognition_status"] == "uncertain"
    assert formula["recognition_provenance"]["uncertain_regions"][0]["kind"] == "decoded_latex_control_characters"
    assert all(path.read_bytes() == value for path, value in originals.items()) and len(factory.calls) == 2
    view = snapshot.root / "READING_VIEW.md"
    assert raw.encode("utf-8") in view.read_bytes()
    view.write_bytes(view.read_bytes().replace(b"\r", b"\n"))
    with pytest.raises(SnapshotError, match="reading_view_not_reconstructable"): validate_snapshot(snapshot.root)


def test_budget_stop_preserves_pending_and_never_calls_other_regions(tmp_path):
    data, _, _, saved, config = _setup(tmp_path, concurrency=1)
    specs = DoclingCliAdapter(saved).prepare(data)
    factory = Factory(("table", RuntimeError("global_budget_exhausted")))
    recognizer = RegionalRecognizer(config, provider_factory=factory)
    first = recognizer.prepare(specs)
    originals = {path: path.read_bytes() for path in config.cache_root.rglob("*.json")}
    second = recognizer.prepare(specs)
    assert len(factory.calls) == 2  # same policy makes a new real reservation attempt
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert len(list(config.cache_root.glob("*/attempt-*/RESULT.json"))) == 2
    table, formula = json.loads(first[0]["data"])["regions"]
    assert table["recognition_status"] == "budget_stopped" and formula["recognition_status"] == "pending"
    assert not table["recognition"]["provided_to_multimodal"]
    assert json.loads(second[0]["data"])["regions"][0]["recognition_status"] == "budget_stopped"
    factory.fail = None
    resumed = recognizer.prepare(specs)
    assert all(region["recognition_status"] == "recognized" for region in json.loads(resumed[0]["data"])["regions"])
    assert len(factory.calls) == 4
    RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    assert len(factory.calls) == 4  # successful regions are never paid again


def test_partial_schema_failure_keeps_other_success_and_bounds_retries(tmp_path):
    data, _, _, saved, config = _setup(tmp_path, max_attempts_per_region=2)
    specs = DoclingCliAdapter(saved).prepare(data)
    class InvalidFormula(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                response = call(messages, **kwargs)
                if "formula" in messages[-1]["content"][-1]["text"]: response["content"] = "invalid JSON"
                return response
            return wrapped
    factory = InvalidFormula(); recognizer = RegionalRecognizer(config, provider_factory=factory)
    first = recognizer.prepare(specs)
    assert len(factory.calls) == 3  # successful table once, invalid formula twice
    assert first == recognizer.prepare(specs) and len(factory.calls) == 3
    table, formula = json.loads(first[0]["data"])["regions"]
    assert table["recognition_status"] == "recognized" and formula["recognition_status"] == "error"


def test_preview_region_limit_and_uncertain_cells_are_explicit(tmp_path):
    data, _, _, saved, config = _setup(tmp_path, max_regions=1)
    specs = DoclingCliAdapter(saved).prepare(data)
    preview = RegionalRecognizer(config).prepare(specs)
    assert all(region["recognition_status"] == "pending" for region in json.loads(preview[0]["data"])["regions"])
    assert not config.cache_root.exists()
    factory = Factory()
    first = RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    assert len(factory.calls) == 1
    assert json.loads(first[0]["data"])["regions"][1]["recognition_status"] == "pending"


def test_omitted_native_caption_notes_survive_actual_snapshot_as_uncertain(tmp_path):
    data, _, _, saved, config = _setup(tmp_path)
    class OmitsNotes(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                response = call(messages, **kwargs)
                content = json.loads(response["content"])
                if "columns" in content: content.update(caption="", footnotes=[])
                response["content"] = json.dumps(content)
                return response
            return wrapped
    adapter = DoclingCliAdapter(saved, recognizer=RegionalRecognizer(config, provider_factory=OmitsNotes()))
    providers = {key: None for key in ("grobid", "openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv")}
    providers["docling"] = adapter
    result = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(cache_root=tmp_path / "acquisition", docling_enabled=True), providers=providers).acquire_local({"canonical_paper_id": "omitted-notes"}, pdf_bytes=data)
    snapshot = result.snapshot
    assert snapshot is not None
    table = next(asset for asset in snapshot.assets if asset.get("parser") and asset["type"] == "table")
    assert table["recognition_status"] == "uncertain"
    material = load_practical_material(snapshot.root)["structured_material"]
    assert "Table caption below the table." in material and "Table footnote below the caption." in material
    assert "omitted linked footnotes" in material


def test_unknown_spend_freezes_retry_even_after_policy_change(tmp_path):
    data, _, _, saved, config = _setup(tmp_path, concurrency=1)
    specs = DoclingCliAdapter(saved).prepare(data)
    factory = Factory(("table", RuntimeError("transport disconnected; spend unknown")))
    first = RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    second = RegionalRecognizer(replace(config, scope="raised-ceiling", max_attempts_per_region=2), provider_factory=factory).prepare(specs)
    assert len(factory.calls) == 1
    assert json.loads(second[0]["data"])["regions"][0]["recognition_status"] == "error"
    assert json.loads(first[0]["data"])["regions"][1]["recognition_status"] == "pending"


def test_expired_recognition_deadline_schedules_no_provider_call(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import regional_recognition as module
    data, _, _, saved, config = _setup(tmp_path, deadline_seconds=0.01, concurrency=1)
    specs = DoclingCliAdapter(saved).prepare(data)
    values = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(values, 3.0))
    factory = Factory()
    result = RegionalRecognizer(config, provider_factory=factory).prepare(specs)
    assert not factory.calls
    assert all(region["recognition_status"] == "pending" for region in json.loads(result[0]["data"])["regions"])


@pytest.mark.parametrize("raw,normalized", [
    (r"\\frac{a}{\\sqrt{b}}", r"\frac{a}{\sqrt{b}}"),
    (r"\begin{matrix}a&b\\c&d\end{matrix}", r"\begin{matrix}a&b\\c&d\end{matrix}"),
    (r"\\begin{matrix}a&b\\[2pt]c&d\\end{matrix}", r"\begin{matrix}a&b\\[2pt]c&d\end{matrix}"),
    (r"\begin{matrix}a\\beta\end{matrix}", r"\begin{matrix}a\\beta\end{matrix}"),
    (r"\alpha+\beta\quad\&", r"\alpha+\beta\quad\&"),
])
def test_latex_normalization_preserves_matrix_separators(raw, normalized):
    assert normalize_latex(raw) == normalized


def test_image_reservation_counter_counts_each_image_bound():
    counter = vision_token_counter(lambda raw, messages: 20)
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:"}, "max_pixels": 2048}, {"type": "text", "text": "transcribe"}]}]
    assert counter(b"", messages) == 20 + 2 + 2 + 512


@pytest.mark.parametrize("ceiling,expected_calls", [(1.0, 2), (0.000001, 0)])
@pytest.mark.parametrize("model,json_mode", [("qwen3.5-plus", True), ("qwen3-vl-flash", True), ("qwen-vl-ocr-2025-11-20", False), ("qwen3.5-ocr", False)])
def test_formal_cli_native_transport_dual_budget_and_zero_send_replay(tmp_path, monkeypatch, ceiling, expected_calls, model, json_mode):
    """Real CLI/factory/reserve/stream/settle boundary, synthetic HTTP only."""
    import io
    from scripts.upgrade3 import build_local_materials as cli
    from scripts.upgrade3 import writer_candidates
    from optomind_research.runtime.upgrade3.module4 import runtime
    from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider

    data, _, _, saved, _ = _setup(tmp_path)
    source = tmp_path / "source.pdf"; source.write_bytes(data)
    recognition = tmp_path / "recognition.json"; recognition.write_text(json.dumps({"model": model, "json_mode": json_mode, "table_output_tokens": 4096 if "ocr" in model else 8192, "max_regions": 30}))
    keys = tmp_path / "synthetic-keys.txt"; keys.write_text("synthetic-regional-key")
    tokenizer = tmp_path / "local-tokenizer.json"; tokenizer.write_text("{}")
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    runtime.GlobalBudgetLedger(limit_cny=80, path=project)
    runtime.GlobalBudgetLedger(limit_cny=300, path=account)
    monkeypatch.setattr(MaterialAcquirer, "_ensure_grobid", lambda self: None)
    monkeypatch.setattr(writer_candidates, "tokenizer_counter", lambda *_: (lambda *_: 100, {"offline_synthetic_tokenizer": True}))
    calls = []
    class Response:
        status = 200
        headers = {"x-request-id": "synthetic-regional-request"}
        def __init__(self, content):
            text = json.dumps(content, ensure_ascii=False)
            if "ocr" in model: text = "```json\n" + text + "\n```"
            payload = {"model": model, "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                       "choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
            self.lines = io.BytesIO(b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\ndata: [DONE]\n\n")
        def readline(self): return self.lines.readline()
        def __enter__(self): return self
        def __exit__(self, *_): pass
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer synthetic-regional-key"
            for ledger in (project, account): assert runtime.GlobalBudgetLedger(path=ledger).as_dict()["reserved_cny"] > 0
            body = json.loads(request.data)
            assert body["model"] == model
            if model != "qwen3.5-plus": assert "max_tokens" in body and "max_completion_tokens" not in body
            if "ocr" in model:
                assert "response_format" not in body and "enable_thinking" not in body
                assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
                assert "faithful visual document transcription tool" in body["messages"][0]["content"][-1]["text"]
            assert body["stream"] is True and body["stream_options"]["include_usage"] is True
            assert body["messages"][-1]["content"][0]["type"] == "image_url"
            calls.append(body)
            return Response(TABLE if "table faithfully" in body["messages"][-1]["content"][-1]["text"] else FORMULA)
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: Opener())
    argv = ["--input", str(source), "--canonical-paper-id", "formal-native-transport", "--disable-rescue",
            "--output-root", str(tmp_path / "out"), "--cache-root", str(tmp_path / "acquisition"),
            "--docling-document-json", str(saved.document_path), "--docling-run-json", str(saved.run_metadata_path),
            "--recognition-config", str(recognition), "--recognition-concurrency", "4", "--recognition-run", "--recognition-cache-root", str(tmp_path / "regional"),
            "--budget-ledger", str(project), "--budget-limit", "80", "--account-ledger", str(account), "--account-budget-limit", "300",
            "--key-file", str(keys), "--key-index", "1", "--tokenizer", str(tokenizer), "--recognition-ceiling-cny", str(ceiling)]
    assert cli.main(argv) == 0
    assert len(calls) == expected_calls
    before = [runtime.GlobalBudgetLedger(path=ledger).as_dict() for ledger in (project, account)]
    assert all(ledger["reserved_cny"] == 0 for ledger in before)
    assert before[0]["actual_cny"] == before[1]["actual_cny"]
    assert cli.main(argv) == 0 and len(calls) == expected_calls
    after = [runtime.GlobalBudgetLedger(path=ledger).as_dict() for ledger in (project, account)]
    assert after == before
    manifest = next((tmp_path / "out").rglob("DOCUMENT_MANIFEST.json"), None)
    # The ordinary provider publishes DOCUMENT_SNAPSHOT.json in some versions.
    if manifest is None: manifest = next((tmp_path / "out").rglob("MANIFEST.json"), None)
    assert manifest is not None
    snapshot = PreparedSnapshotProvider(manifest.parent).load()
    assets = [asset for asset in snapshot.assets if asset.get("parser")]
    assert any(asset["recognition_status"] == ("recognized" if expected_calls else "budget_stopped") for asset in assets)
    if expected_calls and "ocr" in model:
        assert all(asset["recognition_provenance"]["response_content_parse"] == "complete_outer_json_fence_once" for asset in assets)


def _many_specs(tmp_path, count=8, tables=True):
    data, _, _, saved, config = _setup(tmp_path)
    specs = DoclingCliAdapter(saved).prepare(data)
    layout = json.loads(specs[0]["data"])
    formula = layout["regions"][1]
    regions = [copy.deepcopy(formula) for _ in range(count - int(tables))]
    for i, region in enumerate(regions):
        region["region_id"] = f"#/texts/{100 + i}"
    layout["regions"] = regions + ([layout["regions"][0]] if tables else [])  # table deliberately last
    layout["known_gaps"] = []
    return [dict(specs[0], data=json.dumps(layout).encode()), *specs[1:]], config


@pytest.mark.parametrize("value", [0, 33, True, 1.5])
def test_concurrency_requires_bounded_integer(tmp_path, value):
    with pytest.raises(ValueError, match="concurrency_invalid"):
        RegionalRecognitionConfig(cache_root=tmp_path, concurrency=value)


def test_four_way_overlap_ordered_outcomes_and_concurrency_free_replay(tmp_path):
    specs, config = _many_specs(tmp_path)
    barrier, guard = threading.Barrier(4), threading.Lock()
    state = {"active": 0, "peak": 0, "started": 0}
    class Overlapping(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                with guard:
                    state["active"] += 1
                    state["started"] += 1
                    index = state["started"]
                    state["peak"] = max(state["peak"], state["active"])
                try:
                    if index <= 4: barrier.wait(timeout=5)
                    time.sleep(0.01 * (index % 3))
                    return call(messages, **kwargs)
                finally:
                    with guard: state["active"] -= 1
            return wrapped
    parallel = Overlapping()
    output = RegionalRecognizer(config, provider_factory=parallel).prepare(specs)
    assert state["peak"] == 4 and len(parallel.calls) == 8
    assert parallel.calls[0][0] in {"table", "formula"}  # completion order may differ
    assert list(config.cache_root.glob("*/attempt-1/RESULT.json"))
    sequential = Factory()
    serial = RegionalRecognizer(replace(config, cache_root=tmp_path / "serial", concurrency=1), provider_factory=sequential).prepare(specs)
    assert sequential.calls[0][0] == "table"  # dispatch long tables first
    assert json.loads(output[0]["data"])["regions"] == json.loads(serial[0]["data"])["regions"]
    assert [x["region_id"] for x in json.loads(output[0]["data"])["regions"]] == [x["region_id"] for x in json.loads(specs[0]["data"])["regions"]]
    def forbidden(*args, **kwargs): raise AssertionError("cache replay must not construct provider")
    replay = RegionalRecognizer(replace(config, concurrency=8), provider_factory=forbidden).prepare(specs)
    assert json.loads(replay[0]["data"])["regions"] == json.loads(output[0]["data"])["regions"]


def test_same_region_is_executed_once_and_worker_errors_are_not_lost(tmp_path):
    config = RegionalRecognitionConfig(cache_root=tmp_path, concurrency=4)
    recognizer = RegionalRecognizer(config, provider_factory=Factory())
    binding = {"config": config.fingerprint(), "region": {"region_id": "same"}}
    messages = [{"role": "user", "content": [{"type": "text", "text": "formula"}]}]
    jobs = [{"binding": binding, "messages": messages, "kind": "formula"} for _ in range(2)]
    recognizer._run_regions(jobs, time.monotonic() + 60)
    assert len(recognizer.provider_factory.calls) == 1
    assert all(job["result"]["status"] == "recognized" for job in jobs)
    # An unexpected cache failure still drains the other admitted result.
    original = recognizer._transcribe
    entered = threading.Barrier(2)
    def broken(binding, *args):
        entered.wait(timeout=5)
        if binding["region"]["region_id"] == "bad": raise ValueError("cache_corrupt_for_bad")
        return original(binding, *args)
    recognizer._transcribe = broken
    jobs = [{"binding": {**binding, "region": {"region_id": name}}, "messages": messages, "kind": "formula"} for name in ("bad", "good")]
    with pytest.raises(ValueError, match="cache_corrupt_for_bad"):
        recognizer._run_regions(jobs, time.monotonic() + 60)
    assert "result" in jobs[1]  # admitted work is drained, never abandoned


@pytest.mark.parametrize("mode", ["success", "unknown", "project_cap", "account_cap"])
def test_parallel_native_dual_reservations_stops_and_receipts(tmp_path, monkeypatch, mode):
    import io
    from types import SimpleNamespace
    from scripts.upgrade3.writer_candidates import make_live_factory
    from optomind_research.runtime.upgrade3.module4 import runtime
    specs, config = _many_specs(tmp_path, tables=False)
    config = replace(config, max_pixels=1024, formula_output_tokens=64)
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    reserve = runtime.estimated_cost_cny({"prompt_tokens": 615, "completion_tokens": 64}, model=config.model, conservative=True)
    runtime.GlobalBudgetLedger(path=project, limit_cny=80)
    runtime.GlobalBudgetLedger(path=account, limit_cny=reserve * 2.1 if mode == "account_cap" else 300)
    key_file = tmp_path / "synthetic.txt"; key_file.write_text("synthetic-key")
    args = SimpleNamespace(run=True, responses=None, budget_ledger=project, budget_limit=80, allow_max=False,
        account_ledger=account, account_budget_limit=None, account_mapping_dir=tmp_path / "maps", key_file=key_file, key_index=1)
    guard, first_four, stop_recorded = threading.Lock(), threading.Barrier(4), threading.Event()
    calls, clients = [], []
    state = {"counter_active": 0, "counter_peak": 0}
    def text_counter(*_):
        state["counter_active"] += 1
        state["counter_peak"] = max(state["counter_peak"], state["counter_active"])
        time.sleep(0.002)
        state["counter_active"] -= 1
        return 100
    factory = make_live_factory(args, token_counter=vision_token_counter(text_counter), thread_safe=True,
        reservation_ceiling_cny=reserve * 2.1 if mode == "project_cap" else 1)
    original_client = runtime.QwenDirectClient
    class Capture(original_client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            clients.append(self)
    monkeypatch.setattr(runtime, "QwenDirectClient", Capture)
    native_reserve = runtime.GlobalBudgetLedger.reserve
    def observed_reserve(self, *values):
        try: return native_reserve(self, *values)
        except Exception:
            stop_recorded.set()
            raise
    monkeypatch.setattr(runtime.GlobalBudgetLedger, "reserve", observed_reserve)
    snapshot = factory.ledger_snapshot
    def observed_snapshot():
        value = snapshot()
        if any(row["status"] == "uncertain" for row in value["reservations"]): stop_recorded.set()
        return value
    factory.ledger_snapshot = observed_snapshot
    class Response:
        status, headers = 200, {}
        def __init__(self):
            payload = {"model": config.model, "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "choices": [{"delta": {"content": json.dumps(FORMULA)}, "finish_reason": "stop"}]}
            self.lines = io.BytesIO(b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n")
        def __enter__(self):
            if mode != "success": assert stop_recorded.wait(5)
            return self
        def __exit__(self, *_): pass
        def readline(self): return self.lines.readline()
    class Opener:
        def open(self, request, timeout):
            with guard:
                calls.append(json.loads(request.data))
                index = len(calls)
            # Both durable reservations and mapping must exist before HTTP.
            for path in (project, account):
                assert runtime.GlobalBudgetLedger(path=path).as_dict()["reserved_cny"] > 0
            assert any(json.loads(path.read_bytes())["state"] == "ready_for_http" for path in args.account_mapping_dir.glob("dual-*.json"))
            if mode in {"success", "unknown"} and index <= 4: first_four.wait(timeout=5)
            if mode == "unknown" and index == 1: raise TimeoutError("synthetic disconnected stream")
            return Response()
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: Opener())
    recognizer = RegionalRecognizer(config, provider_factory=factory)
    output = recognizer.prepare(specs)
    regions = json.loads(output[0]["data"])["regions"]
    expected = 8 if mode == "success" else 4 if mode == "unknown" else 2
    assert len(calls) == expected and len({id(client) for client in clients}) == len(clients)
    assert state["counter_peak"] == 1
    assert sum(region["recognition_status"] == "recognized" for region in regions) == expected - int(mode == "unknown")
    if mode != "success": assert any(region["recognition_status"] == "pending" for region in regions)
    ledgers = [runtime.GlobalBudgetLedger(path=path).as_dict() for path in (project, account)]
    assert ledgers[0]["actual_cny"] == ledgers[1]["actual_cny"]
    assert all(sum(row["status"] == "uncertain" for row in value["reservations"]) == int(mode == "unknown") for value in ledgers)
    for attempt in config.cache_root.glob("*/attempt-*"):
        assert (attempt / "RESULT.json").is_file() and (attempt / "CACHE_MANIFEST.json").is_file()
    if mode in {"project_cap", "account_cap"}:
        originals = {path: path.read_bytes() for path in config.cache_root.rglob("*") if path.is_file()}
        successful_roots = {path.parent for path in config.cache_root.glob("*/attempt-*/RESPONSE.json")}
        resumed = recognizer.prepare(specs)
        assert len(calls) > expected  # released reservations now fit, with the same cap/policy
        assert all(path.read_bytes() == value for path, value in originals.items())
        assert all(not (path.parent / "attempt-2").exists() for path in successful_roots)
        resumed_regions = json.loads(resumed[0]["data"])["regions"]
        assert all(new["recognition_status"] == "recognized" for old, new in zip(regions, resumed_regions) if old["recognition_status"] == "recognized")
        assert all(runtime.GlobalBudgetLedger(path=path).as_dict()["actual_cny"] <= reserve * 2.1 for path in (project, account))
        return
    def forbidden(*_args, **_kwargs): raise AssertionError("replay attempted provider construction")
    replay = RegionalRecognizer(config, provider_factory=forbidden).prepare(specs)
    assert [x["recognition_status"] for x in json.loads(replay[0]["data"])["regions"]] == [x["recognition_status"] for x in regions]
    assert [runtime.GlobalBudgetLedger(path=path).as_dict() for path in (project, account)] == ledgers


def test_two_pdf_prepares_share_regional_and_docling_bounds(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    data, document, run, saved, config = _setup(tmp_path)
    guard, barrier = threading.Lock(), threading.Barrier(4)
    state = {"local_active": 0, "local_peak": 0, "local_calls": 0, "vision_active": 0, "vision_peak": 0}
    class Overlapping(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                with guard:
                    state["vision_active"] += 1
                    state["vision_peak"] = max(state["vision_peak"], state["vision_active"])
                try:
                    barrier.wait(timeout=5)
                    return call(messages, **kwargs)
                finally:
                    with guard: state["vision_active"] -= 1
            return wrapped
    def runner(command, **kwargs):
        with guard:
            state["local_active"] += 1
            state["local_calls"] += 1
            state["local_peak"] = max(state["local_peak"], state["local_active"])
        try:
            time.sleep(0.02)
            output = Path(command[2])
            (output / "document.json").write_text(json.dumps(document))
            (output / "run.json").write_text(json.dumps({**run, "source_sha256": hashlib.sha256(Path(command[1]).read_bytes()).hexdigest()}))
        finally:
            with guard: state["local_active"] -= 1
    factory = Overlapping()
    adapter = DoclingCliAdapter(replace(saved, document_path="", run_metadata_path="",
        command=("configured-native-runner", "{input}", "{output}")), runner=runner,
        recognizer=RegionalRecognizer(config, provider_factory=factory))
    inputs = [data, data + b"\n% a distinct second PDF\n"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(adapter.prepare, inputs))
    assert state["local_peak"] == 1 and state["local_calls"] == 2
    assert state["vision_peak"] == 4 and len(factory.calls) == 4
    assert all(region["recognition_status"] == "recognized" for output in outputs for region in json.loads(output[0]["data"])["regions"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(adapter.prepare, inputs)) == outputs
    assert state["local_calls"] == 2 and len(factory.calls) == 4


@pytest.mark.parametrize("value", [0, 9, True, 1.5])
def test_docling_process_bound_validation(tmp_path, value):
    from optomind_research.runtime.upgrade3.docling_adapter import DoclingAdapterConfig, DoclingAdapterError
    with pytest.raises(DoclingAdapterError, match="max_processes_invalid"):
        DoclingAdapterConfig(max_processes=value)


def test_formal_local_batch_shares_bounds_and_replays_free(tmp_path, monkeypatch, capsys):
    from scripts.upgrade3 import build_local_materials as cli, writer_candidates
    from optomind_research.runtime.upgrade3 import docling_adapter
    from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
    data, document, run, saved, _ = _setup(tmp_path)
    guard, barrier = threading.Lock(), threading.Barrier(4)
    state = {"local": 0, "local_peak": 0, "local_calls": 0, "vision": 0, "vision_peak": 0}
    class Parallel(Factory):
        def __call__(self, role, path, profile):
            call = super().__call__(role, path, profile)
            def wrapped(messages, **kwargs):
                with guard:
                    state["vision"] += 1
                    state["vision_peak"] = max(state["vision_peak"], state["vision"])
                try:
                    barrier.wait(timeout=5)
                    return call(messages, **kwargs)
                finally:
                    with guard: state["vision"] -= 1
            return wrapped
    def runner(command, **kwargs):
        with guard:
            state["local"] += 1
            state["local_calls"] += 1
            state["local_peak"] = max(state["local_peak"], state["local"])
        try:
            time.sleep(0.02)
            output = Path(command[2])
            (output / "document.json").write_text(json.dumps(document))
            (output / "run.json").write_text(json.dumps({**run, "source_sha256": hashlib.sha256(Path(command[1]).read_bytes()).hexdigest()}))
        finally:
            with guard: state["local"] -= 1
    monkeypatch.setattr(docling_adapter.subprocess, "run", runner)
    monkeypatch.setattr(MaterialAcquirer, "_ensure_grobid", lambda self: None)
    factory = Parallel()
    monkeypatch.setattr(writer_candidates, "make_live_factory", lambda *_args, **_kwargs: factory)
    monkeypatch.setattr(writer_candidates, "tokenizer_counter", lambda *_: (lambda *_: 100, {"offline": True}))
    paths = [tmp_path / "one.pdf", tmp_path / "two.pdf"]
    for i, path in enumerate(paths): path.write_bytes(data + f"\n% fixture {i}\n".encode())
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps({"records": [{"canonical_paper_id": f"batch-{i}", "local_pdf": str(path)} for i, path in enumerate(paths)]}))
    doc_config = tmp_path / "docling.json"
    doc_config.write_text(json.dumps({"parser_version": saved.parser_version, "config": dict(saved.config),
        "command": ["synthetic-native-runner", "{input}", "{output}"]}))
    rec_config = tmp_path / "regional.json"; rec_config.write_text('{"concurrency":1}')
    for name in ("project.sqlite", "account.sqlite", "keys.txt", "tokenizer.json"): (tmp_path / name).write_text("synthetic")
    argv = ["--batch-manifest", str(manifest), "--output-root", str(tmp_path / "out"), "--cache-root", str(tmp_path / "acquisition"),
        "--disable-rescue", "--network-workers", "2", "--docling-cli-config", str(doc_config), "--docling-max-processes", "1",
        "--recognition-config", str(rec_config), "--recognition-concurrency", "4", "--recognition-run", "--recognition-ceiling-cny", "1",
        "--budget-ledger", str(tmp_path / "project.sqlite"), "--account-ledger", str(tmp_path / "account.sqlite"),
        "--key-file", str(tmp_path / "keys.txt"), "--key-index", "1", "--tokenizer", str(tmp_path / "tokenizer.json")]
    assert cli.main(argv) == 0
    summaries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    papers = [row for row in summaries if row.get("paper_id")]
    assert len(papers) == 2 and all(row["status"] == "published" for row in papers)
    assert state["local_peak"] == 1 and state["local_calls"] == 2
    assert state["vision_peak"] == 4 and len(factory.calls) == 4
    for row in papers:
        snapshot = PreparedSnapshotProvider(row["snapshot_path"]).load()
        assert sum(asset.get("parser") == "docling" and asset["type"] in {"formula", "table"} for asset in snapshot.assets) == 2
        assert all(asset["recognition_status"] == "recognized" for asset in snapshot.assets if asset.get("parser") == "docling")
    assert cli.main(argv) == 0
    assert len(factory.calls) == 4 and state["local_calls"] == 2


def test_local_batch_error_keeps_other_completed_snapshot(tmp_path, monkeypatch, capsys):
    from scripts.upgrade3 import build_local_materials as cli
    source = tmp_path / "source.xml"
    source.write_text('<article><body><p>The retained condition is 500 nm.</p></body></article>')
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps({"records": [{"canonical_paper_id": name, "local_xml": str(source)} for name in ("bad", "good")]}))
    acquire = MaterialAcquirer.acquire_local
    def one_failure(self, record, **kwargs):
        if record["canonical_paper_id"] == "bad": raise ValueError("synthetic local failure")
        return acquire(self, record, **kwargs)
    monkeypatch.setattr(MaterialAcquirer, "acquire_local", one_failure)
    monkeypatch.setattr(MaterialAcquirer, "_ensure_grobid", lambda self: None)
    assert cli.main(["--batch-manifest", str(manifest), "--output-root", str(tmp_path / "out"),
        "--cache-root", str(tmp_path / "cache"), "--disable-rescue", "--network-workers", "2"]) == 0
    rows = {row["paper_id"]: row for row in map(json.loads, capsys.readouterr().out.splitlines()) if row.get("paper_id")}
    assert rows["bad"]["status"] == "failed" and "synthetic local failure" in rows["bad"]["errors"][0]
    assert rows["good"]["status"] == "published" and validate_snapshot(rows["good"]["snapshot_path"])["valid"]
    assert len(list((tmp_path / "out" / "_batch_checkpoints").glob("*.json"))) == 2


def test_formal_batch_reenters_unfinished_checkpoint_and_preserves_snapshot(tmp_path, monkeypatch, capsys):
    from scripts.upgrade3 import build_local_materials as cli, writer_candidates
    data, _, _, saved, _ = _setup(tmp_path)
    source = tmp_path / "source.pdf"; source.write_bytes(data)
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps({"records": [{"canonical_paper_id": "resume-budget", "local_pdf": str(source)}]}))
    options = tmp_path / "regional.json"; options.write_text('{"concurrency":1}')
    for name in ("project.sqlite", "account.sqlite", "keys.txt", "tokenizer.json"): (tmp_path / name).write_text("synthetic")
    factory = Factory(("table", RuntimeError("global_budget_exceeded")))
    monkeypatch.setattr(MaterialAcquirer, "_ensure_grobid", lambda self: None)
    monkeypatch.setattr(writer_candidates, "make_live_factory", lambda *_args, **_kwargs: factory)
    monkeypatch.setattr(writer_candidates, "tokenizer_counter", lambda *_: (lambda *_: 100, {"offline": True}))
    argv = ["--batch-manifest", str(manifest), "--output-root", str(tmp_path / "out"), "--cache-root", str(tmp_path / "acquisition"),
        "--disable-rescue", "--docling-document-json", str(saved.document_path), "--docling-run-json", str(saved.run_metadata_path),
        "--recognition-config", str(options), "--recognition-run", "--recognition-ceiling-cny", "1",
        "--budget-ledger", str(tmp_path / "project.sqlite"), "--account-ledger", str(tmp_path / "account.sqlite"),
        "--key-file", str(tmp_path / "keys.txt"), "--key-index", "1", "--tokenizer", str(tmp_path / "tokenizer.json")]
    assert cli.main(argv) == 0
    first = next(row for row in map(json.loads, capsys.readouterr().out.splitlines()) if row.get("paper_id"))
    assert first["status"] == "published" and len(factory.calls) == 1
    first_root = Path(first["snapshot_path"])
    originals = {path: path.read_bytes() for path in first_root.rglob("*") if path.is_file()}
    checkpoint = tmp_path / "out" / "_batch_checkpoints" / "resume-budget.json"
    assert json.loads(checkpoint.read_bytes())["regional_recognition_complete"] is False
    factory.fail = None
    assert cli.main(argv) == 0
    second = next(row for row in map(json.loads, capsys.readouterr().out.splitlines()) if row.get("paper_id"))
    assert second["status"] == "published" and second["snapshot_path"] != first["snapshot_path"]
    assert len(factory.calls) == 3 and all(path.read_bytes() == value for path, value in originals.items())
    assert json.loads(checkpoint.read_bytes())["regional_recognition_complete"] is True
    assert cli.main(argv) == 0
    third = next(row for row in map(json.loads, capsys.readouterr().out.splitlines()) if row.get("paper_id"))
    assert third["status"] == "resumed" and len(factory.calls) == 3
