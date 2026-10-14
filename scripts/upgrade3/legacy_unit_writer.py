"""Current complete BODY materials -> legacy unit writer -> existing assembly.

Default preview is offline and saves every exact request. --run explicitly
uses a dedicated persistent CNY ledger. --retry-failed authorizes a new charged
attempt; otherwise interrupted, uncertain and incomplete attempts stay pending.
No GUIDE conversion, historic manuscript import, automatic model switch, or
previous-body prefix is used by this route.
"""
from __future__ import annotations

from contextlib import closing
import argparse
import hashlib
import json
import math
import sqlite3
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.legacy_unit_route import run_legacy_units, _hash, _read, _write
from optomind_research.runtime.upgrade3.review_unit_writer import BODY_VERSIONS
from scripts.upgrade3.writer_candidates import make_live_factory, tokenizer_counter


def add_arguments(p: argparse.ArgumentParser, *, include_io: bool = True) -> argparse.ArgumentParser:
    """Use the same author/quality/budget flags in standalone and normal CLI."""
    if include_io:
        p.add_argument("--input", help="Current lossless FULL_BODY_INPUT.json")
        p.add_argument("--output-dir", help="Dedicated preview/run/resume directory")
    p.add_argument("--body-version", choices=BODY_VERSIONS, default="baseline",
                   help="baseline preserves saved requests; chapter_coherence adds actual full-outline duties and substantive-gap checks")
    p.add_argument("--model", choices=("qwen3.5-plus",), default="qwen3.5-plus")
    p.add_argument("--output-tokens", type=int, default=32768)
    p.add_argument("--thinking-budget", type=int, default=8192)
    p.add_argument("--budget-limit", type=float, default=30.0,
                   help="Absolute lifetime CNY cap of this separate experiment, never an increment")
    p.add_argument("--ledger", help="Explicit dedicated SQLite ledger, required with --run; reuse for all resumes")
    p.add_argument("--shared-project-binding", help="Explicit immutable JSON authorizing this BODY run to attach to an existing project/account ledger and mapping directory")
    p.add_argument("--account-ledger", help="Optional second SQLite ledger shared by physical calls across projects; reuse its path")
    p.add_argument("--account-budget-limit", type=float, help="Immutable account lifetime CNY cap; required only for a new account ledger")
    p.add_argument("--account-mapping-dir", help="Persistent dual-reservation receipt directory; defaults beside the account ledger; reuse on resume")
    p.add_argument("--budget-scope", help="Explicit experiment identity shared across input subsets and holdout books; immutable for this ledger")
    p.add_argument("--reconcile-receipt", help="Audit and settle an existing open hold from a local JSON receipt; no provider calls")
    p.add_argument("--run", action="store_true", help="Explicitly allow live provider calls")
    p.add_argument("--retry-failed", action="store_true",
                   help="With --run, permit an explicit new writer attempt or one recorded quality failure retry without replayable output; retain prior attempts and ledger holds")
    p.add_argument("--quality-control", action=argparse.BooleanOptionalAction, default=False,
                   help="Independent Plus actual-body assessment, at most one bounded correction/completion and post-check; original writer output retained")
    p.add_argument("--reparse-saved", action="store_true",
                   help="Explicitly reparse saved quality RAW without rebuying attempted stages; unfinished stages require --run and the same owned ledger")
    p.add_argument("--article-edit", action=argparse.BooleanOptionalAction, default=False,
                   help="One resumable Plus whole-article local edit after full-book writing/quality completes; selected subsets explicitly skip")
    p.add_argument("--only-unit", action="append", default=[], metavar="CHAPTER:UNIT",
                   help="Repeatable pilot selection; retains full book/material context and reusable writer request identities")
    p.add_argument("--key-file", help="Local credential file; not read in preview, never copied")
    p.add_argument("--key-index", type=int, help="Select exactly this 1-based candidate in --key-file, without environment priority or key fallback")
    p.add_argument("--tokenizer", help="Existing local tokenizer.json, no downloads")
    return p


def parser() -> argparse.ArgumentParser:
    return add_arguments(argparse.ArgumentParser(description=__doc__))


