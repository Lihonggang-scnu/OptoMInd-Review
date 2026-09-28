"""AgentScope model factory — builds DashScopeChatModel from existing config layer."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, List, Optional

from agentscope.model import DashScopeChatModel
from agentscope.credential import DashScopeCredential

from config.qwen_config import (
    get_model_name,
    get_fallback_model_names,
    get_qwen_api_key_candidates_ordered,
    DASHSCOPE_COMPATIBLE_BASE_URL,
)
from config.secret_pool import secret_fingerprint

logger = logging.getLogger(__name__)


class _ManagedDashScopeChatModel(DashScopeChatModel):
    """DashScope model with deterministic async-client cleanup.

    AgentScope 2.0.2 creates a fresh ``openai.AsyncClient`` inside every
    non-streaming call but does not close it.  During long multi-agent runs its
    deferred destructor may execute after the event loop has closed, producing
    noisy ``AsyncClient.aclose`` task failures and retaining network resources
    longer than necessary.  OptoMind uses non-streaming calls, so we can keep
    AgentScope's formatter and response parser while owning the client lifetime
    explicitly inside the active event loop.
    """

    async def _call_api(
        self,
        model_name,
        messages,
        tools=None,
        tool_choice=None,
        **kwargs,
    ):
        if self.stream:
            # The managed implementation deliberately covers the mode used by
            # ResearchWorker.  Fall back to upstream for an explicitly
            # requested streaming model so semantics are not silently changed.
            return await super()._call_api(
                model_name,
                messages,
                tools,
                tool_choice,
                **kwargs,
            )

        import openai

        formatted_messages = await self.formatter.format(messages)
        request_kwargs = {
            "model": model_name,
            "messages": formatted_messages,
            "stream": False,
        }
        parameters = self.parameters
        for key in ("max_tokens", "temperature", "top_p"):
            value = getattr(parameters, key, None)
            if value is not None:
                request_kwargs[key] = value
        request_kwargs.update(kwargs)

        fmt_tools, fmt_tool_choice = self._format_tools(tools, tool_choice)
        if fmt_tools is not None:
            request_kwargs["tools"] = fmt_tools
            if not parameters.parallel_tool_calls:
                request_kwargs["parallel_tool_calls"] = False
        if fmt_tool_choice is not None:
            request_kwargs["tool_choice"] = fmt_tool_choice

        extra_body = {}
        if parameters.thinking_enable is not None:
            extra_body["enable_thinking"] = parameters.thinking_enable
        if parameters.thinking_budget is not None:
            extra_body["thinking_budget"] = parameters.thinking_budget
        if parameters.top_k is not None:
            extra_body["top_k"] = parameters.top_k
        if extra_body:
            request_kwargs.setdefault("extra_body", {}).update(extra_body)

        start_datetime = datetime.now()
        async with openai.AsyncClient(
            api_key=self.credential.api_key.get_secret_value(),
            base_url=self.credential.base_url,
            **self.client_kwargs,
        ) as client:
            response = await client.chat.completions.create(**request_kwargs)
            return self._parse_completion_response(start_datetime, response)


@dataclass
class _Candidate:
    model_name: str
    api_key: str
    api_key_masked: str
    api_key_fingerprint: str
    api_key_source: str
    model_instance: DashScopeChatModel


def _build_model(model_name: str, api_key: str) -> DashScopeChatModel:
    """Build a single DashScopeChatModel for the given model name and key."""
    credential = DashScopeCredential(
        api_key=api_key,
        base_url=DASHSCOPE_COMPATIBLE_BASE_URL,
    )
    return _ManagedDashScopeChatModel(
        credential=credential,
        model=model_name,
        stream=False,
        max_retries=1,
        retry_delay=0.5,
    )


class AgentScopeModelFactory:
    """Build DashScopeChatModel instances, reusing the existing config/key pool.

    Each key receives the complete ordered model ladder. Recovery selects the
    next same-model key for account failures or the next same-key model for
    model/route failures. Network failures retry the current pair in place.
    """

    # Process-local circuit breaker.  A full review creates several workers;
    # without shared health memory every chapter would retry the same exhausted
    # or overdue account before reaching a working key.  Raw keys are kept only
    # in memory and are never logged or persisted.
    _unhealthy_keys: ClassVar[set[str]] = set()
    _unhealthy_routes: ClassVar[set[tuple[str, str]]] = set()
    _preferred_key: ClassVar[Optional[str]] = None

    def __init__(self, model_tier: str = "standard_model") -> None:
        self._tier = model_tier
        self._candidates: List[_Candidate] = []
        self._current_index: int = 0
        self._mock_mode: bool = False
        self._build_candidates()

    def _build_candidates(self) -> None:
        primary_name = get_model_name(self._tier)
        fallback_names = get_fallback_model_names(self._tier)
        all_names = [primary_name] + fallback_names

        key_candidates = get_qwen_api_key_candidates_ordered()
        healthy_candidates = [
            item
            for item in key_candidates
            if item["api_key"] not in self._unhealthy_keys
        ]
        if self._preferred_key:
            healthy_candidates.sort(
                key=lambda item: item["api_key"] != self._preferred_key
            )
        # If every candidate was marked unhealthy during this process, retain
        # the original list as a last-resort retry rather than silently
        # entering mock mode.  This also permits recovery from transient quota
        # or account-state changes in a long-running application.
        key_candidates = healthy_candidates or key_candidates
        if not key_candidates:
            self._mock_mode = True
            logger.warning(
                "No API keys found for tier '%s'. ResearchWorker will use mock mode.",
                self._tier,
            )
            return

        # Every key receives the complete model fallback ladder. Previously,
        # fallback models existed only for the first key; after a key switch a
        # model-scoped quota error could only trigger more account rotation.
        for key_info in key_candidates:
            api_key = key_info["api_key"]
            for name in all_names:
                if (api_key, name) in self._unhealthy_routes:
                    continue
                try:
                    inst = _build_model(name, api_key)
                    self._candidates.append(_Candidate(
                        model_name=name,
                        api_key=api_key,
                        api_key_masked=key_info["api_key_masked"],
                        api_key_fingerprint=secret_fingerprint(api_key),
                        api_key_source=str(
                            key_info.get("api_key_source") or ""
                        ),
                        model_instance=inst,
                    ))
                    logger.debug(
                        "Registered candidate: %s key=%s source=%s",
                        name,
                        key_info["api_key_masked"],
                        key_info.get("api_key_source", ""),
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to build model %s key=%s: %s",
                        name,
                        key_info["api_key_masked"],
                        exc,
                    )

    @property
    def mock_mode(self) -> bool:
        return self._mock_mode

    @property
    def candidate_count(self) -> int:
        return len(self._candidates)

    @property
    def current_model(self) -> Optional[DashScopeChatModel]:
        if self._mock_mode or not self._candidates:
            return None
        return self._candidates[self._current_index].model_instance

    @property
    def current_model_name(self) -> str:
        if self._mock_mode or not self._candidates:
            return "mock"
        return self._candidates[self._current_index].model_name

    @property
    def current_key_masked(self) -> str:
        if self._mock_mode or not self._candidates:
            return ""
        return self._candidates[self._current_index].api_key_masked

    @property
    def current_key_fingerprint(self) -> str:
        if self._mock_mode or not self._candidates:
            return ""
        return self._candidates[self._current_index].api_key_fingerprint

    @property
    def current_key_source(self) -> str:
        if self._mock_mode or not self._candidates:
            return ""
        return self._candidates[self._current_index].api_key_source

    def has_next_candidate(self) -> bool:
        return self._current_index + 1 < len(self._candidates)

    def advance_to_next_candidate(self) -> bool:
        """Switch to the next candidate in order. Returns True if switched."""
        if not self.has_next_candidate():
            return False
        self._current_index += 1
        c = self._candidates[self._current_index]
        logger.info(
            "Advanced to candidate %d/%d: model=%s key=%s",
            self._current_index + 1, len(self._candidates),
            c.model_name, c.api_key_masked,
        )
        return True

    def advance_to_next_key_candidate(self, model_name: str) -> bool:
        """Find the next candidate with the same model_name but a different key.

        Used for key-level errors (401/403/429): keep model, switch key.
        """
        cur = self._current_index
        if self._candidates:
            self._unhealthy_keys.add(self._candidates[cur].api_key)
            if self._preferred_key == self._candidates[cur].api_key:
                self._preferred_key = None
        for i in range(cur + 1, len(self._candidates)):
            c = self._candidates[i]
            if c.model_name == model_name and c.api_key != self._candidates[cur].api_key:
                self._current_index = i
                logger.info("Switched key for model %s: key=%s", model_name, c.api_key_masked)
                return True
        return False

    def mark_current_candidate_successful(self) -> None:
        """Remember the working key for later workers in this process."""

        if self._mock_mode or not self._candidates:
            return
        key = self._candidates[self._current_index].api_key
        model = self._candidates[self._current_index].model_name
        self._unhealthy_keys.discard(key)
        self._unhealthy_routes.discard((key, model))
        self._preferred_key = key

    def advance_to_next_model_candidate(self) -> bool:
        """Find the next candidate with the same key but a different model.

        Used for model-level errors: keep key, switch model.
        """
        cur = self._current_index
        cur_key = self._candidates[cur].api_key if self._candidates else None
        cur_model = self._candidates[cur].model_name if self._candidates else None
        if cur_key and cur_model:
            self._unhealthy_routes.add((cur_key, cur_model))
        for i in range(cur + 1, len(self._candidates)):
            c = self._candidates[i]
            if c.api_key == cur_key and c.model_name != cur_model:
                self._current_index = i
                logger.info("Switched model for key=***: new model=%s", c.model_name)
                return True
        return False

    def reset(self) -> None:
        self._current_index = 0
