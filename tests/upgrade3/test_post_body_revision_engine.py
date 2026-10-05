from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.post_body_revision import run_revision
from optomind_research.runtime.upgrade3.post_body_revision_contracts import RevisionContractError


@pytest.fixture
def case():
    return {"case_id": "test", "research_question": "What is supported?", "scope": "One tested setting",
            "outline": "Explain the result with its limits", "draft_text": "# Result\r\n\r\nTreatment always increased X [P0001].\r\n\r\nOther text stays.\r\n",
            "materials": {"M1": {"title": "Trial", "summary": "Bounded trial", "text": "Treatment increased X in one tested setting. Other settings were not evaluated.", "source_handles": ["P0001"]}}}


@pytest.fixture
def issue():
    return {"issue_id": "scope", "kind": "scientific", "target_block_id": "B0002",
            "original_text": "Treatment always increased X [P0001].", "problem": "Universal scope is unsupported",
            "evidence_ids": ["M1"], "preserve": ["increased X"], "operation": "replace", "priority": "high"}


def config(variant="B", **extra):
    return {"variant": variant, "models": {"review": "reviewer", "author": "author", "verifier": "judge", "escalation": "stronger"}, **extra}


def proposal(issue):
    return {"issue_id": issue["issue_id"], "operation": "replace", "replacement_text": "Treatment increased X in one tested setting [P0001].", "reason": "Limit scope", "evidence_ids": ["M1"]}


def verdict(kind="accept", **extra):
    return {"verdict": kind, "preservation_ok": True, "problem_improved": kind == "accept", "reason": "Supported scope", "evidence_ids": ["M1"], **extra}


class Client:
    execution_mode = "recording"

    def __init__(self, responses):
        self.responses = responses
        self.seen = []

    def __call__(self, stage, messages, *, model):
        self.seen.append((stage, messages, model))
        value = self.responses[stage]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def responses(issue, variant="B", judge=None):
    result = {"review": {"issues": [issue]}, "issue_001_author": proposal(issue), "issue_001_verifier": judge or verdict()}
    if variant == "A":
        result = {"combined": {"issues": [issue], "proposals": [proposal(issue)]}, "issue_001_verifier": judge or verdict()}
    return result


@pytest.mark.parametrize("variant,expected", [("A", 2), ("B", 3), ("C", 3)])
def test_all_variants_apply_independently(case, issue, tmp_path, variant, expected):
    client = Client(responses(issue, variant))
    report = run_revision(case, config(variant), tmp_path / variant, client)
    assert report["run_completed"] and report["counts"]["applied"] == 1
    assert report["calls"] == expected
    assert report["execution_mode"] == "recording"
    assert Path(report["baseline_path"]).read_bytes() == case["draft_text"].encode()
    candidate = Path(report["candidate_path"]).read_bytes()
    assert candidate == case["draft_text"].replace(issue["original_text"], proposal(issue)["replacement_text"]).encode()
    assert report["cost_cny"] is None
    first_payload = json.loads(client.seen[0][1][1]["content"])
    assert "text" not in first_payload["material_index"][0]
    assert "actual_snapshot_blocks" in first_payload


def test_c_exactly_one_escalation_with_stronger_model(case, issue, tmp_path):
    data = responses(issue, judge=verdict("reject"))
    data.update(issue_001_escalation_author=proposal(issue), issue_001_escalation_verifier=verdict("reject"))
    client = Client(data)
    report = run_revision(case, config("C"), tmp_path, client)
    assert len(client.seen) == 5
    assert [c[2] for c in client.seen[-2:]] == ["stronger", "stronger"]
    assert report["counts"]["pending"] == 1 and not report["applied_patches"]
    assert Path(report["candidate_path"]).read_bytes() == case["draft_text"].encode()


@pytest.mark.parametrize("failure", ["insufficient_evidence", "issue_not_supported"])
def test_c_no_escalation_for_evidence_or_dismissal(case, issue, tmp_path, failure):
    client = Client(responses(issue, judge=verdict(failure)))
    report = run_revision(case, config("C"), tmp_path, client)
    assert len(client.seen) == 3
    assert report["issue_records"][0]["dismissed"] == (failure == "issue_not_supported")