def _ledger_snapshot(ledger: Path, marker: Path, scope: dict) -> dict:
    """Read-only receipt validation: missing/corrupt state is never a new cap."""
    if not ledger.is_file():
        raise ValueError("legacy_ledger_missing:no_budget_reset")
    if not marker.is_file() or _read(marker) != scope:
        raise ValueError("legacy_ledger_scope_conflict:retain_existing_budget_and_reconcile")
    return _ledger_contents_snapshot(ledger, scope["budget_limit_cny"])


def _ledger_contents_snapshot(ledger: Path, limit: float) -> dict:
    """Validate every existing hold before any constructor can initialize SQL."""
    if not ledger.is_file():
        raise ValueError("legacy_ledger_missing:no_budget_reset")
    try:
        with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
            if row is None or not math.isfinite(float(row[0])) or float(row[0]) != limit:
                raise ValueError("legacy_stored_budget_limit_changed")
            rows = db.execute("SELECT reservation_id, amount_cny, actual_cny, status FROM reservations").fetchall()
    except sqlite3.Error as exc:
        raise ValueError("legacy_ledger_invalid_sqlite:no_budget_reset") from exc
    actual = held = used = 0.0
    reservations = []
    for reservation_id, amount, cost, status in rows:
        if status not in {"settled", "reserved", "uncertain"}:
            raise ValueError("legacy_invalid_reservation_status")
        if amount is None or not math.isfinite(float(amount)) or float(amount) < 0 or (cost is not None and
                (not math.isfinite(float(cost)) or float(cost) < 0)):
            raise ValueError("legacy_invalid_reservation_amount")
        actual += float(cost or 0)
        held += float(amount) if status in {"reserved", "uncertain"} else 0
        used += float(cost if cost is not None else amount) if status == "settled" else float(amount)
        reservations.append({"reservation_id": reservation_id, "amount_cny": amount,
                             "actual_cny": cost, "status": status})
    if not all(math.isfinite(value) for value in (actual, held, used)):
        raise ValueError("legacy_invalid_budget_total")
    return {"ledger_path": str(ledger), "limit_cny": limit,
            "actual_cny": actual, "reserved_cny": held, "remaining_cny": max(0, limit - used),
            "reservations": reservations}


def _shared_project_contract(args) -> tuple[dict, dict]:
    """Validate an explicit complete attachment, never infer an experiment."""
    source = Path(args.shared_project_binding).expanduser().resolve()
    try:
        raw = source.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("shared_project_binding_invalid:no_budget_reset") from exc
    if not isinstance(value, dict):
        raise ValueError("shared_project_binding_invalid:no_budget_reset")

    def path_field(name):
        item = value.get(name)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("shared_project_binding_missing:" + name)
        path = Path(item).expanduser()
        return (path if path.is_absolute() else source.parent / path).resolve()

    def cap_field(name):
        item = value.get(name)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item <= 0:
            raise ValueError("shared_project_binding_invalid:" + name)
        return float(item)

    project = path_field("project_ledger")
    account = path_field("account_ledger")
    mappings = path_field("account_mapping_dir")
    project_cap = cap_field("project_limit_cny")
    account_cap = cap_field("account_limit_cny")
    identity = value.get("project_scope") if "project_scope" in value else value.get("question_sha256")
    if "project_scope" not in value and (not isinstance(identity, str) or len(identity) != 64
                                           or any(char not in "0123456789abcdefABCDEF" for char in identity)):
        raise ValueError("shared_project_binding_invalid:question_sha256")
    if not isinstance(identity, str) or not identity.strip() or args.budget_scope != identity:
        raise ValueError("shared_project_scope_conflict:no_budget_reset")
    if not args.ledger or Path(args.ledger).expanduser().resolve() != project or args.budget_limit != project_cap:
        raise ValueError("shared_project_ledger_or_cap_conflict:no_budget_reset")
    if not args.account_ledger or Path(args.account_ledger).expanduser().resolve() != account or account == project:
        raise ValueError("shared_project_account_ledger_conflict:no_budget_reset")
    if args.account_budget_limit is not None and args.account_budget_limit != account_cap:
        raise ValueError("shared_project_account_cap_conflict:no_budget_reset")
    if args.account_mapping_dir and Path(args.account_mapping_dir).expanduser().resolve() != mappings:
        raise ValueError("shared_project_mapping_conflict:no_budget_reset")
    if not mappings.is_dir():
        raise ValueError("shared_project_mapping_missing:no_budget_reset")
    # No GlobalBudgetLedger construction until both existing databases have
    # passed read-only cap and full settled/reserved/uncertain validation.
    _ledger_contents_snapshot(project, project_cap)
    _ledger_contents_snapshot(account, account_cap)
    scope = {"route": "legacy_unit_writer_shared_project", "ledger_path": str(project),
             "budget_limit_cny": project_cap, "experiment_scope": identity,
             "account_ledger": str(account), "account_limit_cny": account_cap,
             "account_mapping_dir": str(mappings), "project_binding_path": str(source),
             "project_binding_sha256": hashlib.sha256(raw).hexdigest()}
    return scope, value


