"""Independent post-BODY revision experiments; offline preview is the default.

Never reads credentials or constructs a provider in preview/recording mode.
Paid execution requires two gates and a finite shared experiment-wide ledger.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
import sys
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SUPPORTED_MODELS = {"qwen3.7-flash", "qwen3.5-plus"}


def read_json(path: str | Path) -> Any:
    path = Path(path)
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return json.loads(data.decode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_config(config: dict[str, Any], variant: str) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("config_must_be_object")
    if set(config) & {"api_key", "key_file", "token", "password", "base_url"}:
        raise ValueError("credentials_and_provider_destination_not_allowed_in_config")
    result = copy.deepcopy(config)
    if result.get("variant", variant) != variant:
        raise ValueError("config_variant_mismatch")
    result["variant"] = variant
    settings = result.get("model_settings")
    if not isinstance(settings, dict) or not settings:
        raise ValueError("explicit_model_settings_required")
    for model, options in settings.items():
        if model not in SUPPORTED_MODELS or not isinstance(options, dict):
            raise ValueError("unsupported_model_configuration")
        maximum = 131072 if model == "qwen3.7-flash" else 65536
        output = options.get("max_output_tokens")
        thinking_budget = options.get("thinking_budget")
        if type(output) is not int or output < 64 or output > maximum:
            raise ValueError("invalid_output_limit")
        if type(thinking_budget) is not int or thinking_budget < 0:
            raise ValueError("invalid_thinking_budget")
        if type(options.get("thinking")) is not bool or type(options.get("json_mode")) is not bool:
            raise ValueError("explicit_thinking_and_json_mode_required")
        if output + (thinking_budget if options["thinking"] else 0) > maximum:
            raise ValueError("model_total_output_exceeded")
        if model == "qwen3.5-plus" and options["thinking"] and options["json_mode"]:
            raise ValueError("thinking_json_unsupported_for_model")
        timeout = options.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout < 5:
            raise ValueError("invalid_timeout")
        allowed = {"max_output_tokens", "thinking_budget", "thinking", "json_mode", "timeout_seconds"}
        if set(options) - allowed:
            raise ValueError("unsupported_provider_option")
    for field in ("max_input_chars", "max_material_index_chars", "max_issues", "max_target_chars"):
        value = result.get(field)
        if type(value) is not int or value < (0 if field == "max_issues" else 1):
            raise ValueError("invalid_" + field)
    models = result.get("models", {})
    if not isinstance(models, dict) or any(model not in settings for model in models.values()):
        raise ValueError("unconfigured_explicit_role_model")
    roles = {"review", "verifier"} | ({"author"} if variant != "A" else set()) | ({"escalation"} if variant == "C" else set())
    if any(not models.get(role) for role in roles):
        raise ValueError("required_role_model_missing")
    if variant == "C" and models.get("escalation") in {models.get("author"), models.get("verifier")}:
        raise ValueError("C_requires_distinct_escalation_model")
    return result


class RecordingClient:
    """Exact staged responses only; no generated success and no provider fallback."""
    def __init__(self, path: str | Path):
        self.execution_mode = "recording"
        self.recordings = read_json(path)
        self.calls: list[str] = []

    def __call__(self, stage_id: str, messages: Any, *, model: str, **kwargs: Any) -> Any:
        if stage_id not in self.recordings:
            raise ValueError("missing_recording_stage:" + stage_id)
        self.calls.append(stage_id)
        response = copy.deepcopy(self.recordings[stage_id])
        envelope = response if isinstance(response, dict) else {"content": response}
        return {**envelope, "cost_cny": 0.0, "cost_provenance": "offline_recording_no_provider_call"}


def make_live_client(args: argparse.Namespace, config: dict[str, Any]):
    """Lazy boundary; validation precedes provider import/ledger creation."""
    if not args.run or not args.allow_paid:
        raise ValueError("paid_execution_requires_run_and_allow_paid")
    if args.recordings:
        raise ValueError("recording_and_paid_modes_are_exclusive")
    if args.budget_cny is None or not math.isfinite(args.budget_cny) or args.budget_cny <= 0:
        raise ValueError("finite_positive_budget_cny_required")
    if not args.budget_ledger:
        raise ValueError("shared_budget_ledger_required")
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenDirectClient, estimated_cost_cny
    ledger = GlobalBudgetLedger(limit_cny=args.budget_cny, path=Path(args.budget_ledger))
    providers: dict[str, Any] = {}
    def call(stage_id: str, messages: Any, *, model: str, **kwargs: Any):
        if model not in config["model_settings"]:
            raise ValueError("unconfigured_explicit_model")
        if model not in providers:
            providers[model] = QwenDirectClient(
                model=model, key_file=args.key_file, max_retries=0, max_keys=1,
                budget_ledger=ledger, **config["model_settings"][model],
            )
        response = providers[model](messages, model=model,
                                    call_id="post-body-" + config["variant"] + "-" + stage_id + "-" + uuid.uuid4().hex[:12])
        response = dict(response)
        try:
            response["cost_cny"] = estimated_cost_cny(response.get("usage", {}), model=model)
            response["cost_provenance"] = "module4_configured_pricing_from_provider_usage"
        except Exception:
            response["cost_cny"] = None
            response["cost_provenance"] = "unknown_or_incomplete_provider_usage"
        return response
    call.execution_mode = "live"
    return call


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Preview, replay recordings, or explicitly authorize one paid variant")
    run.add_argument("--case", required=True)
    run.add_argument("--variant", required=True, choices=["A", "B", "C"])
    run.add_argument("--config", help="Default: config/post_body_revision/<variant>.json")
    run.add_argument("--output-root", default="outputs/post_body_revision")
    run.add_argument("--recordings", help="Offline stage-id to response JSON fixture")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--run", action="store_true", help="First explicit paid execution gate")
    run.add_argument("--allow-paid", action="store_true", help="Second explicit paid execution gate")
    run.add_argument("--budget-cny", type=float)
    run.add_argument("--budget-ledger", help="Same SQLite path and total cap across all variants")
    run.add_argument("--key-file", help="Local provider credential path; never copied to artifacts")
    prep = commands.add_parser("prepare", help="Offline conversion of BODY and an explicitly bounded material manifest")
    prep.add_argument("--body", required=True)
    prep.add_argument("--plan", help="Final-plan/writer-packet JSON or gzip JSON")
    prep.add_argument("--materials", "--material-manifest", dest="materials", help="Explicit bounded material JSON or gzip JSON")
    prep.add_argument("--output", required=True)
    return p



MATERIAL_FIELDS = (
    "study_summary_A", "review_planning_B", "deep_read_material", "deep_read_materials",
    "local_passages", "local_passages_variants", "supplement_gap_material",
    "supplement_gap_materials", "supplement_material", "supplement_materials",
    "tool_materials", "tool_supplement_materials", "usable_content", "material",
)



def project_plan_intent(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Literal allowlisted fine-outline projection, without rewriting or expansion."""
    unit_fields = ("unit_id", "id", "substantive_point", "ordered_development", "evidence_conditions",
                   "synthesis", "transition", "argument_relations")
    brief_fields = ("paragraph_id", "id", "point", "development", "source_handles", "finding_conditions", "evidence_conditions")
    contracts: dict[str, dict[str, Any]] = {}
    sources: dict[str, list[str]] = {}
    def collect(node: Any, location: str):
        if not isinstance(node, dict):
            raise ValueError("invalid_chapter_contract_node")
        cp = node.get("chapter_plan")
        if isinstance(cp, dict) and cp:
            chapter = node.get("chapter", {})
            chapter = chapter if isinstance(chapter, dict) else {}
            chapter_id = cp.get("chapter_id") or node.get("chapter_id") or chapter.get("chapter_id")
            if not isinstance(chapter_id, str) or not chapter_id:
                raise ValueError("fine_outline_chapter_id_required")
            item = {"chapter_id": chapter_id, **{k: copy.deepcopy(cp[k]) for k in ("thesis", "reader_objective") if k in cp}}
            units = cp.get("units", [])
            if not isinstance(units, list):
                raise ValueError("chapter_units_must_be_list:" + chapter_id)
            item["units"] = []
            for unit in units:
                if not isinstance(unit, dict):
                    raise ValueError("chapter_unit_must_be_object:" + chapter_id)
                projected = {k: copy.deepcopy(unit[k]) for k in unit_fields if k in unit}
                briefs = unit.get("paragraph_briefs", [])
                if not isinstance(briefs, list) or any(not isinstance(b, dict) for b in briefs):
                    raise ValueError("paragraph_briefs_must_be_object_list:" + chapter_id)
                if "paragraph_briefs" in unit:
                    projected["paragraph_briefs"] = [{k: copy.deepcopy(b[k]) for k in brief_fields if k in b} for b in briefs]
                item["units"].append(projected)
            if chapter_id in contracts and contracts[chapter_id] != item:
                raise ValueError("conflicting_projected_chapter_plan:" + chapter_id)
            contracts[chapter_id] = item
            sources.setdefault(chapter_id, []).append(location)
        for field in ("chapters", "writer_packets"):
            rows = node.get(field, [])
            rows = list(rows.values()) if isinstance(rows, dict) else rows
            if not isinstance(rows, list):
                raise ValueError("invalid_plan_container:" + field)
            for index, child in enumerate(rows):
                collect(child, location + "/" + field + "/" + str(index))
    collect(plan, "root")
    scope = {key: copy.deepcopy(plan[key]) for key in ("shared_scope", "review_argument") if key in plan}
    outline = {"shared_outline": copy.deepcopy(plan.get("shared_outline", "")), "chapter_contracts": list(contracts.values())}
    projection = {"version": "literal_fine_outline_v1", "scope_fields": ["shared_scope", "review_argument"],
                  "chapter_fields": ["chapter_id", "thesis", "reader_objective", "units"],
                  "unit_fields": list(unit_fields), "paragraph_brief_fields": list(brief_fields),
                  "chapter_sources": sources, "duplicate_policy": "deduplicate_identical_projection; reject_conflicting_same_chapter_id",
                  "excluded": ["supporting_studies", "cases", "source_materials", "locator_reads"], "rewritten": False}
    return scope, outline, projection

