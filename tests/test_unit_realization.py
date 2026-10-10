"""Labeled synthetic fixtures only: bounded production wiring, no network."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import pytest

from optomind_research.runtime.upgrade3 import legacy_unit_route as route
from optomind_research.runtime.upgrade3 import unit_realization as quality
from optomind_research.runtime.upgrade3 import review_unit_writer as writer
from optomind_research.runtime.upgrade3.fullbody_contracts import register_tool_source_identities


def book(units=1):
    return {"language": "en", "source_aliases": {}, "chapters": [{
        "chapter_id": "CH", "chapter_frame": {"chapter_title": "Synthetic chapter", "chapter_argument": "Metadata assertion"},
        "sources": [{"source_handle": "P0001", "doi": "10.1234/fixture-a", "title": "Fixture A",
                     "study_summary_A": {"text": "Synthetic material supports A and B only."}}],
        "units": [{"unit_id": f"U{i}", "focus": "Synthetic focus", "title": "Explicit unit title",
                   "paragraph_tasks": [{"paragraph_id": "p1", "point": "Explain A", "source_uses": [{"source_handle": "P0001"}]},
                                       {"paragraph_id": "p2", "point": "Explain B", "source_uses": [{"source_handle": "P0001"}]}],
                   "table_tasks": []} for i in range(units)]}]}


def counter(raw, messages):
    return 100


def response(data, finish="stop"):
    return {"content": json.dumps(data), "finish_reason": finish, "complete": finish == "stop",
            "model": "qwen3.5-plus", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def assessment(body, second="covered", changes=None):
    return response({"tasks": [
        {"task_id": "p1", "status": "covered", "body_quote": "SYNTHETIC A", "explanation": "A is present", "material_handles": ["P0001"]},
        {"task_id": "p2", "status": second, "body_quote": "SYNTHETIC B" if second == "covered" else "",
         "explanation": "B is present" if second == "covered" else "B has not been explained", "material_handles": ["P0001"]}],
        "changes": changes or [], "issues": []})


class Factory:
    execution_mode = "injected"
    def __init__(self, first_status="missing", partial_step="", edit=None):
        self.calls = []
        self.first_status, self.partial_step, self.edit = first_status, partial_step, edit

    def __call__(self, role, path, profile):
        def call(messages, **kwargs):
            self.calls.append((role, deepcopy(messages), deepcopy(kwargs)))
            payload = json.loads(messages[-1]["content"])
            if role == "writer":
                data = response({"body_markdown": "SYNTHETIC A [P0001].", "issues": []})
            elif role == "completion":
                assert payload["requested_task_ids"] == ["p2"]
                assert [task["paragraph_id"] for task in payload["paragraph_tasks"]] == ["p2"]
                assert [row["task_id"] for row in payload["gap_feedback"]] == ["p2"]
                assert payload["gap_feedback"][0]["explanation"] == "B has not been explained"
                data = response({"body_markdown": "SYNTHETIC B [P0001].", "status": "appended", "covered_task_ids": ["p2"], "issues": []})
            else:
                data = assessment(payload["actual_body_markdown"], self.first_status if role == "assessment" else "covered",
                                  [self.edit] if self.edit and role == "assessment" else [])
            if role == self.partial_step:
                data.update(finish_reason="length", complete=False)
            return data
        call.prompt_token_counter = counter
        return call


def run_stage(tmp_path, factory, body="SYNTHETIC A [P0001].", run=True, reparse_saved=False, retry_failed=False):
    view = route.build_unit_views(book())[0]
    return quality.run_unit_quality(view, payload=route.build_legacy_payload(view, language="en"),
        existing_body=body, output_dir=tmp_path, client_factory=factory, run=run, token_counter=counter,
        language="en", experiment_label="offline_synthetic_fixture", reparse_saved=reparse_saved, retry_failed=retry_failed)


def test_omitted_task_old_response_completed_once_and_prefix_preserved(tmp_path):
    factory = Factory()
    original = "SYNTHETIC A [P0001].\r\nexact prefix"
    result = run_stage(tmp_path, factory, original)
    assert [row[0] for row in factory.calls] == ["assessment", "completion", "post_assessment"]
    assert result["body_markdown"].startswith(original + "\n\n")
    assert result["status"] == "assessed_pending_human_review"
    assert result["scientific_acceptance"] is False
    assert factory.calls[0][2]["thinking_budget"] == 16384
    assert factory.calls[0][2]["max_output_tokens"] == 24576
    assert factory.calls[1][2]["thinking_budget"] == 8192
    assert factory.calls[1][2]["max_output_tokens"] == 32768
    resumed = run_stage(tmp_path, factory, original)
    assert resumed["cache_hit"] and resumed["model_calls"] == 0
    assert len(factory.calls) == 3


def test_naturally_combined_tasks_no_forced_extra_paragraph(tmp_path):
    factory = Factory(first_status="covered")
    original = "SYNTHETIC A and SYNTHETIC B [P0001] share this natural paragraph."
    result = run_stage(tmp_path, factory, original)
    assert len(factory.calls) == 1
    assert result["body_markdown"] == original
    assert not result["changed"] and not result["pending_problems"]


class CandidateFactory(Factory):
    """Synthetic missing-envelope controls; no scientific acceptance claims."""
    def __init__(self, post_finish="stop"):
        super().__init__()
        self.post_finish = post_finish

    def __call__(self, role, path, profile):
        original = super().__call__(role, path, profile)
        def call(messages, **kwargs):
            result = original(messages, **kwargs)
            if role == "completion":
                data = json.loads(result["content"])
                data.pop("status")
                data.pop("covered_task_ids")
                data["issues"] = [{"code": "synthetic_scientific_issue_retained"}]
                return response(data)
            if role == "post_assessment":
                data = json.loads(result["content"])
                data["tasks"][1].update(status="partial", explanation="Synthetic remaining detail; not scientific acceptance")
                data["issues"] = ["Synthetic post-check issue retained"]
                return response(data, finish=self.post_finish)
            return result
        call.prompt_token_counter = counter
        return call


def test_missing_completion_metadata_adopted_only_after_existing_post_preserves_partial(tmp_path):
    factory = CandidateFactory()
    original = "SYNTHETIC A [P0001].\r\nexact prefix"
    result = run_stage(tmp_path, factory, original)
    assert [row[0] for row in factory.calls] == ["assessment", "completion", "post_assessment"]
    assert result["body_markdown"] == original + "\n\nSYNTHETIC B [P0001]."
    assert result["completion"]["pending"] and result["completion"]["candidate_eligible"]
    assert result["completion"]["covered_task_ids"] == [] and result["completion"]["model_status"] == ""
    assert result["completion_candidate"]["status"] == "adopted_pending_human_review"
    assert result["status"] == "assessed_with_pending" and result["scientific_acceptance"] is False
    assert {"code": "synthetic_scientific_issue_retained"} in result["completion"]["issues"]
    assert any(row["code"] == "quality_task_partial" for row in result["pending_problems"])
    assert any(row["code"] == "quality_assessor_issue" for row in result["pending_problems"])
    resumed = run_stage(tmp_path, factory, original, reparse_saved=True)
    assert resumed["model_calls"] == 0 and len(factory.calls) == 3
    assert resumed["body_markdown"] == result["body_markdown"]


def test_missing_completion_metadata_free_preview_then_only_post_missing(tmp_path):
    seeded = run_stage(tmp_path / "seed", CandidateFactory())
    output = tmp_path / "replay"
    target = output / Path(seeded["output_dir"]).name
    for name in ("assessment", "completion"):
        shutil.copytree(Path(seeded["output_dir"]) / name, target / name)
    paid = {path: path.read_bytes() for name in ("assessment", "completion")
            for path in (target / name).glob("*.json") if path.name in {"RAW_RESPONSE.json", "REQUEST.json", "MESSAGES.json"}}
    factory = CandidateFactory()
    planned = run_stage(output, factory, run=False, reparse_saved=True)
    assert planned["status"] == "planned" and planned["model_calls"] == 0 and not factory.calls
    assert planned["body_markdown"] == "SYNTHETIC A [P0001]."
    assert planned["completion_candidate"]["status"] == "awaiting_post_assessment"
    assert Path(planned["completion"]["candidate_body_path"]).is_file()
    completed = run_stage(output, factory, reparse_saved=True)
    assert completed["model_calls"] == 1 and [row[0] for row in factory.calls] == ["post_assessment"]
    assert completed["body_markdown"].endswith("SYNTHETIC B [P0001].")
    assert all(path.read_bytes() == before for path, before in paid.items())
    resumed = run_stage(output, factory, reparse_saved=True)
    assert resumed["model_calls"] == 0 and len(factory.calls) == 1


def test_missing_completion_metadata_failed_post_retains_original_and_candidate(tmp_path):
    factory = CandidateFactory(post_finish="length")
    result = run_stage(tmp_path, factory)
    assert result["status"] == "pending"
    assert result["body_markdown"] == "SYNTHETIC A [P0001]." and not result["changed"]
    assert result["completion_candidate"]["status"] == "post_assessment_failed"
    assert Path(result["completion"]["candidate_body_path"]).read_text(encoding="utf-8").endswith("SYNTHETIC B [P0001].")
    resumed = run_stage(tmp_path, factory, reparse_saved=True)
    assert resumed["model_calls"] == 0 and len(factory.calls) == 3
    assert resumed["body_markdown"] == result["body_markdown"]


@pytest.mark.parametrize("status", ["covered", "partial", "missing"])
def test_unlocated_task_diagnostic_preserved_without_automatic_completion(tmp_path, status):
    factory = Factory(first_status=status)
    def unlocated(role, path, profile):
        base = factory(role, path, profile)
        def client(messages, **kwargs):
            raw = base(messages, **kwargs)
            data = json.loads(raw["content"])
            data["tasks"][1]["body_quote"] = "Invented task quote"
            return response(data)
        client.prompt_token_counter = counter
        return client
    result = run_stage(tmp_path, unlocated)
    assert [row[0] for row in factory.calls] == ["assessment"]
    assert result["body_markdown"] == "SYNTHETIC A [P0001]." and not result["changed"]
    valid, invalid = result["assessments"][0]["report"]["tasks"]
    assert valid["body_quote_verified"] and valid["effective_status"] == "covered"
    assert invalid["status"] == status and invalid["body_quote"] == "Invented task quote"
    assert invalid["effective_status"] == "unverified" and not invalid["body_quote_verified"]
    assert invalid["body_quote_error"] == "quality_body_quote_invalid:p2"
    assert result["status"] == "assessed_with_pending" and result["scientific_acceptance"] is False
    assert any(row["code"] == "quality_task_unverified" for row in result["pending_problems"])
    replayed = run_stage(tmp_path, unlocated, reparse_saved=True)
    assert replayed["model_calls"] == 0 and len(factory.calls) == 1


def test_unlocated_quote_never_bypasses_assessment_structure_or_model_checks():
    view = route.build_unit_views(book())[0]
    body = "SYNTHETIC A [P0001]."
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), body)
    data = json.loads(assessment(body)["content"])
    data["tasks"][1]["body_quote"] = "Invented quote"
    for key, invalid in (("task_id", "p1"), ("status", "unknown"), ("explanation", ""),
                         ("material_handles", ["P9999"]), ("body_quote", None)):
        changed = deepcopy(data)
        changed["tasks"][1][key] = invalid
        with pytest.raises(quality.CandidateError):
            quality.validate_assessment(response(changed), messages, view, body)
    with pytest.raises(quality.CandidateError, match="report_incomplete"):
        quality.validate_assessment(response({**data, "tasks": data["tasks"][:1]}), messages, view, body)
    for raw, error in ((response(data, finish="length"), "response_incomplete"),
                       ({**response(data), "model": "other-model"}, "returned_model_mismatch")):
        with pytest.raises(quality.CandidateError, match=error):
            quality.validate_assessment(raw, messages, view, body)
    # Effective parser fields cannot be supplied by the model to authorize use.
    data["tasks"][1].update(body_quote_verified=True, effective_status="covered")
    parsed = quality.validate_assessment(response(data), messages, view, body)
    assert parsed["tasks"][1]["effective_status"] == "unverified"


@pytest.mark.parametrize("domain", ["battery", "climate"])
def test_other_domain_synthetic_quote_failure_is_pending_in_ordinary_quality_path(tmp_path, domain):
    fixture = book()
    unit = fixture["chapters"][0]["units"][0]
    unit["focus"] = "OFFLINE " + domain + " parser wiring fixture; no scientific conclusion"
    for index, task in enumerate(unit["paragraph_tasks"]):
        task["paragraph_id"] = domain + "_task_" + str(index + 1)
        task["point"] = "Explain offline " + domain + " fixture marker " + str(index + 1)
    view = route.build_unit_views(fixture)[0]
    body = "OFFLINE " + domain + " fixture value 1 [P0001]."
    calls = []
    def offline_factory(role, path, profile):
        assert role == "assessment", "Unverified missing status must not request completion"
        def client(messages, **kwargs):
            calls.append(role)
            tasks = json.loads(messages[-1]["content"])["task_catalog"]
            return response({"tasks": [
                {"task_id": tasks[0]["task_id"], "status": "covered", "body_quote": body,
                 "explanation": "Fixture marker locates actual text", "material_handles": ["P0001"]},
                {"task_id": tasks[1]["task_id"], "status": "missing", "body_quote": body.replace("value 1", "value 2"),
                 "explanation": "Offline changed-number control, not a domain conclusion", "material_handles": ["P0001"]}],
                "changes": [], "issues": []})
        client.prompt_token_counter = counter
        return client
    result = quality.run_unit_quality(view, payload=route.build_legacy_payload(view, language="en"),
        existing_body=body, output_dir=tmp_path, client_factory=offline_factory, run=True,
        token_counter=counter, language="en", experiment_label="offline_cross_domain_parser_control")
    assert calls == ["assessment"] and result["body_markdown"] == body
    assert result["status"] == "assessed_with_pending" and not result["scientific_acceptance"]
    row = result["assessments"][0]["report"]["tasks"][1]
    assert row["status"] == "missing" and row["effective_status"] == "unverified"


def test_one_unlocated_post_task_does_not_discard_valid_diagnostics_or_exact_edit(tmp_path):
    factory = Factory()
    edit = {"operation": "replace", "original_text": "SYNTHETIC B", "replacement_text": "SYNTHETIC B revised",
            "reason": "Synthetic edit consumption control only", "material_handles": ["P0001"],
            "material_quote": "Synthetic material supports A and B only."}
    def post_quote(role, path, profile):
        base = factory(role, path, profile)
        def client(messages, **kwargs):
            raw = base(messages, **kwargs)
            if role == "post_assessment":
                data = json.loads(raw["content"])
                data["tasks"][1]["body_quote"] = "A paraphrase absent from actual text"
                data["changes"] = [edit]
                data["issues"] = ["Synthetic unresolved scientific issue"]
                return response(data)
            return raw
        client.prompt_token_counter = counter
        return client
    result = run_stage(tmp_path, post_quote)
    assert [row[0] for row in factory.calls] == ["assessment", "completion", "post_assessment"]
    assert result["body_markdown"] == "SYNTHETIC A [P0001].\n\nSYNTHETIC B revised [P0001]."
    rows = result["assessments"][-1]["report"]["tasks"]
    assert rows[0]["body_quote_verified"] and rows[1]["effective_status"] == "unverified"
    assert rows[1]["status"] == "covered" and rows[1]["body_quote"] == "A paraphrase absent from actual text"
    assert len(result["post_edits"]["applied"]) == 1 and not result["post_edits"]["rejected"]
    assert {row["code"] for row in result["pending_problems"]} >= {"quality_task_unverified", "quality_assessor_issue"}
    assert not result["scientific_acceptance"]


@pytest.mark.parametrize("diagnostic", ["unlocated", "empty_noncoverage"])
def test_missing_metadata_candidate_without_located_task_retains_base_and_only_base_edits(tmp_path, diagnostic):
    factory = CandidateFactory()
    base_edit = {"operation": "replace", "original_text": "SYNTHETIC A", "replacement_text": "SYNTHETIC A revised",
                 "reason": "Synthetic base edit control", "material_handles": ["P0001"],
                 "material_quote": "Synthetic material supports A and B only."}
    candidate_edit = {**base_edit, "original_text": "SYNTHETIC B", "replacement_text": "Must not enter retained body"}
    def no_located_post(role, path, profile):
        base = factory(role, path, profile)
        def client(messages, **kwargs):
            raw = base(messages, **kwargs)
            if role == "post_assessment":
                data = json.loads(raw["content"])
                for row in data["tasks"]:
                    row.update(body_quote="Invented quote" if diagnostic == "unlocated" else "")
                    if diagnostic == "empty_noncoverage":
                        row["status"] = "material_limited"
                data["changes"] = [base_edit, candidate_edit]
                return response(data)
            return raw
        client.prompt_token_counter = counter
        return client
    result = run_stage(tmp_path, no_located_post)
    assert [row[0] for row in factory.calls] == ["assessment", "completion", "post_assessment"]
    assert result["body_markdown"] == "SYNTHETIC A revised [P0001]."
    assert result["completion_candidate"]["status"] == "post_assessment_has_no_located_diagnostic"
    assert "SYNTHETIC B" in Path(result["completion"]["candidate_body_path"]).read_text(encoding="utf-8")
    assert len(result["post_edits"]["applied"]) == len(result["post_edits"]["rejected"]) == 1
    assert "anchor" in result["post_edits"]["rejected"][0]["error"]
    assert any(row["code"] == "quality_completion_candidate_unverified" for row in result["pending_problems"])
    assert not result["scientific_acceptance"]
    replayed = run_stage(tmp_path, no_located_post, reparse_saved=True)
    assert replayed["model_calls"] == 0 and len(factory.calls) == 3
    assert replayed["body_markdown"] == result["body_markdown"]


def test_material_limited_does_not_force_completion(tmp_path):
    factory = Factory(first_status="material_limited")
    result = run_stage(tmp_path, factory)
    assert len(factory.calls) == 1
    assert result["body_markdown"] == "SYNTHETIC A [P0001]."
    assert result["pending_problems"][0]["code"] == "quality_task_material_limited"


@pytest.mark.parametrize("step", ["assessment", "completion", "post_assessment"])
def test_partial_quality_response_saved_and_never_rebought(tmp_path, step):
    factory = Factory(partial_step=step)
    first = run_stage(tmp_path, factory)
    count = len(factory.calls)
    result = run_stage(tmp_path, factory)
    assert len(factory.calls) == count and result["model_calls"] == 0
    assert result["pending_problems"]
    assert (Path(first["output_dir"]) / step / "RAW_RESPONSE.json").is_file()
    if step == "post_assessment":
        assert "SYNTHETIC B" in result["body_markdown"]
    else:
        assert result["body_markdown"] == "SYNTHETIC A [P0001]."


def test_preview_then_run_and_raw_recovery_do_not_rebuy(tmp_path):
    factory = Factory(first_status="covered")
    body = "SYNTHETIC A and SYNTHETIC B [P0001]."
    planned = run_stage(tmp_path, factory, body, run=False)
    assert not factory.calls and planned["status"] == "planned"
    first = run_stage(tmp_path, factory, body)
    assert len(factory.calls) == 1
    target = Path(first["output_dir"])
    (target / "QUALITY_RESULT.json").unlink()
    (target / "RESULT_SEAL.json").unlink()
    recovered = run_stage(tmp_path, factory, body)
    assert len(factory.calls) == 1 and recovered["model_calls"] == 0


class RetryFactory(Factory):
    """Explicit offline stopped exceptions with no returned content."""
    def __init__(self, role="assessment", failures=1, first_status="missing"):
        super().__init__(first_status=first_status)
        self.failed_role, self.failures = role, failures
        self.paths = []

    def __call__(self, role, path, profile):
        self.paths.append((role, Path(path)))
        base = super().__call__(role, path, profile)
        def call(messages, **kwargs):
            if role == self.failed_role and self.failures:
                self.failures -= 1
                self.calls.append((role, deepcopy(messages), deepcopy(kwargs)))
                failure = RuntimeError("SYNTHETIC terminal account rejection; no content")
                failure.record = {"content": "", "complete": False}
                raise failure
            return base(messages, **kwargs)
        call.prompt_token_counter = counter
        return call


@pytest.mark.parametrize("failed_role", ["assessment", "completion", "post_assessment"])
def test_explicit_quality_retry_keeps_failed_attempt_and_reuses_successful_steps(tmp_path, failed_role):
    factory = RetryFactory(failed_role)
    failed = run_stage(tmp_path, factory)
    stage = Path(failed["output_dir"]) / failed_role
    old = {path.relative_to(stage): path.read_bytes() for path in stage.rglob("*") if path.is_file()}
    assert Path("REQUEST.json") in old and Path("ERROR.json") in old and Path("RAW_RESPONSE.json") not in old
    count = len(factory.calls)
    no_flag = run_stage(tmp_path, factory)
    assert no_flag["model_calls"] == 0 and len(factory.calls) == count
    free = run_stage(tmp_path, factory, run=False, retry_failed=True, reparse_saved=True)
    assert free["model_calls"] == 0 and len(factory.calls) == count and not (stage / "retries").exists()
    retried = run_stage(tmp_path, factory, retry_failed=True)
    expected = {"assessment": ["assessment", "completion", "post_assessment"],
                "completion": ["completion", "post_assessment"], "post_assessment": ["post_assessment"]}[failed_role]
    assert [row[0] for row in factory.calls[count:]] == expected
    assert retried["model_calls"] == len(expected) and retried["changed"]
    assert retried["scientific_acceptance"] is False
    attempt = stage / "retries" / "attempt_002"
    assert (attempt / "RAW_RESPONSE.json").is_file()
    assert (attempt / "REQUEST.json").read_bytes() == old[Path("REQUEST.json")]
    assert any(role == failed_role and path == attempt for role, path in factory.paths)
    initial_id = next(row[2]["call_id"] for row in factory.calls if row[0] == failed_role)
    retry_id = next(row[2]["call_id"] for row in factory.calls[count:] if row[0] == failed_role)
    assert initial_id != retry_id and retry_id.endswith("retry-attempt_002")
    assert sum(row["new_retry"] for row in retried["stage_attempts"]) == 1
    assert all((stage / name).read_bytes() == content for name, content in old.items())
    assert Path(retried["reparse_saved"]["history_dir"]).is_dir()
    calls = len(factory.calls)
    resumed = run_stage(tmp_path, factory, retry_failed=True, reparse_saved=True)
    assert resumed["model_calls"] == 0 and len(factory.calls) == calls
    assert resumed["body_markdown"] == retried["body_markdown"]


def test_failed_explicit_quality_retry_stops_once_until_another_explicit_flag(tmp_path):
    factory = RetryFactory(failures=3)
    first = run_stage(tmp_path, factory)
    stage = Path(first["output_dir"]) / "assessment"
    second = run_stage(tmp_path, factory, retry_failed=True)
    assert second["model_calls"] == 1 and len(factory.calls) == 2 and second["status"] == "pending"
    assert (stage / "retries/attempt_002/ERROR.json").is_file()
    assert not (stage / "retries/attempt_003").exists()
    ordinary = run_stage(tmp_path, factory, reparse_saved=True)
    assert ordinary["model_calls"] == 0 and len(factory.calls) == 2
    third = run_stage(tmp_path, factory, retry_failed=True)
    assert third["model_calls"] == 1 and len(factory.calls) == 3
    assert (stage / "retries/attempt_003/ERROR.json").is_file()
    assert len({row[2]["call_id"] for row in factory.calls}) == 3


@pytest.mark.parametrize("case", ["unknown_request", "partial_raw", "error_record_has_body", "request_mismatch"])
def test_quality_retry_never_rebuys_unknown_or_replayable_or_changed_requests(tmp_path, case):
    view = route.build_unit_views(book())[0]
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), "SYNTHETIC A")
    profile = route._profile("qwen3.5-plus", 24576, 16384)
    stage = tmp_path / "assessment"
    stage.mkdir()
    request = {"profile": profile, "messages_sha256": quality._hash(messages)}
    if case == "request_mismatch":
        request["messages_sha256"] = "invented"
    quality._write(stage / "REQUEST.json", request)
    if case != "unknown_request":
        quality._write(stage / "ERROR.json", {"error": "SYNTHETIC stopped exception",
            "record": {"content": "SYNTHETIC returned body"} if case == "error_record_has_body" else None})
        quality._write(stage / "STAGE_ERROR.json", {"error": "SYNTHETIC stopped exception"})
    if case == "partial_raw":
        quality._write(stage / "RAW_RESPONSE.json", response({"tasks": []}, finish="length"))
    originals = {path: path.read_bytes() for path in stage.glob("*.json")}
    def no_provider(*args):
        pytest.fail("Explicit retry must not dispatch this stage")
    options = dict(step=stage, messages=messages, profile=profile, client_factory=no_provider,
                   run=True, token_counter=counter, retry_failed=True)
    if case == "request_mismatch":
        with pytest.raises(ValueError, match="quality_cached_request_identity_changed"):
            quality._cached_call(**options)
    else:
        _, calls, state = quality._cached_call(**options)
        assert calls == 0 and state in {"pending_existing_attempt", "cache_hit"}
    assert not (stage / "retries").exists()
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_real_route_retry_flag_recovers_quality_only_preserves_author(tmp_path):
    factory = RetryFactory("assessment")
    factory.execution_mode = "live"  # Offline seam exercises actual assembler.
    first = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory,
                                   token_counter=counter, quality_control=True)
    author = Path(first["units"][0]["attempt_dir"])
    preserved = {name: (author / name).read_bytes() for name in
                 ("UNIT_BODY.md", "UNIT_RESULT.json", "RESULT_SEAL.json", "RAW_RESPONSE.json", "REQUEST.json")}
    count = len(factory.calls)
    retried = route.run_legacy_units(book(), output_dir=tmp_path, run=True, retry_failed=True,
        client_factory=factory, token_counter=counter, quality_control=True)
    assert retried["model_calls"] == 3 and [row[0] for row in factory.calls[count:]] == ["assessment", "completion", "post_assessment"]
    assert retried["units"][0]["cache_hit"] and "SYNTHETIC B" in retried["units"][0]["quality"]["body_markdown"]
    assert all((author / name).read_bytes() == content for name, content in preserved.items())


def test_quality_retry_preserves_durable_settled_spend_and_unsettled_hold(tmp_path):
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
    ledger = GlobalBudgetLedger(limit_cny=2.0, path=tmp_path / "same_owned_ledger.sqlite")
    paid = ledger.reserve(0.4, "prior_paid_call")
    ledger.settle(paid["reservation_id"], 0.4)
    hold = ledger.reserve(0.8, "prior_unknown_call")
    ledger.settle(hold["reservation_id"], None, uncertain=True)
    before = deepcopy(ledger.reservations)
    failed = run_stage(tmp_path / "quality", RetryFactory(first_status="covered"))
    attempts = []
    def same_ledger_factory(role, path, profile):
        def client(messages, **kwargs):
            attempts.append(kwargs["call_id"])
            ledger.reserve(0.9, kwargs["call_id"])  # Existing spend/hold must refuse this amount.
            pytest.fail("Budget-refused request cannot reach even the synthetic provider")
        client.prompt_token_counter = counter
        return client
    result = run_stage(tmp_path / "quality", same_ledger_factory, retry_failed=True)
    assert len(attempts) == 1 and attempts[0].endswith("retry-attempt_002")
    assert result["status"] == "pending" and Path(failed["output_dir"]) == Path(result["output_dir"])
    assert ledger.reservations == before and ledger.limit_cny == 2.0
    reloaded = GlobalBudgetLedger(path=ledger.path)
    reloaded._refresh_from_db()
    assert reloaded.actual_cny == pytest.approx(0.4) and reloaded.reserved_cny == pytest.approx(0.8)
    assert reloaded.limit_cny == 2.0


def test_quotes_task_ids_and_handles_are_checked():
    view = route.build_unit_views(book())[0]
    body = "SYNTHETIC A and SYNTHETIC B"
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), body)
    for field, value in [("task_id", "invented"), ("body_quote", 123), ("material_handles", ["P9999"]), ("status", "covered_elsewhere")]:
        data = json.loads(assessment(body)["content"])
        data["tasks"][0][field] = value
        with pytest.raises(ValueError, match="quality_"):
            quality.validate_assessment(response(data), messages, view, body)


def test_ordered_omission_quotes_strictly_anchor_all_fragments():
    body = "SYNTHETIC A first [P0001]. Middle omitted. Last fragment. SYNTHETIC B."
    view = route.build_unit_views(book())[0]
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), body)
    data = json.loads(assessment(body)["content"])
    data["tasks"][0]["body_quote"] = "SYNTHETIC A first [P0001]. ... Last fragment."
    parsed = quality.validate_assessment(response(data), messages, view, body)
    row = parsed["tasks"][0]
    assert row["body_quote"] == data["tasks"][0]["body_quote"]
    assert row["body_quote_match"] == "ordered_omission_excerpt"
    assert [body[span["start"]:span["end"]] for span in row["body_quote_spans"]] == [
        "SYNTHETIC A first [P0001].", "Last fragment."]
    for invalid in ("Last fragment. ... SYNTHETIC A first [P0001].",
                    "SYNTHETIC A changed [P0001]. ... Last fragment.", "..."):
        data["tasks"][0]["body_quote"] = invalid
        unverified = quality.validate_assessment(response(data), messages, view, body)["tasks"][0]
        assert unverified["effective_status"] == "unverified" and unverified["status"] == "covered"
        assert not unverified["body_quote_verified"] and unverified["body_quote"] == invalid
        assert unverified["body_quote_error"].startswith("quality_body_quote_invalid:")
    spans, match = quality._body_quote_spans("Literal ... source text", "Literal ... source text")
    assert match == "exact" and len(spans) == 1


def test_boundary_omission_quotes_match_one_nonempty_original_fragment():
    body = "Prefix SYNTHETIC value 39 cm, no increase. Suffix ... literal."
    excerpt = "SYNTHETIC value 39 cm, no increase"
    record = {"evidence": body}
    for quote in (excerpt + "...", "… " + excerpt, "... " + excerpt + " …"):
        spans, match = quality._body_quote_spans(body, quote)
        assert match == "ordered_omission_excerpt" and len(spans) == 1
        assert body[spans[0]["start"]:spans[0]["end"]] == excerpt
        material_spans, material_match = quality._material_quote_spans(record, quote)
        assert material_match == match and material_spans == [{"path": ["evidence"], **spans[0]}]
    for quote in ("...", "…", " ... … ", excerpt.replace("39", "40") + "...",
                  excerpt.replace("no increase", "increase") + "..."):
        assert quality._body_quote_spans(body, quote)[0] == []
        assert quality._material_quote_spans(record, quote)[0] == []


def test_whitespace_quote_mapping_preserves_original_characters_and_order():
    body = "整合5个队列，AUC=0.75。 变化未显示增加。 Middle omitted. 尾段ABC。"
    quote = "整合 5 个队列，AUC = 0.75。"
    spans, kind = quality._body_quote_spans(body, quote)
    assert kind == "whitespace_normalized" and spans[0]["text"] == "整合5个队列，AUC=0.75。"
    assert body[spans[0]["start"]:spans[0]["end"]] == spans[0]["text"]
    spans, kind = quality._body_quote_spans(body, quote + " ... 尾段 A B C。")
    assert kind == "ordered_omission_excerpt_whitespace_normalized"
    assert [s["text"] for s in spans] == ["整合5个队列，AUC=0.75。", "尾段ABC。"]
    for invalid in ("整合6个队列，AUC=0.75。", "整合5个队列，AUC=0.76。",
                    "变化显示增加。", "尾段ABC。 ... 整合5个队列，AUC=0.75。"):
        assert quality._body_quote_spans(body, invalid)[0] == []


def test_complete_quote_typo_json_recovery_retains_content_and_validation():
    view = route.build_unit_views(book())[0]
    body = "SYNTHETIC A and SYNTHETIC B"
    messages = quality.assessment_messages(view, route.build_legacy_payload(view), body)
    data = json.loads(assessment(body)["content"])
    data["tasks"][0]["explanation"] = 'Explains "A“ without changing content'
    raw = json.dumps(data).replace('\\"A', '"A')
    malformed = {**response(data), "content": raw}
    parsed = quality.validate_assessment(malformed, messages, view, body)
    assert parsed["parsed_via"] == "json_repair_quote_escape_crosschecked"
    assert parsed["tasks"][0]["explanation"] == data["tasks"][0]["explanation"]
    assert malformed["content"] == raw
    for invalid in ({**malformed, "finish_reason": "length", "complete": False},
                    {**malformed, "complete": False},
                    {**malformed, "content": raw[:-2]},
                    {**malformed, "content": raw[:-3] + "}"}):
        with pytest.raises(ValueError, match="quality_"):
            quality.validate_assessment(invalid, messages, view, body)
    changed = quality.validate_assessment({**malformed, "content": raw.replace("SYNTHETIC A", "Invented A", 1)}, messages, view, body)
    assert changed["tasks"][0]["effective_status"] == "unverified"
    assert changed["tasks"][1]["body_quote_verified"]


def test_material_excerpt_same_record_paths_and_strict_body_anchor():
    view = route.build_unit_views(book())[0]
    view.materials[0]["study_summary_A"] = {"findings": [
        {"text": "SYNTHETIC value 39 cm, no increase."}, {"text": "SYNTHETIC value 18 cm and 47 cm."}]}
    edit = {"operation": "replace", "original_text": "WRONG value", "replacement_text": "Supported value",
            "reason": "Synthetic diagnostic", "material_handles": ["P0001"],
            "material_quote": "SYNTHETIC value39 cm, no increase. ... SYNTHETIC value18 cm and47 cm."}
    body = "Prefix WRONG value. Untouched suffix."
    changed, applied = quality.apply_evidence_edits(body, [edit], view)
    assert changed == "Prefix Supported value. Untouched suffix."
    evidence = applied[0]["material_quote_evidence"]
    assert evidence["match"] == "ordered_omission_excerpt_whitespace_normalized"
    assert [s["path"] for s in evidence["spans"]] == [
        ["study_summary_A", "findings", 0, "text"], ["study_summary_A", "findings", 1, "text"]]
    assert [s["text"] for s in evidence["spans"]] == [
        "SYNTHETIC value 39 cm, no increase.", "SYNTHETIC value 18 cm and 47 cm."]
    for quote in (edit["material_quote"].replace("39", "40"),
                  edit["material_quote"].replace("no increase", "increase"),
                  "Invented claim ... SYNTHETIC value 18 cm and 47 cm."):
        with pytest.raises(ValueError, match="material_quote"):
            quality.apply_evidence_edits(body, [{**edit, "material_quote": quote}], view)
    with pytest.raises(ValueError, match="anchor"):
        quality.apply_evidence_edits(body, [{**edit, "original_text": "WRONGvalue"}], view)
    first, second = deepcopy(view.materials[0]), deepcopy(view.materials[0])
    first["study_summary_A"]["findings"] = first["study_summary_A"]["findings"][:1]
    second["study_summary_A"]["findings"] = second["study_summary_A"]["findings"][1:]
    view.materials = [first, second]
    with pytest.raises(ValueError, match="material_quote"):
        quality.apply_evidence_edits(body, [edit], view)


def test_reparse_preview_then_run_reuses_raw_and_only_continues_missing_steps(tmp_path, monkeypatch):
    factory = Factory()
    validator = quality.validate_assessment
    with monkeypatch.context() as previous_parser:
        previous_parser.setattr(quality, "validate_assessment", lambda *a: (_ for _ in ()).throw(
            ValueError("quality_body_quote_invalid:old_parser")))
        failed = run_stage(tmp_path, factory)
    assert len(factory.calls) == 1 and failed["status"] == "pending"
    target = Path(failed["output_dir"])
    originals = {name: (target / name).read_bytes() for name in
                 ("QUALITY_RESULT.json", "RESULT_SEAL.json", "QUALITY_BODY.md", "assessment/RAW_RESPONSE.json", "assessment/REQUEST.json")}
    ordinary = run_stage(tmp_path, factory)
    assert ordinary["model_calls"] == 0 and ordinary["status"] == "pending"
    planned = run_stage(tmp_path, factory, run=False, reparse_saved=True)
    assert planned["model_calls"] == 0 and planned["status"] == "planned"
    assert len(factory.calls) == 1 and planned["assessments"]
    history = Path(planned["reparse_saved"]["history_dir"])
    for name in ("QUALITY_RESULT.json", "RESULT_SEAL.json", "QUALITY_BODY.md"):
        assert (history / name).read_bytes() == originals[name]
    completed = run_stage(tmp_path, factory, reparse_saved=True)
    assert completed["model_calls"] == 2
    assert [call[0] for call in factory.calls] == ["assessment", "completion", "post_assessment"]
    assert completed["body_markdown"].startswith("SYNTHETIC A [P0001].\n\n")
    for name in ("assessment/RAW_RESPONSE.json", "assessment/REQUEST.json"):
        assert (target / name).read_bytes() == originals[name]
    feedback = json.loads(factory.calls[1][1][-1]["content"])["gap_feedback"]
    assert not {"body_quote_spans", "body_quote_match", "body_quote_verified", "body_quote_error",
                "effective_status"}.intersection(feedback[0])
    assert quality.validate_assessment is validator


@pytest.mark.parametrize("step", ["assessment", "completion", "post_assessment"])
def test_explicit_reparse_does_not_rebuy_partial_stage(tmp_path, step):
    factory = Factory(partial_step=step)
    first = run_stage(tmp_path, factory)
    calls = len(factory.calls)
    replayed = run_stage(tmp_path, factory, reparse_saved=True)
    assert replayed["model_calls"] == 0 and len(factory.calls) == calls
    assert replayed["body_markdown"] == first["body_markdown"]
    assert replayed["pending_problems"]


def test_reparse_completed_request_retains_exact_completion_identity(tmp_path):
    factory = Factory()
    first = run_stage(tmp_path, factory)
    target = Path(first["output_dir"])
    request = (target / "completion/REQUEST.json").read_bytes()
    messages = (target / "completion/MESSAGES.json").read_bytes()
    feedback = json.loads(messages)[-1]["content"]
    assert json.loads(feedback)["gap_feedback"] == [{"task_id": "p2", "status": "missing", "body_quote": "",
        "explanation": "B has not been explained", "material_handles": ["P0001"]}]
    replayed = run_stage(tmp_path, factory, reparse_saved=True)
    assert replayed["model_calls"] == 0 and len(factory.calls) == 3
    assert replayed["body_markdown"] == first["body_markdown"]
    assert (target / "completion/REQUEST.json").read_bytes() == request
    assert (target / "completion/MESSAGES.json").read_bytes() == messages


def test_budget_refusal_keeps_request_and_cannot_be_rebought_on_reparse(tmp_path):
    calls = []
    def refused(role, path, profile):
        calls.append(role)
        raise ValueError("synthetic_budget_reservation_refused")
    first = run_stage(tmp_path, refused)
    target = Path(first["output_dir"])
    request = (target / "assessment/REQUEST.json").read_bytes()
    replayed = run_stage(tmp_path, refused, reparse_saved=True)
    assert first["model_calls"] == replayed["model_calls"] == 0 and len(calls) == 1
    assert (target / "assessment/REQUEST.json").read_bytes() == request
    assert any(row["code"] == "quality_assessment_pending_existing_attempt" for row in replayed["pending_problems"])


def test_invalid_post_assessment_changes_preserve_body_and_explicit_pending(tmp_path):
    factory = Factory()
    def with_post_changes(role, path, profile):
        base = factory(role, path, profile)
        def client(messages, **kwargs):
            returned = base(messages, **kwargs)
            if role == "post_assessment":
                data = json.loads(returned["content"])
                data["changes"] = [{"operation": "replace", "original_text": "SYNTHETIC B", "replacement_text": "Must not apply"}]
                returned["content"] = json.dumps(data)
            return returned
        client.prompt_token_counter = counter
        return client
    result = run_stage(tmp_path, with_post_changes)
    assert len(factory.calls) == 3 and "SYNTHETIC B" in result["body_markdown"]
    assert "Must not apply" not in result["body_markdown"]
    assert result["status"] == "assessed_with_pending"
    assert any(row["code"] == "quality_postcheck_edits_not_applied" for row in result["pending_problems"])
    edits = result["post_edits"]
    assert edits["status"] == "rejected" and not edits["applied"]
    assert Path(edits["before_path"]).read_bytes() == Path(edits["after_path"]).read_bytes()


def test_valid_post_edits_apply_once_free_reparse_preserves_paid_requests_and_diagnostics(tmp_path):
    factory = Factory()
    def with_post_edits(role, path, profile):
        base = factory(role, path, profile)
        def client(messages, **kwargs):
            returned = base(messages, **kwargs)
            if role == "post_assessment":
                data = json.loads(returned["content"])
                data["tasks"][1].update(status="partial", explanation="Offline residual gap remains")
                data["issues"] = ["OFFLINE scientific uncertainty remains"]
                data["changes"] = [{"operation": "replace", "original_text": anchor,
                    "replacement_text": anchor + " corrected", "reason": "Offline exact replacement control",
                    "material_handles": ["P0001"], "material_quote": "Synthetic material supports A and B only."}
                    for anchor in ("SYNTHETIC A", "SYNTHETIC B")]
                data["changes"].append({"operation": "replace", "original_text": "[P0001]",
                    "replacement_text": "Invalid ambiguity", "reason": "Offline rejection control",
                    "material_handles": ["P0001"], "material_quote": "Fabricated material quote"})
                returned["content"] = json.dumps(data)
            return returned
        client.prompt_token_counter = counter
        return client
    first = run_stage(tmp_path, with_post_edits)
    assert len(factory.calls) == 3 and first["scientific_acceptance"] is False
    edits = first["post_edits"]
    assert edits["status"] == "partially_applied_pending_human_review" and len(edits["applied"]) == 2
    assert len(edits["rejected"]) == 1 and edits["rejected"][0]["proposal_index"] == 2
    before = Path(edits["before_path"]).read_text(encoding="utf-8")
    assert first["body_markdown"] == before.replace("SYNTHETIC A", "SYNTHETIC A corrected").replace("SYNTHETIC B", "SYNTHETIC B corrected")
    assert Path(edits["after_path"]).read_text(encoding="utf-8") == first["body_markdown"]
    assert first["assessments"][-1]["report"]["tasks"][1]["status"] == "partial"
    assert any(row["code"] == "quality_task_partial" for row in first["pending_problems"])
    assert any(row["code"] == "quality_assessor_issue" for row in first["pending_problems"])
    assert edits["assessment_task_status_basis"] == "before_post_edits"
    target = Path(first["output_dir"])
    paid_files = {name: (target / name).read_bytes() for step in ("assessment", "completion", "post_assessment")
        for name in (step + "/RAW_RESPONSE.json", step + "/REQUEST.json", step + "/MESSAGES.json")}
    # Labeled fixture of the old sealed result before local post-edit consumption.
    old = {key: deepcopy(value) for key, value in first.items() if key != "post_edits"}
    old["body_markdown"] = before
    old["pending_problems"] = [row for row in old["pending_problems"]
        if row["code"] != "quality_postcheck_edits_applied_pending_review"]
    old["pending_problems"].append({"code": "quality_postcheck_edits_not_applied", "note": "Labeled old local parser control"})
    quality._write(target / "QUALITY_RESULT.json", old)
    (target / "QUALITY_BODY.md").write_bytes(before.encode("utf-8"))
    quality._write(target / "RESULT_SEAL.json", {"result_sha256": quality._hash(old),
        "body_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest()})
    prior = {name: (target / name).read_bytes() for name in ("QUALITY_RESULT.json", "RESULT_SEAL.json", "QUALITY_BODY.md")}
    ordinary = run_stage(tmp_path, with_post_edits)
    assert ordinary["body_markdown"] == before and ordinary["model_calls"] == 0
    recovered = run_stage(tmp_path, with_post_edits, run=False, reparse_saved=True)
    assert recovered["body_markdown"] == first["body_markdown"] and recovered["model_calls"] == 0
    assert len(factory.calls) == 3 and recovered["status"] == "assessed_with_pending"
    history = Path(recovered["reparse_saved"]["history_dir"])
    assert all((history / name).read_bytes() == value for name, value in prior.items())
    assert all((target / name).read_bytes() == value for name, value in paid_files.items())
    again = run_stage(tmp_path, with_post_edits, reparse_saved=True)
    assert again["body_markdown"] == recovered["body_markdown"] and again["model_calls"] == 0
    assert len(factory.calls) == 3


def test_post_conflict_group_all_rejected_and_independent_valid_edit_survives(tmp_path):
    view = route.build_unit_views(book())[0]
    body = "AAA BBB CCC. Independent DDD. Untouched suffix."
    changes = [{"operation": "replace", "original_text": anchor, "replacement_text": "Changed " + str(index),
        "reason": "Offline conflict control", "material_handles": ["P0001"],
        "material_quote": "Synthetic material supports A and B only."}
        for index, anchor in enumerate(("AAA BBB", "BBB CCC", "BBB", "DDD"))]
    output, edits = quality._consume_post_edits(tmp_path, body, changes, view)
    assert output == "AAA BBB CCC. Independent Changed 3. Untouched suffix."
    assert [row["proposal_index"] for row in edits["applied"]] == [3]
    assert [row["proposal_index"] for row in edits["rejected"]] == [0, 1, 2]
    assert all(row["error"] == "quality_edit_anchors_overlap" for row in edits["rejected"])
    assert all(row["conflicting_proposal_indices"] for row in edits["rejected"])
    assert Path(edits["before_path"]).read_text(encoding="utf-8") == body
    assert Path(edits["after_path"]).read_text(encoding="utf-8") == output


def test_only_verified_missing_table_structure_resolves_other_issues_remain():
    view = route.build_unit_views(book())[0]
    view.table_tasks = [{"table_id": "t1"}, {"table_id": "t2"}]
    original = [{"code": "markdown_table_missing_or_invalid"}, {"code": "table_markdown_missing_or_invalid"},
                {"problem": "Unresolved scientific attribution", "source_handles": ["P0001"]}]
    result = {"body_markdown": "|Combined task|Result|\n|---|---|\n|t1 and t2|SYNTHETIC evidence|",
              "assessments": [{"report": {"tasks": [{"task_id": "t1", "status": "covered"}, {"task_id": "t2", "status": "covered"}]}}]}
    retained, resolved = quality.retained_original_issues(view, original, result)
    assert retained == original[2:] and resolved == original[:2]
    result["assessments"][0]["report"]["tasks"][1]["status"] = "partial"
    assert quality.retained_original_issues(view, original, result) == (original, [])
    result["assessments"][0]["report"]["tasks"][1]["status"] = "covered"
    result["changed"] = True
    assert quality.retained_original_issues(view, original, result) == (original, [])
    result["assessments"][0]["step"] = "post_assessment"
    assert quality.retained_original_issues(view, original, result) == (original[2:], original[:2])
    # New unverified rows cannot clear structural issues; old strict reports
    # without the new flag remain compatible above.
    task = result["assessments"][0]["report"]["tasks"][1]
    task.update(body_quote_verified=False, effective_status="unverified")
    assert quality.retained_original_issues(view, original, result) == (original, [])
    task["body_quote_verified"] = True
    assert quality.retained_original_issues(view, original, result) == (original[2:], original[:2])
    result["body_markdown"] = "Claimed table without actual Markdown"
    assert quality.retained_original_issues(view, original, result) == (original, [])


def test_evidence_edit_unique_anchor_preserves_other_bytes():
    view = route.build_unit_views(book())[0]
    body = "prefix\r\nSYNTHETIC WRONG [P0001].\n\nuntouched suffix"
    edit = {"operation": "replace", "original_text": "SYNTHETIC WRONG", "replacement_text": "SYNTHETIC A",
            "reason": "Actual material supports only A and B", "material_handles": ["P0001"],
            "material_quote": "Synthetic material supports A and B only."}
    changed, applied = quality.apply_evidence_edits(body, [edit], view)
    assert changed == body.replace("SYNTHETIC WRONG", "SYNTHETIC A") and len(applied) == 1
    with pytest.raises(ValueError, match="anchor"):
        quality.apply_evidence_edits(body + " SYNTHETIC WRONG", [edit], view)
    overlap = {**edit, "original_text": "WRONG", "replacement_text": "A"}
    with pytest.raises(ValueError, match="overlap"):
        quality.apply_evidence_edits(body, [edit, overlap], view)


def test_multiline_material_quote_matches_actual_recursive_string():
    view = route.build_unit_views(book())[0]
    view.materials[0]["study_summary_A"]["detail"] = {"text": "First material line\nSecond material line"}
    edit = {"operation": "replace", "original_text": "Wrong", "replacement_text": "Correct",
            "reason": "Supplied lines support correction", "material_handles": ["P0001"],
            "material_quote": "First material line\nSecond material line"}
    changed, applied = quality.apply_evidence_edits("Untouched\r\nWrong\nSuffix", [edit], view)
    assert changed == "Untouched\r\nCorrect\nSuffix" and applied


def test_optional_gap_feedback_old_default_and_explicit_task_validation():
    view = route.build_unit_views(book())[0]
    payload = writer.build_completion_payload(view, "Original", ["p2"])
    assert "gap_feedback" not in payload
    feedback = [{"task_id": "p2", "status": "partial", "explanation": "Explain missing boundary", "body_quote": "Original", "material_handles": ["P0001"]}]
    messages = writer.completion_messages(view, "Original", ["p2"], gap_feedback=feedback)
    actual = json.loads(messages[-1]["content"])
    assert actual["gap_feedback"] == feedback
    assert actual["paragraph_tasks"] == payload["paragraph_tasks"] and actual["sources"] == payload["sources"]
    with pytest.raises(writer.UnitWritingError, match="gap_feedback_task_invalid"):
        writer.build_completion_payload(view, "Original", ["p2"], gap_feedback=[{"task_id": "p1"}])


def test_completion_full_recursive_review_evidence_and_covered_ids():
    data = book()
    data["chapters"][0]["sources"][0]["review_planning_B"] = {"review_source_handle": "P0002"}
    data["chapters"][0]["sources"].append({"source_handle": "P0002", "title": "Review-only source", "review_content": "complete review " * 5000})
    view = route.build_unit_views(data)[0]
    payload = writer.build_completion_payload(view, "original", ["p2"])
    assert [item["source_handle"] for item in payload["sources"]] == ["P0001", "P0002"]
    assert payload["sources"][1]["review_content"] == "complete review " * 5000
    result = writer.run_unit_completion(view, existing_body="original", task_ids=["p2"],
        client=lambda m, **kw: response({"body_markdown": "Candidate", "status": "appended", "covered_task_ids": ["p1"]}),
        model="offline-synthetic")
    assert result["pending"] and result["body_markdown"] == "original"


def test_tool_same_identity_reuse_new_identity_conflict_and_unknown_retained():
    data = book()
    data["chapters"][0]["chapter_tool_materials"] = [{"unit_key": "CH:U0", "usable_content": "FULL MULTISOURCE ANSWER " * 500,
        "sources": [{"doi": "https://doi.org/10.1234/fixture-a", "source_handle": "P0099", "title": "Existing"},
                    {"doi": "10.1234/fixture-b", "source_handle": "P0001", "title": "New"},
                    {"source_handle": "P0998", "title": "Unidentified", "content": "Retained unknown content"}]}]
    before = deepcopy(data)
    registered = register_tool_source_identities(data)
    tool = registered["chapters"][0]["chapter_tool_materials"][0]
    assert tool["sources"][0]["source_handle"] == "P0001"
    assert tool["sources"][1]["source_handle"] == "P0002"
    assert tool["sources"][1]["conflicting_handle"] == "P0001"
    assert tool["sources"][2]["content"] == "Retained unknown content"
    assert "source_handle" not in tool["sources"][2]
    assert tool["source_identity_diagnostics"]
    assert data == before
    assert registered["chapters"][0]["units"][0]["paragraph_tasks"] == before["chapters"][0]["units"][0]["paragraph_tasks"]
    view = route.build_unit_views(data)[0]
    assert set(view.sources) == {"P0001", "P0002"}
    assert view.chapter_tool_materials[0]["usable_content"] == tool["usable_content"]
    assert "usable_content" not in view.sources["P0002"]
    assert register_tool_source_identities(registered) == registered


def test_tool_identity_allocation_does_not_rebind_uncatalogued_task_handle():
    data = book()
    task = data["chapters"][0]["units"][0]["paragraph_tasks"][1]
    task["source_uses"] = [{"source_handle": "P0002"}]
    data["chapters"][0]["chapter_tool_materials"] = [{"unit_key": "CH:U0", "usable_content": "Synthetic new identity evidence",
        "sources": [{"doi": "10.1234/fixture-new", "source_handle": "P0001", "title": "Different new source"}]}]
    registered = register_tool_source_identities(data)
    source = registered["chapters"][0]["chapter_tool_materials"][0]["sources"][0]
    assert source["source_handle"] == "P0003"
    assert registered["chapters"][0]["units"][0]["paragraph_tasks"][1] == task
    assert not any(row["source_handle"] == "P0002" for row in registered["chapters"][0]["sources"])
    with pytest.raises(ValueError, match="legacy_unit_source_missing:CH:U0:P0002"):
        route.build_unit_views(registered)


def test_integrated_quality_derivative_assembly_and_selection_resume(tmp_path):
    factory = Factory()
    factory.execution_mode = "live"  # Synthetic offline seam exercises actual assembler.
    data = book(units=2)
    first = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True, only_units=["CH:U0"])
    assert first["requested_unit_count"] == 1 and first["full_unit_count"] == 2
    assert first["units"][1]["status"] == "not_selected"
    original = Path(first["units"][0]["attempt_dir"])
    before = (original / "UNIT_RESULT.json").read_bytes(), (original / "UNIT_BODY.md").read_bytes()
    text = (tmp_path / "assembled" / "REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")
    assert "SYNTHETIC B" in text and "本章论断" not in text
    derivative = Path(first["units"][0]["quality"]["output_dir"]) / "assembly_derivative"
    saved_derivative = json.loads((derivative / "UNIT_RESULT.json").read_text(encoding="utf-8"))
    actual_diagnostics = writer._output_diagnostics(saved_derivative["body_markdown"], ["P0001"], [])
    for key in ("used_source_handles", "unknown_citations", "unresolved_numeric_citations", "citation_problems", "fence_report"):
        assert saved_derivative[key] == actual_diagnostics[key]
    second = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True)
    assert second["complete_units"] == 2 and second["model_calls"] == 4
    assert before == ((original / "UNIT_RESULT.json").read_bytes(), (original / "UNIT_BODY.md").read_bytes())
    third = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True)
    assert third["model_calls"] == 0


def test_route_reparse_preserves_old_sealed_derivative_and_selects_new_body(tmp_path, monkeypatch):
    factory = Factory()
    factory.execution_mode = "live"  # Offline synthetic assembler control only.
    with monkeypatch.context() as old_parser:
        old_parser.setattr(quality, "validate_assessment", lambda *a: (_ for _ in ()).throw(
            ValueError("quality_body_quote_invalid:old_parser")))
        first = route.run_legacy_units(book(), output_dir=tmp_path, run=True,
            client_factory=factory, token_counter=counter, quality_control=True)
    original = Path(first["units"][0]["attempt_dir"])
    quality_root = Path(first["units"][0]["quality"]["output_dir"])
    old_derivative = quality_root / "assembly_derivative"
    preserved = {path: path.read_bytes() for path in [original / "UNIT_RESULT.json", original / "UNIT_BODY.md",
                 old_derivative / "UNIT_RESULT.json", old_derivative / "RESULT_SEAL.json", old_derivative / "UNIT_BODY.md"]}
    replayed = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, quality_control=True, reparse_saved=True)
    assert replayed["model_calls"] == 2 and len(factory.calls) == 4
    for path, content in preserved.items():
        assert path.read_bytes() == content
    job = json.loads((tmp_path / "batch/BATCH_JOBS.json").read_text(encoding="utf-8"))[0]
    assert Path(job["output"]).parent == quality_root / "assembly_derivatives"
    assert "SYNTHETIC B" in (tmp_path / "assembled/REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")


def test_route_clears_repaired_table_code_and_keeps_scientific_issue(tmp_path):
    data = book()
    data["chapters"][0]["units"][0]["table_tasks"] = [{"table_id": "t1", "purpose": "Synthetic task table",
        "columns": ["Dimension", "Result"], "row_tasks": [{"content": "Synthetic row", "source_uses": [{"source_handle": "P0001"}]}]}]
    scientific = {"problem": "Synthetic unresolved scientific attribution", "source_handles": ["P0001"], "action": "chapter_owner"}
    calls = []
    def factory(role, path, profile):
        def client(messages, **kwargs):
            calls.append(role)
            payload = json.loads(messages[-1]["content"])
            if role == "writer":
                return response({"body_markdown": "SYNTHETIC A and SYNTHETIC B [P0001].", "issues": [scientific]})
            if role == "completion":
                assert payload["requested_task_ids"] == ["t1"]
                return response({"body_markdown": "|Dimension|Result|\n|---|---|\n|SYNTHETIC T|Supported [P0001]|",
                                 "status": "appended", "covered_task_ids": ["t1"], "issues": []})
            assessed = json.loads(assessment(payload["actual_body_markdown"])["content"])
            assessed["tasks"].append({"task_id": "t1", "status": "missing" if role == "assessment" else "covered",
                "body_quote": "" if role == "assessment" else "SYNTHETIC T",
                "explanation": "Synthetic table is absent" if role == "assessment" else "Synthetic table is present",
                "material_handles": ["P0001"]})
            return response(assessed)
        client.prompt_token_counter = counter
        return client
    factory.execution_mode = "live"
    result = route.run_legacy_units(data, output_dir=tmp_path, run=True, client_factory=factory,
                                     token_counter=counter, quality_control=True)
    assert calls == ["writer", "assessment", "completion", "post_assessment"]
    row = result["units"][0]
    original = json.loads((Path(row["attempt_dir"]) / "UNIT_RESULT.json").read_text(encoding="utf-8"))
    assert {"code": "markdown_table_missing_or_invalid"} in original["issues"]
    derivative = Path(row["quality"]["output_dir"]) / "assembly_derivative"
    final = json.loads((derivative / "UNIT_RESULT.json").read_text(encoding="utf-8"))
    assert final["table_check"]["valid"] is True
    assert final["issues"] == [scientific] and row["pending_problems"] == [scientific]
    assert final["resolved_original_issues"] == [{"code": "markdown_table_missing_or_invalid"}]
    assert result["status"] == "restricted_draft"


def test_explicit_real_saved_completion_recovery_only_offline_post_no_source_mutation(tmp_path):
    """Optional paid RAW clones; only missing post uses labeled synthetic output."""
    supplied = os.environ.get("OPTO_QUALITY_COMPLETION_FIXTURES")
    if not supplied:
        pytest.skip("Set OPTO_QUALITY_COMPLETION_FIXTURES to explicitly supplied read-only unit fixtures")
    root = Path(supplied)
    stages = sorted({path.parents[1] for path in root.glob("*/**/completion/RAW_RESPONSE.json")})
    assert stages
    for index, source in enumerate(stages):
        before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
        payload = json.loads(before[Path("ORIGINAL_PAYLOAD.json")].decode("utf-8"))
        identity = json.loads(before[Path("IDENTITY.json")].decode("utf-8"))
        body = before[Path("ORIGINAL_BODY.md")].decode("utf-8")
        completion_input = json.loads(json.loads(before[Path("completion/MESSAGES.json")].decode("utf-8"))[-1]["content"])
        view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
            focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"],
            unit_count=payload["unit_position"]["of"], sibling_units=payload.get("sibling_units", []),
            chapter_frame=payload.get("chapter_frame", {}), other_chapters=payload.get("other_chapters", []),
            paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
            materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
            chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
            owner_unit_context=payload.get("owner_unit_context", {}))
        output = tmp_path / f"explicit_saved_completion_offline_control_{index}"
        clone = output / source.name
        shutil.copytree(source, clone)
        paid_names = [name for name in before if name.parent.name in {"assessment", "completion"}
                      and name.name in {"RAW_RESPONSE.json", "REQUEST.json", "MESSAGES.json", "PROFILE.json"}]
        calls = []
        def offline_factory(role, path, profile):
            assert role == "post_assessment", "Saved completion/assessment must not be called again"
            def client(messages, **kwargs):
                calls.append(role)
                request = json.loads(messages[-1]["content"])
                return response({"tasks": [{"task_id": row["task_id"], "status": "partial",
                    "body_quote": request["actual_body_markdown"][:16], "material_handles": [],
                    "explanation": "OFFLINE SYNTHETIC continuation wiring only; no scientific acceptance"}
                    for row in request["task_catalog"]], "changes": [],
                    "issues": ["OFFLINE SYNTHETIC post-check diagnostic retained"]})
            client.prompt_token_counter = counter
            return client
        options = dict(payload=payload, existing_body=body, output_dir=output,
            client_factory=offline_factory, token_counter=counter, language=payload["language"],
            writer_profile=identity["author_profile"], experiment_label=identity["experiment_label"], reparse_saved=True)
        post_failed = (clone / "post_assessment/REQUEST.json").is_file()
        planned = quality.run_unit_quality(view, run=False, **options)
        assert planned["status"] == ("pending" if post_failed else "planned")
        assert planned["model_calls"] == 0 and not calls
        assert planned["identity"] == identity
        completion = planned["completion"]
        if completion["candidate_eligible"]:
            assert completion["pending"] and completion["covered_task_ids"] == []
            assert planned["body_markdown"] == completion_input["existing_body_markdown"]
            expected = Path(completion["candidate_body_path"]).read_bytes().decode("utf-8")
            assert planned["completion_candidate"]["status"] == "awaiting_post_assessment"
        else:
            assert not completion["pending"] and completion["model_status"] == "appended"
            assert completion["parsed_via"] == "json_repair_quote_escape_crosschecked"
            expected = planned["body_markdown"]
        completed = quality.run_unit_quality(view, run=True, retry_failed=post_failed, **options)
        assert calls == ["post_assessment"] and completed["model_calls"] == 1
        assert completed["body_markdown"] == expected
        assert completed["body_markdown"].startswith(completion_input["existing_body_markdown"] + "\n\n")
        assert completed["scientific_acceptance"] is False and completed["status"] == "assessed_with_pending"
        assert all(row["status"] == "partial" for row in completed["assessments"][-1]["report"]["tasks"])
        assert all((clone / name).read_bytes() == before[name] for name in paid_names)
        assert completed["reparse_saved"]["history_dir"]
        resumed = quality.run_unit_quality(view, run=True, **options)
        assert resumed["model_calls"] == 0 and calls == ["post_assessment"]
        assert resumed["body_markdown"] == expected
        assert all(path.read_bytes() == before[path.relative_to(source)] for path in source.rglob("*") if path.is_file())


def test_explicit_real_failed_quality_attempt_only_retries_with_flag_offline(tmp_path):
    supplied = os.environ.get("OPTO_QUALITY_FAILED_FIXTURE")
    if not supplied:
        pytest.skip("Set OPTO_QUALITY_FAILED_FIXTURE to an explicit read-only saved failed stage")
    source = Path(supplied)
    originals = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    payload = json.loads(originals[Path("ORIGINAL_PAYLOAD.json")].decode("utf-8"))
    identity = json.loads(originals[Path("IDENTITY.json")].decode("utf-8"))
    body = originals[Path("ORIGINAL_BODY.md")].decode("utf-8")
    view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
        focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"], unit_count=payload["unit_position"]["of"],
        sibling_units=payload.get("sibling_units", []), chapter_frame=payload.get("chapter_frame", {}),
        other_chapters=payload.get("other_chapters", []), paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
        materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
        chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
        owner_unit_context=payload.get("owner_unit_context", {}))
    output = tmp_path / "explicit_failed_quality_offline_control"
    clone = output / source.name
    shutil.copytree(source, clone)
    calls = []
    def offline_factory(role, path, profile):
        assert role == "assessment" and path == clone / "assessment/retries/attempt_002"
        assert profile == identity["reviewer_profile"]
        def client(messages, **kwargs):
            calls.append(kwargs["call_id"])
            request = json.loads(messages[-1]["content"])
            return response({"tasks": [{"task_id": row["task_id"], "status": "material_limited",
                "body_quote": "", "explanation": "OFFLINE SYNTHETIC retry wiring only; not a scientific assessment",
                "material_handles": []} for row in request["task_catalog"]], "changes": [], "issues": []})
        client.prompt_token_counter = counter
        return client
    options = dict(payload=payload, existing_body=body, output_dir=output, client_factory=offline_factory,
        token_counter=counter, language=payload["language"], writer_profile=identity["author_profile"],
        experiment_label=identity["experiment_label"], reparse_saved=True)
    ordinary = quality.run_unit_quality(view, run=True, **options)
    assert ordinary["model_calls"] == 0 and not calls
    free = quality.run_unit_quality(view, run=False, retry_failed=True, **options)
    assert free["model_calls"] == 0 and not calls and not (clone / "assessment/retries").exists()
    result = quality.run_unit_quality(view, run=True, retry_failed=True, **options)
    assert result["model_calls"] == 1 and len(calls) == 1 and calls[0].endswith("retry-attempt_002")
    assert result["identity"] == identity and result["body_markdown"] == body
    assert result["scientific_acceptance"] is False and result["status"] == "assessed_with_pending"
    for name in originals:
        if name.parts[0] == "assessment":
            assert (clone / name).read_bytes() == originals[name]
    resumed = quality.run_unit_quality(view, run=True, retry_failed=True, **options)
    assert resumed["model_calls"] == 0 and len(calls) == 1
    assert all(path.read_bytes() == originals[path.relative_to(source)] for path in source.rglob("*") if path.is_file())


def test_explicit_real_saved_response_free_reparse_then_offline_missing_steps(tmp_path):
    """Optional read-only paid RAW fixture; all continuation calls are synthetic."""
    supplied = os.environ.get("OPTO_QUALITY_REPLAY_FIXTURE")
    if not supplied:
        pytest.skip("Set OPTO_QUALITY_REPLAY_FIXTURE to a labeled read-only saved quality stage")
    source = Path(supplied)
    names = ("IDENTITY.json", "ORIGINAL_PAYLOAD.json", "ORIGINAL_BODY.md", "QUALITY_BODY.md",
             "QUALITY_RESULT.json", "RESULT_SEAL.json", "assessment/RAW_RESPONSE.json",
             "assessment/REQUEST.json", "assessment/MESSAGES.json", "assessment/PROFILE.json")
    original_files = {name: (source / name).read_bytes() for name in names}
    identity = json.loads(original_files["IDENTITY.json"].decode("utf-8"))
    payload = json.loads(original_files["ORIGINAL_PAYLOAD.json"].decode("utf-8"))
    body = original_files["ORIGINAL_BODY.md"].decode("utf-8")
    view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
        focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"],
        unit_count=payload["unit_position"]["of"], sibling_units=payload.get("sibling_units", []),
        chapter_frame=payload.get("chapter_frame", {}), other_chapters=payload.get("other_chapters", []),
        paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
        materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
        chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
        owner_unit_context=payload.get("owner_unit_context", {}))
    output = tmp_path / "explicit_real_raw_offline_replay_only"
    clone = output / source.name
    for name, content in original_files.items():
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    roles = []
    def offline_factory(role, path, profile):
        def client(messages, **kwargs):
            roles.append(role)
            request = json.loads(messages[-1]["content"])
            if role == "completion":
                return response({"body_markdown": "OFFLINE SYNTHETIC REPLAY CONTROL ONLY: completion marker.",
                    "status": "appended", "covered_task_ids": request["requested_task_ids"], "issues": []})
            assert role == "post_assessment"
            return response({"tasks": [{"task_id": row["task_id"], "status": "covered",
                "body_quote": "OFFLINE SYNTHETIC REPLAY CONTROL ONLY", "explanation": "Offline wiring control only",
                "material_handles": []} for row in request["task_catalog"]], "changes": [], "issues": []})
        client.prompt_token_counter = counter
        return client
    options = dict(payload=payload, existing_body=body, output_dir=output,
        client_factory=offline_factory, token_counter=counter, language=payload["language"],
        writer_profile=identity["author_profile"], experiment_label=identity["experiment_label"], reparse_saved=True)
    planned = quality.run_unit_quality(view, run=False, **options)
    assert planned["status"] == "planned" and planned["model_calls"] == 0 and not roles
    assert Path(planned["output_dir"]) == clone
    assert planned["identity"] == identity
    assert any(row["body_quote_match"] == "ordered_omission_excerpt"
               for row in planned["assessments"][0]["report"]["tasks"])
    completed = quality.run_unit_quality(view, run=True, **options)
    assert roles == ["completion", "post_assessment"] and completed["model_calls"] == 2
    assert completed["body_markdown"].startswith(body) and completed["scientific_acceptance"] is False
    assert (clone / "assessment/RAW_RESPONSE.json").read_bytes() == original_files["assessment/RAW_RESPONSE.json"]
    assert (clone / "assessment/REQUEST.json").read_bytes() == original_files["assessment/REQUEST.json"]
    for name, content in original_files.items():
        assert (source / name).read_bytes() == content


def test_display_heading_precedence_and_plain_body_preserved():
    from scripts.upgrade3 import full_review_draft as draft
    assert draft.author_unit_title("## Author title\n\nPlain [P0001].") == "Author title"
    assert draft.clean_unit_body("## Author title\n\nPlain [P0001].\n\n### Internal heading\nText") == "Plain [P0001].\n\n##### Internal heading\nText"
    assert draft.clean_unit_body("Plain [P0001].") == "Plain [P0001]."
    assert draft.author_unit_title("## Table 1. Evidence\n|A|B|") == ""


@pytest.mark.parametrize("fixture_env, category", [
    ("OPTO_QUALITY_WHITESPACE_FIXTURE", "whitespace"),
    ("OPTO_QUALITY_JSON_QUOTE_FIXTURE", "json_quote_and_material_excerpts"),
])
def test_explicit_real_quote_recovery_free_then_offline_continuation(tmp_path, fixture_env, category):
    """Read-only real RAW fixtures; subsequent responses are labeled controls."""
    supplied = os.environ.get(fixture_env)
    if not supplied:
        pytest.skip("Set " + fixture_env + " to a labeled saved quality stage")
    source = Path(supplied)
    names = ("IDENTITY.json", "ORIGINAL_PAYLOAD.json", "ORIGINAL_BODY.md", "QUALITY_BODY.md",
             "QUALITY_RESULT.json", "RESULT_SEAL.json", "assessment/RAW_RESPONSE.json",
             "assessment/REQUEST.json", "assessment/MESSAGES.json", "assessment/PROFILE.json")
    originals = {name: (source / name).read_bytes() for name in names}
    identity = json.loads(originals["IDENTITY.json"])
    payload = json.loads(originals["ORIGINAL_PAYLOAD.json"])
    body = originals["ORIGINAL_BODY.md"].decode("utf-8")
    view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
        focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"],
        unit_count=payload["unit_position"]["of"], sibling_units=payload.get("sibling_units", []),
        chapter_frame=payload.get("chapter_frame", {}), other_chapters=payload.get("other_chapters", []),
        paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
        materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
        chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
        owner_unit_context=payload.get("owner_unit_context", {}))
    clone_root = tmp_path / "explicit_paid_raw_offline_control"
    clone = clone_root / source.name
    for name, content in originals.items():
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    calls = []
    def offline_factory(role, path, profile):
        def client(messages, **kwargs):
            calls.append((role, deepcopy(messages)))
            request = json.loads(messages[-1]["content"])
            if role == "completion":
                return response({"body_markdown": "OFFLINE SYNTHETIC QUOTE REPLAY CONTROL ONLY.",
                    "status": "appended", "covered_task_ids": request["requested_task_ids"], "issues": []})
            assert role == "post_assessment"
            return response({"tasks": [{"task_id": row["task_id"], "status": "covered",
                "body_quote": "OFFLINE SYNTHETIC QUOTE REPLAY CONTROL ONLY.",
                "explanation": "Offline parser wiring control only", "material_handles": []}
                for row in request["task_catalog"]], "changes": [], "issues": []})
        client.prompt_token_counter = counter
        return client
    options = dict(payload=payload, existing_body=body, output_dir=clone_root,
        client_factory=offline_factory, token_counter=counter, language=payload["language"],
        writer_profile=identity["author_profile"], experiment_label=identity["experiment_label"], reparse_saved=True)
    planned = quality.run_unit_quality(view, run=False, **options)
    assert planned["status"] in {"planned", "assessed_pending_human_review"} and planned["model_calls"] == 0 and not calls
    assert planned["identity"] == identity and Path(planned["output_dir"]) == clone
    report = planned["assessments"][0]["report"]
    if category == "whitespace":
        assert any("whitespace_normalized" in row["body_quote_match"] for row in report["tasks"])
    else:
        assert report["parsed_via"] == "json_repair_quote_escape_crosschecked"
        assert len(planned["applied_edits"]) == 1
        evidence = planned["applied_edits"][0]["material_quote_evidence"]
        assert evidence["match"] == "ordered_omission_excerpt_whitespace_normalized"
        assert len(evidence["spans"]) == 2 and evidence["spans"][0]["path"] != evidence["spans"][1]["path"]
    completed = quality.run_unit_quality(view, run=True, **options)
    expected_roles = ["completion", "post_assessment"] if category != "whitespace" else []
    assert [row[0] for row in calls] == expected_roles and completed["model_calls"] == len(expected_roles)
    assert completed["body_markdown"].startswith(planned["body_markdown"])
    if calls:
        feedback = json.loads(calls[0][1][-1]["content"])["gap_feedback"]
        expected_feedback = [{key: value for key, value in row.items()
            if key not in {"body_quote_spans", "body_quote_match", "body_quote_verified", "body_quote_error", "effective_status"}}
            for row in report["tasks"] if quality._effective_task_status(row) in {"partial", "missing"}]
        assert feedback == expected_feedback
    completion_request = (clone / "completion/REQUEST.json").read_bytes() if calls else None
    again = quality.run_unit_quality(view, run=True, **options)
    assert again["model_calls"] == 0 and len(calls) == len(expected_roles)
    if completion_request:
        assert (clone / "completion/REQUEST.json").read_bytes() == completion_request
    assert (clone / "assessment/RAW_RESPONSE.json").read_bytes() == originals["assessment/RAW_RESPONSE.json"]
    assert (clone / "assessment/REQUEST.json").read_bytes() == originals["assessment/REQUEST.json"]
    assert all((source / name).read_bytes() == value for name, value in originals.items())


def test_cli_matching_flags():
    from scripts.upgrade3.legacy_unit_writer import parser
    args = parser().parse_args(["--quality-control", "--reparse-saved", "--only-unit", "CH:U0", "--only-unit", "CH:U1"])
    assert args.quality_control and args.reparse_saved and args.only_unit == ["CH:U0", "CH:U1"]


def test_explicit_real_paid_post_edits_free_reparse_no_calls_or_source_mutation(tmp_path):
    supplied = os.environ.get("OPTO_QUALITY_POST_EDIT_FIXTURE")
    if not supplied:
        pytest.skip("Set OPTO_QUALITY_POST_EDIT_FIXTURE to a labeled read-only completed quality stage")
    source = Path(supplied)
    originals = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    payload = json.loads(originals[Path("ORIGINAL_PAYLOAD.json")])
    identity = json.loads(originals[Path("IDENTITY.json")])
    body = originals[Path("ORIGINAL_BODY.md")].decode("utf-8")
    view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
        focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"],
        unit_count=payload["unit_position"]["of"], sibling_units=payload.get("sibling_units", []),
        chapter_frame=payload.get("chapter_frame", {}), other_chapters=payload.get("other_chapters", []),
        paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
        materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
        chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
        owner_unit_context=payload.get("owner_unit_context", {}))
    clone_root = tmp_path / "explicit_paid_post_replay_control"
    clone = clone_root / source.name
    shutil.copytree(source, clone)
    def no_provider(*args, **kwargs):
        pytest.fail("Free paid-post replay attempted a provider call")
    options = dict(payload=payload, existing_body=body, output_dir=clone_root,
        client_factory=no_provider, token_counter=counter, language=payload["language"],
        writer_profile=identity["author_profile"], experiment_label=identity["experiment_label"], reparse_saved=True)
    result = quality.run_unit_quality(view, run=False, **options)
    assert result["model_calls"] == 0 and result["identity"] == identity
    edits = result["post_edits"]
    assert edits["status"] == "partially_applied_pending_human_review"
    assert len(edits["applied"]) == 1 and len(edits["rejected"]) == 1
    assert result["scientific_acceptance"] is False and result["status"] == "assessed_with_pending"
    before = Path(edits["before_path"]).read_text(encoding="utf-8")
    post_messages = json.loads(originals[Path("post_assessment/MESSAGES.json")])
    assert before == json.loads(post_messages[-1]["content"])["actual_body_markdown"]
    proposals = result["assessments"][-1]["report"]["changes"]
    applied_indices = {row["proposal_index"] for row in edits["applied"]}
    changes = [row for index, row in enumerate(proposals) if index in applied_indices]
    expected = before
    for change in sorted(changes, key=lambda row: before.index(row["original_text"]), reverse=True):
        start = before.index(change["original_text"])
        expected = expected[:start] + change["replacement_text"] + expected[start + len(change["original_text"]):]
    assert result["body_markdown"] == expected == Path(edits["after_path"]).read_text(encoding="utf-8")
    assert edits["assessment_task_status_basis"] == "before_post_edits"
    assert any(row["code"] == "quality_task_partial" for row in result["pending_problems"])
    history = Path(result["reparse_saved"]["history_dir"])
    assert (history / "QUALITY_BODY.md").read_bytes() == originals[Path("QUALITY_BODY.md")]
    again = quality.run_unit_quality(view, run=True, **options)
    assert again["model_calls"] == 0 and again["body_markdown"] == expected
    for path, content in originals.items():
        assert (source / path).read_bytes() == content
        if path.name in {"REQUEST.json", "RAW_RESPONSE.json", "MESSAGES.json"}:
            assert (clone / path).read_bytes() == content


@pytest.mark.parametrize("label, valid_count, edit_count", [("new_ch2", 2, 2), ("old_ch3", 3, 0)])
def test_explicit_frozen_paid_post_partial_quote_diagnostics_free_replay(tmp_path, label, valid_count, edit_count):
    """Frozen paid RAW only: no provider or scientific answer is supplied."""
    supplied = os.environ.get("OPTO_POST_QUOTE_PARTIAL_FIXTURES")
    if not supplied:
        pytest.skip("Set OPTO_POST_QUOTE_PARTIAL_FIXTURES to the explicitly frozen offline copies")
    candidates = list((Path(supplied) / label / "quality").glob("*/ORIGINAL_PAYLOAD.json"))
    assert len(candidates) == 1
    source = candidates[0].parent
    originals = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    payload = json.loads(originals[Path("ORIGINAL_PAYLOAD.json")])
    identity = json.loads(originals[Path("IDENTITY.json")])
    body = originals[Path("ORIGINAL_BODY.md")].decode("utf-8")
    view = writer.UnitWritingView(chapter_id=payload["chapter_id"], unit_id=payload["unit_id"],
        focus=payload["unit_focus"], unit_index=payload["unit_position"]["index"],
        unit_count=payload["unit_position"]["of"], sibling_units=payload.get("sibling_units", []),
        chapter_frame=payload.get("chapter_frame", {}), other_chapters=payload.get("other_chapters", []),
        paragraph_tasks=payload["paragraph_tasks"], table_tasks=payload["table_tasks"],
        materials=payload["sources"], sources={row["source_handle"]: row for row in payload["sources"]},
        chapter_tool_materials=payload.get("chapter_tool_materials", []), unit_notes=payload.get("unit_notes", ""),
        owner_unit_context=payload.get("owner_unit_context", {}))
    post = [step for step in quality._stage_attempts(source / "post_assessment")
            if (step / "RAW_RESPONSE.json").is_file()][-1]
    post_relative = post.relative_to(source)
    raw = json.loads(originals[post_relative / "RAW_RESPONSE.json"])
    original_post, _ = quality._assessment_content(raw["content"], raw.get("complete"))
    post_messages = json.loads(originals[post_relative / "MESSAGES.json"])
    assessed_body = json.loads(post_messages[-1]["content"])["actual_body_markdown"]
    clone_root = tmp_path / label
    clone = clone_root / source.name
    shutil.copytree(source, clone)
    def no_provider(*args, **kwargs):
        pytest.fail("Paid partial-quote replay must use saved RAW only")
    options = dict(payload=payload, existing_body=body, output_dir=clone_root,
        client_factory=no_provider, token_counter=counter, language=payload["language"],
        writer_profile=identity["author_profile"], experiment_label=identity["experiment_label"], reparse_saved=True)
    result = quality.run_unit_quality(view, run=False, **options)
    assert result["model_calls"] == 0 and result["identity"] == identity
    assert result["status"] == "assessed_with_pending" and not result["scientific_acceptance"]
    report = result["assessments"][-1]["report"]
    assert result["assessments"][-1]["step"] == "post_assessment"
    assert sum(row["body_quote_verified"] for row in report["tasks"]) == valid_count
    unverified = [row for row in report["tasks"] if row["effective_status"] == "unverified"]
    assert len(unverified) == 1 and not unverified[0]["body_quote_spans"]
    assert unverified[0]["body_quote_error"] == "quality_body_quote_invalid:" + unverified[0]["task_id"]
    for parsed, original in zip(report["tasks"], original_post["tasks"]):
        assert {key: parsed[key] for key in original} == original
    assert any(row["code"] == "quality_task_unverified" for row in result["pending_problems"])
    assert not any(row["code"] == "quality_stage_failed" for row in result["pending_problems"])
    if edit_count:
        assert len(result["post_edits"]["applied"]) == edit_count and not result["post_edits"]["rejected"]
        assert Path(result["post_edits"]["before_path"]).read_bytes().decode("utf-8") == assessed_body
        expected, _ = quality.apply_evidence_edits(assessed_body, original_post["changes"], view)
        assert result["body_markdown"] == expected
    else:
        assert result["body_markdown"] == assessed_body and not result.get("post_edits")
    history = Path(result["reparse_saved"]["history_dir"])
    assert (history / "QUALITY_RESULT.json").read_bytes() == originals[Path("QUALITY_RESULT.json")]
    assert (history / "RESULT_SEAL.json").read_bytes() == originals[Path("RESULT_SEAL.json")]
    assert (history / "QUALITY_BODY.md").read_bytes() == originals[Path("QUALITY_BODY.md")]
    again = quality.run_unit_quality(view, run=True, **options)
    assert again["model_calls"] == 0 and again["body_markdown"] == result["body_markdown"]
    for path, content in originals.items():
        assert (source / path).read_bytes() == content
        if path.name in {"REQUEST.json", "RAW_RESPONSE.json", "MESSAGES.json", "PROFILE.json"}:
            assert (clone / path).read_bytes() == content
