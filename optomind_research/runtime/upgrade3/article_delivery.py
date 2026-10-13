"""C14 complete manuscripts enter the existing checker→numbering→publication.

No planning, BODY, editing, or second front/back generation occurs here.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from .manuscript_delivery import run_manuscript_delivery, verify_selected


def _catalogs(article_report, config, text):
    from .delivery_citations import _iter_catalog_identities
    catalogs = list(config.get("identity_catalogs") or [])
    namespace = article_report.get("citation_namespace")
    if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", namespace):
        raise ValueError("article_citation_namespace_required")
    identities = article_report.get("source_identity_map") or {}
    entries = []
    for token, identity in identities.items():
        token_ns, handle = token.split("::", 1) if "::" in token else (namespace, token)
        if token_ns != namespace:
            raise ValueError("article_identity_namespace_mismatch")
        if not (identity.get("canonical_paper_id") or identity.get("paper_id") or identity.get("doi")):
            raise ValueError("article_canonical_source_identity_required")
        entries.append({**identity, "paper_id": identity.get("canonical_paper_id") or identity.get("paper_id"),
                        "source_handle": handle})
    catalogs.insert(0, {"namespace": namespace, "entries": entries})
    handles = {}
    exact = {}
    for catalog in catalogs:
        ns = catalog.get("namespace") or ""
        for row in _iter_catalog_identities(catalog):
            for handle in row.get("handles") or [row.get("source_handle") or row.get("handle") or row.get("source_key")]:
                identity = row.get("canonical_paper_id") or row.get("paper_id") or row.get("doi")
                handles.setdefault(handle, set()).add(ns)
                key = (ns, handle)
                if key in exact and exact[key] != identity:
                    raise ValueError("conflicting_source_identity:" + str(key))
                exact[key] = identity
    bare = re.findall(r"(?<![:\w])P\d{3,}(?!\w)", text)
    if any(len(handles.get(token, ())) > 1 for token in bare):
        raise ValueError("ambiguous_bare_citation_namespace")
    return catalogs


def run_article_delivery(*, article_report, config, out_dir, run=False,
                         client_factory=None, token_counter=None, retry_failed=False,
                         live_args=None, tokenizer_info=None):
    if article_report.get("status") not in {"complete", "generated", "complete_with_diagnostics"}:
        raise ValueError("complete_article_manuscript_required")
    selected = article_report.get("selected_manuscript")
    path = verify_selected(selected)
    adopted_result_ref = article_report.get("adopted_result_ref")
    if adopted_result_ref and (adopted_result_ref.get("path") or adopted_result_ref.get("sha256")):
        verify_selected(adopted_result_ref)
    text = path.read_text(encoding="utf-8")
    catalogs = _catalogs(article_report, config, text)
    effective = {**config, "front_back_mode": "article_complete", "identity_catalogs": catalogs,
                 "manuscript_checker": {"enabled": True, **(config.get("manuscript_checker") or {})},
                 "post_body_context": {**(config.get("post_body_context") or {}),
                                       "material_records": article_report.get("material_records") or []}}
    # Shared coordinator accepts a completed manuscript report. The source
    # stage is explicitly labelled; it does not invoke 03 or its model.
    source_report = {"status": "complete", "final_manuscript": str(path), "source": "article_complete",
        "input_sha256": selected["sha256"], "adopted_result_ref": adopted_result_ref, "model_calls": 0}
    if live_args is not None:
        from .manuscript_delivery import run_live_delivery
        if Path(live_args.output).resolve() != Path(out_dir).resolve():
            raise ValueError("article_live_output_mismatch")
        report = run_live_delivery(args=live_args, config=effective, front_back_report=source_report,
            token_counter=token_counter, tokenizer_info=tokenizer_info, client_factory=client_factory)
    else:
        report = run_manuscript_delivery(config=effective, front_back_report=source_report, out_dir=out_dir,
            run=run, client_factory=client_factory, token_counter=token_counter, retry_failed=retry_failed)
    stages = report.get("stages") or {}
    if "03_front_back" in stages:
        stages["article_complete_input"] = stages.pop("03_front_back")
    report.update(input_branch="article_complete", source_article=selected,
                  source_namespace=article_report["citation_namespace"], adopted_result_ref=adopted_result_ref, front_back_generated=False,
                  scientific_acceptance=False, pdf_validated=False)
    output = Path(out_dir)
    (output / "ARTICLE_DELIVERY_REPORT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
