"""Recovery policy — handles model/key failures and restores AgentState."""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Optional, Tuple

from agentscope.state import AgentState

if TYPE_CHECKING:
    from .agent_model_factory import AgentScopeModelFactory
    from .event_logger import EventLogger

logger = logging.getLogger(__name__)

_KEY_ERROR_CODES = {401, 403, 429}
_KEY_ERROR_KEYWORDS = (
    "invalid api key", "unauthorized", "quota", "rate limit", "too many requests",
    "arrearage", "overdue", "account not in good standing",
    "insufficient balance", "payment", "billing",
)
_MODEL_ERROR_KEYWORDS = ("model not found", "does not exist", "not supported", "protocol", "incompatible")
_NETWORK_ERROR_TYPES = ("ConnectionError", "TimeoutError", "OSError", "SSLError", "ConnectTimeout")
_MAX_NETWORK_RETRIES = 2


class ErrorCategory(str, Enum):
    key = "key"        # 401/403/429/quota — switch key, keep model
    model = "model"    # model not found, protocol error — switch model, keep key
    network = "network"  # connection/timeout — limited retry, keep model+key
    unknown = "unknown"  # fallback: try next candidate


def classify_error(exc: BaseException) -> ErrorCategory:
    """Classify an exception into one of the three recovery categories."""
    exc_type = type(exc).__name__
    exc_msg = str(exc).lower()

    # Network errors
    if exc_type in _NETWORK_ERROR_TYPES:
        return ErrorCategory.network
    if any(kw in exc_msg for kw in ("connection", "timeout", "ssl", "network")):
        return ErrorCategory.network

    # A valid key can be paid-enabled for one model and FreeTierOnly for
    # another. Preserve the key and descend the model ladder first.
    if any(
        marker in exc_msg
        for marker in (
            "allocationquota.freetieronly",
            "free tier only",
            "free-tier-only",
            "use free tier only",
        )
    ):
        return ErrorCategory.model

    # Key-level HTTP errors
    if any(kw in exc_msg for kw in _KEY_ERROR_KEYWORDS):
        return ErrorCategory.key
    # Check for HTTP status codes in the error string
    for code in _KEY_ERROR_CODES:
        if str(code) in exc_msg:
            return ErrorCategory.key

    # Model-level errors
    if any(kw in exc_msg for kw in _MODEL_ERROR_KEYWORDS):
        return ErrorCategory.model

    return ErrorCategory.unknown


class RecoveryPolicy:
    """Classifies failures and selects the right candidate from the factory.

    Recovery strategy:
    - key error:     keep primary model, advance to next key (same model, different key)
    - model error:   keep primary key, advance to fallback model
    - network error: limited in-place retry (up to _MAX_NETWORK_RETRIES), then advance
    - unknown:       advance to next candidate (could be key or model switch)

    All switches are logged to EVENTS.jsonl.
    """

    def __init__(
        self,
        factory: "AgentScopeModelFactory",
        event_logger: "EventLogger",
        max_recovery_attempts: Optional[int] = None,
    ) -> None:
        self._factory = factory
        self._event_logger = event_logger
        self._attempts = 0
        self._network_retries = 0
        # Default to candidate_count so every candidate gets exactly one attempt
        self._max_attempts = max_recovery_attempts if max_recovery_attempts is not None else factory.candidate_count

    def handle_failure(
        self,
        exc: BaseException,
        category: ErrorCategory,
        saved_state: Optional[dict],
        current_model: object,
    ) -> Tuple[bool, Optional[AgentState], Optional[object]]:
        """Try to recover from a failure.

        Returns:
            (recovered, new_agent_state, new_model)
        """
        if self._attempts >= self._max_attempts:
            logger.error("Max recovery attempts (%d) reached.", self._max_attempts)
            return False, None, None

        old_name = self._factory.current_model_name
        old_key_fingerprint = self._factory.current_key_fingerprint
        old_key_source = self._factory.current_key_source
        reason = f"{type(exc).__name__}: {str(exc)[:150]}"

        if category == ErrorCategory.network:
            if self._network_retries >= _MAX_NETWORK_RETRIES:
                logger.warning("Max network retries reached; advancing to next candidate.")
                category = ErrorCategory.unknown
            else:
                self._network_retries += 1
                self._attempts += 1
                logger.info("Network error, retry %d/%d with same model+key.", self._network_retries, _MAX_NETWORK_RETRIES)
                self._event_logger.log_recovery(
                    "network_retry",
                    old_name,
                    old_name,
                    reason,
                    old_key_fingerprint=old_key_fingerprint,
                    new_key_fingerprint=old_key_fingerprint,
                    old_key_source=old_key_source,
                    new_key_source=old_key_source,
                )
                new_state = self._restore_state(saved_state)
                return True, new_state, self._factory.current_model

        if category == ErrorCategory.key:
            # Switch to next key — factory keeps same model name, different key
            switched = self._factory.advance_to_next_key_candidate(old_name)
            if not switched:
                logger.warning("No more key candidates for model %s; trying fallback.", old_name)
                switched = self._factory.advance_to_next_candidate()
        elif category == ErrorCategory.model:
            # Skip same-key fallback models, go straight to model switch
            switched = self._factory.advance_to_next_model_candidate()
            if not switched:
                switched = self._factory.advance_to_next_candidate()
        else:
            switched = self._factory.advance_to_next_candidate()

        if not switched:
            logger.error("No more candidates available for category=%s.", category)
            return False, None, None

        self._attempts += 1
        new_name = self._factory.current_model_name
        self._event_logger.log_recovery(
            category.value,
            old_name,
            new_name,
            reason,
            old_key_fingerprint=old_key_fingerprint,
            new_key_fingerprint=self._factory.current_key_fingerprint,
            old_key_source=old_key_source,
            new_key_source=self._factory.current_key_source,
        )
        logger.info("Recovery: %s → %s (category=%s)", old_name, new_name, category)

        new_state = self._restore_state(saved_state)
        return True, new_state, self._factory.current_model

    def _restore_state(self, saved_state: Optional[dict]) -> Optional[AgentState]:
        if saved_state is None:
            return None
        try:
            state = AgentState.model_validate(saved_state)
            logger.info("AgentState restored after recovery.")
            return state
        except Exception as exc:
            logger.warning("State restore failed: %s. Starting fresh.", exc)
            return None
