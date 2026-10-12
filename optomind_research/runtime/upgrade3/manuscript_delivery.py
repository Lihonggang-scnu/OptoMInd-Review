"""Shared 03 -> bounded checker -> selected manuscript -> 04 -> 05 delivery.

Both ordinary delivery and independent serial-parts output use this function.
No provider is constructed here unless the explicit CLI live adapter is used.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .dual_budget import bind_json, read_json, write_json


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checker_profile(value=None):
    from .writer_candidates import _profile
    return _profile({"model": "qwen3.5-plus", "thinking": True,
        "thinking_budget": 16384, "max_output_tokens": 32768, "stream": True,
        "timeout_seconds": 900.0, "stream_overall_timeout_seconds": 1800.0,
        **dict(value or {})}, "manuscript_checker")


def verify_selected(selected):
    if not isinstance(selected, Mapping) or not selected.get("path") or not selected.get("sha256"):
        raise ValueError("selected_manuscript_missing")
    path = Path(selected["path"]).resolve()
    if not path.is_file():
        raise ValueError("selected_manuscript_file_missing:" + str(path))
    if sha256(path) != selected["sha256"]:
        raise ValueError("selected_manuscript_hash_mismatch")
    return path


def run_manuscript_delivery(*, config, front_back_report, out_dir, stages=None,
                            client_factory=None, run=False, token_counter=None,
                            retry_failed=False):
    """Return the real selected artifact and its exact downstream consumers."""
    from . import review_delivery as delivery
    from .delivery_citations import run_figures_citations_stage

    output = Path(out_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stages = dict(stages or {})
    r3 = dict(front_back_report.get("runtime_report") or front_back_report)
    stages["03_front_back"] = r3
    final = Path(str(r3.get("final_manuscript") or ""))
    if (r3.get("status") not in ({"generated", "complete"} if config.get("front_back_mode") == "post_body" else {"generated", "complete", "partial"})) or not final.is_file():
        return delivery._downstream_report(config, stages, ["03_front_back"])
    catalogs = config.get("identity_catalogs") or []
    if config.get("front_back_mode") == "post_body":
        from .manuscript_front_back import FrontBackError
        try:
            catalogs = delivery._post_body_delivery_catalogs(catalogs, r3.get("source_identity_map") or {})
        except FrontBackError as exc:
            stages["04_figures_citations"] = {"stage": "figures_citations", "status": "failed",
                "error": str(exc), "model_calls": 0, "external_requests": 0}
            return delivery._downstream_report(config, stages, ["04_figures_citations"])
    checker = dict(config.get("manuscript_checker") or {})
    enabled = checker.get("enabled", config.get("front_back_mode") == "post_body")
    context = config.get("post_body_context") or {}
    if enabled:
        from .manuscript_checker import run_manuscript_checker
        result = run_manuscript_checker(draft_path=final,
            out_dir=output / "03_manuscript_checker", identity_catalogs=catalogs,
            material_records=context.get("material_records") or [],
            figure_assets=config.get("figure_assets") or [],
            chapter_roles=config.get("chapter_roles") or [],
            language=str(config.get("language") or "zh"),
            fixture_responses=checker.get("fixture"), recordings=checker.get("recordings"),
            client_factory=client_factory, run=run, token_counter=token_counter,
            profile=checker_profile(checker.get("profile")), retry_failed=retry_failed,
            enabled=enabled, max_model_rounds=checker.get("max_model_rounds", 2))
    else:
        result = {"stage": "manuscript_checker", "status": "disabled",
            "selected_manuscript": {"path": str(final.resolve()), "sha256": sha256(final),
                "status": "disabled", "provenance": {"source": "03_front_back", "reason": "checker_explicitly_disabled"}},
            "applied": [], "pending": [], "diagnostics": [], "model_calls": 0, "external_requests": 0}
    if enabled or config.get("front_back_mode") == "post_body":
        stages["03_manuscript_checker"] = result
    selected = result.get("selected_manuscript")
    if not selected:
        return delivery._downstream_report(config, stages, ["03_manuscript_checker"])
    selected_path = verify_selected(selected)
    numbering_input, projection = (selected_path, None)
    if config.get("front_back_mode") == "post_body":
        numbering_input, projection = delivery._post_body_numbering_input(
            selected_path, output, config.get("chapter_roles") or [])
    r4 = run_figures_citations_stage(final_draft_path=numbering_input,
        identity_catalogs=catalogs, out_dir=output / "04_figures_citations",
        figure_assets=config.get("figure_assets") or [],
        table_moves=config.get("table_moves") or None, figure_moves=config.get("figure_moves") or None)
    r4["selected_manuscript"] = selected
    if projection is not None:
        r4["input_projection"] = projection
    mapping_check = {"figures_missing": r4.get("figures_missing") or [],
        "unknown_citation_tokens": r4.get("unknown_citation_tokens") or [],
        "unresolved_figure_tokens": []}
    reader = Path(str(r4.get("reader_draft") or ""))
    if reader.is_file():
        import re
        mapping_check["unresolved_figure_tokens"] = re.findall(r"@@FIG:[^@]+@@", reader.read_text(encoding="utf-8"))
    mapping_check["all_pass"] = not any(mapping_check.values())
    r4["post_numbering_mapping_check"] = mapping_check
    write_json(output / "04_figures_citations" / "STAGE_REPORT.json", r4)
    stages["04_figures_citations"] = r4
    if r4.get("status") != "complete" or not reader.is_file():
        return delivery._downstream_report(config, stages, ["04_figures_citations"])
    sidecar = {"selected_manuscript": selected, "checker_status": result.get("status"),
        "checker_report": str(output / "03_manuscript_checker" / "CHECKER_REPORT.json") if enabled else None,
        "applied": result.get("applied") or [], "pending": result.get("pending") or [],
        "diagnostics": result.get("diagnostics") or [],
        "unresolved_questions": result.get("unresolved_questions") or [],
        "post_numbering_mapping_check": mapping_check,
        "source_body_delivery": front_back_report.get("source_body_delivery"),
        "scientific_acceptance": False}
    r5 = delivery.run_publication_delivery(reader_draft_path=reader,
        references_path=r4["references_path"], figure_map_path=r4.get("mapping_path"),
        assets_dir=r4.get("assets_dir"), out_dir=output / "05_publication",
        language=str(config.get("language") or "zh"), compile_pdf=bool(config.get("compile_pdf")))
    sidecar["consumer_artifacts"] = {
        "numbering_input": {"path": str(numbering_input), "sha256": sha256(numbering_input)},
        "reader": {"path": str(reader), "sha256": sha256(reader)},
        "published": {"path": r5.get("published_markdown"),
            "sha256": sha256(r5["published_markdown"]) if r5.get("published_markdown") else None},
    }
    sidecar_path = output / "05_publication" / "MANUSCRIPT_SELECTION.json"
    write_json(sidecar_path, sidecar)
    r5.update(selected_manuscript=selected, manuscript_checker=sidecar,
              manuscript_selection_path=str(sidecar_path))
    write_json(output / "05_publication" / "DELIVERY_REPORT.json", r5)
    stages["05_publication"] = r5
    report = delivery._downstream_report(config, stages, [])
    report.update(selected_manuscript=selected, manuscript_checker=sidecar)
    write_json(output / "MANUSCRIPT_DELIVERY_REPORT.json", report)
    return report


def run_live_delivery(*, args, config, front_back_report, token_counter=None,
                      tokenizer_info=None, client_factory=None):
    """Bind existing dual ledgers before preparing the lazy production factory.

    Preview only reads existing balances. Successful/cache-recovered calls never
    construct a client. The original incremental baseline survives all resumes.
    """
    if not (config.get("manuscript_checker") or {}).get("enabled", config.get("front_back_mode") == "post_body"):
        return run_manuscript_delivery(config=config, front_back_report=front_back_report, out_dir=args.output)
    from .serial_parts_live import ledger_snapshot
    from .writer_candidates import _output_lock, _estimate
    from scripts.upgrade3.writer_candidates import make_live_factory
    output = Path(args.output).resolve()
    if not math.isfinite(args.incremental_budget) or args.incremental_budget <= 0:
        raise ValueError("checker_incremental_budget_must_be_positive")
    project = ledger_snapshot(args.ledger, args.budget_limit)
    account = ledger_snapshot(args.account_ledger, args.account_budget_limit)
    if project["path"] == account["path"]:
        raise ValueError("dual_budget_requires_distinct_persistent_ledgers")
    r3 = front_back_report.get("runtime_report") or front_back_report
    checker = config.get("manuscript_checker") or {}
    from .manuscript_checker import build_checker_messages
    from .review_delivery import _post_body_delivery_catalogs
    catalogs = config.get("identity_catalogs") or []
    if config.get("front_back_mode") == "post_body":
        catalogs = _post_body_delivery_catalogs(catalogs, r3.get("source_identity_map") or {})
    context = config.get("post_body_context") or {}
    first_messages = build_checker_messages(draft_text=Path(r3["final_manuscript"]).read_bytes().decode("utf-8"),
        identity_catalogs=catalogs, material_records=context.get("material_records") or [],
        figure_assets=config.get("figure_assets") or [], chapter_roles=config.get("chapter_roles") or [],
        language=str(config.get("language") or "zh"))
    estimate = _estimate(first_messages, checker_profile(checker.get("profile")), None, token_counter)
    identity = {"source_sha256": sha256(r3["final_manuscript"]),
        "profile": checker_profile(checker.get("profile")),
        "config": json.loads(json.dumps(config, default=str)),
        "project_path": project["path"], "project_limit_cny": args.budget_limit,
        "account_path": account["path"], "account_limit_cny": args.account_budget_limit,
        "mapping_dir": str(Path(args.account_mapping_dir).resolve()),
        "incremental_budget": args.incremental_budget, "tokenizer": tokenizer_info}
    with _output_lock(output):
        binding_path = output / "CHECKER_LIVE_BINDING.json"
        if binding_path.exists():
            binding = read_json(binding_path)
            if binding["inputs"] != identity:
                raise ValueError("checker_live_binding_changed:retain_budget_and_cache")
        else:
            binding = {"inputs": identity, "baseline_project": project, "baseline_account": account,
                "reservation_ceiling_cny": min(args.budget_limit, project["used_cny"] + args.incremental_budget)}
            bind_json(binding_path, binding)
        write_json(output / "CHECKER_ESTIMATE.json", estimate)
        factory = None
        if args.run and not estimate["fits"]:
            raise ValueError("checker_input_capacity_exceeded")
        if args.run:
            args.responses = None
            args.budget_ledger = args.ledger
            factory = client_factory or make_live_factory(args, token_counter=token_counter,
                reservation_ceiling_cny=binding["reservation_ceiling_cny"])
        result = run_manuscript_delivery(config=config, front_back_report=front_back_report,
            out_dir=output, client_factory=factory, run=args.run, token_counter=token_counter,
            retry_failed=args.retry_failed)
        result["estimate"] = estimate
        result["budget_binding"] = binding
        result["project_after"] = ledger_snapshot(args.ledger, args.budget_limit)
        result["account_after"] = ledger_snapshot(args.account_ledger, args.account_budget_limit)
        result["project_actual_delta_cny"] = result["project_after"]["actual_cny"] - binding["baseline_project"]["actual_cny"]
        write_json(output / "MANUSCRIPT_DELIVERY_REPORT.json", result)
        return result