def prepare_case(args: argparse.Namespace) -> dict[str, Any]:
    from optomind_research.runtime.upgrade3.post_body_revision_contracts import normalize_case
    if not args.plan and not args.materials:
        raise ValueError("prepare_requires_plan_or_materials")
    body = Path(args.body).read_bytes()
    case: dict[str, Any] = {"case_id": Path(args.body).stem, "draft_text": body.decode("utf-8"),
                            "materials": {}, "source_identity_map": {}}
    provenance = {"body_sha256": hashlib.sha256(body).hexdigest(), "inputs": []}
    identities: dict[str, Any] = {}

    def add_identity(handle: str, value: Any):
        if handle not in identities:
            identities[handle] = copy.deepcopy(value)
            return
        previous = identities[handle]
        if previous == value:
            return
        if not isinstance(previous, dict) or not isinstance(value, dict):
            raise ValueError("conflicting_source_identity:" + handle)
        def canonical(field, item):
            result = str(item).strip()
            if field == "doi":
                result = result.lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")
            return result
        for field in ("paper_id", "doi", "source_handle", "material_id", "material_ids"):
            old, new = previous.get(field), value.get(field)
            if old and new and canonical(field, old) != canonical(field, new):
                raise ValueError("conflicting_source_identity:" + handle + ":" + field)
        # Missing metadata is complementary; it is not an identity conflict.
        # Distinct display metadata is retained as provenance variants.
        merged = dict(previous)
        for field, new in value.items():
            if not merged.get(field):
                merged[field] = copy.deepcopy(new)
            elif merged[field] != new and field not in {"card_path", "source_handle"}:
                variants = merged.setdefault("metadata_variants", {})
                options = variants.setdefault(field, [merged[field]])
                if new not in options:
                    options.append(copy.deepcopy(new))
        identities[handle] = merged

    def add_material(key: str, row: dict[str, Any], explicit: bool = False):
        if not isinstance(key, str) or not key:
            raise ValueError("material_id_required")
        if row.get("material_identity_conflict"):
            raise ValueError("material_identity_conflict:" + key)
        handles = row.get("source_handles", [row["source_handle"]] if row.get("source_handle") else [])
        if not isinstance(handles, list) or any(not isinstance(h, str) for h in handles):
            raise ValueError("invalid_source_handles")
        row_identity = {field: row[field] for field in ("paper_id", "doi", "title", "year", "source_handle") if row.get(field)}
        if row_identity:
            for handle in handles:
                add_identity(handle, row_identity)
        content = {k: row[k] for k in MATERIAL_FIELDS if row.get(k) not in (None, "", [], {})}
        text = row.get("text", "") if explicit else ""
        if content and not text:
            text = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if not isinstance(text, str):
            raise ValueError("material_text_not_string")
        item = {"text": text, "title": row.get("title", ""), "summary": row.get("summary", ""),
                "source_handles": handles}
        if content:
            item["content_fields"] = content
        if key in case["materials"] and case["materials"][key] != item:
            if explicit:
                raise ValueError("conflicting_material_id:" + key)
            # Same source can have distinct chapter-specific deep/tool snapshots.
            # Keep each exact content variant; never let last occurrence win.
            key += "__" + hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            if key in case["materials"] and case["materials"][key] != item:
                raise ValueError("material_variant_hash_collision")
        case["materials"][key] = item
        for handle in handles:
            mapping = case["source_identity_map"].setdefault(handle, {"material_ids": []})
            if key not in mapping["material_ids"]:
                mapping["material_ids"].append(key)

    def visit(node: Any):
        if not isinstance(node, dict):
            raise ValueError("unsupported_plan_node")
        for handle, identity in (node.get("source_identity_map") or {}).items():
            add_identity(handle, identity)
        for row in node.get("source_materials", []):
            if not isinstance(row, dict):
                raise ValueError("invalid_source_material")
            key = row.get("material_id") or row.get("id") or row.get("source_handle")
            add_material(key, row)
        # Traverse only documented final-plan packet containers, never arbitrary keys.
        for field in ("chapters", "writer_packets"):
            children = node.get(field, [])
            if isinstance(children, dict):
                children = list(children.values())
            for child in children:
                visit(child)

    if args.plan:
        plan = read_json(args.plan)
        if not isinstance(plan, dict):
            raise ValueError("plan_must_be_object")
        visit(plan)
        case["research_question"] = plan.get("research_question", "")
        case["scope"], case["outline"], provenance["intent_projection"] = project_plan_intent(plan)
        provenance["inputs"].append({"kind": "plan", "sha256": hashlib.sha256(Path(args.plan).read_bytes()).hexdigest()})
    if args.materials:
        explicit = read_json(args.materials)
        raw = explicit.get("materials", explicit) if isinstance(explicit, dict) else explicit
        if isinstance(explicit, dict) and "materials" in explicit:
            for field in ("research_question", "scope", "outline"):
                if field in explicit:
                    case[field] = explicit[field]
                    provenance.setdefault("explicit_intent_overrides", []).append(field)
            for handle, identity in explicit.get("source_identity_map", {}).items():
                add_identity(handle, identity)
        rows = raw.items() if isinstance(raw, dict) else ((r.get("material_id", r.get("id")), r) for r in raw)
        for key, row in rows:
            add_material(key, {"text": row} if isinstance(row, str) else row, explicit=True)
        provenance["inputs"].append({"kind": "materials", "sha256": hashlib.sha256(Path(args.materials).read_bytes()).hexdigest()})
    if not case["materials"]:
        raise ValueError("unrecognized_or_empty_plan_materials_supply_explicit_materials")
    for handle, identity in identities.items():
        declared_ids = ([identity] if isinstance(identity, str) else
                        identity.get("material_ids", [identity["material_id"]] if identity.get("material_id") else [])
                        if isinstance(identity, dict) else [])
        if declared_ids:
            if not isinstance(declared_ids, list) or any(k not in case["materials"] for k in declared_ids):
                raise ValueError("identity_links_unknown_material:" + handle)
            existing = case["source_identity_map"].get(handle, {}).get("material_ids", [])
            if existing and set(existing) != set(declared_ids):
                raise ValueError("conflicting_identity_material_links:" + handle)
            case["source_identity_map"][handle] = {"material_ids": list(declared_ids)}
        if handle in case["source_identity_map"]:
            case["source_identity_map"][handle]["original_identity"] = identity
        else:
            case["source_identity_map"][handle] = {"original_identity": identity}
    case["preparation_provenance"] = provenance
    case["source_catalog"] = [{"material_id": key, "title": item["title"],
                                "summary": item["summary"], "source_handles": item["source_handles"],
                                "has_text": bool(item["text"])} for key, item in case["materials"].items()]
    return normalize_case(case)