def _shared_project_factory(args, counter):
    from optomind_research.runtime.upgrade3.dual_budget import bind_json
    scope, source = _shared_project_contract(args)
    output = Path(args.output_dir).expanduser().resolve()
    binding = output / "BUDGET_BINDING.json"
    attachment = output / "SHARED_PROJECT_ATTACHMENT.json"
    if binding.exists() and _read(binding) != scope:
        raise ValueError("shared_project_output_binding_conflict:no_budget_reset")
    evidence = {"schema_version": "shared_project_attachment.v1", "budget_binding": scope,
                "source_binding": source}
    if attachment.exists() and _read(attachment) != evidence:
        raise ValueError("shared_project_output_binding_conflict:no_budget_reset")
    report_path = output / "RUN_REPORT.json"
    if not binding.exists() and report_path.is_file():
        old = (_read(report_path).get("ledger") or {}).get("ledger_path")
        if old and Path(old).resolve() != Path(scope["ledger_path"]):
            raise ValueError("legacy_output_ledger_changed:no_budget_reset")
    args.account_budget_limit = scope["account_limit_cny"]
    args.account_mapping_dir = scope["account_mapping_dir"]
    account = _bind_account(args, output, Path(scope["ledger_path"]))
    bind_json(binding, scope)
    bind_json(attachment, evidence)
    args.budget_ledger = scope["ledger_path"]
    args.responses = None
    args.allow_max = False
    base_factory = make_live_factory(args, token_counter=counter)

    def snapshot():
        current, _ = _shared_project_contract(args)
        if current != scope or not binding.is_file() or _read(binding) != scope:
            raise ValueError("shared_project_output_binding_conflict:no_budget_reset")
        return _ledger_contents_snapshot(Path(scope["ledger_path"]), scope["budget_limit_cny"])

    def guarded_factory(role, attempt, profile):
        snapshot()
        return base_factory(role, attempt, profile)

    guarded_factory.execution_mode = "live"
    guarded_factory.ledger_snapshot = snapshot
    guarded_factory.account_ledger_snapshot = account.as_dict
    return guarded_factory


