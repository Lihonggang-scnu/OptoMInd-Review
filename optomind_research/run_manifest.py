"""ResearchRunManifest — lightweight run state for the full research pipeline.

Design rules (from spec OPTOMIND-TOP-TIER-ROADMAP-V1, section 6.1):
- Only path / ID / status / summary fields are stored here.
- Full text, M3 reports, DAG registries, and chunk lists must NOT be embedded.
- Large artifacts are referenced via ArtifactRef (see artifact_registry.py).
- This file is the single source of truth for run provenance across sessions.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from optomind_research.artifact_registry import ArtifactRef, ArtifactRegistry


PIPELINE_VERSION = "optomind.v1.0"
VALID_STATUSES = ("running", "needs_human", "failed", "completed")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class StageStatus:
    stage_id: str
    status: str                  # "pending" | "running" | "completed" | "failed" | "skipped"
    started_at: str = ""
    completed_at: str = ""
    error: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "notes": self.notes,
        }


@dataclass
class ModelUsageRecord:
    stage: str
    module: str
    model_tier: str
    model_name: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    retries: int = 0
    cost_usd: float = 0.0
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "module": self.module,
            "model_tier": self.model_tier,
            "model_name": self.model_name,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "retries": self.retries,
            "cost_usd": self.cost_usd,
            "timestamp": self.timestamp,
        }


@dataclass
class HumanEvent:
    event_type: str        # "confirmation" | "edit" | "feedback" | "approval" | "rejection"
    stage: str
    description: str
    timestamp: str = field(default_factory=utc_now)
    operator: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "stage": self.stage,
            "description": self.description,
            "timestamp": self.timestamp,
            "operator": self.operator,
        }


@dataclass
class ErrorRecord:
    stage: str
    module: str
    error_type: str
    message: str
    recoverable: bool = True
    fallback_used: str = ""
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "module": self.module,
            "error_type": self.error_type,
            "message": self.message,
            "recoverable": self.recoverable,
            "fallback_used": self.fallback_used,
            "timestamp": self.timestamp,
        }


@dataclass
class ResearchRunManifest:
    """Lightweight, path-only run state.

    Forbidden content (will raise ValueError if added):
    - Full text chunks
    - Complete M3 gap loop reports
    - Entire DAG node/edge registries
    - Inline chunk text
    """
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    pipeline_version: str = PIPELINE_VERSION
    domain: str = ""
    status: str = "running"
    problem_frame_ref: Optional[ArtifactRef] = None
    corpus_ref: Optional[ArtifactRef] = None
    knowledge_base_ref: Optional[ArtifactRef] = None
    blueprint_ref: Optional[ArtifactRef] = None
    review_ref: Optional[ArtifactRef] = None
    research_plan_ref: Optional[ArtifactRef] = None
    artifact_registry: ArtifactRegistry = field(default_factory=ArtifactRegistry)
    stage_statuses: dict[str, StageStatus] = field(default_factory=dict)
    model_usage: list[ModelUsageRecord] = field(default_factory=list)
    tool_usage: list[dict[str, Any]] = field(default_factory=list)
    human_events: list[HumanEvent] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    notes: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    # -------------------------------------------------------------------------- #
    # Mutation helpers
    # -------------------------------------------------------------------------- #

    def set_status(self, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {status!r}; expected one of {VALID_STATUSES}")
        self.status = status
        self.updated_at = utc_now()

    def mark_stage(self, stage_id: str, status: str, *, error: str = "", notes: str = "") -> None:
        now = utc_now()
        if stage_id not in self.stage_statuses:
            self.stage_statuses[stage_id] = StageStatus(stage_id=stage_id, status=status, started_at=now)
        ss = self.stage_statuses[stage_id]
        ss.status = status
        if status in ("completed", "failed", "skipped"):
            ss.completed_at = now
        if error:
            ss.error = error
        if notes:
            ss.notes = notes
        self.updated_at = now

    def add_model_usage(
        self,
        stage: str,
        module: str,
        model_tier: str,
        *,
        model_name: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        retries: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.model_usage.append(ModelUsageRecord(
            stage=stage,
            module=module,
            model_tier=model_tier,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            retries=retries,
            cost_usd=cost_usd,
        ))
        self.updated_at = utc_now()

    def add_error(
        self,
        stage: str,
        module: str,
        error_type: str,
        message: str,
        *,
        recoverable: bool = True,
        fallback_used: str = "",
    ) -> None:
        self.errors.append(ErrorRecord(
            stage=stage,
            module=module,
            error_type=error_type,
            message=message,
            recoverable=recoverable,
            fallback_used=fallback_used,
        ))
        self.updated_at = utc_now()

    def add_human_event(
        self,
        event_type: str,
        stage: str,
        description: str,
        operator: str = "user",
    ) -> None:
        self.human_events.append(HumanEvent(
            event_type=event_type,
            stage=stage,
            description=description,
            operator=operator,
        ))
        self.updated_at = utc_now()

    def register_artifact(self, ref: ArtifactRef) -> None:
        self.artifact_registry.register(ref)
        self.updated_at = utc_now()

    def total_tokens(self) -> dict[str, int]:
        return {
            "input": sum(r.input_tokens for r in self.model_usage),
            "output": sum(r.output_tokens for r in self.model_usage),
        }

    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.model_usage)

    # -------------------------------------------------------------------------- #
    # Serialisation
    # -------------------------------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": "research_run_manifest.v1",
            "run_id": self.run_id,
            "pipeline_version": self.pipeline_version,
            "domain": self.domain,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes": self.notes,
            "stage_status": {sid: ss.to_dict() for sid, ss in self.stage_statuses.items()},
            "model_usage": [r.to_dict() for r in self.model_usage],
            "tool_usage": list(self.tool_usage),
            "human_events": [e.to_dict() for e in self.human_events],
            "errors": [e.to_dict() for e in self.errors],
            "artifact_registry": self.artifact_registry.to_dict(),
            "token_summary": self.total_tokens(),
            "cost_summary_usd": self.total_cost_usd(),
        }
        for attr in ("problem_frame_ref", "corpus_ref", "knowledge_base_ref",
                     "blueprint_ref", "review_ref", "research_plan_ref"):
            ref = getattr(self, attr)
            d[attr] = ref.to_dict() if ref else None
        return d

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "ResearchRunManifest":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        m = cls(
            run_id=d.get("run_id", str(uuid.uuid4())),
            pipeline_version=d.get("pipeline_version", PIPELINE_VERSION),
            domain=d.get("domain", ""),
            status=d.get("status", "running"),
            notes=d.get("notes", ""),
            created_at=d.get("created_at", utc_now()),
            updated_at=d.get("updated_at", utc_now()),
        )
        for attr in ("problem_frame_ref", "corpus_ref", "knowledge_base_ref",
                     "blueprint_ref", "review_ref", "research_plan_ref"):
            ref_dict = d.get(attr)
            if isinstance(ref_dict, dict):
                setattr(m, attr, ArtifactRef.from_dict(ref_dict))
        for sid, ss_dict in (d.get("stage_status") or {}).items():
            m.stage_statuses[sid] = StageStatus(
                stage_id=str(ss_dict.get("stage_id", sid)),
                status=str(ss_dict.get("status", "pending")),
                started_at=str(ss_dict.get("started_at", "")),
                completed_at=str(ss_dict.get("completed_at", "")),
                error=str(ss_dict.get("error", "")),
                notes=str(ss_dict.get("notes", "")),
            )
        for r in (d.get("model_usage") or []):
            if isinstance(r, dict):
                m.model_usage.append(ModelUsageRecord(**{k: v for k, v in r.items() if k in ModelUsageRecord.__dataclass_fields__}))
        m.tool_usage = list(d.get("tool_usage") or [])
        for e in (d.get("human_events") or []):
            if isinstance(e, dict):
                m.human_events.append(HumanEvent(**{k: v for k, v in e.items() if k in HumanEvent.__dataclass_fields__}))
        for e in (d.get("errors") or []):
            if isinstance(e, dict):
                m.errors.append(ErrorRecord(**{k: v for k, v in e.items() if k in ErrorRecord.__dataclass_fields__}))
        reg_dict = d.get("artifact_registry") or {}
        m.artifact_registry = ArtifactRegistry.from_dict(reg_dict)
        return m
