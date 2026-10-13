"""Collect the raw citation edges for the Candidate Corpus core.

Kept in its own step because it is the only part of the skeleton pipeline that
talks to the network at length; everything downstream reads the parquet it
writes.

The nested shape of the edge payload is a trap: references answer with
citedPaper and citations answer with citingPaper, and the paper metadata lives
inside that object, not at the top level.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]


def _safe(value: Any, limit: int = 90) -> str:
    return ("" if value is None else str(value)).encode("ascii", "replace").decode("ascii")[:limit]


def collect(
    corpus_path: Path,
    out_dir: Path,
    *,
    references_limit: int = 300,
    citations_limit: int = 300,
    max_workers: int = 12,
    progress_every: int = 50,
    gateway: Any = None,
) -> dict[str, Any]:
    """Fetch references and citations for every core paper, with a disk cache."""

    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    papers = corpus["papers"]
    ids = sorted({str(p["paper_id"]) for p in papers if p.get("paper_id")})
    print("core papers with an S2 id:", len(ids), flush=True)

    if gateway is None:
        from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
        gateway = S2IntelligenceGateway()
    out_dir.mkdir(parents=True, exist_ok=True)
    edge_path = out_dir / "GRAPH_EDGES.jsonl"

    previous = {}
    if edge_path.exists():
        with edge_path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                    pid = str(record["source_paper"])
                    # Reusing a directory for another corpus must not silently
                    # treat old empty failures as success in the new run.
                    if corpus.get("run_id") and record.get("run_id") != corpus["run_id"]:
                        continue
                    if corpus.get("plan_hash") and record.get("plan_hash") != corpus["plan_hash"]:
                        continue
                    previous[pid] = record
                except (ValueError, KeyError, TypeError):
                    continue

    def completed(record, relation):
        status = (record.get("relation_status") or {}).get(relation) or {}
        return (status.get("state") == "success" and status.get("ok") is True
                and int(status.get("requested_limit") or 0) >=
                (references_limit if relation == "references" else citations_limit))

    done_ids = {pid for pid, record in previous.items()
                if all(completed(record, relation) for relation in ("references", "citations"))}
    if previous:
        print("resuming; successful papers:", len(done_ids), flush=True)

    lock = threading.Lock()
    handle = edge_path.open("a", encoding="utf-8")
    counters = {"papers": 0, "refs": 0, "cites": 0, "fail_refs": 0, "fail_cites": 0}
    started = time.time()

    def one(paper_id: str) -> None:
        record: dict[str, Any] = {**previous.get(paper_id, {}),
            "source_paper": paper_id, "run_id": corpus.get("run_id"),
            "plan_hash": corpus.get("plan_hash")}
        statuses = record.setdefault("relation_status", {})
        for relation, limit in (("references", references_limit), ("citations", citations_limit)):
            if completed(record, relation):
                continue
            old_status = statuses.get(relation) or {}
            try:
                items, response = gateway._paper_edges(paper_id, relation=relation, limit=limit)
                status = getattr(response, "status_code", 0)
                ok = bool(getattr(response, "ok", status == 200))
                category = str(getattr(response, "status_category", "") or "")
                audit = dict(getattr(response, "audit", {}) or {})
                error = str(getattr(response, "error", "") or "")
                payload = getattr(response, "payload", {}) or {}
                partial = category == "partial_availability" or bool(audit.get("partial_availability"))
                success = ok and not partial
                truncated = bool(payload.get("next") is not None or audit.get("truncated"))
                receipt = {"ok": ok, "state": "success" if success else ("partial" if items else "failed"),
                           "status_code": status, "status_category": category,
                           "error": error, "audit": audit, "truncated": truncated,
                           "requested_limit": limit, "attempts": int(old_status.get("attempts") or 0) + 1}
            except Exception as exc:  # noqa: BLE001
                items, success = [], False
                receipt = {"ok": False, "state": "failed", "status_code": 0,
                           "status_category": "exception", "error": str(exc), "audit": {},
                           "truncated": False, "requested_limit": limit,
                           "attempts": int(old_status.get("attempts") or 0) + 1}
            statuses[relation] = receipt
            if not success:
                with lock:
                    counters["fail_refs" if relation == "references" else "fail_cites"] += 1
            inner = "citedPaper" if relation == "references" else "citingPaper"
            rows = []
            for item in items:
                node = item.get(inner) or {}
                if not isinstance(node, dict):
                    continue
                rows.append({
                    "paper_id": str(node.get("paperId") or ""),
                    "corpus_id": str(node.get("corpusId") or ""),
                    "title": str(node.get("title") or "")[:300],
                    "year": node.get("year"),
                    "citation_count": node.get("citationCount"),
                    "is_influential": bool(item.get("isInfluential")),
                    "has_context": bool(item.get("contexts")),
                })
            rows = [r for r in rows if r["paper_id"] or r["corpus_id"]]
            if not success:
                # Keep partial evidence while retrying the failed relation later.
                combined = {(r.get("paper_id"), r.get("corpus_id")): r
                            for r in record.get(relation) or []}
                combined.update({(r["paper_id"], r["corpus_id"]): r for r in rows})
                rows = list(combined.values())
            record[relation] = rows
            with lock:
                counters["refs" if relation == "references" else "cites"] += len(record[relation])
        with lock:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            counters["papers"] += 1
            if progress_every and counters["papers"] % progress_every == 0:
                elapsed = time.time() - started
                rate = counters["papers"] / elapsed * 60 if elapsed else 0
                left = (len(ids) - len(done_ids) - counters["papers"]) / max(rate, 0.01)
                print("  %d/%d | refs=%d cites=%d | %.1f papers/min | ~%.0f min left"
                      % (counters["papers"], len(ids) - len(done_ids), counters["refs"],
                         counters["cites"], rate, left), flush=True)

    todo = [i for i in ids if i not in done_ids]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(one, todo))
    handle.close()

    summary = {
        "schema_version": "optomind.graph_collection.v2",
        "run_id": corpus.get("run_id"), "plan_hash": corpus.get("plan_hash"),
        "core_papers": len(ids),
        "previously_successful": len(done_ids),
        "newly_collected": counters["papers"],
        "references_edges": counters["refs"],
        "citations_edges": counters["cites"],
        "reference_failures": counters["fail_refs"],
        "citation_failures": counters["fail_cites"],
        "elapsed_seconds": round(time.time() - started, 1),
        "edge_file": str(edge_path),
    }
    (out_dir / "EDGE_COLLECTION_SUMMARY.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="outputs/upgrade3/QP02/CANDIDATE_CORPUS.json")
    parser.add_argument("--out", default="outputs/upgrade3/SKELETON")
    parser.add_argument("--references-limit", type=int, default=300)
    parser.add_argument("--citations-limit", type=int, default=300)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    cfg = json.loads((REPO / "config/scholarly_skeleton.json").read_text(encoding="utf-8"))
    collect(REPO / args.corpus, REPO / args.out,
            references_limit=args.references_limit,
            citations_limit=args.citations_limit,
            max_workers=args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