def test_wrong_review_dismissed_without_forced_edit(case, issue, tmp_path):
    data = responses(issue, judge=verdict("issue_not_supported"))
    data["issue_001_author"] = {"issue_id": "scope", "no_change": True, "reason": "Review concern is wrong"}
    report = run_revision(case, config(), tmp_path, Client(data))
    assert report["counts"]["dismissed"] == 1
    assert not report["issue_records"][0]["proposed"]
    assert Path(report["candidate_path"]).read_bytes() == case["draft_text"].encode()


@pytest.mark.parametrize("change", ["no_source", "unknown_source", "bad_anchor", "empty_material"])
def test_missing_or_invalid_evidence_is_pending_without_author_calls(case, issue, tmp_path, change):
    if change == "no_source": issue["evidence_ids"] = []
    if change == "unknown_source": issue["evidence_ids"] = ["UNKNOWN"]
    if change == "bad_anchor": issue["original_text"] = "absent"
    if change == "empty_material": case["materials"]["M1"]["text"] = ""
    client = Client(responses(issue))
    report = run_revision(case, config("C"), tmp_path, client)
    assert len(client.seen) == 1
    assert report["run_completed"] and report["counts"]["pending"] == 1
    assert not report["model_assessed_issues_resolved"]


def test_exact_resume_zero_calls_changed_contract_refused(case, issue, tmp_path):
    client = Client(responses(issue))
    report = run_revision(case, config(), tmp_path, client)
    resumed = run_revision(case, config(), tmp_path, Client({}), resume=True)
    assert resumed == report
    changed = copy.deepcopy(case); changed["draft_text"] += "\nNew"
    with pytest.raises(RevisionContractError, match="contract_changed"):
        run_revision(changed, config(), tmp_path, Client({}), resume=True)
    with pytest.raises(RevisionContractError, match="contract_changed"):
        run_revision(case, config(max_issues=2), tmp_path, Client({}), resume=True)
    Path(report["candidate_path"]).write_text("tampered")
    with pytest.raises(RevisionContractError, match="artifact_changed"):
        run_revision(case, config(), tmp_path, Client({}), resume=True)


def test_partial_resume_uses_durable_calls(case, issue, tmp_path):
    first = Client(responses(issue))
    report = run_revision(case, config(), tmp_path, first)
    (tmp_path / "report.json").unlink()
    second = Client({})
    resumed = run_revision(case, config(), tmp_path, second, resume=True)
    assert not second.seen
    assert resumed == report


@pytest.mark.parametrize("raw", ["", {"content": "{}", "finish_reason": "length"}, {"complete": False, "issues": []}, {"choices": []}, ValueError("offline")])
def test_failed_review_never_claims_complete_and_durable_no_retry(case, tmp_path, raw):
    client = Client({"review": raw})
    report = run_revision(case, config(), tmp_path, client)
    assert report["status"] == "failed" and not report["run_completed"]
    assert not report["model_assessed_issues_resolved"]
    assert (tmp_path / "calls" / "review.json").exists()
    (tmp_path / "report.json").unlink()
    assert run_revision(case, config(), tmp_path, Client({}), resume=True)["status"] == "failed"


def test_oversized_input_rejected_not_truncated(case, tmp_path):
    client = Client({})
    report = run_revision(case, config(max_input_chars=10), tmp_path, client)
    assert not client.seen and report["status"] == "failed"
    assert "max_input_chars_exceeded" in report["errors"][0]
    saved = json.loads((tmp_path / "calls" / "review.json").read_text())
    assert case["draft_text"].split("\r\n\r\n")[1] in saved["messages"][1]["content"]


@pytest.mark.parametrize("bad", [{"preservation_ok": False}, {"problem_improved": "true"}, {"evidence_ids": []}, {"evidence_ids": ["ALIEN"]}])
def test_accept_requires_true_booleans_and_actual_evidence(case, issue, tmp_path, bad):
    report = run_revision(case, config(), tmp_path, Client(responses(issue, judge=verdict(**bad))))
    assert not report["applied_patches"] and report["counts"]["pending"] == 1


def test_budget_is_cap_not_quota_and_fixed_issues_bypass_review(case, issue, tmp_path):
    empty = run_revision(case, config(max_issues=6), tmp_path / "empty", Client({"review": {"issues": []}}))
    assert empty["verification_status"] == "no_issues_reported"
    assert empty["calls"] == 1 and not empty["model_assessed_issues_resolved"]
    client = Client(responses(issue))
    report = run_revision(case, config(known_fixed_issues=[issue]), tmp_path / "fixed", client)
    assert report["calls"] == 2 and all(x[0] != "review" for x in client.seen)


