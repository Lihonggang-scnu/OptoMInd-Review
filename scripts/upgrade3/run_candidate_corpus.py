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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default="outputs/upgrade3/QP02/FINAL_SCHEMA.json")
    parser.add_argument("--out", default="outputs/upgrade3/QP02/CANDIDATE_CORPUS.json")
    parser.add_argument("--snippets", default="outputs/upgrade3/QP02/SNIPPET_TEXT.sqlite")
    parser.add_argument("--per-query-limit", type=int, default=100)
    parser.add_argument("--outcome", help="Original M1 outcome; its plan must match --schema")
    parser.add_argument("--preview", "--preflight-only", action="store_true", help="Offline input/entry preview")
    parser.add_argument("--run", action="store_true", help="Explicit execution (also the historical CLI default)")
    parser.add_argument("--no-initial-topup", action="store_true", help="Exact one-pass legacy retrieval; no Qwen supplementation")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--key-index", type=int, default=1)
    parser.add_argument("--budget-ledger", type=Path, help="Existing shared project ledger")
    parser.add_argument("--budget-limit-cny", type=float, help="Original absolute cumulative cap, not new allowance")
    parser.add_argument("--account-ledger", type=Path)
    parser.add_argument("--account-limit-cny", type=float)
    parser.add_argument("--account-mapping-dir", type=Path)
    args = parser.parse_args(argv)
    if args.preview and args.run:
        parser.error("--preview and --run are mutually exclusive")

    from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
    from optomind_research.runtime.upgrade3.candidate_corpus import (
        SnippetTextStore, build_candidate_corpus, plan_queries, write_corpus)
    from optomind_research.runtime.upgrade3.initial_retrieval import execute_initial_m2

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

    qwen_config = {"key_file": str(args.key_file.resolve()) if args.key_file else None,
                   "key_index": args.key_index,
                   "budget_ledger_path": str(args.budget_ledger.resolve()) if args.budget_ledger else None,
                   "budget_limit_cny": args.budget_limit_cny,
                   "account_ledger_path": str(args.account_ledger.resolve()) if args.account_ledger else None,
                   "account_limit_cny": args.account_limit_cny,
                   "account_mapping_directory": str(args.account_mapping_dir.resolve()) if args.account_mapping_dir else None}
    if args.preview or not args.no_initial_topup:
        corpus = execute_initial_m2(
            plan_path=REPO / args.schema, outcome_path=REPO / args.outcome if args.outcome else None,
            output_dir=(REPO / args.out).parent, run=not args.preview,
            per_query_limit=args.per_query_limit, corpus_path=REPO / args.out,
            snippets_path=REPO / args.snippets, initial_topup=not args.no_initial_topup,
            qwen_config=qwen_config)
        if args.preview:
            print("PREVIEW: no provider/model requests; target500; at most3 supplementary rounds")
            return 0
    else:
        store = SnippetTextStore(REPO / args.snippets)
        try:
            corpus = build_candidate_corpus(
                plan, gateway=S2IntelligenceGateway(), per_query_limit=args.per_query_limit,
                enrich=True, snippet_store=store)
        finally:
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
    store = SnippetTextStore(REPO / args.snippets)
    try:
        print("  SQLite 中正文片段条数  :", store.count())
    finally:
        store.close()

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

    # The normal runner already published/hashed the consumer handoff.
    path = REPO / args.out
    if args.no_initial_topup:
        path = write_corpus(path, corpus)
    print()
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
