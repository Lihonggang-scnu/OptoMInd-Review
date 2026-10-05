from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any


SELECTED_IDS = ("P0289", "P0585", "P0085", "P0388")
ISSUE_IDS = ("R1", "R2", "R3")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the approved publication staging package without modifying source outputs.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    # Preserve this rerunnable exporter while clearing only its prior generated package.
    for child in list(out.iterdir()):
        if child.name == Path(__file__).name:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    copied: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []

    def copy_file(source: Path, dest_rel: str, *, kind: str = "copied") -> None:
        dest = out / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        copied.append({"path": dest_rel.replace("\\", "/"), "source": rel(root, source), "kind": kind})

    def copy_text(source: Path, dest_rel: str, *, kind: str = "copied") -> None:
        copy_file(source, dest_rel, kind=kind)

    def omit(source: Path, reason: str, *, field: str | None = None, source_kind: str = "local_artifact") -> None:
        item: dict[str, Any] = {
            "source": rel(root, source) if source.is_relative_to(root) else str(source),
            "bytes": source.stat().st_size if source.exists() else None,
            "sha256": sha256_file(source) if source.is_file() else None,
            "reason": reason,
            "source_kind": source_kind,
        }
        if field:
            item["field"] = field
        omissions.append(item)

    # Root-level experimental evidence and records.
    for name in (
        "ROOT_EXPERIMENT_PLAN.md", "ROOT_EXPERIMENT_REVIEW.md", "ROOT_EXPERIMENT_RESULT.json", "ROOT_COST_PREVIEW.json",
        "ROOT_OFFLINE_READINESS.md", "ROOT_FIXED_ISSUES.json", "ROOT_METHOD_REVIEW_FIRST.md",
    ):
        source = root / name
        if source.exists():
            copy_file(source, "METHOD_REVIEW_FIRST.md" if name == "ROOT_METHOD_REVIEW_FIRST.md" else f"experiment/{name}")

    records = root / "records"
    for source in sorted(records.iterdir()):
        if source.is_file():
            copy_file(source, f"experiment/records/{source.name}")

    # The actual source state is represented by the immutable snapshot and two-file diff.
    for name in ("ROOT_SOURCE_SNAPSHOT.json", "local_source_patch.diff"):
        source = records / name
        if source.exists():
            copy_file(source, f"source/{name}")

    # Preserve the exact configs used by A/B/C, without copying the worktree.
    config_root = root / "worktree" / "config" / "post_body_revision"
    for variant in "ABC":
        source = config_root / f"{variant}.json"
        copy_file(source, f"source/config/{variant}.json")

    case_path = root / "ROOT_FIXED_CASE.json"
    case = json.loads(case_path.read_text(encoding="utf-8"))
    blocks = {block.get("block_id"): block for block in case.get("blocks", []) if isinstance(block, dict)}
    issues = [issue for issue in case.get("known_fixed_issues", []) if issue.get("issue_id") in ISSUE_IDS]
    selected_materials: dict[str, Any] = {}
    selected_identities: dict[str, Any] = {}
    material_omissions: list[dict[str, Any]] = []
    material_text_provenance: dict[str, Any] = {}
    for source_id in SELECTED_IDS:
        material = case.get("materials", {}).get(source_id, {})
        material_copy = dict(material)
        material_text = material.get("text") if isinstance(material, dict) else None
        content_fields = material.get("content_fields") if isinstance(material, dict) else None
        if isinstance(material_text, str) and isinstance(content_fields, (dict, list)):
            try:
                serialized_fields = json.loads(material_text)
            except json.JSONDecodeError:
                serialized_fields = None
            if serialized_fields == content_fields:
                material_text_provenance[source_id] = {
                    "field": f"materials.{source_id}.text",
                    "status": "retained",
                    "kind": "generated_content_fields_serialization",
                    "chars": len(material_text),
                    "sha256": sha256_bytes(material_text.encode("utf-8")),
                    "note": "Exact JSON serialization of content_fields; not paper fulltext.",
                }
            else:
                material_copy.pop("text", None)
                material_omissions.append({
                    "source_id": source_id,
                    "field": f"materials.{source_id}.text",
                    "chars": len(material_text),
                    "sha256": sha256_bytes(material_text.encode("utf-8")),
                    "reason": "text did not verify as content_fields serialization; omitted pending rights review",
                })
        selected_materials[source_id] = material_copy
        identity = dict(case.get("source_identity_map", {}).get(source_id, {}))
        original = dict(identity.get("original_identity", {}))
        card_path = original.pop("card_path", None)
        if card_path:
            original["card_path_sha256"] = sha256_bytes(str(card_path).encode("utf-8"))
            original["card_path_public"] = "LOCAL_PATH_OMITTED"
        identity["original_identity"] = original
        selected_identities[source_id] = identity
        if isinstance(material, dict) and isinstance(material.get("text"), str):
            material_omissions.append({
                "source_id": source_id,
                "field": f"materials.{source_id}.text",
                "chars": len(material["text"]),
                "sha256": sha256_bytes(material["text"].encode("utf-8")),
                "reason": "rights-unclear reading-card text omitted; structured generated summary fields retained",
            })

    material_slice = {
        "case_id": case.get("case_id"),
        "base_sha256": case.get("base_sha256"),
        "draft_text_sha256": sha256_bytes(case.get("draft_text", "").encode("utf-8")),
        "draft_text_chars": len(case.get("draft_text", "")),
        "known_fixed_issues": issues,
        "issue_target_blocks": {issue.get("target_block_id"): blocks.get(issue.get("target_block_id")) for issue in issues},
        "research_question": case.get("research_question"),
        "scope": case.get("scope"),
        "outline": case.get("outline"),
        "selected_materials": selected_materials,
        "selected_text_provenance": material_text_provenance,
        "selected_source_identities": selected_identities,
        "inventory_counts": {"materials": len(case.get("materials", {})), "source_identity_map": len(case.get("source_identity_map", {}))},
        "inventory_note": "536 material snapshots and 609 identity entries are inventory counts, not citation counts.",
    }
    write_json(out / "materials" / "selected_issue_materials.json", material_slice)
    write_json(out / "materials" / "selected_text_omissions.json", material_omissions)
    omit(case_path, "full 13 MB fixed case omitted; one selected issue/material projection is staged", source_kind="full_case")
    prepared = root / "prepared" / "real_case_from_final_plan.json"
    if prepared.exists():
        omit(prepared, "duplicate prepared case; hashes and selected projection retained", source_kind="duplicate_case")

    run_specs = {
        "A_first": root / "live" / "A" / "live",
        "A_retry": root / "live_attempt2" / "A" / "live",
        "B_original": root / "live" / "B" / "live",
        "B_recovery": root / "live_B_retry" / "B" / "recovery",
        "C": root / "live" / "C" / "live",
    }
    actual_call_sources: list[tuple[str, Path]] = []
    for label, run_dir in run_specs.items():
        report_path = run_dir / "report.json"
        if report_path.exists():
            copy_file(report_path, f"runs/{label}/report.json")
        candidate = run_dir / "candidate.md"
        if candidate.exists():
            copy_file(candidate, f"runs/{label}/candidate.md")
        baseline = run_dir / "baseline.md"
        if baseline.exists():
            copy_file(baseline, "runs/baseline.md", kind="shared_baseline")
        manifest = run_dir / "manifest.json"
        if manifest.exists():
            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            report_value = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            projection = {
                "source_manifest_sha256": sha256_file(manifest),
                "source_manifest_bytes": manifest.stat().st_size,
                "source_manifest_path": rel(root, manifest),
                "variant": report_value.get("variant"),
                "execution_mode": report_value.get("execution_mode", report_value.get("generation_mode")),
                "case_id": report_value.get("case_id"),
                "base_sha256": report_value.get("base_sha256"),
                "case_fingerprint": report_value.get("case_fingerprint"),
                "evidence_fingerprint": report_value.get("evidence_fingerprint"),
                "report_sha256": sha256_file(report_path) if report_path.exists() else None,
                "config": manifest_value.get("config", {}),
                "public_projection_note": "Original manifest contains duplicated full case; this projection preserves run identity/config and its source hash.",
            }
            write_json(out / f"runs/{label}/manifest_projection.json", projection)
            omit(manifest, "duplicate manifest embeds the full fixed case; manifest projection and source SHA retained", source_kind="duplicate_manifest")
        calls_dir = run_dir / "calls"
        if calls_dir.exists():
            for source in sorted(calls_dir.glob("*.json")):
                # B recovery has four saved replays; retain the original B files and only the new boundary call here.
                if label == "B_recovery" and source.name != "issue_003_verifier.json":
                    continue
                actual_call_sources.append((label, source))
                copy_file(source, f"runs/{label}/calls/{source.name}", kind="actual_call_record")
    replay_map = {
        "source_run": "B_original",
        "recovery_run": "B_recovery",
        "saved_replay_calls": [
            {"stage_id": stage, "source": f"runs/B_original/calls/{stage}.json", "new_provider_call": False, "new_cost_cny": 0.0}
            for stage in ("issue_001_author", "issue_001_verifier", "issue_002_author", "issue_003_author")
        ],
        "new_provider_call": {"stage_id": "issue_003_verifier", "source": "runs/B_recovery/calls/issue_003_verifier.json", "new_provider_call": True},
        "note": "Four replay call files are not duplicated; original B raw records remain the provenance source.",
    }
    write_json(out / "runs/B_recovery/replay_provenance.json", replay_map)
    write_json(out / "runs/actual_call_accounting.json", {
        "actual_attempt_count": len(actual_call_sources),
        "attempts": [{"run": label, "source": rel(root, source), "staged": f"runs/{label}/calls/{source.name}", "sha256": sha256_file(source), "bytes": source.stat().st_size} for label, source in actual_call_sources],
        "replay_note": "B has four saved-response replays represented by replay_provenance.json and the original B call files; they are not additional provider attempts.",
    })

    # Evaluation originals, consistency correction, decode key, constructor, and reports.
    eval_root = root / "evaluation"
    for name in ("judgments_primary.json", "judgments_primary_consistent.json", "manifest.json", "decode_key.json", "assessment_schema.json", "JUDGE_GUIDE.md", "INDEPENDENT_REVIEW_LIMITATIONS.md"):
        source = eval_root / name
        if source.exists():
            copy_file(source, f"evaluation/{name}")
    for source in sorted((eval_root / "report_offline").glob("*.json")):
        copy_file(source, f"evaluation/report_offline/{source.name}")
    constructor = root / "worktree" / "scripts" / "upgrade3" / "evaluate_post_body_revision.py"
    copy_file(constructor, "evaluation/packet_constructor.py", kind="packet_constructor")
    packets = eval_root / "packets"
    if packets.exists():
        omit(packets, "66 MB duplicate blinded packets omitted; manifest/decode key/constructor retained", source_kind="duplicate_evaluation_packets")

    native = root / "native"
    for name in ("b_retry_driver.py", "post_body_revision_tests_after_fixture_patch.stdout.txt", "live_B_retry_offline_proof.json"):
        source = native / name
        if source.exists():
            copy_file(source, f"experiment/native/{name}", kind="native_acceptance_evidence")

    # No PDFs or full paper files are copied. The selected text fields are recorded above.
    for source in root.rglob("*.pdf"):
        if "publication_staging" not in source.parts:
            omit(source, "paper/PDF artifact prohibited from public staging", source_kind="copyrighted_or_rights_unclear")
    for item in material_omissions:
        omissions.append({"source": "ROOT_FIXED_CASE.json", **item, "source_kind": "rights_unclear_material_text"})
    write_json(out / "omissions.json", {"omissions": omissions, "count": len(omissions)})

    README = """# BODY revision A/B/C publication staging\n\n**Read first: [METHOD_REVIEW_FIRST.md](METHOD_REVIEW_FIRST.md)**\n\nThis is a public-review projection of the local, fixed-three-issue experiment. It is not a new algorithm, a new outline/plan validation, a full BODY regeneration, or a scientific gold standard. Source HEAD is `abc97d5713904d8acc000f1f576c4785b4fa2ef7`; the only source diff is the generic fixed-issue protocol paragraph plus the two Windows fixture byte writes, captured in `source/local_source_patch.diff`.\n\n## Reading order\n\n1. `METHOD_REVIEW_FIRST.md`\n2. `source/ROOT_SOURCE_SNAPSHOT.json`, `source/local_source_patch.diff`, and `source/config/`\n3. `experiment/ROOT_EXPERIMENT_PLAN.md`, `experiment/records/LIVE_COMMANDS_AND_RECOVERY.md`, and `experiment/records/FINAL_EXECUTION_CHECK.json`\n4. `materials/selected_issue_materials.json` and `materials/selected_text_omissions.json`\n5. `runs/actual_call_accounting.json`, then the five run reports/candidates and their call records\n6. `evaluation/` originals, consistent judgments, reports, and packet-constructor source\n7. `MANIFEST_SHA256.json`, `omissions.json`, and `SECRET_SCAN.json`\n\n## Experiment identity and cost\n\nThe package retains all five baseline/candidate texts, 15 actual provider-attempt records, B replay provenance, A/B/C configurations, ledger/cost records, evaluation originals, and selected structured issue material. A first attempt and one retry are retained; B has one verifier-only recovery; C did not use its stronger-model escalation. Known settled cost is 0.2785574 CNY with 0.2137224 CNY uncertain/held. B recovery reports 0.006509 CNY as incremental recovery cost; full known B cost is 0.0484344 CNY plus the held uncertainty. No model call was made during export.\n\nThe 15 staged call JSON files are byte-for-byte copies of the actual provider-attempt records, including original `messages`, parsed responses, raw response envelopes, model/status/usage/cost fields, and hashes. The four B saved-response replays are represented by `runs/B_recovery/replay_provenance.json` and the original B call files, so they are not duplicated as new attempts. The call records are generated experiment evidence; they may contain the selected supplied material embedded in actual requests. No HTTP authorization header or API key value is present.\n\nThe selected-material projection keeps issue anchors, source identities, titles/DOIs/years, and structured generated summary fields for P0289/P0585/P0085/P0388. Each retained `materials.*.text` field was verified to be an exact JSON serialization of its `content_fields`, so it is generated summary material rather than paper fulltext; provenance is recorded in `materials/selected_issue_materials.json`. Full fixed-case copies, duplicate run manifests, the 66 MB packet set, paper PDFs/fulltext, and any text that fails that generated-summary check are omitted and listed in `omissions.json`.\n\n`SECRET_SCAN.json` reports categories and relative locations only. Its credential-path hits are local key-file path provenance in commands/records; the exporter never opened or copied credentials.\n"""
    (out / "README.md").write_text(README, encoding="utf-8", newline="\n")

    # Secret/signature scan: report categories and locations only, never matched values.
    patterns = {
        "private_key_pem": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "authorization_header": re.compile(rb"(?i)authorization\s*[:=]|bearer\s+[A-Za-z0-9._-]{16,}"),
        "api_key_assignment": re.compile(rb"(?i)(?:api[_-]?key|secret[_-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9._-]{16,}"),
        "signed_url_query": re.compile(rb"(?i)[?&](?:signature|sig|access_token|x-amz-signature|token)="),
        "credential_file_path": re.compile(rb"(?i)(?:api_keys?|qwen-api-key|secret[_-]?key)"),
        "secretlike_prefix": re.compile(rb"(?i)(?:sk-[A-Za-z0-9]{20,}|dashscope-[A-Za-z0-9]{20,})"),
    }
    scan_hits: dict[str, list[str]] = {name: [] for name in patterns}
    for source in out.rglob("*"):
        if not source.is_file() or source.name in {"SECRET_SCAN.json", "export_publication_package.py"}:
            continue
        data = source.read_bytes()
        for name, pattern in patterns.items():
            if pattern.search(data):
                scan_hits[name].append(rel(out, source))
    positive_controls = {
        "authorization_header": b"Authorization: Bearer dummy-token-value-123456",
        "api_key_assignment": b'api_key = "dummy-key-value-1234567890"',
        "signed_url_query": b"https://example.invalid/file?signature=dummy",
        "private_key_pem": b"-----BEGIN PRIVATE KEY-----",
    }
    positive_control_results = {name: bool(patterns[name].search(sample)) for name, sample in positive_controls.items()}
    if not all(positive_control_results.values()):
        raise RuntimeError("secret_scan_positive_control_failed")
    write_json(out / "SECRET_SCAN.json", {
        "scanned_files": sum(1 for p in out.rglob("*") if p.is_file() and p.name != "SECRET_SCAN.json"),
        "hits_by_category": {name: {"count": len(paths), "paths": paths[:100]} for name, paths in scan_hits.items()},
        "positive_controls": {"all_passed": True, "categories": sorted(positive_control_results), "dummy_values_written": False},
        "values_printed": False,
        "interpretation": "credential_file_path may identify local key-path text in command provenance; secretlike_prefix is classified separately because generated dr-task identifiers can match a broad prefix regex.",
    })

    entries = []
    for source in sorted(out.rglob("*")):
        if source.is_file() and source.name != "MANIFEST_SHA256.json":
            entries.append({"path": rel(out, source), "bytes": source.stat().st_size, "sha256": sha256_file(source)})
    write_json(out / "MANIFEST_SHA256.json", {
        "package_root": str(out),
        "file_count_excluding_manifest": len(entries),
        "total_bytes_excluding_manifest": sum(item["bytes"] for item in entries),
        "entries": entries,
        "source_experiment_root": str(root),
        "source_head": "abc97d5713904d8acc000f1f576c4785b4fa2ef7",
        "actual_provider_attempts": 15,
        "new_calls_during_export": 0,
    })
    print(json.dumps({"status": "exported", "file_count": len(entries), "bytes": sum(item["bytes"] for item in entries), "largest": max(entries, key=lambda item: item["bytes"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