@pytest.mark.parametrize("content,finish_reason", [("", "stop"), ("not JSON", "stop"), ("{\"issues\": []}", "length")])
def test_failed_response_retains_reported_usage_and_explicit_cost(case, tmp_path, content, finish_reason):
    raw = {"content": content, "finish_reason": finish_reason, "usage": {"prompt_tokens": 123, "completion_tokens": 7}, "cost_cny": 0.125, "cost_provenance": "configured_provider_pricing"}
    report = run_revision(case, config(), tmp_path, Client({"review": raw}))
    assert report["usage"]["prompt_tokens"] == 123
    assert report["cost_cny"] == 0.125 and report["cost_complete"]


def test_unknown_proposal_source_fails_closed(case, issue, tmp_path):
    data = responses(issue)
    data["issue_001_author"]["evidence_ids"] = ["ALIEN"]
    client = Client(data)
    report = run_revision(case, config("C"), tmp_path, client)
    assert len(client.seen) == 2 and report["counts"]["pending"] == 1
    assert "proposal_unknown_evidence_id" in report["issue_records"][0]["reason"]
    assert not report["model_assessed_issues_resolved"]


def test_malformed_review_cannot_claim_resolved(case, tmp_path):
    report = run_revision(case, config(), tmp_path, Client({"review": {"issues": [{"issue_id": "bad", "problem": "serious error"}]}}))
    assert report["counts"]["pending"] == 1 and not report["model_assessed_issues_resolved"]


def test_preflight_exact_sizes_and_target_scope_limit(case, issue, tmp_path):
    from optomind_research.runtime.upgrade3.post_body_revision import preflight_revision
    preflight = preflight_revision(case, config())
    assert preflight["review_input_chars"] > preflight["material_index_chars"] > 0
    with pytest.raises(RevisionContractError, match="max_input_chars_exceeded"):
        preflight_revision(case, config(max_input_chars=10))
    client = Client(responses(issue))
    report = run_revision(case, config(max_target_chars=10), tmp_path, client)
    assert len(client.seen) == 1 and report["counts"]["pending"] == 1
    assert "max_target_chars_exceeded" in report["issue_records"][0]["reason"]


def test_interrupted_dispatch_keeps_cost_unknown(case, issue, tmp_path):
    class CrashClient(Client):
        def __call__(self, stage, messages, *, model):
            if stage == "issue_001_author":
                raise KeyboardInterrupt("simulated process death")
            return {"response": {"issues": [issue]}, "cost_cny": 0.1, "cost_provenance": "explicit_test_receipt"}
    with pytest.raises(KeyboardInterrupt):
        run_revision(case, config(), tmp_path, CrashClient({}))
    durable = json.loads((tmp_path / "calls" / "issue_001_author.json").read_text())
    assert durable["dispatched"] is True
    client = Client({})
    report = run_revision(case, config(), tmp_path, client, resume=True)
    assert not client.seen and not report["cost_complete"] and report["cost_cny"] is None
    assert report["known_cost_cny"] == 0.1


def test_priority_budget_keeps_original_stage_ids(case, issue, tmp_path):
    low = dict(issue, issue_id="minor", kind="editorial", priority="low", evidence_ids=[])
    high = dict(issue, issue_id="major", priority="high")
    client = Client({"review": {"issues": [low, high]}, "issue_002_author": proposal(high), "issue_002_verifier": verdict()})
    report = run_revision(case, config(max_issues=1), tmp_path, client)
    assert [c[0] for c in client.seen] == ["review", "issue_002_author", "issue_002_verifier"]
    assert report["issue_records"][0]["reason"] == "issue_processing_budget_exhausted"
    assert report["issue_records"][1]["applied"]
    assert report["counts"]["pending"] == 1


