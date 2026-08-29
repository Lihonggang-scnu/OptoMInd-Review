"""Authenticated OpenAlex cached-content downloads without leaking API keys."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping

from optomind_research.provider_key_router import ProviderKeyRouter
from tools.academic_backends.openalex_backend import (
    RATE_LIMIT_SECONDS,
    _openalex_api_keys,
)


def is_openalex_content_url(url: str) -> bool:
    try:
        return (
            urllib.parse.urlparse(str(url or "")).netloc.lower()
            == "content.openalex.org"
        )
    except Exception:
        return False


def fetch_openalex_content(
    url: str,
    *,
    timeout: float = 60,
    headers: Mapping[str, str] | None = None,
) -> tuple[bytes | None, str]:
    """Fetch PDF/TEI content through the shared ``openalex`` lane router.

    The authenticated URL is never returned or logged, so secrets cannot enter
    provenance reports or failed-download manifests.  Key selection, pacing,
    429 progressive cooldown, and 401/403 quarantine are shared with the
    OpenAlex backend through the provider namespace.
    """

    keys = list(_openalex_api_keys())
    if not keys:
        return None, "openalex_content_key_missing"
    router = ProviderKeyRouter(
        provider="openalex",
        keys=keys,
        min_interval_seconds=RATE_LIMIT_SECONDS,
        max_attempts=len(keys) or 1,
    )
    errors: list[str] = []
    transport_failures = 0
    # One logical download tries each configured lane at most once.
    for attempt in range(len(router.lanes)):
        lane, _ = router.acquire_lane()
        if lane is None:
            errors.append("no_lane")
            break
        key = lane.key
        parsed = urllib.parse.urlparse(url)
        query = dict(
            urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
        )
        query["api_key"] = key
        authenticated = urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(query))
        )
        try:
            req = urllib.request.Request(
                authenticated,
                headers=dict(headers or {}),
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                router.reset_lane_penalty(lane)
                return resp.read(), ""
        except urllib.error.HTTPError as exc:
            errors.append(f"key#{attempt + 1}:HTTP_{exc.code}")
            if exc.code in {401, 403}:
                router.quarantine_lane(lane)
                continue
            if exc.code == 429:
                retry_after = ""
                try:
                    retry_after = (
                        exc.headers.get("Retry-After")
                        if exc.headers
                        else ""
                    )
                except Exception:
                    retry_after = ""
                try:
                    wait_s = min(20.0, max(3.0, float(retry_after or 5.0)))
                except Exception:
                    wait_s = 5.0
                router.rate_limit_cool_lane(lane, wait_s)
                continue
            if 500 <= exc.code <= 599:
                router.cool_lane(lane, min(8.0, 5.0))
            break
        except Exception as exc:
            errors.append(f"key#{attempt + 1}:{type(exc).__name__}")
            router.cool_lane(lane, 2.0)
            transport_failures += 1
            # One alternate key is enough for a transport failure; secrets do
            # not repair a broken endpoint indefinitely.
            if transport_failures >= 2:
                break
            continue
        finally:
            router.release_lane(lane)
    return None, "openalex_content_failed:" + ",".join(errors[-4:])
