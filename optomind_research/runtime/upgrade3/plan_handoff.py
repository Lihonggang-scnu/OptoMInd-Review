from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

SCHEMA = 'optomind.plan_handoff.v2'


def json_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    with Path(path).open(encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('handoff_object_required:' + str(path))
    return value


def validate_plan_handoff(root, *, manifest_path=None, pointer=None):
    root = Path(root).resolve()
    pointer = pointer or {}
    if 'manifest' in pointer and (not isinstance(pointer['manifest'], str) or not pointer['manifest'].strip()):
        raise ValueError('handoff_manifest_path_required')
    if pointer.get('schema_version') == SCHEMA and 'manifest' not in pointer:
        raise ValueError('handoff_manifest_path_required')
    manifest_path = Path(manifest_path).resolve() if manifest_path else root / 'HANDOFF_MANIFEST.json'
    explicit = bool(pointer.get('manifest')) or pointer.get('schema_version') == SCHEMA
    if not manifest_path.is_file():
        if explicit:
            raise ValueError('handoff_manifest_missing:' + str(manifest_path))
        return []
    manifest = _read(manifest_path)
    if manifest_path.parent != root:
        raise ValueError('handoff_manifest_root_mismatch')
    declared_root = manifest.get('packet_root')
    if not isinstance(declared_root, str) or not declared_root:
        raise ValueError('handoff_packet_root_required')
    bound_root = Path(declared_root)
    bound_root = bound_root if bound_root.is_absolute() else manifest_path.parent / bound_root
    if bound_root.resolve() != root:
        raise ValueError('handoff_packet_root_mismatch')
    version = manifest.get('schema_version')
    if version not in (None, SCHEMA):
        raise ValueError('handoff_schema_unsupported')
    if pointer.get('schema_version') == SCHEMA and version != SCHEMA:
        raise ValueError('handoff_schema_downgrade')
    if pointer.get('revision') is not None and pointer['revision'] != manifest.get('revision'):
        raise ValueError('handoff_revision_mismatch')
    expected_manifest = pointer.get('manifest_sha256')
    if pointer.get('schema_version') == SCHEMA and not expected_manifest:
        raise ValueError('handoff_manifest_hash_required')
    if expected_manifest is not None and file_hash(manifest_path) != expected_manifest:
        raise ValueError('handoff_manifest_hash_mismatch')
    plan_path = root / 'DETAILED_REVIEW_PLAN.json'
    plan = _read(plan_path)
    expected_plan = manifest.get('plan_sha256')
    if version == SCHEMA and not expected_plan:
        raise ValueError('handoff_plan_hash_required')
    if expected_plan is not None and json_hash(plan) != expected_plan:
        raise ValueError('handoff_plan_hash_mismatch')
    rows = plan.get('writer_packets')
    if not isinstance(rows, list) or not rows:
        raise ValueError('handoff_writer_packets_required')
    chapter_ids = [row.get('chapter_id') if isinstance(row, Mapping) else None for row in rows]
    if (any(not isinstance(ch, str) or not ch or Path(ch).name != ch or any(c in ch for c in '/\\:') for ch in chapter_ids)
            or len(set(chapter_ids)) != len(chapter_ids)):
        raise ValueError('handoff_chapter_identity_invalid')
    if manifest.get('chapter_ids') != chapter_ids:
        raise ValueError('handoff_chapter_scope_mismatch')
    hashes = manifest.get('packet_sha256')
    if not isinstance(hashes, dict) or set(hashes) != set(chapter_ids):
        raise ValueError('handoff_packet_hash_scope_mismatch')
    paths = [manifest_path, plan_path]
    for row, chapter_id in zip(rows, chapter_ids):
        path = root / 'writer_packets' / (chapter_id + '.json')
        declared = Path(row.get('json_path') or ('writer_packets/' + chapter_id + '.json'))
        declared = declared if declared.is_absolute() else root / declared
        if declared.resolve() != path.resolve():
            raise ValueError('handoff_packet_path_mismatch:' + chapter_id)
        packet = _read(path)
        if json_hash(packet) != hashes[chapter_id]:
            raise ValueError('handoff_packet_hash_mismatch:' + chapter_id)
        actual_id = packet.get('chapter_id') or (packet.get('chapter') or {}).get('chapter_id')
        if actual_id and actual_id != chapter_id:
            raise ValueError('handoff_packet_identity_mismatch:' + chapter_id)
        paths.append(path)
    return paths
