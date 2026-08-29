from __future__ import annotations

import threading
import time


def _claim(claim_id: str) -> dict:
    return {
        "claim_id": claim_id,
        "statement": "Whether this mechanism remains robust is unresolved.",
        "evidence_binding_status": "insufficient",
        "supporting_text_chunk_ids": [],
        "missing_evidence_components": ["robustness"],
    }


def test_claim_adaptation_uses_explicit_bounded_model_fallback(monkeypatch):
    import optomind_research.claim_adaptation_agent as module

    calls: list[tuple[str, dict]] = []

    def fake_call(_agent_name, _messages, **kwargs):
        calls.append((kwargs["model_tier"], kwargs))
        if kwargs["model_tier"] == "premium_model":
            raise TimeoutError("premium unavailable")
        return {
            "content": (
                '{"disposition":"retain_open_question",'
                '"open_question":"Which conditions preserve the mechanism?",'
                '"reason":"Direct evidence remains incomplete.",'
                '"confidence":"medium"}'
            ),
            "_llm_usage": {"success": True},
        }

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    decision = module.ClaimAdaptationAgent(real_llm=True).decide(
        _claim("S01-C01"),
        {"title": "Mechanism", "argument_role": "Define the boundary."},
        {"gap_type": "frontier_unknown"},
        [],
    )
    assert [tier for tier, _ in calls] == ["premium_model", "advanced_model"]
    assert all(kwargs["max_retries"] == 0 for _, kwargs in calls)
    assert all(kwargs["allow_model_fallback"] is False for _, kwargs in calls)
    assert decision["disposition"] == "retain_open_question"
    assert decision["_llm_usage"]["explicit_fallback_used"] is True


def test_adaptive_closure_decisions_are_bounded_parallel(monkeypatch):
    import optomind_research.claim_adaptation_agent as module

    thread_ids: set[int] = set()
    lock = threading.Lock()

    def fake_decide(self, claim, section, gap_classification, retrieval_history):
        with lock:
            thread_ids.add(threading.get_ident())
        time.sleep(0.04)
        return {
            "disposition": "retain_open_question",
            "revised_statement": "",
            "open_question": "What evidence would resolve this claim?",
            "reason": "The current evidence is incomplete.",
            "confidence": "medium",
            "remaining_evidence_need": "Independent full-text evidence.",
            "_llm_usage": {},
        }

    monkeypatch.setattr(module.ClaimAdaptationAgent, "decide", fake_decide)
    claims = [_claim(f"S01-C{index:02d}") for index in range(1, 5)]
    blueprint = {
        "sections": [{
            "section_id": "S01",
            "title": "Mechanism",
            "argument_role": "Define the boundary.",
            "claims": claims,
        }]
    }
    results = module.adapt_m3_claims(
        blueprint,
        target_claim_ids=[row["claim_id"] for row in claims],
        gap_classifications={
            row["claim_id"]: {"gap_type": "frontier_unknown"} for row in claims
        },
        round_reports=[],
        real_llm=True,
        max_workers=4,
    )
    assert len(results) == 4
    assert len(thread_ids) > 1
    assert all(row["closure_disposition"] == "open_question" for row in claims)
