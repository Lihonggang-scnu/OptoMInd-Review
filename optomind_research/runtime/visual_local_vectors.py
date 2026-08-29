"""Deterministic local visual vectors and a hash-backed local index.

No paid model is involved.  Feature vectors are deterministic content
features computed from image pixels/file identity and are explicitly marked
as non-semantic.  The index never claims semantic embeddings when they are
absent; callers that require semantic embeddings receive an explicit
``no_semantic_embeddings`` status instead.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LOCAL_VECTOR_SCHEMA_VERSION = "optomind.visual_local_vectors.v1"
LOCAL_VECTOR_MODEL = "local_content_features_v1"
LOCAL_INDEX_SCHEMA_VERSION = "optomind.visual_local_index.v1"
FEATURE_DIM = 12
LOCAL_VECTOR_THUMBNAIL_MAX = 48


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _image_feature(image) -> list[float]:
    """Deterministic content features from a PIL image."""

    grayscale = image.convert("L")
    width, height = grayscale.size
    pixels = list(grayscale.getdata())
    total = max(1, len(pixels))
    histogram = [0.0] * 6
    for value in pixels:
        histogram[min(5, int(value) * 6 // 256)] += 1.0
    histogram = [round(count / total, 6) for count in histogram]

    rgb = image.convert("RGB")
    rgb_pixels = list(rgb.getdata())
    means = [0.0, 0.0, 0.0]
    for r, g, b in rgb_pixels:
        means[0] += r
        means[1] += g
        means[2] += b
    means = [round(value / total / 255.0, 6) for value in means]

    edge_count = 0
    samples = 0
    for row in range(0, height, max(1, height // 24)):
        previous = None
        for x in range(0, width, max(1, width // 32)):
            value = pixels[row * width + x]
            if previous is not None and abs(value - previous) > 24:
                edge_count += 1
            previous = value
            samples += 1
    edge_density = round(edge_count / max(1, samples), 6)

    aspect = round(
        min(4.0, max(0.25, width / max(1, height))) / 4.0,
        6,
    )
    return [*histogram, *means, edge_density, aspect, 1.0]


def local_image_feature_vector(
    image_path: str | Path,
) -> list[float]:
    """Return a deterministic 12-dim local content feature vector.

    Features are computed on a bounded thumbnail (max side
    ``LOCAL_VECTOR_THUMBNAIL_MAX`` px) so ingestion stays fast for
    full-resolution figures while output stays deterministic and schema-stable.
    """

    from PIL import Image

    with Image.open(Path(image_path)) as opened:
        image = opened.convert("RGB")
        image.thumbnail(
            (LOCAL_VECTOR_THUMBNAIL_MAX, LOCAL_VECTOR_THUMBNAIL_MAX)
        )
        features = _image_feature(image)
    if len(features) != FEATURE_DIM:
        raise ValueError(
            f"unexpected feature dimension: {len(features)}"
        )
    return features


def build_local_vector_refs(
    image_path: str | Path,
    *,
    unit_id: str,
    image_sha256: str = "",
    model: str = LOCAL_VECTOR_MODEL,
) -> dict[str, Any]:
    """Build an explicit non-semantic local vector-ref payload for a unit."""

    image_path = Path(image_path)
    sha256 = image_sha256 or _sha256_file(image_path)
    vector = local_image_feature_vector(image_path)
    return {
        "schema_version": LOCAL_VECTOR_SCHEMA_VERSION,
        "model": model,
        "semantic": False,
        "indexed": True,
        "index_status": "local_deterministic_content_features",
        "entries": [
            {
                "vector_id": f"vl:{sha256[:16]}",
                "unit_id": _text(unit_id),
                "model": model,
                "semantic": False,
                "embedding": None,
                "vector": vector,
                "image_sha256": sha256,
                "indexed": True,
                "index_status": "local_deterministic_content_features",
            }
        ],
    }


def attach_local_vector_refs(
    unit: Mapping[str, Any],
    image_path: str | Path,
) -> dict[str, Any]:
    """Attach local non-semantic vector refs when none are already present."""

    updated = dict(unit)
    vector_refs = dict(updated.get("vector_refs") or {})
    existing_entries = [
        dict(entry)
        for entry in (vector_refs.get("entries") or [])
        if isinstance(entry, Mapping)
    ]
    if existing_entries:
        # Preserve the caller-provided index status: only mark the unit
        # indexed when an entry actually declares itself indexed.
        vector_refs["indexed"] = any(
            entry.get("indexed") is True for entry in existing_entries
        )
        updated["vector_refs"] = vector_refs
        return updated
    local = build_local_vector_refs(
        image_path,
        unit_id=str(updated.get("unit_id") or ""),
        image_sha256=str(
            (updated.get("hashes") or {}).get("image_sha256") or ""
        ),
    )
    vector_refs["entries"] = list(local["entries"])
    vector_refs["indexed"] = True
    vector_refs["model"] = local["model"]
    vector_refs["semantic"] = False
    vector_refs["index_status"] = local["index_status"]
    updated["vector_refs"] = vector_refs
    return updated


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left = list(left)
    right = list(right)
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left)) or 1.0
    norm_right = math.sqrt(sum(b * b for b in right)) or 1.0
    return round(max(0.0, min(1.0, dot / (norm_left * norm_right))), 6)


class LocalVisualIndex:
    """Deterministic, hash-backed local visual index over durable units."""

    def __init__(
        self,
        entries: Iterable[Mapping[str, Any]] = (),
        *,
        schema_version: str = LOCAL_INDEX_SCHEMA_VERSION,
    ) -> None:
        self.schema_version = schema_version
        self.entries: dict[str, dict[str, Any]] = {}
        for entry in entries:
            self.add(entry)

    @classmethod
    def from_units(
        cls,
        units: Iterable[Mapping[str, Any]],
    ) -> "LocalVisualIndex":
        entries: list[dict[str, Any]] = []
        for unit in units:
            vector_refs = unit.get("vector_refs") if isinstance(
                unit, Mapping
            ) else {}
            if not isinstance(vector_refs, Mapping):
                continue
            for entry in vector_refs.get("entries") or []:
                if isinstance(entry, Mapping) and entry.get("indexed"):
                    entries.append(dict(entry))
        return cls(entries)

    def add(self, entry: Mapping[str, Any]) -> None:
        vector_id = _text(entry.get("vector_id"))
        if not vector_id:
            return
        self.entries[vector_id] = dict(entry)

    def retrieve(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int = 5,
        require_semantic: bool = False,
    ) -> dict[str, Any]:
        """Local retrieval; never claims semantic embeddings when absent."""

        if require_semantic:
            return {
                "status": "no_semantic_embeddings",
                "semantic": False,
                "matches": [],
                "note": (
                    "only local deterministic content features are indexed; "
                    "no semantic embedding model is present"
                ),
            }
        scored = [
            (
                _cosine(query_vector, entry.get("vector") or []),
                vector_id,
                entry,
            )
            for vector_id, entry in self.entries.items()
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        matches = [
            {
                "vector_id": vector_id,
                "unit_id": _text(entry.get("unit_id")),
                "image_sha256": _text(entry.get("image_sha256")),
                "score": score,
                "semantic": False,
                "model": _text(entry.get("model")) or LOCAL_VECTOR_MODEL,
            }
            for score, vector_id, entry in scored[: max(1, int(top_k))]
            if score > 0.0
        ]
        return {
            "status": "local_content_features",
            "semantic": False,
            "matches": matches,
            "note": (
                "deterministic local content features; not semantic "
                "embeddings"
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "semantic": False,
            "entry_count": len(self.entries),
            "entries": [
                dict(entry) for entry in sorted(
                    self.entries.values(),
                    key=lambda entry: str(entry.get("vector_id") or ""),
                )
            ],
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "LocalVisualIndex":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = raw.get("entries") if isinstance(raw, dict) else []
        return cls(entries)


__all__ = [
    "FEATURE_DIM",
    "LOCAL_INDEX_SCHEMA_VERSION",
    "LOCAL_VECTOR_MODEL",
    "LOCAL_VECTOR_THUMBNAIL_MAX",
    "LOCAL_VECTOR_SCHEMA_VERSION",
    "LocalVisualIndex",
    "attach_local_vector_refs",
    "build_local_vector_refs",
    "local_image_feature_vector",
]
