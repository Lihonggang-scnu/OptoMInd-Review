"""Run the Facet-conditioned Multiplex Scholarly Skeleton, and the ablation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ABLATION = {
    "A_retrieval_only": {"citation": 0.0, "bibliographic_coupling": 0.0,
                         "co_citation": 0.0, "contextual_reference": 0.0, "retrieval": 1.0},
    "B_plus_citation": {"citation": 1.0, "bibliographic_coupling": 0.0,
                        "co_citation": 0.0, "contextual_reference": 0.0, "retrieval": 0.8},
    "C_plus_coupling_cocitation": {"citation": 1.0, "bibliographic_coupling": 0.7,
                                   "co_citation": 0.7, "contextual_reference": 0.0,
                                   "retrieval": 0.8},
    "D_plus_contextual": {"citation": 1.0, "bibliographic_coupling": 0.7,
                          "co_citation": 0.7, "contextual_reference": 1.2,
                          "retrieval": 0.8},
    "E_complete": None,
}


def coverage_of(portfolio, skeleton):
    selected = portfolio["selected"]
    facets, communities, roles = set(), set(), set()
    for row in portfolio.get("explanations") or ():
        for facet in (row.get("cross_facet") or {}).get("facets") or ():
            facets.add(facet)
        community = (row.get("community") or {}).get("community_id")
        if community is not None:
            communities.add(community)
        for role in (row.get("candidate_roles") or {}):
            roles.add(role)
    years = sorted({(row.get("temporal") or {}).get("year")
                    for row in portfolio.get("explanations") or ()
                    if (row.get("temporal") or {}).get("year")})
    return {"selected": len(selected), "facets": len(facets),
            "communities": len(communities), "roles": len(roles),
            "year_span": (years[0], years[-1]) if years else None}


def main() -> int:
    from optomind_research.runtime.upgrade3.scholarly_skeleton import (
        load_config, run_skeleton, run_skeleton_v2)

    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="outputs/upgrade3/QP02/CANDIDATE_CORPUS.json")
    parser.add_argument("--edges", default="outputs/upgrade3/SKELETON/GRAPH_EDGES.jsonl")
    parser.add_argument("--out", default="outputs/upgrade3/SKELETON")
    parser.add_argument("--ablation", action="store_true")
    args = parser.parse_args()

    base = load_config()
    if not args.ablation:
        run_skeleton_v2(REPO / args.corpus, REPO / args.edges, REPO / args.out, base)
        print("done:", REPO / args.out)
        return 0

    results = {}
    for name, weights in ABLATION.items():
        config = json.loads(json.dumps(base))
        if weights is not None:
            config["fusion"]["layer_weights"] = weights
        out = REPO / args.out / "ablation" / name
        print("\n===== ablation", name, flush=True)
        skeleton = run_skeleton(REPO / args.corpus, REPO / args.edges, out, config,
                                dump_edges=False)
        portfolio = skeleton["portfolio"]
        stats = skeleton["edge_statistics"]
        results[name] = {
            "layer_weights": config["fusion"]["layer_weights"],
            "communities": skeleton["communities"]["summary"]["community_count"],
            "modularity": round(skeleton["communities"]["summary"]["modularity"], 4),
            "mean_stability": skeleton["communities"]["summary"].get("mean_stability"),
            "coverage": coverage_of(portfolio, skeleton),
            "selected": portfolio["selected"],
            "candidate_pool": portfolio["candidate_pool"],
            "citation_edges_active": stats["citation_edges_active"],
        }
    keys = list(results)
    for other in keys[1:]:
        a, b = set(results["E_complete"]["selected"]), set(results[other]["selected"])
        results[other]["overlap_with_complete"] = len(a & b)
        results[other]["jaccard_with_complete"] = round(len(a & b) / max(len(a | b), 1), 4)
    for name in keys[:-1]:
        a, b = set(results[name]["selected"]), set(results["E_complete"]["selected"])
        results[name]["jaccard_with_complete"] = round(len(a & b) / max(len(a | b), 1), 4)

    (REPO / args.out / "ABLATION.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== ablation summary ===")
    for name, row in results.items():
        print("%-28s comm=%-3d modularity=%-7s stability=%-7s sel=%-3d "
              "jaccard_vs_E=%s" % (name, row["communities"], row["modularity"],
                                   row["mean_stability"], row["coverage"]["selected"],
                                   row.get("jaccard_with_complete")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
