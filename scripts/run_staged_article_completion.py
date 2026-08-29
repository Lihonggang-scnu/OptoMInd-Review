"""Standalone CLI for the staged full-manuscript completion runner.

The CLI reads JSON inputs (and optional per-stage inputs/metadata), selects
which stages may use live Qwen providers via ``--live-stages``, and runs the
deterministic staged runner.  Only allowlisted stages receive Qwen providers;
all other stages use deterministic offline providers.  No stage calls Qwen
without being named in ``--live-stages``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.qwen_config import load_model_policy  # noqa: E402
from optomind_research.runtime.staged_article_completion import (  # noqa: E402
    SCHEMA_VERSION,
    STAGE_ORDER,
    QwenMultiReviewerProvider,
    QwenStagedProvider,
    StagedArticleCompletionState,
    make_editorial_revision_qwen_provider,
    make_multi_reviewer_qwen_provider,
    make_qwen_stage_provider,
    run_staged_article_completion,
)


DEFAULT_REVIEWER_ROLES = (
    "continuity",
    "clarity",
    "reader_flow",
    "logic",
    "overlap",
)
LIVE_PROVIDER_STAGES = frozenset(
    {
        "conclusion",
        "introduction",
        "abstract",
        "whole_manuscript_review",
        "bounded_patch_proposals",
        "editorial_revision",
    }
)
DEFAULT_STAGE_MODEL_TIERS = {
    "conclusion": "c_model",
    "introduction": "c_model",
    "abstract": "c_model",
    "bounded_patch_proposals": "c_model",
    "whole_manuscript_review": "c2_model",
    "editorial_revision": "c_model",
}


class StagedCliError(RuntimeError):
    """Raised for invalid CLI inputs."""


class _TrackExplicitOption(argparse.Action):
    """Argparse action that marks when an option was explicitly provided."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, self.dest + "_explicit", True)