def test_selected_identity_metadata_reaches_author_and_verifier_only(case, issue, tmp_path):
    case["materials"]["M2"] = {"title": "Unrelated paper", "text": "Unrelated content", "source_handles": ["P0002"]}
    case["source_identity_map"] = {
        "P0001": {"material_ids": ["M1"], "original_identity": {"paper_id": "stable-paper-1", "doi": "10.1234/selected", "title": "Selected actual paper", "year": "2024", "card_path": "/private/historical/path"}},
        "P0002": {"material_ids": ["M2"], "original_identity": {"paper_id": "unrelated-id", "doi": "10.1234/unrelated", "title": "Unrelated paper"}}}
    client = Client(responses(issue))
    report = run_revision(case, config(), tmp_path, client)
    assert report["counts"]["applied"] == 1
    for stage, messages, _ in client.seen:
        payload = json.loads(messages[1]["content"])
        if stage in {"issue_001_author", "issue_001_verifier"}:
            selected = payload["selected_source_identities"]
            assert list(selected) == ["P0001"]
            assert selected["P0001"]["original_identity"]["doi"] == "10.1234/selected"
            assert selected["P0001"]["original_identity"]["paper_id"] == "stable-paper-1"
            assert "card_path" not in selected["P0001"]["original_identity"]
            assert "unrelated-id" not in messages[1]["content"]
        else:
            assert payload["material_index"][0]["source_handles"] == ["P0001"]


def test_related_counterpart_blocks_reach_author_and_verifier(case, issue, tmp_path):
    case["draft_text"] += "\r\n# Another chapter\r\n\r\nCounterpart evidence is discussed here.\r\n"
    issue["related_block_ids"] = ["B0005"]
    client = Client(responses(issue))
    report = run_revision(case, config(), tmp_path, client)
    assert report["counts"]["applied"] == 1
    for stage, messages, _ in client.seen:
        if stage in {"issue_001_author", "issue_001_verifier"}:
            payload = json.loads(messages[1]["content"])
            assert [block["block_id"] for block in payload["related_blocks"]] == ["B0005"]
            assert payload["related_blocks"][0]["text"] == "Counterpart evidence is discussed here.\r\n"
            assert "B0005" not in [block["block_id"] for block in payload["neighbors"]]


@pytest.mark.parametrize("related", [["B9999"], ["B0003", "B0003"], "B0003", [123]])
def test_invalid_related_block_references_are_pending_before_author(case, issue, tmp_path, related):
    issue["related_block_ids"] = related
    client = Client(responses(issue))
    report = run_revision(case, config("C"), tmp_path, client)
    assert [call[0] for call in client.seen] == ["review"]
    assert report["counts"]["pending"] == 1 and not report["applied_patches"]
    assert "related_block" in report["issue_records"][0]["reason"]


@pytest.mark.parametrize("first_related,second_related", [(["B0003"], ["B0002"]), (["B0003"], []), ([], ["B0002"])])
def test_mutually_dependent_disjoint_edits_do_not_both_apply(case, issue, tmp_path, first_related, second_related):
    first = dict(issue, related_block_ids=first_related)
    second = dict(issue, issue_id="other", target_block_id="B0003", original_text="Other text stays.", preserve=[], related_block_ids=second_related)
    client = Client({"review": {"issues": [first, second]}, "issue_001_author": proposal(first), "issue_001_verifier": verdict()})
    report = run_revision(case, config(), tmp_path, client)
    assert report["counts"]["applied"] == 1 and report["counts"]["pending"] == 1
    assert "context_dependency" in report["issue_records"][1]["reason"]
    assert [call[0] for call in client.seen] == ["review", "issue_001_author", "issue_001_verifier"]
    assert "Other text stays." in Path(report["candidate_path"]).read_text()


def test_disjoint_edits_can_share_unchanged_related_context(case, issue, tmp_path):
    first = dict(issue, related_block_ids=["B0001"])
    second = dict(issue, issue_id="other", target_block_id="B0003", original_text="Other text stays.", preserve=[], related_block_ids=["B0001"])
    second_proposal = dict(proposal(second), replacement_text="Other text stays in one tested setting.")
    client = Client({"review": {"issues": [first, second]}, "issue_001_author": proposal(first), "issue_001_verifier": verdict(), "issue_002_author": second_proposal, "issue_002_verifier": verdict()})
    report = run_revision(case, config(), tmp_path, client)
    assert report["counts"]["applied"] == 2 and report["counts"]["pending"] == 0
    assert len(client.seen) == 5
    assert Path(report["candidate_path"]).read_bytes().startswith(b"# Result\r\n")
