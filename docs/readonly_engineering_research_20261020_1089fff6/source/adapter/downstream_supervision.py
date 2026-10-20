"""Bounded developer-supervision launcher for the formal downstream CLIs.

This module is deliberately a thin seam around the already reviewed
``planning_supervision_candidate.planning_transport`` implementation.  It
never creates a budget or approval protocol.  Preview is the default; the
native provider is reachable only when the formal argv contains ``--run``.

The module is stored under ``preparation`` so that it is not a production
pipeline import.  A later, explicitly approved run may import this launcher
with the source and binding selected by its immutable run record.  Tests use a
fake provider and temporary ledgers.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


E_ROOT = Path(__file__).resolve().parents[2]
PREPARATION_ROOT = E_ROOT / "preparation"
CANDIDATE_ROOT = PREPARATION_ROOT / "planning_supervision_candidate"
SOURCE_ROOT = E_ROOT / "worktree"
DEFAULT_BINDING = E_ROOT / "RUN_BINDING.json"
# Reuse the run's existing supervision namespace by default.  Tests and an
# explicitly isolated developer run pass a temporary root instead.
DEFAULT_SUPERVISION_ROOT = E_ROOT / "supervision"


def _add_import_roots(source_root: Path = SOURCE_ROOT) -> None:
    """Make the bound source and reviewed candidate adapter importable."""

    # The candidate adapter is fixed under preparation, while the formal
    # source is selected by the immutable binding.  Keeping the bound source
    # first lets a later approved v20 binding use the same launcher without
    # silently importing v19 modules.
    for root in (Path(source_root).resolve(), CANDIDATE_ROOT):
        value = str(root)
        if value not in sys.path:
            sys.path.insert(0, value)


_add_import_roots()

import planning_transport as _planning_transport  # noqa: E402
from planning_transport import PlanningTransport, source_pin  # noqa: E402


FORMAL_ENTRIES = ("strengthening", "arrangement", "body", "parts", "checker")
# writer_candidates is the one downstream module not in the reviewed planning
# allowlist.  It is added only for the duration of an explicitly supervised
# run and the old frozenset is restored in all cases.
_DOWNSTREAM_MODULES = frozenset({"scripts.upgrade3.writer_candidates"})


@dataclass(frozen=True)
class FormalRun:
    entry: str
    run_enabled: bool
    returncode: int
    adapter: PlanningTransport


@contextmanager
def downstream_allowlist() -> Iterable[None]:
    """Temporarily permit the existing BODY/parts/checker factory module.

    This is a process-local parameterization of the reviewed adapter, not a
    new authorization mechanism.  It is intentionally scoped to one native
    run and restores the exact prior allowlist on exit.
    """

    previous = _planning_transport._PLANNING_MODULES
    _planning_transport._PLANNING_MODULES = frozenset(previous | _DOWNSTREAM_MODULES)
    try:
        yield
    finally:
        _planning_transport._PLANNING_MODULES = previous


def _formal_main(entry: str, argv: Sequence[str]) -> int:
    """Call the repository's formal CLI entry point without copying it."""

    if entry == "strengthening":
        module = importlib.import_module("scripts.upgrade3.outline_strengthening_pipeline")
        result = module.main(list(argv))
    elif entry == "arrangement":
        module = importlib.import_module("scripts.upgrade3.chapter_arrangement")
        result = module.main(list(argv))
    elif entry == "parts":
        module = importlib.import_module("scripts.upgrade3.manuscript_parts")
        result = module.main(list(argv))
    elif entry == "checker":
        module = importlib.import_module("scripts.upgrade3.manuscript_delivery")
        result = module.main(list(argv))
    elif entry == "body":
        module = importlib.import_module("run_review_harness")
        old_argv = sys.argv
        try:
            sys.argv = ["run_review_harness.py", *argv]
            result = module.main()
        finally:
            sys.argv = old_argv
    else:  # pragma: no cover - argparse and run_formal both guard this.
        raise ValueError(f"unsupported formal entry: {entry}")
    return int(result or 0)


def run_formal(
    entry: str,
    argv: Sequence[str],
    *,
    binding_path: Path | str = DEFAULT_BINDING,
    supervision_root: Path | str = DEFAULT_SUPERVISION_ROOT,
    fake_provider: Callable[..., Any] | None = None,
    pin_check: Callable[[Mapping[str, Any]], Any] = source_pin,
) -> FormalRun:
    """Run one formal downstream CLI through the reviewed supervision seam.

    ``argv`` is passed verbatim to the formal CLI.  No provider or gate is
    touched unless ``--run`` is present in that formal argv.  Supplying a
    ``fake_provider`` is intended only for offline tests; with no fake, an
    explicit ``--run`` reaches the native provider through the existing
    PlanningTransport and its bound dual ledger.
    """

    if entry not in FORMAL_ENTRIES:
        raise ValueError(f"unsupported formal entry: {entry}")
    formal_argv = tuple(str(value) for value in argv)
    run_enabled = "--run" in formal_argv
    adapter = PlanningTransport(
        binding_path=Path(binding_path),
        supervision_root=Path(supervision_root),
        allow_planning_max=entry == "strengthening",
        fake_provider=fake_provider,
        pin_check=pin_check,
    )
    # Binding/source/dual-ledger checks are free and happen before any CLI
    # import that could construct a provider.
    adapter.check()
    _add_import_roots(Path(adapter.binding["source_worktree"]))
    if run_enabled:
        with downstream_allowlist():
            with adapter.activate():
                code = _formal_main(entry, formal_argv)
    else:
        code = _formal_main(entry, formal_argv)
    return FormalRun(entry=entry, run_enabled=run_enabled, returncode=code, adapter=adapter)


def _cli(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        separator = raw.index("--")
    except ValueError:
        wrapper_argv, formal_argv = raw, []
    else:
        wrapper_argv, formal_argv = raw[:separator], raw[separator + 1:]
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Use the first -- to separate launcher options from formal CLI argv.",
    )
    parser.add_argument("entry", choices=FORMAL_ENTRIES)
    parser.add_argument("--binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--supervision-root", type=Path, default=DEFAULT_SUPERVISION_ROOT)
    args = parser.parse_args(wrapper_argv)
    result = run_formal(
        args.entry, formal_argv,
        binding_path=args.binding,
        supervision_root=args.supervision_root,
    )
    print(
        json.dumps(
            {
                "entry": result.entry,
                "run_enabled": result.run_enabled,
                "returncode": result.returncode,
                "binding": str(args.binding),
                "source_commit": result.adapter.binding.get("source_commit"),
                "constructed_provider_count": len(result.adapter.constructed),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return result.returncode


if __name__ == "__main__":  # pragma: no cover - exercised by the argv smoke command.
    raise SystemExit(_cli())


__all__ = [
    "DEFAULT_BINDING",
    "DEFAULT_SUPERVISION_ROOT",
    "FORMAL_ENTRIES",
    "FormalRun",
    "PlanningTransport",
    "downstream_allowlist",
    "run_formal",
]