def _load_json(path: str | Path, label: str) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise StagedCliError(f"missing {label}: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StagedCliError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise StagedCliError(f"{label} must be a JSON object: {path}")
    return dict(payload)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _unwrap_stage_inputs(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap a STAGED_STAGE_INPUTS.json wrapper or keep a flat mapping."""

    stages = payload.get("stages")
    if isinstance(stages, Mapping):
        return dict(stages)
    return dict(payload)


def _build_live_providers(
    *,
    live_stages: list[str],
    model_tier: str,
    reviewer_roles: list[str],
    stage_model_tiers: Mapping[str, str] | None = None,
    model_tier_explicit: bool = False,
    editorial_verifier_tier: str = "c2_model",
) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    known = set(STAGE_ORDER)
    stage_model_tiers = dict(stage_model_tiers or {})
    for stage in live_stages:
        if stage not in known:
            raise StagedCliError(f"unknown live stage: {stage}")
        if stage not in LIVE_PROVIDER_STAGES:
            raise StagedCliError(
                f"live stage not supported by a provider: {stage}"
            )
        if stage in stage_model_tiers:
            tier = stage_model_tiers[stage]
        elif model_tier_explicit:
            tier = model_tier
        else:
            tier = DEFAULT_STAGE_MODEL_TIERS.get(stage, model_tier)
        if stage == "editorial_revision":
            providers[stage] = make_editorial_revision_qwen_provider(
                model_tier=tier,
                verifier_tier=editorial_verifier_tier,
            )
        elif stage == "whole_manuscript_review":
            providers[stage] = make_multi_reviewer_qwen_provider(
                reviewers=[
                    {"reviewer_id": role, "role": role}
                    for role in reviewer_roles
                ],
                model_tier=tier,
            )
        else:
            providers[stage] = make_qwen_stage_provider(
                stage, model_tier=tier
            )
    return providers


def _supported_model_tiers() -> set[str]:
    try:
        policy = load_model_policy()
        aliases = dict(policy.get("model_aliases", {}) or {})
    except Exception as exc:
        raise StagedCliError(
            f"cannot load model policy for tier validation: {exc}"
        ) from exc
    return {str(name) for name in aliases if str(name).strip()}


def _parse_stage_model_tiers(
    entries: list[str] | None,
) -> dict[str, str]:
    """Parse repeatable STAGE=TIER entries with clear validation errors."""

    mapping: dict[str, str] = {}
    if not entries:
        return mapping
    supported_tiers = _supported_model_tiers()
    for entry in entries:
        if "=" not in entry:
            raise StagedCliError(
                f"invalid --stage-model-tier {entry!r}: expected STAGE=TIER"
            )
        stage, tier = entry.split("=", 1)
        stage = stage.strip()
        tier = tier.strip()
        if not stage or not tier:
            raise StagedCliError(
                "invalid --stage-model-tier "
                f"{entry!r}: STAGE and TIER must not be empty"
            )
        if stage not in LIVE_PROVIDER_STAGES:
            raise StagedCliError(
                "unknown or unsupported stage in --stage-model-tier: "
                + stage
            )
        if tier not in supported_tiers:
            raise StagedCliError(
                f"unsupported model tier for {stage}: {tier}"
            )
        if stage in mapping:
            raise StagedCliError(
                f"duplicate --stage-model-tier for stage: {stage}"
            )
        mapping[stage] = tier
    return mapping


def _count_usage_tokens(value: Any) -> tuple[int, int]:
    """Recursively count input/output tokens without double counting.

    A mapping that already carries aggregate ``total_input_tokens`` /
    ``total_output_tokens`` is used as-is (nested reviewer records are not
    added again).  Otherwise leaf ``input_tokens`` / ``output_tokens`` values
    are summed recursively, which covers repaired-stage ``initial``/``repair``
    structures and per-reviewer records.
    """

    if not isinstance(value, Mapping):
        return 0, 0
    if "total_input_tokens" in value or "total_output_tokens" in value:
        return (
            int(value.get("total_input_tokens") or 0),
            int(value.get("total_output_tokens") or 0),
        )
    input_tokens = 0
    output_tokens = 0
    for key, nested in value.items():
        if isinstance(nested, Mapping):
            nested_input, nested_output = _count_usage_tokens(nested)
            input_tokens += nested_input
            output_tokens += nested_output
        elif key == "input_tokens":
            input_tokens += int(nested or 0)
        elif key == "output_tokens":
            output_tokens += int(nested or 0)
    return input_tokens, output_tokens


def _summary(state: StagedArticleCompletionState) -> dict[str, Any]:
    total_input_tokens = 0
    total_output_tokens = 0
    stage_model_tiers: dict[str, str] = {}
    for record in state.stages.values():
        stage_input, stage_output = _count_usage_tokens(record.usage or {})
        total_input_tokens += stage_input
        total_output_tokens += stage_output
    for stage in state.stage_order:
        record = state.stages.get(stage)
        usage = record.usage if record else {}
        tier = (
            usage.get("model_tier")
            if isinstance(usage, Mapping) and usage
            else None
        )
        if tier:
            stage_model_tiers[stage] = str(tier)
    return {
        "schema_version": "optomind.staged_article_completion.cli.v1",
        "status": state.status,
        "stage_statuses": {
            stage: record.status for stage, record in state.stages.items()
        },
        "approval_required_stages": state.awaiting_approval_stages,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "stage_model_tiers": stage_model_tiers,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the staged full-manuscript completion runner. "
            "Only --live-stages may use Qwen; everything else is offline."
        )
    )
    parser.add_argument("--inputs-json", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stage-inputs-json", type=Path, default=None)
    parser.add_argument("--metadata-json", type=Path, default=None)
    parser.add_argument("--handoff-json", type=Path, default=None)
    parser.add_argument("--commander-work-order-json", type=Path, default=None)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--model-tier",
        action=_TrackExplicitOption,
        default="c_model",
        help=(
            "Model tier for all live stages; per-stage "
            "--stage-model-tier entries override it."
        ),
    )
    parser.add_argument(
        "--stage-model-tier",
        action="append",
        dest="stage_model_tiers",
        default=None,
        metavar="STAGE=TIER",
        help=(
            "Optional per-stage model tier, repeatable "
            "(e.g. --stage-model-tier conclusion=c_model "
            "--stage-model-tier whole_manuscript_review=c2_model)."
        ),
    )
    parser.add_argument(
        "--editorial-verifier-tier",
        default="c2_model",
        help=(
            "Independent verifier model tier for the editorial_revision "
            "stage (default c2_model / Qwen 3.7 Flash)."
        ),
    )
    parser.set_defaults(model_tier_explicit=False)
    parser.add_argument("--live-stages", default="")
    parser.add_argument(
        "--reviewer-roles",
        default=",".join(DEFAULT_REVIEWER_ROLES),
        help=(
            "Comma-separated reviewer roles/IDs used for the "
            "whole_manuscript_review provider."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        inputs = _load_json(args.inputs_json, "inputs JSON")
        if args.handoff_json:
            handoff = _load_json(args.handoff_json, "handoff JSON")
            inputs["full_manuscript_handoff"] = handoff
            inputs["full_manuscript_handoff_path"] = str(args.handoff_json)
        if args.commander_work_order_json:
            work_order = _load_json(
                args.commander_work_order_json, "commander work order JSON"
            )
            inputs["commander_work_order"] = work_order
            inputs["commander_work_order_path"] = str(
                args.commander_work_order_json
            )
        stage_inputs = (
            _unwrap_stage_inputs(
                _load_json(args.stage_inputs_json, "stage inputs JSON")
            )
            if args.stage_inputs_json
            else {}
        )
        metadata = (
            _load_json(args.metadata_json, "metadata JSON")
            if args.metadata_json
            else {}
        )
        live_stages = _split_csv(args.live_stages)
        reviewer_roles = _split_csv(args.reviewer_roles)
        if not reviewer_roles:
            raise StagedCliError(
                "--reviewer-roles must contain at least one role"
            )
        stage_model_tiers = _parse_stage_model_tiers(args.stage_model_tiers)
        if args.editorial_verifier_tier not in _supported_model_tiers():
            raise StagedCliError(
                "unsupported editorial verifier tier: "
                + args.editorial_verifier_tier
            )
        providers = _build_live_providers(
            live_stages=live_stages,
            model_tier=args.model_tier,
            stage_model_tiers=stage_model_tiers,
            model_tier_explicit=bool(
                getattr(args, "model_tier_explicit", False)
            ),
            editorial_verifier_tier=args.editorial_verifier_tier,
            reviewer_roles=reviewer_roles,
        )
        state = run_staged_article_completion(
            work_dir=args.output_dir,
            inputs=inputs,
            stage_inputs=stage_inputs,
            metadata=metadata,
            stage_providers=providers,
            resume=args.resume,
            run_id=args.run_id,
            execution_context={
                "work_dir": str(args.output_dir),
                "resume": args.resume,
            },
        )
    except StagedCliError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(_summary(state), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_REVIEWER_ROLES",
    "DEFAULT_STAGE_MODEL_TIERS",
    "LIVE_PROVIDER_STAGES",
    "StagedCliError",
    "_load_json",
    "_build_live_providers",
    "_parse_stage_model_tiers",
    "_count_usage_tokens",
    "build_arg_parser",
    "main",
]
