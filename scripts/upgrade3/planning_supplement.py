"""CLI for bounded planning-support literature supplementation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.planning_supplement import (
    PlanningSupplementError,
    CompositeS2OpenAlexGateway,
    load_planning_supplement_request,
    preflight_planning_supplement,
    revalidate_planning_supplement_judgments,
    run_planning_supplement,
    run_qwen_fulfillment_judge,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or run one bounded literature supplement and produce practical writing material."
    )
    parser.add_argument("--request", default="", help="Planning supplement request JSON")
    parser.add_argument("--output-dir", default="", help="New output directory under outputs/; defaults to a request-specific path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Default: preview the plan without network/model calls")
    mode.add_argument("--run", action="store_true", help="Run bounded retrieval, acquisition and practical material preparation")
    mode.add_argument("--revalidate-judgment", action="store_true", help="Offline revalidation from an existing supplement output directory; no model/acquisition calls")
    parser.add_argument("--key-file", default="", help="Qwen key file for the A/B card and fulfillment judge")
    parser.add_argument("--budget-ledger", default="", help="Shared Qwen budget ledger path")
    parser.add_argument("--budget-limit-cny", type=float, default=None, help="Finite total CNY ceiling shared with the A/B card")
    return parser


def _output_path(value: str, request_id: str, gap_id: str) -> Path:
    if value:
        target = Path(value)
        target = target if target.is_absolute() else PROJECT_ROOT / target
        target = target.resolve()
    else:
        request_token = re.sub(r"[^A-Za-z0-9_-]+", "_", request_id).strip("_-")[:48] or "request"
        gap_token = re.sub(r"[^A-Za-z0-9_-]+", "_", gap_id).strip("_-")[:48] or "gap"
        target = (PROJECT_ROOT / "outputs" / "upgrade3" / "planning_supplements" /
                  (request_token + "_" + gap_token)).resolve()
    allowed_root = (PROJECT_ROOT / "outputs").resolve()
    try:
        target.relative_to(allowed_root)
    except ValueError as exc:
        raise PlanningSupplementError("output_dir_must_be_under_outputs") from exc
    return target


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        if args.revalidate_judgment:
            if not args.output_dir:
                raise PlanningSupplementError("revalidation_output_dir_required")
            output_dir = _output_path(args.output_dir, "", "")
            result = revalidate_planning_supplement_judgments(output_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if not args.request:
            raise PlanningSupplementError("request_required_for_preflight_or_run")
        request = load_planning_supplement_request(args.request)
        output_dir = _output_path(args.output_dir, request.request_id, request.gap_id)
        if not args.run:
            result = preflight_planning_supplement(args.request, output_dir=output_dir)
        else:
            citation_only = bool(request.reviewed_references) and not (
                request.targeted_queries or request.reuse_plan_facet_ids or request.known_papers
            )
            if citation_only:
                def no_material_call(*_args, **_kwargs):
                    raise PlanningSupplementError("citation_only_request_must_not_acquire_or_read")

                result = run_planning_supplement(
                    args.request,
                    output_dir=output_dir,
                    gateway=None,
                    acquirer_factory=no_material_call,
                    card_runner=no_material_call,
                    fulfillment_judge=None,
                )
            else:
                if not args.key_file:
                    raise PlanningSupplementError("live_run_key_file_required")
                if not args.budget_ledger:
                    raise PlanningSupplementError("live_run_shared_budget_ledger_required")
                if args.budget_limit_cny is None:
                    raise PlanningSupplementError("live_run_finite_positive_budget_required")

                from optomind_research.runtime.upgrade3.material_acquisition import (
                    AcquisitionConfig,
                    MaterialAcquirer,
                )
                from optomind_research.runtime.upgrade3.paper_reading_card import run_paper_reading_card
                from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
                from tools.academic_backends.openalex_backend import OpenAlexBackend

                def acquirer_factory(material_root: Path):
                    return MaterialAcquirer(
                        material_root,
                        config=AcquisitionConfig(cache_root=material_root / "cache"),
                    )

                judge = run_qwen_fulfillment_judge(
                    key_file=args.key_file,
                    budget_ledger_path=args.budget_ledger,
                    budget_limit_cny=args.budget_limit_cny,
                )
                result = run_planning_supplement(
                    args.request,
                    output_dir=output_dir,
                    gateway=CompositeS2OpenAlexGateway(S2IntelligenceGateway(), OpenAlexBackend()),
                    acquirer_factory=acquirer_factory,
                    card_runner=run_paper_reading_card,
                    fulfillment_judge=judge,
                    card_options={
                        "key_file": args.key_file,
                        "budget_ledger_path": args.budget_ledger,
                        "budget_limit_cny": args.budget_limit_cny,
                    },
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PlanningSupplementError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # bounded CLI failure summary; artifacts remain in the new output dir
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:500]}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
