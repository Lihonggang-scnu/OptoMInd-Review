"""Model tier routing policy for OptoMind agents."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .qwen_config import get_model_name, load_model_policy


DETERMINISTIC_TASK_TYPES = {
    "deterministic",
    "deterministic_tool",
    "target_builder",
    "target_validation",
    "spectrum_construction",
}

ESCALATION_TERMS = {
    "blocking",
    "major",
    "critical",
    "conflict",
    "unverified_literature_used",
    "missing_source",
}


def _contains_term(value: Any, terms: Iterable[str]) -> bool:
    terms_l = [str(term).lower() for term in terms]
    if value is None:
        return False
    if isinstance(value, Mapping):
        return any(_contains_term(k, terms_l) or _contains_term(v, terms_l) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_term(item, terms_l) for item in value)
    text = str(value).lower()
    return any(term in text for term in terms_l)


def should_escalate(
    validation_report: Any = None,
    self_review_report: Any = None,
    evidence_status: Any = None,
) -> bool:
    """Return true when a task should route to advanced_model."""

    if isinstance(validation_report, Mapping) and validation_report.get("errors"):
        return True
    if _contains_term(validation_report, {"blocking", "major", "critical"}):
        return True
    if _contains_term(self_review_report, {"blocking", "major", "critical"}):
        return True
    if _contains_term(evidence_status, {"conflict", "unverified_literature_used", "missing_source"}):
        return True
    return False


def select_model_tier(
    task_type: str | None = None,
    complexity: str | None = None,
    risk_level: str | None = None,
    agent_name: str | None = None,
    validation_report: Any = None,
    self_review_report: Any = None,
    evidence_status: Any = None,
) -> str:
    """Select a model tier for an agent/task."""

    if task_type and str(task_type).strip().lower() in DETERMINISTIC_TASK_TYPES:
        return "deterministic"
    if should_escalate(validation_report, self_review_report, evidence_status):
        return "advanced_model"

    policy = load_model_policy()
    if agent_name and agent_name in dict(policy.get("agent_model_policy", {})):
        return str(policy["agent_model_policy"][agent_name])

    task_key = str(task_type or "").strip().lower()
    routing_rules = dict(policy.get("routing_rules", {}))
    if task_key in routing_rules:
        return str(dict(routing_rules[task_key]).get("default_tier", policy.get("default_model_tier", "standard_model")))

    if str(risk_level or "").strip().lower() in {"high", "high_risk", "critical"}:
        return "advanced_model"
    if str(complexity or "").strip().lower() in {"high", "xhigh", "complex"}:
        return "advanced_model"
    return str(policy.get("default_model_tier", "standard_model"))


def select_model_name(
    task_type: str | None = None,
    complexity: str | None = None,
    risk_level: str | None = None,
    agent_name: str | None = None,
    validation_report: Any = None,
    self_review_report: Any = None,
    evidence_status: Any = None,
) -> str:
    """Select the concrete model name for an agent/task."""

    tier = select_model_tier(
        task_type=task_type,
        complexity=complexity,
        risk_level=risk_level,
        agent_name=agent_name,
        validation_report=validation_report,
        self_review_report=self_review_report,
        evidence_status=evidence_status,
    )
    if tier == "deterministic":
        return "deterministic"
    return get_model_name(tier)
