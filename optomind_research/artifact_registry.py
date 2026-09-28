"""ArtifactRef and ArtifactRegistry — lightweight large-artifact pointer system.

Design rule: large artifacts (chunks, M3 reports, DAG registries) are never
embedded inline in the run manifest or compact blueprint.  They are always
referenced by ArtifactRef, which contains only path, hash, schema and producer.
Downstream agents load them on demand.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


VALID_ARTIFACT_TYPES = frozenset({
    "text_chunk",
    "visual_chunk",
    "report",
    "blueprint",
    "evidence_packet",
    "manifest",
    "corpus",
    "knowledge_base",
    "review",
    "research_plan",
    "problem_frame",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    """Return hex SHA-256 of a file, or empty string if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


@dataclass
class ArtifactRef:
    """Pointer to a large artifact stored on disk.

    Never embed the artifact content here.  Use path + sha256 for integrity.
    """
    artifact_id: str
    artifact_type: str
    path: str
    sha256: str = ""
    schema_version: str = ""
    producer: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "path": self.path,
            "sha256": self.sha256,
            "schema_version": self.schema_version,
            "producer": self.producer,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ArtifactRef":
        return cls(
            artifact_id=str(d.get("artifact_id", "")),
            artifact_type=str(d.get("artifact_type", "report")),
            path=str(d.get("path", "")),
            sha256=str(d.get("sha256", "")),
            schema_version=str(d.get("schema_version", "")),
            producer=str(d.get("producer", "")),
            created_at=str(d.get("created_at", utc_now())),
        )

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        artifact_id: Optional[str] = None,
        artifact_type: str = "report",
        schema_version: str = "",
        producer: str = "",
        compute_hash: bool = True,
    ) -> "ArtifactRef":
        """Create an ArtifactRef from an existing file, optionally hashing it."""
        p = Path(path)
        return cls(
            artifact_id=artifact_id or str(uuid.uuid4()),
            artifact_type=artifact_type,
            path=str(p.resolve()),
            sha256=sha256_file(p) if compute_hash and p.exists() else "",
            schema_version=schema_version,
            producer=producer,
        )

    def exists(self) -> bool:
        return Path(self.path).exists()

    def load_json(self) -> Any:
        """Load and parse the referenced JSON file."""
        p = Path(self.path)
        if not p.exists():
            raise FileNotFoundError(f"Artifact not found: {self.path}")
        return json.loads(p.read_text(encoding="utf-8"))


class ArtifactRegistry:
    """In-memory registry of ArtifactRef objects for one research run.

    This is a thin index.  The actual content remains on disk.
    """

    def __init__(self) -> None:
        self._refs: dict[str, ArtifactRef] = {}

    def register(self, ref: ArtifactRef) -> None:
        self._refs[ref.artifact_id] = ref

    def register_file(
        self,
        path: Path,
        *,
        artifact_id: Optional[str] = None,
        artifact_type: str = "report",
        schema_version: str = "",
        producer: str = "",
        compute_hash: bool = True,
    ) -> ArtifactRef:
        ref = ArtifactRef.from_file(
            path,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            producer=producer,
            compute_hash=compute_hash,
        )
        self.register(ref)
        return ref

    def get(self, artifact_id: str) -> Optional[ArtifactRef]:
        return self._refs.get(artifact_id)

    def list_by_type(self, artifact_type: str) -> list[ArtifactRef]:
        return [r for r in self._refs.values() if r.artifact_type == artifact_type]

    def to_dict(self) -> dict[str, Any]:
        return {aid: ref.to_dict() for aid, ref in self._refs.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "ArtifactRegistry":
        reg = cls()
        for aid, ref_dict in (d or {}).items():
            if isinstance(ref_dict, dict):
                reg.register(ArtifactRef.from_dict({**ref_dict, "artifact_id": aid}))
        return reg
