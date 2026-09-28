"""CLI for the deterministic local-first publication metadata resolver.

Resolves every ``[REF:identity]`` used by the latest staged manuscript into an
auditable bibliography catalog using local metadata first, with optional
provider enrichment (OpenAlex / Crossref / Semantic Scholar) that is off by
default.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from optomind_research.runtime.publication_metadata_resolver import (  # noqa: E402
    CATALOG_FILENAME,
    SCHEMA_VERSION,
    PublicationMetadataError,
    ResolverOptions,
    build_publication_metadata_catalog,
    infer_publication_project_root,
    make_default_crossref_provider,
    make_default_openalex_provider,
    make_default_s2_provider,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve [REF:...] identities in the staged manuscript into "
            "auditable bibliography metadata (local-first, deterministic)."
        )
    )
    parser.add_argument(
        "--staged-manuscript",
        required=True,
        type=Path,
        help="Latest staged Markdown manuscript containing [REF:...] markers.",
    )
    parser.add_argument(
        "--handoff",
        required=True,
        type=Path,
        help="UNIFIED_MANUSCRIPT_HANDOFF.json built for the same manuscript.",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        type=Path,
        help=(
            "Project root that owns relative source paths. When omitted, "
            "the root is inferred from the unified handoff anchors."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Directory for PUBLICATION_METADATA_CATALOG.json and "
            "PUBLICATION_METADATA_AUDIT.json "
            "(default: <project-root>/outputs/publication_metadata_resolution)."
        ),
    )
    parser.add_argument(
        "--staged-context",
        type=Path,
        help=(
            "Optional explicit STAGED_GLOBAL_INPUTS.json; auto-discovered "
            "under outputs/staged_context_* when omitted."
        ),
    )
    parser.add_argument(
        "--material-cache-dir",
        action="append",
        default=[],
        type=Path,
        help=(
            "Extra long-term material cache root to scan (repeatable). "
            "The latest snapshot of each root is used."
        ),
    )
    parser.add_argument(
        "--supplemental-metadata",
        action="append",
        default=[],
        type=Path,
        help=(
            "Repeatable auditable supplemental metadata JSON file (list of "
            "records or {'records': [...]}); each record requires "
            "provenance {source, source_path_or_url, reason} and at least "
            "one identity or a title."
        ),
    )
    parser.add_argument(
        "--no-material-caches",
        action="store_true",
        help="Disable auto-discovery of long-term material caches.",
    )
    parser.add_argument(
        "--max-material-cache-roots",
        type=int,
        default=8,
        help=(
            "Resource guard for auto-discovered cache roots (0 = unlimited); "
            "explicit --material-cache-dir roots are always scanned."
        ),
    )
    parser.add_argument(
        "--no-s2-cache",
        action="store_true",
        help="Disable reading the local Semantic Scholar response cache.",
    )
    parser.add_argument(
        "--s2-cache-path",
        type=Path,
        help="Override the local Semantic Scholar cache sqlite path.",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help=(
            "Allow live OpenAlex/Crossref/Semantic Scholar enrichment after "
            "local sources are exhausted. Off by default (local-first)."
        ),
    )
    parser.add_argument(
        "--crossref-only",
        action="store_true",
        help="With --online, enable only Crossref enrichment.",
    )
    parser.add_argument(
        "--s2-only",
        action="store_true",
        help="With --online, enable only Semantic Scholar enrichment.",
    )
    parser.add_argument(
        "--max-provider-calls",
        type=int,
        default=200,
        help="Cap on total provider calls per run (safety; 0 = unlimited).",
    )
    parser.add_argument(
        "--no-digest-verification",
        action="store_true",
        help="Skip sha256 verification of handoff-referenced files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        project_root = infer_publication_project_root(
            args.handoff,
            explicit_project_root=args.project_root,
        )
    except PublicationMetadataError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    output_dir = Path(args.output_dir) if args.output_dir else (
        project_root / "outputs" / "publication_metadata_resolution"
    )
    allow_openalex = args.online and not (args.crossref_only or args.s2_only)
    allow_crossref = args.online and not args.s2_only
    allow_s2 = args.online and not args.crossref_only
    if args.online:
        if args.max_provider_calls <= 0:
            provider_cap = 0
        else:
            provider_cap = args.max_provider_calls
    else:
        provider_cap = 0
    options = ResolverOptions(
        allow_openalex=allow_openalex,
        allow_crossref=allow_crossref,
        allow_s2=allow_s2,
        max_provider_calls=provider_cap,
        openalex_provider=(
            make_default_openalex_provider() if allow_openalex else None
        ),
        crossref_provider=(
            make_default_crossref_provider() if allow_crossref else None
        ),
        s2_provider=make_default_s2_provider() if allow_s2 else None,
    )
    try:
        summary = build_publication_metadata_catalog(
            staged_manuscript_path=args.staged_manuscript,
            handoff_path=args.handoff,
            project_root=project_root,
            output_dir=output_dir,
            options=options,
            staged_context_path=args.staged_context,
            material_cache_dirs=args.material_cache_dir,
            scan_material_caches=not args.no_material_caches,
            max_material_cache_roots=args.max_material_cache_roots,
            supplemental_metadata_paths=args.supplemental_metadata,
            s2_cache_path=args.s2_cache_path,
            include_s2_cache=not args.no_s2_cache,
            verify_digests=not args.no_digest_verification,
        )
    except PublicationMetadataError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    compact = {
        "schema_version": SCHEMA_VERSION,
        "staged_manuscript": summary["staged_manuscript"],
        "unified_handoff": summary["unified_handoff"],
        "input_fingerprint": summary["input_fingerprint"],
        "catalog_fingerprint": summary["catalog_fingerprint"],
        "output_paths": summary["output_paths"],
        "catalog_filename": CATALOG_FILENAME,
        "audit": summary["audit"],
        "malformed_refs": summary["malformed_refs"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA_VERSION",
    "CATALOG_FILENAME",
    "PublicationMetadataError",
    "build_arg_parser",
    "main",
]