def _dedicated_factory(args, book, counter):
    if getattr(args, "shared_project_binding", None):
        return _shared_project_factory(args, counter)
    if not math.isfinite(args.budget_limit) or args.budget_limit <= 0:
        raise ValueError("legacy_budget_must_be_finite_positive")
    if not args.ledger:
        raise ValueError("legacy_run_requires_explicit_dedicated_ledger:reuse_for_every_resume")
    ledger = Path(args.ledger).expanduser().resolve()
    marker = ledger.with_name(ledger.name + ".legacy-route.json")
    scope = {"route": "legacy_unit_writer", "ledger_path": str(ledger),
             "budget_limit_cny": args.budget_limit}
    if args.budget_scope is not None:
        if not args.budget_scope.strip():
            raise ValueError("legacy_budget_scope_must_be_nonempty")
        scope["experiment_scope"] = args.budget_scope
    else:
        scope["book_sha256"] = _hash(book)
    output = Path(args.output_dir).expanduser().resolve()
    binding = output / "BUDGET_BINDING.json"
    if binding.exists() and _read(binding) != scope:
        raise ValueError("legacy_output_ledger_changed:legacy_ledger_scope_conflict:no_budget_reset")
    # Upgrade old output directories only when the existing run receipt names
    # the same ledger. A new ledger must never turn a resume into a new budget.
    report_path = output / "RUN_REPORT.json"
    if not binding.exists() and report_path.is_file():
        old_ledger = (_read(report_path).get("ledger") or {}).get("ledger_path")
        if old_ledger and Path(old_ledger).resolve() != ledger:
            raise ValueError("legacy_output_ledger_changed:no_budget_reset")
    # The same ledger remains valid when code fixes require a new output root.
    # Never adopt, reset, migrate or raise an earlier experiment's budget.
    if ledger.exists() and not marker.exists():
        raise ValueError("legacy_existing_ledger_not_owned_by_this_run:retain_existing_budget_and_reconcile")
    if marker.exists() and _read(marker) != scope:
        raise ValueError("legacy_ledger_scope_conflict:retain_existing_budget_and_reconcile")
    if marker.exists() and not ledger.is_file():
        raise ValueError("legacy_ledger_missing:no_budget_reset")
    if not ledger.exists():
        from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.touch(exist_ok=False)  # Concurrent first creators fail closed.
        GlobalBudgetLedger(limit_cny=args.budget_limit, path=ledger)
        _write(marker, scope)
    _ledger_snapshot(ledger, marker, scope)
    account = _bind_account(args, output, ledger)
    _write(binding, scope)
    args.budget_ledger = str(ledger)
    args.responses = None
    args.allow_max = False
    base_factory = make_live_factory(args, token_counter=counter)

    def snapshot():
        return _ledger_snapshot(ledger, marker, scope)

    def guarded_factory(role, attempt, profile):
        snapshot()  # Validate durable ownership; reserve atomically enforces cap.
        return base_factory(role, attempt, profile)

    guarded_factory.execution_mode = "live"
    guarded_factory.ledger_snapshot = snapshot
    if account is not None:
        guarded_factory.account_ledger_snapshot = account.as_dict
    return guarded_factory


def _bind_account(args, output, project_path):
    """Keep account accounting optional, but immutable once this output uses it."""
    from optomind_research.runtime.upgrade3.dual_budget import (
        DualBudgetLedger, account_mapping_dir, bind_json, open_account_ledger, read_json)
    binding = output / "ACCOUNT_BUDGET_BINDING.json"
    prior = read_json(binding) if binding.is_file() else None
    if not args.account_ledger:
        if prior is not None:
            raise ValueError("account_ledger_required_for_bound_output:no_budget_reset")
        if args.account_budget_limit is not None or args.account_mapping_dir:
            raise ValueError("account_options_require_account_ledger")
        return None
    path = Path(args.account_ledger).expanduser().resolve()
    mappings = (Path(args.account_mapping_dir).expanduser().resolve() if args.account_mapping_dir
                else account_mapping_dir(path))
    if path == project_path:
        raise ValueError("dual_budget_requires_distinct_persistent_ledgers")
    if prior is not None:
        if prior["ledger_path"] != str(path) or prior["mapping_dir"] != str(mappings):
            raise ValueError("account_budget_binding_conflict:no_budget_reset")
        if not path.is_file():
            raise ValueError("account_ledger_missing:no_budget_reset")
    account = open_account_ledger(path, args.account_budget_limit)
    scope = {"schema_version": "dual_budget.output.v1", "ledger_path": str(path),
             "limit_cny": account.limit_cny, "mapping_dir": str(mappings)}
    bind_json(binding, scope)
    args.account_ledger = str(path)
    args.account_budget_limit = account.limit_cny
    args.account_mapping_dir = str(mappings)
    # Receipt-only recovery also runs when every model stage is already cached.
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
    DualBudgetLedger(GlobalBudgetLedger(path=project_path), account, mappings).recover_settlements()
    return account


