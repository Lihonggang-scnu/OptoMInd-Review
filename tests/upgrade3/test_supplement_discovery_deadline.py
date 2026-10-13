import time
from types import SimpleNamespace

from optomind_research.provider_key_router import ProviderKeyRouter
from optomind_research.runtime.upgrade3.planning_supplement import (
    CompositeS2OpenAlexGateway,
)
from optomind_research.s2_intelligence_gateway import (
    S2GatewayResponse,
    S2IntelligenceGateway,
)


def test_composite_fast_fallback_keeps_s2_429_audit():
    class S2:
        def search_papers(self, query, **kwargs):
            assert kwargs["fast_fail_retryable"] is True
            return [], SimpleNamespace(status_code=429, audit={"page_count": 1})

    class OpenAlex:
        last_status = "ok"
        last_error = ""

        def search(self, query, **kwargs):
            assert kwargs["deadline"] > time.monotonic()
            return [{"openalex_id": "W1", "title": "fallback"}]

    gateway = CompositeS2OpenAlexGateway(S2(), OpenAlex())
    rows, response = gateway.search_papers(
        "bounded fallback", limit=2, deadline=time.monotonic() + 2
    )

    assert response.status_code == 200
    assert len(rows) == 1
    assert gateway.last_provider_audit[0]["status_code"] == 429
    assert gateway.last_provider_audit[1]["status_code"] == 200


def test_s2_deadline_keeps_first_page_when_later_page_is_not_started():
    class Transport:
        def __init__(self):
            self.calls = 0

        def request_json(self, *args, **kwargs):
            self.calls += 1
            assert kwargs["deadline"] is not None
            assert kwargs["fast_fail_retryable"] is True
            time.sleep(0.03)
            return S2GatewayResponse(
                ok=True,
                status_code=200,
                status_category="ok",
                payload={
                    "total": 8,
                    "data": [{"paperId": "P1", "title": "first page"}] * 4,
                },
            )

    transport = Transport()
    rows, response = S2IntelligenceGateway(transport).search_papers(
        "bounded pages",
        limit=4,
        deadline=time.monotonic() + 0.01,
        fast_fail_retryable=True,
    )

    assert len(rows) == 1
    assert response.audit["truncated_reason"] == "deadline"
    assert response.audit["page_count"] == 1
    assert transport.calls == 1


def test_router_all_busy_returns_at_shared_deadline():
    provider = "test_deadline"
    ProviderKeyRouter.reset_process_lanes(provider=provider)
    clock = [0.0]
    router = ProviderKeyRouter(
        provider,
        ["test-key"],
        min_interval_seconds=0.0,
        now_fn=lambda: clock[0],
        sleep_fn=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    router.lanes[0].busy = True
    try:
        lane, waited = router.acquire_lane(deadline=0.11)
        assert lane is None
        assert waited <= 0.11
        assert clock[0] <= 0.11
    finally:
        ProviderKeyRouter.reset_process_lanes(provider=provider)


def test_legacy_transport_keeps_original_default_call_shape():
    class LegacyTransport:
        def request_json(self, method, url, *, params, ttl_seconds):
            return S2GatewayResponse(ok=True, status_code=200, status_category="ok",
                                     payload={"total": 1, "data": [{"paperId": "legacy", "title": "Legacy"}]})
    rows, response = S2IntelligenceGateway(LegacyTransport()).search_papers("legacy", limit=1)
    assert len(rows) == 1
    assert response.ok


def test_legacy_composite_gateway_is_not_misclassified_as_provider_failure():
    class LegacyGateway:
        def search_papers(self, query, *, limit):
            return [SimpleNamespace(paper_id="legacy", title="Legacy")], SimpleNamespace(status_code=200)
    class NeverFallback:
        def search(self, *args, **kwargs):
            raise AssertionError("Legacy signature must not be treated as an outage")
    gateway = CompositeS2OpenAlexGateway(LegacyGateway(), NeverFallback())
    rows, response = gateway.search_papers("legacy", limit=1)
    assert len(rows) == 1 and response.status_code == 200
    assert len(gateway.last_provider_audit) == 1
    assert gateway.last_provider_audit[0]["status_code"] == 200