def execute(args: argparse.Namespace) -> int:
    from optomind_research.runtime.upgrade3.post_body_revision_contracts import load_case
    if args.command == "prepare":
        case = prepare_case(args)
        destination = Path(args.output)
        if destination.resolve() in {Path(p).resolve() for p in (args.body, args.plan, args.materials) if p}:
            raise ValueError("prepare_output_must_not_overwrite_input")
        if destination.exists():
            raise ValueError("prepare_output_already_exists")
        write_json(destination, case)
        print(json.dumps({"status": "prepared", "case": str(destination), "base_sha256": case["base_sha256"]}))
        return 0
    if args.run != args.allow_paid:
        raise ValueError("paid_execution_requires_run_and_allow_paid")
    if args.recordings and args.run:
        raise ValueError("recording_and_paid_modes_are_exclusive")
    config_path = Path(args.config) if args.config else PROJECT_ROOT / "config/post_body_revision" / (args.variant + ".json")
    config = validate_config(read_json(config_path), args.variant)
    mode = "live" if args.run else "recording" if args.recordings else "preview"
    config["execution_mode"] = mode
    if mode == "live":
        config["paid_budget"] = {"limit_cny": args.budget_cny, "shared_ledger": str(Path(args.budget_ledger).resolve()) if args.budget_ledger else None}
    if args.recordings:
        config["recordings_sha256"] = hashlib.sha256(Path(args.recordings).read_bytes()).hexdigest()
    case = load_case(args.case)
    from optomind_research.runtime.upgrade3.post_body_revision import preflight_revision
    sizing = preflight_revision(case, config) if mode in {"preview", "live"} else None
    output = Path(args.output_root) / args.variant / mode
    if mode == "preview":
        if output.exists() and any(output.iterdir()) and not args.resume:
            raise ValueError("output_exists_use_new_root_or_resume")
        manifest = {"status": "preview", "variant": args.variant, "base_sha256": case["base_sha256"],
                    "effective_config": config, "case_fingerprint": hashlib.sha256(json.dumps(case, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                    "material_count": len(case["materials"]),
                    "block_count": len(case["blocks"]), "paid_calls": 0, "preflight": sizing}
        target = output / "preview.json"
        if args.resume and target.exists() and read_json(target) != manifest:
            raise ValueError("preview_resume_fingerprint_mismatch")
        write_json(target, manifest)
        print(json.dumps({"status": "preview", "manifest": str(target), "paid_calls": 0}))
        return 0
    client = RecordingClient(args.recordings) if args.recordings else make_live_client(args, config)
    from optomind_research.runtime.upgrade3.post_body_revision import run_revision
    report = run_revision(case, config, output, client, resume=args.resume)
    print(json.dumps({"status": report.get("status"), "variant": args.variant, "output_dir": str(output), "mode": mode}))
    return 0 if report.get("run_completed") else 2

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except Exception as exc:
        # Deliberately omit provider exception text: may contain response text or secrets.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "execution_failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
