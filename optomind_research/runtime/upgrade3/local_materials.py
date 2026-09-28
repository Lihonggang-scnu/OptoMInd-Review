"""Local material providers and bounded reading adapters for upgrade 3.

Snapshot construction and validation remain deterministic and model-free.  The
prepared snapshot also exposes an explicit, scope-preserving adapter for
building Qwen messages and normalizing quoted observations; it does not route
models or certify scientific truth.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .document_snapshot import SnapshotError, build_snapshot, build_snapshot_from_bytes, load_blocks, validate_snapshot


@dataclass(frozen=True)
class PreparedSnapshot:
    root: Path
    manifest: Mapping[str, Any]
    blocks: tuple[Mapping[str, Any], ...]
    assets: tuple[Mapping[str, Any], ...]
    references: tuple[Mapping[str, Any], ...]
    reading_view: str

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest.get("snapshot_id") or "")

    @property
    def content_depth(self) -> str:
        claim = self.manifest.get("producer_fulltext_claim") or {}
        return str(claim.get("content_depth") or ("fulltext" if claim.get("claimed") else "unknown"))

    def reading_policy(self) -> dict[str, Any]:
        """Return the computed, scope-preserving policy for downstream reading."""

        from .material_reading import build_reading_policy

        return build_reading_policy(self.manifest, self.blocks, root=self.root)

    def build_reading_packet(self) -> dict[str, Any]:
        """Build bounded Qwen input material without mutating this snapshot."""

        from .material_reading import build_reading_packet

        return build_reading_packet(
            self.manifest,
            self.blocks,
            assets=self.assets,
            references=self.references,
            root=self.root,
        )

    def build_reading_messages(
        self,
        question: str,
        facets: Sequence[Mapping[str, Any]] = (),
        *,
        packet: Mapping[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        """Build deterministic system/user messages for a Qwen reader."""

        from .material_reading import build_reading_messages

        packet = self._trusted_reading_packet(packet)
        return build_reading_messages(question, facets, packet)

    def normalize_reading_result(
        self,
        result: Any,
        *,
        packet: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Normalize model observations against trusted packet provenance."""

        from .material_reading import normalize_reading_result

        packet = self._trusted_reading_packet(packet)
        return normalize_reading_result(result, packet)

    def _trusted_reading_packet(self, packet: Mapping[str, Any] | None) -> dict[str, Any]:
        trusted = self.build_reading_packet()
        if packet is None:
            return trusted
        candidate = dict(packet)
        if candidate.get("snapshot_id") != self.snapshot_id:
            raise SnapshotError("reading_packet_snapshot_mismatch")
        digest = lambda value: hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        if digest(candidate) != digest(trusted):
            raise SnapshotError("reading_packet_trust_mismatch")
        return candidate

    def as_material(self) -> dict[str, Any]:
        """Return a JSON-shaped, provider-neutral material payload."""

        return {
            "snapshot_id": self.snapshot_id,
            "manifest": dict(self.manifest),
            "blocks": [dict(row) for row in self.blocks],
            "assets": [dict(row) for row in self.assets],
            "references": [dict(row) for row in self.references],
            "reading_view": self.reading_view,
            "content_depth": self.content_depth,
            "reading_policy": self.reading_policy(),
        }


class PreparedSnapshotProvider:
    """Load a prepared snapshot after structural and source integrity checks."""

    def __init__(self, snapshot_dir: str | Path):
        self.snapshot_dir = Path(snapshot_dir)

    def load(self) -> PreparedSnapshot:
        result = validate_snapshot(self.snapshot_dir)
        if not result.get("valid"):
            raise SnapshotError("prepared_snapshot_invalid")
        root = self.snapshot_dir
        manifest = _read_json(root / "DOCUMENT_MANIFEST.json")
        assets_payload = _read_json(root / "DOCUMENT_ASSETS.json")
        references_payload = _read_json(root / "REFERENCES.json")
        reading_view = (root / "READING_VIEW.md").read_text(encoding="utf-8")
        return PreparedSnapshot(
            root=root,
            manifest=manifest,
            blocks=tuple(load_blocks(root)),
            assets=tuple(assets_payload.get("assets") or ()),
            references=tuple(references_payload.get("references") or ()),
            reading_view=reading_view,
        )


class LocalTeiMaterialProvider:
    """Build one immutable snapshot from a local TEI/JATS XML source."""

    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)

    def build(
        self,
        input_path: str | Path,
        *,
        canonical_paper_id: str = "",
        publication_version: str = "",
        acquisition_revision: str = "",
        source_document_id: str = "main",
        source_role: str = "main",
        source_uri: str = "",
        metadata: Mapping[str, Any] | None = None,
        additional_sources: Sequence[Mapping[str, Any]] | None = None,
        material_depth_override: str = "",
        producer_fulltext_claim_override: Mapping[str, Any] | None = None,
        known_gaps_extra: Sequence[Mapping[str, Any]] | None = None,
    ) -> PreparedSnapshot:
        manifest = build_snapshot(
            input_path,
            self.output_root,
            canonical_paper_id=canonical_paper_id,
            publication_version=publication_version,
            acquisition_revision=acquisition_revision,
            source_document_id=source_document_id,
            source_role=source_role,
            source_uri=source_uri,
            metadata=metadata,
            additional_sources=additional_sources,
            material_depth_override=material_depth_override,
            producer_fulltext_claim_override=producer_fulltext_claim_override,
            known_gaps_extra=known_gaps_extra,
        )
        return PreparedSnapshotProvider(self.output_root / str(manifest["snapshot_id"])).load()

    def build_from_bytes(
        self,
        source_bytes: bytes,
        *,
        canonical_paper_id: str = "",
        publication_version: str = "",
        acquisition_revision: str = "",
        source_document_id: str = "main",
        source_role: str = "main",
        source_uri: str = "",
        source_name: str = "source.xml",
        metadata: Mapping[str, Any] | None = None,
        additional_sources: Sequence[Mapping[str, Any]] | None = None,
        material_depth_override: str = "",
        producer_fulltext_claim_override: Mapping[str, Any] | None = None,
        known_gaps_extra: Sequence[Mapping[str, Any]] | None = None,
    ) -> PreparedSnapshot:
        """Publish one snapshot from acquired bytes and load it after validation."""

        manifest = build_snapshot_from_bytes(
            bytes(source_bytes),
            self.output_root,
            canonical_paper_id=canonical_paper_id,
            publication_version=publication_version,
            acquisition_revision=acquisition_revision,
            source_document_id=source_document_id,
            source_role=source_role,
            source_uri=source_uri,
            source_name=source_name,
            metadata=metadata,
            additional_sources=additional_sources,
            material_depth_override=material_depth_override,
            producer_fulltext_claim_override=producer_fulltext_claim_override,
            known_gaps_extra=known_gaps_extra,
        )
        return PreparedSnapshotProvider(self.output_root / str(manifest["snapshot_id"])).load()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"invalid_material_file:{path.name}") from exc
    if not isinstance(value, dict):
        raise SnapshotError(f"invalid_material_shape:{path.name}")
    return value


__all__ = ["LocalTeiMaterialProvider", "PreparedSnapshot", "PreparedSnapshotProvider"]