def run_from_args(args, *, delivery_config_path=None, body_manifest_path=None) -> dict:
    """Build the owned existing factory once, then run the common delivery API."""
    if args.key_index is not None and (args.key_index < 1 or not args.key_file):
        raise ValueError("--key-index is 1-based and requires --key-file")
    if not args.account_ledger and (args.account_budget_limit is not None or args.account_mapping_dir):
        raise ValueError("account options require --account-ledger")
    if args.reparse_saved and not args.quality_control:
        raise ValueError("--reparse-saved requires --quality-control")
    if (not args.input and not body_manifest_path) or not args.output_dir:
        raise ValueError("--input and --output-dir are required for preview/run/resume")
    if args.input and body_manifest_path:
        raise ValueError("body_input_and_manifest_are_alternative_sources")
    if args.reconcile_receipt:
        raise ValueError("use the standalone ledger reconciliation command")
    prepared = None
    if body_manifest_path:
        from scripts.upgrade3.fullbody_writer import load_body_manifest
        book, prepared = load_body_manifest(body_manifest_path)
    else:
        book = _read(Path(args.input).expanduser().resolve())
    counter, tokenizer = tokenizer_counter(args.tokenizer)
    # The existing live factory remains lazy: cached/preflight requests do not
    # read credentials, and all physical calls share its project/account ledger.
    factory = _dedicated_factory(args, book, counter) if args.run else None
    from optomind_research.runtime.upgrade3.review_delivery import run_review_delivery
    report = run_review_delivery(start="body", out_dir=args.output_dir, body_input=book,
        body_options={"model": args.model, "output_tokens": args.output_tokens,
            "thinking_budget": args.thinking_budget, "budget_limit": args.budget_limit,
            "run": args.run, "retry_failed": args.retry_failed, "client_factory": factory,
            "token_counter": counter, "quality_control": args.quality_control,
            "only_units": args.only_unit, "article_edit": args.article_edit,
            "reparse_saved": args.reparse_saved, "body_version": args.body_version},
        config_path=delivery_config_path)
    _write(Path(args.output_dir).expanduser().resolve() / "TOKENIZER.json", tokenizer)
    if prepared is not None:
        _write(Path(args.output_dir).expanduser().resolve() / "SOURCE_MANIFEST.json", prepared)
    if factory is not None and hasattr(factory, "account_ledger_snapshot"):
        _write(Path(args.output_dir).expanduser().resolve() / "ACCOUNT_BUDGET_SNAPSHOT.json",
               factory.account_ledger_snapshot())
    return report


def main(argv=None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.key_index is not None and (args.key_index < 1 or not args.key_file):
        argument_parser.error("--key-index is 1-based and requires --key-file")
    if not args.account_ledger and (args.account_budget_limit is not None or args.account_mapping_dir):
        argument_parser.error("account options require --account-ledger")
    if args.reconcile_receipt:
        if not args.ledger or args.run or args.retry_failed:
            argument_parser.error("--reconcile-receipt requires --ledger and forbids live/retry flags")
        ledger = Path(args.ledger).expanduser().resolve()
        marker = ledger.with_name(ledger.name + ".legacy-route.json")
        if not marker.is_file():
            raise ValueError("legacy_reconciliation_requires_owned_ledger")
        scope = _read(marker)
        if scope.get("route") != "legacy_unit_writer" or scope.get("ledger_path") != str(ledger):
            raise ValueError("legacy_ledger_scope_conflict")
        _ledger_snapshot(ledger, marker, scope)
        from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
        receipt = _read(Path(args.reconcile_receipt).expanduser().resolve())
        audit = GlobalBudgetLedger(path=ledger).reconcile(
            receipt["reservation_id"], receipt["actual_cny"], evidence=receipt["evidence"],
            actor=receipt["actor"], reason=receipt["reason"])
        print(json.dumps({"reconciliation": audit, "ledger": _ledger_snapshot(ledger, marker, scope)},
                         ensure_ascii=False, indent=2))
        return 0
    if not args.input or not args.output_dir:
        argument_parser.error("--input and --output-dir are required for preview/run/resume")
    if args.reparse_saved and not args.quality_control:
        argument_parser.error("--reparse-saved requires --quality-control")
    report = run_from_args(args)
    print(json.dumps({key: report[key] for key in
          ("status", "model", "original_units", "complete_units", "model_calls",
           "estimated_all_units_cny", "estimated_all_units_within_budget", "capacity_blocked_units")},
          ensure_ascii=False, indent=2))
    from optomind_research.runtime.upgrade3.review_delivery import body_delivery_exit_code
    return body_delivery_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
