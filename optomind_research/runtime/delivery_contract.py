"""Strict final artifact contract for a bilingual review + research plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def validate_pdf(path: Optional[Path]) -> Dict[str, Any]:
    """Validate a PDF without trusting a filename or a prior status flag."""
    if not path:
        return {"path": "", "ok": False, "reason": "missing_path"}
    candidate = Path(path)
    if not candidate.is_file():
        return {"path": str(candidate), "ok": False, "reason": "missing_file"}
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return {"path": str(candidate), "ok": False, "reason": f"stat:{exc}"}
    if size < 128 or candidate.read_bytes()[:5] != b"%PDF-":
        return {"path": str(candidate), "ok": False, "reason": "invalid_pdf_header"}
    pages = None
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(str(candidate)).pages)
        if pages < 1:
            return {"path": str(candidate), "ok": False, "reason": "zero_pages", "bytes": size}
    except ImportError:
        # Header + nonempty is the strongest check available in minimal installs.
        pass
    except Exception as exc:
        return {"path": str(candidate), "ok": False, "reason": f"pdf_open:{type(exc).__name__}", "bytes": size}
    result = {"path": str(candidate), "ok": True, "bytes": size}
    if pages is not None:
        result["pages"] = pages
    return result


def _read_json(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not Path(path).is_file():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _status_ok(report: Dict[str, Any], accepted: Iterable[str]) -> bool:
    return str(report.get("status") or "").strip().lower() in set(accepted)


_ACCEPTED_REVIEW_AUDIT_STATUSES = {"passed", "completed", "ready"}
_ACCEPTED_PUBLICATION_STATUSES = {
    "submission_ready",
    "compiled",
    "compiled_awaiting_metadata",
    "completed",
}

# A degraded status means the stage produced usable, paid-for content while
# explicitly recording what failed.  It never blocks delivery on its own.
_DEGRADED_STATUSES = {"completed_with_warnings", "partial"}
# A stage that is legitimately waiting for a human decision is not a failure.
_AWAITING_HUMAN_STATUSES = {
    "awaiting_human_review",
    "awaiting_approval",
    "waiting_for_human",
}


def build_delivery_gate(
    *,
    work_dir: Path,
    package: Dict[str, Any],
    quality_report: Optional[Dict[str, Any]] = None,
    latex_report: Optional[Dict[str, Any]] = None,
    chinese_translation_report: Optional[Dict[str, Any]] = None,
    chinese_latex_report: Optional[Dict[str, Any]] = None,
    research_plan_publication_report: Optional[Dict[str, Any]] = None,
    require_review: bool,
    require_chinese_review: bool,
    require_research_plan: bool,
) -> Dict[str, Any]:
    """Return a deterministic, auditable terminal gate.

    The gate is intentionally independent of the worker's claimed status.  It
    may accept ``compiled_awaiting_metadata`` because missing author metadata
    is an explicit nonblocking warning, but never accepts a missing or
    unreadable PDF.

    Top-level "status" enumeration (exactly three values):
        "failed"   -- blocking_checks is non-empty;
        "degraded" -- no blocking, but degraded_checks or
                      awaiting_human_checks is non-empty;
        "passed"   -- blocking / degraded / awaiting_human all empty.
    Awaiting-human is expressed through the awaiting_human_checks list,
    NOT as an independent top-level status value ("blocking" itself is a
    per-check boolean attribute, not a top-level state).
    """
    work_dir = Path(work_dir)
    checks: Dict[str, Any] = {}
    artifacts = package.get("artifacts") or {}
    review_pdf = package.get("latex_pdf_path") or artifacts.get("latex_pdf")
    review_zh_pdf = package.get("chinese_latex_pdf_path") or artifacts.get("chinese_latex_pdf")
    plan_pdf = package.get("research_plan_latex_pdf_path") or artifacts.get("research_plan_latex_pdf")
    plan_zh_pdf = package.get("research_plan_chinese_latex_pdf_path") or artifacts.get("research_plan_chinese_latex_pdf")
    if require_review:
        checks["english_review_pdf"] = validate_pdf(Path(review_pdf) if review_pdf else None)
        checks["english_review_audit"] = {
            "ok": _status_ok(
                quality_report or {},
                _ACCEPTED_REVIEW_AUDIT_STATUSES,
            ),
            "status": (quality_report or {}).get("status", "missing"),
            "accepted_statuses": sorted(_ACCEPTED_REVIEW_AUDIT_STATUSES),
        }
    if require_chinese_review:
        chinese_report = chinese_translation_report or {}
        chinese_status = str(chinese_report.get("status") or "missing").strip().lower()
        # The PDF check validates only a real caller-provided PDF path.  A
        # Markdown partial transcript is not a PDF: feeding it through
        # validate_pdf() can never succeed and would turn the degraded path
        # into a permanently blocking check.
        checks["chinese_review_pdf"] = validate_pdf(
            Path(review_zh_pdf) if review_zh_pdf else None
        )
        markers_preserved = (
            int(chinese_report.get("citation_marker_count_translation") or 0)
            == int(chinese_report.get("citation_marker_count_source") or 0)
        )
        checks["chinese_translation_audit"] = {
            "ok": chinese_status == "completed",
            "degraded": chinese_status in _DEGRADED_STATUSES,
            "status": chinese_report.get("status", "missing"),
            "failed_unit_ids": chinese_report.get("failed_unit_ids") or [],
            "citation_markers_preserved": markers_preserved,
        }
        partial_md_path = str(chinese_report.get("partial_translated_path") or "")
        if partial_md_path:
            # A claimed Markdown partial is degraded evidence of paid-for work
            # only when it is readable and its citations survived.  A claimed
            # but unreadable or citation-losing partial must block instead of
            # masquerading as a deliverable.
            try:
                partial_readable = bool(
                    Path(partial_md_path).read_text(encoding="utf-8").strip()
                )
            except (OSError, UnicodeDecodeError):
                # UnicodeDecodeError derives from ValueError, not OSError, so a
                # mojibake partial used to crash build_delivery_gate outright --
                # taking down every unrelated check with it.  An undecodable
                # partial is simply not a deliverable.
                partial_readable = False
            checks["chinese_translation_partial"] = {
                "ok": False,
                "degraded": partial_readable and markers_preserved,
                "status": (
                    "present" if partial_readable else "claimed_but_unreadable"
                ),
                "path": partial_md_path,
            }
    plan_audit_status = ""
    if require_research_plan:
        checks["english_research_plan_pdf"] = validate_pdf(Path(plan_pdf) if plan_pdf else None)
        checks["chinese_research_plan_pdf"] = validate_pdf(Path(plan_zh_pdf) if plan_zh_pdf else None)
        plan_publication_report_path = (
            artifacts.get("research_plan_publication_report")
            or work_dir
            / "publication"
            / "research_plan"
            / "BILINGUAL_RESEARCH_PLAN_REPORT.json"
        )
        plan_audit_path = (
            package.get("research_plan_audit_path")
            or work_dir / "research_program" / "RESEARCH_PLAN_AUDIT.json"
        )
        checks["research_plan_publication_audit"] = {
            # This is the top-level bilingual publication report.  Renderer
            # statuses such as compiled_awaiting_metadata belong inside its
            # English/Chinese subreports and must not masquerade as the
            # completed publication audit itself.
            "ok": _status_ok(
                research_plan_publication_report or {},
                ("completed",),
            ),
            "awaiting_human": str(
                (research_plan_publication_report or {}).get("status", "")
            ).strip().lower()
            in _AWAITING_HUMAN_STATUSES,
            "status": (research_plan_publication_report or {}).get("status", "missing"),
            "path": str(plan_publication_report_path),
            "accepted_statuses": ["completed"],
        }
        plan_audit = _read_json(Path(plan_audit_path))
        plan_audit_status = str(plan_audit.get("status", "")).strip().lower()
        checks["research_plan_audit"] = {
            "ok": _status_ok(plan_audit, ("passed", "completed")),
            "awaiting_human": plan_audit_status in _AWAITING_HUMAN_STATUSES,
            "status": plan_audit.get("status", "missing"),
            "path": str(plan_audit_path),
            "accepted_statuses": ["passed", "completed"],
        }
    # Every research-plan check above is downstream of the research_plan stage
    # itself.  When that stage legitimately stops for a human decision it never
    # writes RESEARCH_PLAN_AUDIT.json and never reaches the publication step, so
    # keying the exemption on the audit file alone turns a correct stop into four
    # blocking failures.  Verified on rhr_be780761: research_plan stopped at
    # ``waiting_for_human`` (initial_discovery_focus_not_completed_needs_more_
    # literature), no audit file was ever produced, and the gate reported
    # status=failed for a run that had already delivered a 41-page English and a
    # 39-page Chinese PDF.  The stage's own status is the authoritative signal
    # that the absence was a decision rather than a missing artifact.
    plan_stage_row = (package.get("stage_status") or {}).get("research_plan") or {}
    plan_stage_status = str(plan_stage_row.get("status") or "").strip().lower()
    # A human gate that times out rewrites the stage to ``completed`` and keeps
    # what it was in ``original_status``.  The artifacts stay absent either way
    # -- a gate settles a decision, it does not produce a research plan -- so
    # reading the live status alone silently undid the exemption above and sent
    # the run back to ``failed``, this time without even the awaiting_human
    # trace that explained it.  Replayed on rhr_be780761: flipping that stage
    # from waiting_for_human to completed moved the gate degraded -> failed.
    plan_original_status = str(
        plan_stage_row.get("original_status") or ""
    ).strip().lower()
    plan_awaiting_reason = next(
        (
            value
            for value in (
                plan_stage_status,
                plan_original_status,
                plan_audit_status,
            )
            if value in _AWAITING_HUMAN_STATUSES
        ),
        "",
    )
    if plan_awaiting_reason:
        for plan_key in (
            "english_research_plan_pdf",
            "chinese_research_plan_pdf",
            "research_plan_publication_audit",
            "research_plan_audit",
        ):
            check = checks.get(plan_key)
            if isinstance(check, dict) and not check.get("ok"):
                check["awaiting_human"] = True
                check["awaiting_human_reason"] = (
                    "research_plan_" + plan_awaiting_reason
                )
    checks["latex_audit"] = {
        "ok": (not require_review)
        or _status_ok(latex_report or {}, _ACCEPTED_PUBLICATION_STATUSES),
        "status": (latex_report or {}).get("status", "not_required"),
        "accepted_statuses": sorted(_ACCEPTED_PUBLICATION_STATUSES),
    }
    if require_chinese_review:
        chinese_latex_status = str(
            (chinese_latex_report or {}).get("status") or "missing"
        ).strip().lower()
        checks["chinese_latex_audit"] = {
            "ok": _status_ok(
                chinese_latex_report or {},
                _ACCEPTED_PUBLICATION_STATUSES,
            ),
            # disabled_translation_failed is downstream collateral of the
            # translation stage, not an independent content failure: the
            # translation audit above carries the real signal.
            "degraded": chinese_latex_status
            in (_DEGRADED_STATUSES | {"disabled_translation_failed"}),
            "status": (chinese_latex_report or {}).get("status", "missing"),
            "accepted_statuses": sorted(_ACCEPTED_PUBLICATION_STATUSES),
        }
    blocking: list[str] = []
    degraded: list[str] = []
    awaiting_human: list[str] = []
    for name, value in checks.items():
        if not isinstance(value, dict) or value.get("ok", False):
            continue
        if value.get("awaiting_human"):
            awaiting_human.append(name)
        elif value.get("degraded"):
            degraded.append(name)
        else:
            blocking.append(name)
    # Only a run that was asked to produce the English side can have an
    # English deliverable; latex_audit exists even when review production is
    # disabled, so key presence alone would be vacuously true.
    english_deliverable = bool(require_review) and all(
        (checks.get(name) or {}).get("ok", False)
        for name in ("english_review_pdf", "english_review_audit", "latex_audit")
        if name in checks
    )
    if blocking:
        status = "failed"
    elif degraded or awaiting_human:
        status = "degraded"
    else:
        status = "passed"
    author_warning = (
        "author_metadata_placeholder_allowed"
        if not package.get("publication_metadata_path")
        else ""
    )
    return {
        "schema_version": "research_harness.delivery_gate.v2",
        "status": status,
        "passed": not blocking and not degraded and not awaiting_human,
        "blocking_checks": blocking,
        "degraded_checks": degraded,
        "awaiting_human_checks": awaiting_human,
        "english_deliverable": english_deliverable,
        "checks": checks,
        "author_metadata_warning": author_warning,
        "policy": (
            "blocking means a requested artifact is missing or unreadable; "
            "degraded means usable content was produced with recorded failures; "
            "awaiting_human means a stage legitimately stopped for a decision. "
            "Only blocking fails delivery."
        ),
    }
