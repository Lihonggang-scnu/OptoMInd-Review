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
) -> dict[str, Any]:
    """Fetch references and citations for every core paper, with a disk cache."""

    from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway

    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    papers = corpus["papers"]
    ids = [p["paper_id"] for p in papers if p.get("paper_id")]
    print("core papers with an S2 id:", len(ids), flush=True)

    gateway = S2IntelligenceGateway()
    out_dir.mkdir(parents=True, exist_ok=True)
    edge_path = out_dir / "GRAPH_EDGES.jsonl"

    done_ids: set[str] = set()
    if edge_path.exists():
        with edge_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    done_ids.add(json.loads(line)["source_paper"])
                except Exception:  # noqa: BLE001
                    continue
        print("resuming; already collected:", len(done_ids), flush=True)

    lock = threading.Lock()
    handle = edge_path.open("a", encoding="utf-8")
    counters = {"papers": 0, "refs": 0, "cites": 0, "fail_refs": 0, "fail_cites": 0}
    started = time.time()

    def one(paper_id: str) -> None:
        record: dict[str, Any] = {"source_paper": paper_id, "references": [], "citations": []}
        for relation, limit in (("references", references_limit), ("citations", citations_limit)):
            try:
                items, response = gateway._paper_edges(paper_id, relation=relation, limit=limit)
                status = getattr(response, "status_code", 0)
            except Exception as exc:  # noqa: BLE001
                items, status = [], "ERR:" + type(exc).__name__
            if status != 200:
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
            record[relation] = [r for r in rows if r["paper_id"] or r["corpus_id"]]
            with lock:
                counters["refs" if relation == "references" else "cites"] += len(record[relation])
        with lock:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            counters["papers"] += 1
            if counters["papers"] % progress_every == 0:
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
        "core_papers": len(ids),
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
