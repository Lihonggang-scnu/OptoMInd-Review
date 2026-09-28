"""Run Candidate Corpus layer 1 against a real schema and report what came back."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# The Windows console defaults to GBK and mangles the Chinese section headers.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - older interpreters
    pass


def safe(value, limit=100):
    return ("" if value is None else str(value)).encode("ascii", "replace").decode("ascii")[:limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default="outputs/upgrade3/QP02/FINAL_SCHEMA.json")
    parser.add_argument("--out", default="outputs/upgrade3/QP02/CANDIDATE_CORPUS.json")
    parser.add_argument("--snippets", default="outputs/upgrade3/QP02/SNIPPET_TEXT.sqlite")
    parser.add_argument("--per-query-limit", type=int, default=100)
    args = parser.parse_args()

    from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
    from optomind_research.runtime.upgrade3.candidate_corpus import (
        SnippetTextStore, build_candidate_corpus, plan_queries, write_corpus)

    plan = json.loads((REPO / args.schema).read_text(encoding="utf-8"))
    specs = plan_queries(plan)
    print("schema      :", args.schema)
    print("research    :", safe(plan.get("research_object"), 70))
    print("queries     :", len(specs),
          "(keyword %d / question %d)" % (
              sum(1 for s in specs if s.query_type == "keyword"),
              sum(1 for s in specs if s.query_type == "question")))
    print("per-query   :", args.per_query_limit, "  hard cap: 400")
    print()

    store = SnippetTextStore(REPO / args.snippets)
    corpus = build_candidate_corpus(
        plan, gateway=S2IntelligenceGateway(), per_query_limit=args.per_query_limit,
        enrich=True, snippet_store=store)
    store.close()

    print("--- per query ---", flush=True)
    for row in corpus["query_audit"]:
        print("  %-10s %-4s %-9s %-4s -> %-4d  %s" % (
            row["query_id"], row["facet_id"], row["query_type"], row["status"],
            row["returned"], safe(row["query_text"], 58)), flush=True)

    stats = corpus["statistics"]
    papers = corpus["papers"]
    hits = corpus["retrieval_hits"]
    print()
    print("=== 三个数字 ===")
    print("  distinct_papers        :", stats["distinct_papers"])
    print("  retrieval_hits 总数    :", stats["raw_hits"])
    print("    其中 snippet hits    :", stats["snippet_hits"])
    print("    其中 paper_search    :", stats["paper_search_hits"])
    print("  hit 未能关联到论文     :", stats["hits_with_unresolved_paper"])

    print()
    print("--- 同一 paper + 同一 query 的多条 snippet hit 是否保住 ---")
    grouped = {}
    for hit in hits:
        if hit["retrieval_source"] != "snippet_search":
            continue
        grouped.setdefault((hit["paper_id"], hit["query_id"]), []).append(hit)
    multi = {k: v for k, v in grouped.items() if len(v) > 1}
    print("  paper/query 组合数     :", len(grouped))
    print("  其中多条 snippet 的    :", len(multi))
    print("  这些组合贡献的 hit 数  :", sum(len(v) for v in multi.values()))
    for (paper_id, query_id), rows in sorted(multi.items(),
                                             key=lambda kv: -len(kv[1]))[:5]:
        print("    %s x%d  paper=%s" % (query_id, len(rows), safe(paper_id, 14)))
        for row in rows[:3]:
            loc = row["snippet_locator"] or {}
            print("        rank=%-3s score=%-8s %s p.%s-%s" % (
                row["result_rank"],
                round(row["score"], 4) if isinstance(row["score"], float) else row["score"],
                safe(loc.get("section"), 26), loc.get("start"), loc.get("end")))

    print()
    print("--- snippet locator 完整度 ---")
    snip = [h for h in hits if h["retrieval_source"] == "snippet_search"]
    print("  snippet hits           :", len(snip))
    print("  有 section             :", sum(1 for h in snip if h["snippet_locator"]["section"]))
    print("  有 offsets             :", sum(1 for h in snip if h["snippet_locator"]["start"] is not None))
    print("  有 sentence_offsets    :", sum(1 for h in snip if h["snippet_locator"]["sentence_offsets"]))
    print("  有 ref_mentions        :", sum(1 for h in snip if h["snippet_locator"]["ref_mentions"]))
    mentions = [m for h in snip for m in h["snippet_locator"]["ref_mentions"]]
    print("  ref_mentions 总数      :", len(mentions))
    print("    其中带 matchedPaperCorpusId:", sum(1 for m in mentions if m["matched_paper_corpus_id"]))
    print("  有 text_hash           :", sum(1 for h in snip if h["text_hash"]))
    print("  SQLite 中正文片段条数  :", SnippetTextStore(REPO / args.snippets).count())

    print()
    print("--- 字段覆盖 ---")
    for field_name in ("title", "year", "authors", "venue", "abstract", "doi"):
        have = sum(1 for p in papers if p.get(field_name))
        print("  %-9s %5d / %d" % (field_name, have, len(papers)))

    print()
    print("--- hit 最多的论文 ---")
    for row in sorted(stats["per_paper"],
                      key=lambda r: -r["hit_summary"]["total_hits"])[:5]:
        summary = row["hit_summary"]
        print("  hits=%-4d queries=%-3d facets=%-2d %s" % (
            summary["total_hits"], summary["distinct_queries"],
            summary["distinct_facets"], safe(row["paper_id"], 14)))

    path = write_corpus(REPO / args.out, corpus)
    print()
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
