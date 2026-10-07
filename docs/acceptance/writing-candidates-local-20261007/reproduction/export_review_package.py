"""Export a reviewable public projection; never publish paper full text or a ledger DB."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "worktree/docs/acceptance/writing-candidates-local-20261007"
PRIVATE_FIELDS = {
    "card_material", "deep_read_material", "deep_read_material_trimmed",
    "supplement_material", "supplement_materials", "chapter_tool_materials",
    "local_excerpt", "local_excerpts", "source_text", "full_text", "raw_text",
    "deep_read", "tool_materials", "tool_supplement", "tool_supplements",
}
OMISSIONS: list[dict] = []
EXPORTED: list[dict] = []


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def projection(value, origin: str, pointer: str = ""):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            pos = f"{pointer}/{key}"
            if key in PRIVATE_FIELDS and item:
                data = json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
                record = {"origin": origin, "json_pointer": pos,
                          "reason": "paper-derived reading/excerpt: rights not established for public release",
                          "serialized_bytes": len(data), "serialized_sha256": sha(data)}
                OMISSIONS.append(record)
                out[key] = {"public_omission": True, **record}
            elif key in {"key_file", "key_path", "api_key", "authorization", "password", "access_token", "refresh_token"}:
                OMISSIONS.append({"origin": origin, "json_pointer": pos, "reason": "authentication field withheld"})
                out[key] = "<authentication field withheld>"
            elif key in {"raw_response", "normalized_response", "normalized_response_json"}:
                # Transport wrappers duplicate text and can contain request bodies.
                out[key] = {"public_omission": True, "reason": "duplicate transport wrapper; model text exported separately"}
            elif key == 'content' and isinstance(item, str):
                try:
                    embedded_payload = json.loads(item)
                except (ValueError, TypeError):
                    out[key] = item
                else:
                    out[key] = json.dumps(projection(embedded_payload, origin, pos), ensure_ascii=False, indent=2)
            else:
                out[key] = projection(item, origin, pos)
        return out
    if isinstance(value, list):
        return [projection(v, origin, f"{pointer}/{i}") for i, v in enumerate(value)]
    return value


def write(relative: str, value, *, origin: Path | None = None, purpose: str = ""):
    path = DEST / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8") if not isinstance(value, (str, bytes)) else (value.encode("utf-8") if isinstance(value, str) else value)
    path.write_bytes(data)
    record = {"path": relative, "purpose": purpose, "sha256": sha(data), "bytes": len(data)}
    if origin:
        record.update(local_origin=str(origin), original_file_sha256=sha(origin.read_bytes()),
                      same_bytes=origin.read_bytes() == data)
    EXPORTED[:] = [previous for previous in EXPORTED if previous['path'] != relative]
    EXPORTED.append(record)


def json_projection(origin: Path, relative: str, purpose: str):
    write(relative, projection(load(origin), str(origin)), origin=origin, purpose=purpose)


def export_messages(origin: Path, relative: str):
    messages = load(origin)
    exported = []
    for i, msg in enumerate(messages):
        item = dict(msg)
        content = item.get("content")
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except (ValueError, TypeError):
                pass  # system instructions are original source, no paper text.
            else:
                item["content"] = json.dumps(projection(payload, str(origin), f"/{i}/content"), ensure_ascii=False, indent=2)
        exported.append(item)
    write(relative, {"projection_only": True, "not_replayable_as_exact_request": True,
                     "original_messages_file_sha256": sha(origin.read_bytes()), "messages": exported},
          origin=origin, purpose="Actual prompt and payload public projection; full scientific request remains local")


def export_route(name: str):
    source = ROOT / name
    if not source.exists():
        return
    for filename in ["CLI_CONTEXT.json", "CLI_RUN.json"]:
        original = source / filename
        if original.exists():
            json_projection(original, f"runs/{name}/{filename}", "Actual official CLI context and result; authentication fields withheld")
    for original in sorted((source / "cli_invocations").rglob("*.json")):
        json_projection(original, f"runs/{name}/cli_invocations/" + str(original.relative_to(source / "cli_invocations")).replace('\\', '/'), "Preserved CLI invocation history; separate attempts and configuration")
    for path in sorted(source.glob("stages/*/*/attempt_*")):
        label = path.parent.parent.name + "/" + path.parent.name + "/" + path.name
        prefix = f"runs/{name}/{label}"
        if (path / "MESSAGES.json").exists():
            export_messages(path / "MESSAGES.json", prefix + "/MESSAGES_PUBLIC_PROJECTION.json")
        for filename in ["ACTUAL_REQUEST.json", "REQUEST.json", "USAGE.json", "ERROR.json", "RESULT.json"]:
            if (path / filename).exists():
                json_projection(path / filename, prefix + "/" + filename, "Effective parameters, request signature, capacity/usage")
        if (path / "RAW_RESPONSE.json").exists():
            original = path / "RAW_RESPONSE.json"
            raw = load(original)
            allowed = ["content", "reasoning_content", "requested_model", "returned_model", "finish_reason", "complete",
                       "request_id", "usage", "status_code", "call_id", "attempt", "elapsed_seconds", "effective_request",
                       "raw_response_sha256", "raw_stream_sha256", "stream_event_count", "execution_mode"]
            write(prefix + "/MODEL_RETURN.json", {"original_file_sha256": sha(original.read_bytes()),
                  "export_kind": "original model content and metadata; duplicate transport wrappers omitted",
                  **{key: raw[key] for key in allowed if key in raw}}, origin=original,
                  purpose="Exact decoded model content, including malformed JSON if returned; no manual science edits")
        if (path / "BODY.md").exists():
            original = path / "BODY.md"
            write(prefix + "/BODY.md", original.read_bytes(), origin=original, purpose="Original parsed stage body")
    for run in sorted(source.glob("runs/*")):
        for original in sorted((run / "plans").rglob("*.json")):
            relative = f"runs/{name}/{run.name}/plans/" + str(original.relative_to(run / "plans")).replace('\\', '/')
            if original.name == "MESSAGES.json":
                export_messages(original, relative.replace("MESSAGES.json", "MESSAGES_PUBLIC_PROJECTION.json"))
            else:
                json_projection(original, relative, "Offline preparation/preview, not an executed paid stage; paper-text fields withheld")
        for filename in ["DRAFT_BODY.md", "CHAPTER_BODY.md", "DRAFT_RESULT.json", "CHAPTER_RESULT.json", "IMPLEMENTATION_REPORT.json", "RUN_MANIFEST.json"]:
            origin = run / filename
            if not origin.exists():
                continue
            relative = f"runs/{name}/{run.name}/{filename}"
            if filename.endswith(".md"):
                write(relative, origin.read_bytes(), origin=origin, purpose="Untouched run draft/selected prose")
            else:
                json_projection(origin, relative, "Run result, diagnostics, implementation and lineage")


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    previous_manifest = DEST / "MANIFEST.json"
    if previous_manifest.exists():
        # Only replace our own unchanged prior exports. Resolve each exact file
        # within this task's public directory before deleting it.
        for record in load(previous_manifest).get("files", []):
            if record["path"] in {"README.md", "REPRODUCTION.md", ".gitattributes"}:
                continue
            old = (DEST / record["path"]).resolve()
            if not old.is_relative_to(DEST.resolve()):
                raise ValueError("Prior manifest path escapes public directory")
            if old.is_file() and sha(old.read_bytes()) == record.get("sha256"):
                old.unlink()
    for filename in ["USER_SCOPE.md", "PREPARED.md", "ROOT_REVIEW_A.md", "ROOT_REVIEW_B.md", "ROOT_REVIEW_PLUS_REASONING.md", "ROOT_REVIEW_MAX.md", "FINAL_REVIEW.md", "offline_acceptance.log"]:
        original = ROOT / filename
        if original.exists():
            write("records/" + filename, original.read_bytes(), origin=original, purpose="Human authorization or independent review; never model input")
    for filename in ["ROUND_BUDGET_START.json", "PROJECTION_CHECK.json", "FINAL_BUDGET.json", "ROUND_FINAL_FINANCE_CORRECTION.json", "QUALITY_TRACE.json", "ROOT_ACCEPTANCE.json", "COMPARISON_PROOF.json"]:
        original = ROOT / filename
        if original.exists():
            json_projection(original, "records/" + filename, "Budget baseline or full-field input check")
    original = ROOT / "FINAL_SELECTED_CHAPTER.md"
    if original.exists():
        write("FINAL_SELECTED_CHAPTER.md", original.read_bytes(), origin=original, purpose="Byte-exact selected original model output; not manually improved")
    for filename in ["export_review_package.py", "final_budget_readonly.py"]:
        original = ROOT / filename
        write("reproduction/" + filename, original.read_bytes(), origin=original, purpose="Local archival/accounting helper source; paths refer to the preserved original local run root")
    original = next((ROOT / "chapter_live/inputs").glob("*/CHAPTER_INPUT.json"))
    json_projection(original, "inputs/CHAPTER_INPUT_PUBLIC_PROJECTION.json", "Adopted complete chapter/tasks and model-generated A/B summaries; reading text omitted by hash")
    for route in ["chapter_live", "units_edit_live", "units_edit_live_after_timeout_fix", "units_reasoning_edit_live", "selective_max_live", "second_chapter_live", "chapter_preview", "units_edit_preview", "hierarchical_preview", "units_edit_plus_reasoning_preview"]:
        export_route(route)
    for name in ["B_STOP_REPORT.json", "B_STOP_RECORD.json", "UNIT_002_PARTIAL_INSPECTION.json", "UNIT_002_PARTIAL_RESULT.json", "UNIT_002_PARTIAL_BODY.md", "B_STOP_FINANCE_CORRECTION.json", "B_LEDGER_CORRECTION.json", "TIMEOUT_FIX_VERIFICATION.json"]:
        original = ROOT / "units_edit_live" / name
        if original.exists():
            if original.suffix == ".json":
                json_projection(original, "runs/units_edit_live/cancelled/" + name, "Cancelled request/partial result; not a completed candidate")
            else:
                write("runs/units_edit_live/cancelled/" + name, original.read_bytes(), origin=original, purpose="Preserved partial prose; no completed model response")
    route_root = ROOT / "units_edit_live_after_timeout_fix"
    for pattern in ["*PREVIEW_EVIDENCE.json", "PLUS_REASONING_EDITOR_RESULT.json", "MAX_ESCALATION*.json", "MAX_MODERATE*.json", "*RESTORE*.json", "*RECOVERY*.json", "ROUND_FINAL_FINANCE_CORRECTION*.json"]:
        for original in sorted(route_root.glob(pattern)):
            json_projection(original, "records/execution/" + original.name, "Actual interface checks, recovery and escalation evidence; preserve original and correction separately")
    for original in sorted((ROOT / "offline_selected_assembly").glob("*")):
        if original.suffix == '.json':
            json_projection(original, "assembly/" + original.name, "Official selected-chapter consumer, offline and no prose rewriting")
        elif original.suffix == '.md':
            write("assembly/" + original.name, original.read_bytes(), origin=original, purpose="Untouched selected chapter consumed by the official adapter")
    for original in sorted((ROOT / "chapter_live/format_repair").glob("*")):
        if original.suffix not in {".json", ".md", ".py"}:
            continue
        relative = "runs/chapter_live/format_repair/" + original.name
        if original.suffix == ".json":
            json_projection(original, relative, "Saved paid response recovery/adoption; zero new calls")
        else:
            write(relative, original.read_bytes(), origin=original, purpose="Format-only replay or recovered prose")
    for filename in ["balanced.json", "plus_reasoning.json", "selective_max.json"]:
        original = ROOT / "worktree/config/writer_candidates" / filename
        write("profiles/" + filename, original.read_bytes(), origin=original, purpose="Available official profile; exported existence does not mean executed")
    for original in sorted((ROOT / "profiles").glob("*.json")):
        json_projection(original, "profiles/actual/" + original.name, "Actual local role composition; writer reuse kept exact")
    for original in sorted((ROOT / "units_edit_live_after_timeout_fix/profiles").glob("*.json")):
        json_projection(original, "profiles/actual/" + original.name, "Editor-only upgrade; balanced writers kept exact")
    for filename in ["README.md", "REPRODUCTION.md", ".gitattributes"]:
        path = DEST / filename
        if path.exists():
            EXPORTED.append({"path": filename, "purpose": "Public reading/reproduction guide", "sha256": sha(path.read_bytes()), "bytes": path.stat().st_size})
    # Manifest enumerates exported artifacts, and separately enumerates every removed paper-text field.
    (DEST / "MANIFEST.json").write_text(json.dumps({"schema_version": "optomind.public_acceptance_manifest.v1",
        "locked_cloud_source_sha": "fe2f1c2adde414e71341287845d0520325e602c6",
        "format_seam_source_sha": "14257f4274e1deeb77aa6d125447bfb2a5d4c7d3",
        "timeout_seam_source_sha": "32772fbe1a1d3de0969f4d2e27b0fd5cf725b939",
        "files": EXPORTED, "omissions": OMISSIONS,
        "rights": "No PDF/XML/HTML paper full text or ledger database exported. A/B are existing model-generated understanding/planning summaries. Public request projections are not exact replay inputs."},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Detect credential-like values, not ordinary token budgets or DOI URLs.
    secret = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{16,}|(?i:password|api_key|access_token)\s*[:=]\s*[\"'](?!<)[A-Za-z0-9._-]{12,}|[?&](?:X-Amz-Signature|signature|access_token)=[A-Za-z0-9%._-]{12,})")
    problems = []
    for path in DEST.rglob("*"):
        if path.is_file():
            for match in secret.finditer(path.read_text(encoding="utf-8")):
                problems.append({"path": str(path.relative_to(DEST)), "character_offset": match.start(), "matched_value": "withheld"})
    receipt = {"files_checked": sum(p.is_file() for p in DEST.rglob("*")),
               "public_bytes": sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file()),
               "credential_findings": problems, "large_files_gt_10MB": [str(p.relative_to(DEST)) for p in DEST.rglob("*") if p.is_file() and p.stat().st_size > 10_000_000]}
    (DEST / "PUBLICATION_CHECK.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if problems:
        raise SystemExit("Credential-like material requires review; do not publish")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
