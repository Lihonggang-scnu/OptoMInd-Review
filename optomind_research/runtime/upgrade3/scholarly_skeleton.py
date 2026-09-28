"""Facet-conditioned Multiplex Scholarly Skeleton -- experimental v0.

Turns a flat Candidate Corpus into facet-conditioned multiplex academic networks
and reports which papers are worth reading next, and why.

Design commitments, taken from the work order:

* no single global score and no Top-K.  Every importance dimension is kept
  separate and every role is ranked on its own feature vector;
* the four relation types stay in separate layers.  A composite graph may be
  built for community detection, but each layer is normalised on its own first
  and the layer weights are recorded in the output;
* graph structure decides WHERE to look.  It never declares that a paper IS the
  origin or the landmark -- those remain candidates until body evidence says so;
* core and boundary papers are strictly separated, and every boundary paper
  records the path by which it entered.

Nothing here downloads a PDF, calls GROBID, or asks an LLM anything.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

REPO = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO / "config" / "scholarly_skeleton.json"


class DegradedCorpusError(RuntimeError):
    """Raised when a corpus from a degraded plan is used as if it were whole.

    The cross-domain run produced a corpus whose keyword channel was empty and
    nothing in the artefacts said so; the skeleton then reported pools, roles and
    a portfolio as if the retrieval had been complete.
    """

    def __init__(self, state):
        self.state = dict(state or {})
        super().__init__(
            "candidate corpus is degraded (%s): missing %s"
            % (self.state.get("plan_state") or "unknown",
               ", ".join(self.state.get("missing_channels") or ["?"])))


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))


def percentile_rank(values: Mapping[str, float]) -> dict[str, float]:
    """Percentile of every key, with TIED VALUES RECEIVING THE SAME PERCENTILE.

    The previous version ranked by sort position, so a set of identical values
    was spread arbitrarily across the whole range.  Many papers have a
    participation coefficient of exactly zero, and one of them was therefore
    reported at the 99.8th percentile of "participation" while its raw value was
    zero -- and the graph then promoted it as a top bridge.
    """

    if not values:
        return {}
    keys = list(values)
    total = len(keys)
    if total == 1:
        return {keys[0]: 1.0}
    ordered = sorted(keys, key=lambda k: values[k])
    out: dict[str, float] = {}
    index = 0
    while index < total:
        end = index
        while end + 1 < total and values[ordered[end + 1]] == values[ordered[index]]:
            end += 1
        # mid-rank of the tie group, normalised to [0, 1]
        mid = (index + end) / 2.0
        share = mid / (total - 1)
        for position in range(index, end + 1):
            out[ordered[position]] = share
        index = end + 1
    return out


def _minmax(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if high <= low:
        return {k: 0.0 for k in values}
    return {k: (v - low) / (high - low) for k, v in values.items()}


# ------------------------------------------------------------ facet layer --


def facet_features(corpus: Mapping[str, Any],
                   config: Mapping[str, Any]) -> dict[str, Any]:
    """Layer 1: how strongly each paper belongs to each facet.

    Query-level relevance and snippet evidence density are both kept.  Multiple
    snippets from one query are NOT collapsed: they carry how densely a paper's
    body answers that question.
    """

    hits = corpus.get("retrieval_hits") or []
    facets = [str(f.get("id")) for f in corpus.get("facets") or ()]
    if not facets:
        facets = sorted({str(h.get("facet_id")) for h in hits})

    queries_per_facet: dict[str, set] = defaultdict(set)
    for hit in hits:
        queries_per_facet[str(hit.get("facet_id"))].add(str(hit.get("query_id")))

    acc: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {
            "queries": set(), "keyword_queries": set(), "question_queries": set(),
            "snippet_hits": 0, "ranks": [], "snippet_scores": [], "rrf": 0.0}))

    for hit in hits:
        paper = str(hit.get("paper_id") or "")
        facet = str(hit.get("facet_id") or "")
        if not paper or not facet:
            continue
        row = acc[paper][facet]
        query_id = str(hit.get("query_id"))
        row["queries"].add(query_id)
        if hit.get("query_type") == "question":
            row["question_queries"].add(query_id)
            row["snippet_hits"] += 1
        else:
            row["keyword_queries"].add(query_id)
        rank = hit.get("result_rank")
        if isinstance(rank, int) and rank > 0:
            row["ranks"].append(rank)
            # Reciprocal Rank Fusion across the independent ranked lists.
            row["rrf"] += 1.0 / (60.0 + rank)
        score = hit.get("score")
        if isinstance(score, (int, float)):
            row["snippet_scores"].append(float(score))

    out: dict[str, dict[str, Any]] = {}
    for paper, by_facet in acc.items():
        out[paper] = {}
        for facet, row in by_facet.items():
            total_queries = len(queries_per_facet.get(facet, ())) or 1
            scores = row["snippet_scores"]
            out[paper][facet] = {
                "distinct_query_hits": len(row["queries"]),
                "query_coverage": round(len(row["queries"]) / total_queries, 4),
                "keyword_query_hits": len(row["keyword_queries"]),
                "question_query_hits": len(row["question_queries"]),
                "snippet_hit_count": row["snippet_hits"],
                "best_rank": min(row["ranks"]) if row["ranks"] else None,
                "RRF_score": round(row["rrf"], 6),
                "max_snippet_score": round(max(scores), 4) if scores else None,
                "mean_snippet_score": round(sum(scores) / len(scores), 4) if scores else None,
            }
    return {"features": out, "facets": facets,
            "queries_per_facet": {k: sorted(v) for k, v in queries_per_facet.items()}}


# --------------------------------------------------------- citation layer --


@dataclass
class EdgeSet:
    """Raw edges plus the boundary admission decision."""

    citation: list[dict[str, Any]] = field(default_factory=list)
    coupling: list[dict[str, Any]] = field(default_factory=list)
    cocitation: list[dict[str, Any]] = field(default_factory=list)
    contextual: list[dict[str, Any]] = field(default_factory=list)
    boundary: dict[str, dict[str, Any]] = field(default_factory=dict)
    statistics: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)


STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "by",
        "using", "via", "from", "at", "as", "is", "are", "based", "toward", "towards"}


def object_terms(corpus: Mapping[str, Any]) -> list[str]:
    """The topic words the schema itself states, not a hand-written list."""

    text = str(corpus.get("research_object") or "").casefold()
    return [w for w in "".join(c if c.isalnum() else " " for c in text).split()
            if w not in STOP and len(w) > 2]


def token_idf(titles: Iterable[str]) -> dict[str, float]:
    """Inverse document frequency over the candidate titles actually retrieved.

    Derived from the data, so a word that is rare here counts as specific and a
    word that appears everywhere does not.
    """

    titles = list(titles)
    total = max(len(titles), 1)
    seen: dict[str, int] = defaultdict(int)
    for title in titles:
        for word in set(str(title or "").casefold().split()):
            seen[word] += 1
    return {word: math.log(total / (1 + count)) for word, count in seen.items()}


def object_relatedness(title: str, terms: Sequence[str],
                       idf: Mapping[str, float]) -> float:
    """How specifically a title is about the stated research object."""

    words = set(str(title or "").casefold().split())
    return round(max((idf.get(t, 0.0) for t in terms if t in words), default=0.0), 4)


def load_raw_edges(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read the jsonl the collector wrote, keyed by source paper."""

    rows: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            rows[str(record.get("source_paper"))] = record
    return rows

# ------------------------------------------------------------ four layers --


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


#: Below this many characters a folded title is not specific enough to identify a
#: work: "editorial", "introduction" and "preface" all fold to short strings.
WORK_TITLE_MIN_CHARS = 20


def _work_fold(value: Any) -> str:
    text = str(value or "").casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _title_variants(title: Any) -> list[str]:
    """Folded title, plus the form with a provider disambiguation suffix removed.

    Semantic Scholar serves a second record for a work it already holds and
    disambiguates it by appending a bare index: a title ending in " 1" is the same
    paper as the title without it.  Only a trailing 1-3 digit token is stripped,
    and only as a second chance after the exact folded title has failed to match.
    """

    folded = _work_fold(title)
    variants = [folded] if folded else []
    stripped = re.sub(r"\s+\d{1,3}\s*$", "", str(title or ""))
    folded_stripped = _work_fold(stripped)
    if folded_stripped and folded_stripped != folded:
        variants.append(folded_stripped)
    return variants


def work_identity_meta(corpus: Mapping[str, Any],
                       raw_edges: Mapping[str, Mapping[str, Any]],
                       enrichment: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Everything known about every node id, from the corpus, the edges and the
    enrichment file, without fetching anything."""

    rows: dict[str, dict[str, Any]] = {}

    def note(pid: Any, *, title=None, year=None, doi=None, authors=None,
             corpus_id=None, source="") -> None:
        pid = str(pid or "")
        if not pid:
            return
        row = rows.setdefault(pid, {"paper_id": pid, "title": "", "year": None,
                                    "doi": "", "authors": [], "corpus_id": "",
                                    "sources": []})
        if title and not row["title"]:
            row["title"] = str(title)
        if year not in (None, "") and row["year"] in (None, ""):
            row["year"] = year
        if doi and not row["doi"]:
            row["doi"] = str(doi)
        if authors and not row["authors"]:
            row["authors"] = [str(a) for a in authors]
        if corpus_id not in (None, "") and not row["corpus_id"]:
            row["corpus_id"] = str(corpus_id)
        if source and source not in row["sources"]:
            row["sources"].append(source)

    for paper in corpus.get("papers") or ():
        note(paper.get("paper_id"), title=paper.get("title"), year=paper.get("year"),
             doi=paper.get("doi"), authors=paper.get("authors"),
             corpus_id=paper.get("corpus_id"), source="corpus")
    for pid, info in (enrichment or {}).items():
        note(pid, title=(info or {}).get("title"), year=(info or {}).get("year"),
             doi=(info or {}).get("doi"), authors=(info or {}).get("authors"),
             corpus_id=(info or {}).get("corpus_id"), source="enrichment")
    for record in (raw_edges or {}).values():
        for relation in ("references", "citations"):
            for node in (record or {}).get(relation) or ():
                note(node.get("paper_id"), title=node.get("title"), year=node.get("year"),
                     corpus_id=node.get("corpus_id"), source="edges")
    return rows


def canonical_works(rows: Mapping[str, Mapping[str, Any]],
                    core_ids: Iterable[str]) -> dict[str, Any]:
    """One work, one id: a conservative alias map over the node ids we hold.

    Strong keys first -- DOI, then folded title with the year.  A title key is only
    used when the folded title is long enough to be specific, and when the years
    agree; when both sides name authors and the first surnames differ, the merge is
    refused rather than risked.  Everything else keeps its own id, and every
    original id survives in the registry with the rule that merged it.
    """

    core = {str(pid) for pid in core_ids}
    groups: dict[str, list[str]] = defaultdict(list)
    rule_of: dict[str, str] = {}

    def add(key: str, rule: str, pid: str) -> None:
        groups[key].append(pid)
        rule_of.setdefault(key, rule)

    for pid, row in sorted(rows.items()):
        doi = _work_fold(row.get("doi"))
        if doi:
            add("doi:" + doi, "doi", pid)
            continue
        variants = [v for v in _title_variants(row.get("title"))
                    if len(v) >= WORK_TITLE_MIN_CHARS]
        year = row.get("year")
        if variants and year not in (None, ""):
            add("t:%s|%s" % (variants[0], year), "title_year", pid)
        elif variants:
            add("t:" + variants[0], "title_only", pid)
        else:
            add("own:" + pid, "own_id", pid)

    # Second chance for the provider disambiguation suffix: a pair that only
    # matches after the trailing index is removed, and only when the years agree.
    by_base: dict[str, list[str]] = defaultdict(list)
    for pid, row in sorted(rows.items()):
        variants = _title_variants(row.get("title"))
        if len(variants) > 1 and len(variants[1]) >= WORK_TITLE_MIN_CHARS:
            by_base[variants[1]].append(pid)
    for base, members in by_base.items():
        for key in list(groups):
            if not key.startswith("t:") or key[2:].split("|")[0] != base:
                continue
            existing = groups[key]
            years = {rows[p].get("year") for p in members + existing}
            if len(years) > 1:
                continue
            for pid in members:
                if pid not in existing:
                    existing.append(pid)
            if len(existing) > 1:
                rule_of[key] = "title_year_trailing_index"
            break

    alias: dict[str, str] = {}
    registry: list[dict[str, Any]] = []
    merged = 0
    for key, members in groups.items():
        unique = sorted(set(members))
        if len(unique) < 2:
            continue
        by_id = {pid: (rows.get(pid) or {}) for pid in unique}
        surnames = []
        for pid in unique:
            authors = by_id[pid].get("authors") or []
            surnames.append(_work_fold(str(authors[0]).split()[-1]) if authors else "")
        named = [s for s in surnames if s]
        if len(set(named)) > 1:
            continue
        canonical = sorted(
            unique,
            key=lambda pid: (pid not in core,
                             -(int(by_id[pid].get("citation_count") or 0)),
                             pid))[0]
        for pid in unique:
            if pid != canonical:
                alias[pid] = canonical
        merged += 1
        registry.append({
            "canonical_work_id": canonical,
            "aliases": [pid for pid in unique if pid != canonical],
            "identity_key": key,
            "merge_rule": rule_of.get(key, "unknown"),
            "title": (by_id[canonical].get("title") or ""),
            "sources": sorted({s for pid in unique for s in (by_id[pid].get("sources") or [])}),
            "is_core": canonical in core,
        })
    return {"alias": alias, "registry": registry, "merged_works": merged,
            "nodes_considered": len(rows)}


def build_layers(
    raw_edges: Mapping[str, Mapping[str, Any]],
    corpus: Mapping[str, Any],
    facet_feature: Mapping[str, Any],
    config: Mapping[str, Any],
    identity_meta: Mapping[str, Any] | None = None,
) -> EdgeSet:
    """Layers 2-5: citation backbone plus the three derived relations."""

    layers_cfg = config.get("layers") or {}
    admit_cfg = config.get("boundary_admission") or {}
    fanout = int(layers_cfg.get("max_pair_fanout", 60))

    papers = corpus.get("papers") or []
    core_ids = {str(p["paper_id"]) for p in papers if p.get("paper_id")}
    # One work, one node.  Two provider records for the same paper used to arrive
    # as a core node and a boundary node, and both could take a slot in the
    # portfolio.  The alias map is applied to every edge endpoint below, before
    # any of the derived structures exist, so the whole graph is built on canon
    # ids rather than repaired afterwards.
    identity = canonical_works(work_identity_meta(corpus, raw_edges, identity_meta),
                               core_ids)
    alias = identity["alias"]
    if alias:
        core_ids = {alias.get(pid, pid) for pid in core_ids}
        print("       work identity: merged %d duplicate works (%d aliases)" % (
            identity["merged_works"], len(alias)), flush=True)
    terms = object_terms(corpus)
    idf = token_idf([str(p.get("title") or "") for p in papers])
    core_by_corpus = {str(p["corpus_id"]): str(p["paper_id"])
                      for p in papers if p.get("corpus_id") and p.get("paper_id")}
    meta: dict[str, dict[str, Any]] = {}
    for paper in papers:
        pid = str(paper.get("paper_id") or "")
        if pid:
            meta[pid] = {"year": paper.get("year"), "title": paper.get("title") or "",
                         "citation_count": None, "core": True}

    citation: list[dict[str, Any]] = []
    references_of: dict[str, set] = defaultdict(set)
    cited_by: dict[str, set] = defaultdict(set)
    boundary_reasons: dict[str, set] = defaultdict(set)
    boundary_titles: dict[str, str] = {}

    def note_boundary(pid: str, node: Mapping[str, Any], reason: str) -> None:
        if not pid or pid in core_ids:
            return
        boundary_reasons[pid].add(reason)
        if pid not in meta:
            meta[pid] = {"year": node.get("year"), "title": node.get("title") or "",
                         "citation_count": node.get("citation_count"), "core": False}
        elif node.get("title") and not meta[pid].get("title"):
            meta[pid]["title"] = node["title"]

    # Every node the edges mention, so a snippet refMention can be resolved by
    # corpus id even when the target is a boundary paper, not a core one.
    corpus_to_paper: dict[str, str] = dict(core_by_corpus)
    for record in raw_edges.values():
        for relation in ("references", "citations"):
            for node in record.get(relation) or ():
                cid = str(node.get("corpus_id") or "")
                pid = str(node.get("paper_id") or "")
                if cid and pid:
                    corpus_to_paper.setdefault(cid, pid)

    for raw_source, record in raw_edges.items():
        source = alias.get(str(raw_source), str(raw_source))
        if source not in core_ids:
            continue
        for node in record.get("references") or ():
            target = alias.get(str(node.get("paper_id") or ""),
                               str(node.get("paper_id") or ""))
            if not target or target == source:
                continue
            references_of[source].add(target)
            cited_by[target].add(source)
            citation.append({"source": source, "target": target,
                             "direction": "references", "provider": "semantic_scholar",
                             "influential": bool(node.get("is_influential")),
                             "has_context": bool(node.get("has_context"))})
            note_boundary(target, node, "referenced_by_core")
        for node in record.get("citations") or ():
            citing = alias.get(str(node.get("paper_id") or ""),
                               str(node.get("paper_id") or ""))
            if not citing or citing == source:
                continue
            references_of[citing].add(source)
            cited_by[source].add(citing)
            citation.append({"source": citing, "target": source,
                             "direction": "references", "provider": "semantic_scholar",
                             "influential": bool(node.get("is_influential")),
                             "has_context": bool(node.get("has_context"))})
            note_boundary(citing, node, "cites_core")

    # ---- contextual references, from the snippets that answered a facet ----
    contextual: list[dict[str, Any]] = []
    contextual_by_target: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"mentions": 0, "sources": set(), "queries": set(), "snippets": set()})
    for hit in corpus.get("retrieval_hits") or ():
        locator = hit.get("snippet_locator") or {}
        mentions = locator.get("ref_mentions") or []
        if not mentions:
            continue
        source = alias.get(str(hit.get("paper_id") or ""), str(hit.get("paper_id") or ""))
        if not source:
            continue
        for mention in mentions:
            corpus_id = mention.get("matched_paper_corpus_id")
            if not corpus_id:
                continue
            target = corpus_to_paper.get(str(corpus_id), "")
            if not target:
                target = "corpus:" + str(corpus_id)
            target = alias.get(target, target)
            if target == source:
                continue
            contextual.append({
                "source": source, "target": target,
                "facet_id": hit.get("facet_id"), "query_id": hit.get("query_id"),
                "query_type": hit.get("query_type"), "hit_id": hit.get("hit_id"),
                "matched_paper_corpus_id": str(corpus_id)})
            bucket = contextual_by_target[target]
            bucket["mentions"] += 1
            bucket["sources"].add(source)
            bucket["queries"].add(str(hit.get("query_id")))
            bucket["snippets"].add(str(hit.get("hit_id")))
            if target not in core_ids:
                boundary_reasons[target].add("mentioned_in_snippet")

    # ---- bibliographic coupling, via an inverted reference index ----------
    citers_of: dict[str, list] = defaultdict(list)
    for source, refs in references_of.items():
        for ref in refs:
            citers_of[ref].append(source)
    shared: dict[tuple, int] = defaultdict(int)
    ref_count: dict[str, int] = defaultdict(int)
    for ref, citers in citers_of.items():
        unique = sorted(set(citers))
        if len(unique) > fanout:
            unique = unique[:fanout]
        for source in unique:
            ref_count[source] += 1
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                shared[_pair(unique[i], unique[j])] += 1

    # A reference shared by 400 papers says nothing about two of them being in
    # the same subfield; a reference shared by 3 says a lot.  Weighting each
    # shared reference by its rarity is what stops the general canon (Adam,
    # ResNet, VGG) from single-handedly merging unrelated literatures into one
    # community.  Measured without it, landslide-susceptibility papers formed a
    # 257-node community with 120 of our core papers.
    core_total = max(len(core_ids), 2)
    ref_idf = {ref: math.log(core_total / (1 + len(set(citers))))
               for ref, citers in citers_of.items()}

    min_shared = int(layers_cfg.get("bibliographic_coupling_min_shared", 2))
    min_weight = float(layers_cfg.get("bibliographic_coupling_min_weight", 1.0))
    coupling = []
    for (a, b), count in shared.items():
        if count < min_shared:
            continue
        both = references_of[a] & references_of[b]
        denom = math.sqrt(max(ref_count[a], 1) * max(ref_count[b], 1))
        weighted = sum(ref_idf.get(r, 0.0) for r in both)
        if weighted < min_weight:
            continue
        coupling.append({"source": a, "target": b, "shared_references": count,
                         "cosine": round(count / denom, 6) if denom else 0.0,
                         "adamic_adar": round(weighted, 6),
                         "weight": round(weighted, 6)})

    # ---- co-citation, via the same inverted idea on the other side --------
    # Same idea on the other side: a paper with 100 references co-cites any pair
    # only weakly, and its vote is discounted accordingly.
    cocite: dict[tuple, float] = defaultdict(float)
    cocite_count: dict[tuple, int] = defaultdict(int)
    for source, refs in references_of.items():
        unique = sorted(refs)
        if len(unique) > fanout:
            unique = unique[:fanout]
        vote = 1.0 / math.log(max(len(refs), 2))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                key = _pair(unique[i], unique[j])
                cocite[key] += vote
                cocite_count[key] += 1
    min_cocite = int(layers_cfg.get("co_citation_min_count", 2))
    cocitation = [{"source": a, "target": b, "co_citation_count": cocite_count[(a, b)],
                   "normalized_co_citation": round(weight, 6),
                   "weight": round(weight, 6)}
                  for (a, b), weight in cocite.items()
                  if cocite_count[(a, b)] >= min_cocite]

    # ---- boundary admission ----------------------------------------------
    min_ref_by = int(admit_cfg.get("min_core_papers_referencing_it", 2))
    min_refs = int(admit_cfg.get("min_core_papers_it_references", 2))
    min_share = float(admit_cfg.get("min_field_share", 0.01))
    idf = token_idf([str(p.get("title") or "") for p in papers])
    admit_mentions = bool(admit_cfg.get("admit_if_in_snippet_ref_mentions", True))
    max_boundary = int(admit_cfg.get("max_boundary_papers", 4000))

    admitted: dict[str, dict[str, Any]] = {}
    for pid, reasons in boundary_reasons.items():
        if pid in core_ids:
            continue
        core_citers = len(cited_by[pid] & core_ids)
        core_refs = len(references_of[pid] & core_ids)
        title = (meta.get(pid) or {}).get("title", "")
        total_citations = (meta.get(pid) or {}).get("citation_count") or 0
        # Being cited by core papers is NOT by itself evidence of belonging to
        # this field: the most-cited boundary papers are general tools such as
        # Adam, ResNet, ImageNet and a 1989 genetic-algorithms textbook, cited
        # by our papers as instruments rather than as prior work.
        #
        # The discriminator is what share of a paper's known citations come from
        # this field.  The denominator takes max(total, core_citers) because the
        # provider's citation count lags badly for recent papers -- a 2026 paper
        # cited 37 times by the core can be reported with a total of 1.  That
        # makes the measure immune to the lag: it stays 1.0 for a new paper and
        # collapses for a mature general-purpose tool.
        field_share = core_citers / max(total_citations, core_citers, 1)
        keep = False
        if core_refs >= min_refs:
            keep = True
        if admit_mentions and "mentioned_in_snippet" in reasons:
            keep = True
        if core_citers >= min_ref_by and field_share >= min_share:
            keep = True
        if keep:
            info = meta.get(pid) or {}
            bucket = contextual_by_target.get(pid) or {}
            admitted[pid] = {
                "paper_id": pid,
                "title": info.get("title", ""),
                "year": info.get("year"),
                "object_relatedness": object_relatedness(info.get("title", ""), terms, idf),
                "field_share": round(
                    len(cited_by[pid] & core_ids)
                    / max(info.get("citation_count") or 0,
                          len(cited_by[pid] & core_ids), 1), 4),
                "reasons": sorted(reasons),
                "core_papers_referencing_it": core_citers,
                "core_papers_it_references": core_refs,
                "contextual_mentions": bucket.get("mentions", 0),
                "distinct_context_sources": len(bucket.get("sources") or ()),
            }
    if len(admitted) > max_boundary:
        ordered = sorted(admitted.values(),
                         key=lambda r: (-(r["core_papers_referencing_it"]
                                          + r["core_papers_it_references"]),
                                        -(r["contextual_mentions"] or 0)))
        admitted = {r["paper_id"]: r for r in ordered[:max_boundary]}

    active = core_ids | set(admitted)

    def keep_edge(row: Mapping[str, Any]) -> bool:
        return str(row.get("source")) in active and str(row.get("target")) in active

    statistics = {
        "core_papers": len(core_ids),
        "boundary_candidates": len(boundary_reasons),
        "boundary_admitted": len(admitted),
        "citation_edges_raw": len(citation),
        "citation_edges_active": sum(1 for r in citation if keep_edge(r)),
        "references_edges": sum(len(v) for v in references_of.values()),
        "bibliographic_coupling_pairs": len(coupling),
        "co_citation_pairs": len(cocitation),
        "contextual_edges": len(contextual),
        "contextual_distinct_targets": len(contextual_by_target),
        "admission_thresholds": {
            "min_core_papers_referencing_it": min_ref_by,
            "min_core_papers_it_references": min_refs,
            "min_field_share": min_share,
            "admit_if_in_snippet_ref_mentions": admit_mentions,
            "max_boundary_papers": max_boundary,
        },
    }

    statistics["duplicate_works_merged"] = identity["merged_works"]
    statistics["work_aliases"] = len(alias)

    return EdgeSet(
        citation=[r for r in citation if keep_edge(r)],
        coupling=[r for r in coupling if keep_edge(r)],
        cocitation=[r for r in cocitation if keep_edge(r)],
        contextual=[r for r in contextual if keep_edge(r)],
        boundary=admitted,
        statistics=statistics,
        identity=identity,
    )

# ---------------------------------------------------------- graph features --


def _retrieval_layer(corpus: Mapping[str, Any], active: set) -> list[dict[str, Any]]:
    """Papers reached by the same query are related in the retrieval layer."""

    by_query: dict[str, set] = defaultdict(set)
    for hit in corpus.get("retrieval_hits") or ():
        paper = str(hit.get("paper_id") or "")
        if paper and paper in active:
            by_query[str(hit.get("query_id"))].add(paper)
    shared: dict[tuple, int] = defaultdict(int)
    for papers in by_query.values():
        ordered = sorted(papers)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                shared[_pair(ordered[i], ordered[j])] += 1
    return [{"source": a, "target": b, "shared_queries": n} for (a, b), n in shared.items()]


def build_graphs(
    edge_set: EdgeSet,
    corpus: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """One igraph per layer, plus a composite formed from normalised layers."""

    import igraph as ig

    core_ids = {str(p["paper_id"]) for p in corpus.get("papers") or () if p.get("paper_id")}
    active = set(core_ids) | set(edge_set.boundary)
    nodes = sorted(active)
    index = {node: i for i, node in enumerate(nodes)}

    def graph_from(rows, weight_key, directed=False):
        edges, weights = [], []
        for row in rows:
            a, b = index.get(str(row["source"])), index.get(str(row["target"]))
            if a is None or b is None or a == b:
                continue
            edges.append((a, b))
            weights.append(float(row.get(weight_key) or 1.0))
        graph = ig.Graph(n=len(nodes), edges=edges, directed=directed)
        graph.es["weight"] = weights or [1.0] * len(edges)
        return graph

    layers = {
        "citation": graph_from(edge_set.citation, "influential", directed=True),
        "bibliographic_coupling": graph_from(edge_set.coupling, "weight"),
        "co_citation": graph_from(edge_set.cocitation, "weight"),
        "contextual_reference": graph_from(edge_set.contextual, "mentions", directed=True),
        "retrieval": graph_from(_retrieval_layer(corpus, active), "shared_queries"),
    }

    fusion = config.get("fusion") or {}
    weights = fusion.get("layer_weights") or {}
    composite_w: dict[tuple, float] = defaultdict(float)
    for name, graph in layers.items():
        layer_weight = float(weights.get(name, 1.0))
        if not graph.ecount():
            continue
        values = graph.es["weight"]
        top = max(values) if values else 1.0
        top = top if top else 1.0
        for edge, value in zip(graph.get_edgelist(), values):
            a, b = nodes[edge[0]], nodes[edge[1]]
            composite_w[_pair(a, b)] += layer_weight * (value / top)

    composite = ig.Graph(n=len(nodes),
                         edges=[(index[a], index[b]) for a, b in composite_w],
                         directed=False)
    composite.es["weight"] = [composite_w[k] for k in composite_w]

    return {"nodes": nodes, "index": index, "layers": layers, "composite": composite,
            "core_ids": core_ids}


def graph_features(graphs: Mapping[str, Any], communities: Sequence[int]) -> dict[str, dict[str, Any]]:
    """Per-paper structural features, computed on the composite graph."""

    composite = graphs["composite"]
    nodes = graphs["nodes"]
    n = len(nodes)
    degrees = composite.degree()
    pagerank = composite.pagerank(weights="weight") if composite.ecount() else [0.0] * n
    coreness = composite.coreness(mode="all") if composite.ecount() else [0] * n
    # Exact betweenness on a graph this size is needlessly slow; a bounded
    # cutoff keeps it tractable and is enough to rank bridges.
    betweenness = (composite.betweenness(directed=False, cutoff=4)
                   if composite.ecount() else [0.0] * n)

    membership: dict[int, list[int]] = defaultdict(list)
    for vertex, community in enumerate(communities):
        membership[community].append(vertex)
    size_of = {c: len(v) for c, v in membership.items()}
    mean_degree = {c: (sum(degrees[v] for v in v_list) / len(v_list)) for c, v_list in membership.items()}
    std_degree = {}
    for community, v_list in membership.items():
        values = [degrees[v] for v in v_list]
        mean = mean_degree[community]
        std_degree[community] = math.sqrt(
            sum((x - mean) ** 2 for x in values) / len(values)) if len(values) > 1 else 0.0

    out: dict[str, dict[str, Any]] = {}
    neighbours = composite.get_adjlist()
    for vertex, node in enumerate(nodes):
        links_by_community: dict[int, int] = defaultdict(int)
        for neighbour in neighbours[vertex]:
            links_by_community[communities[neighbour]] += 1
        total = sum(links_by_community.values())
        participation = (1.0 - sum((count / total) ** 2 for count in links_by_community.values())
                         if total else 0.0)
        own = communities[vertex]
        std = std_degree.get(own, 0.0)
        within_z = ((degrees[vertex] - mean_degree.get(own, 0.0)) / std) if std > 0 else 0.0
        out[node] = {
            "local_in_degree": int(composite.degree(vertex, mode="in")),
            "local_out_degree": int(composite.degree(vertex, mode="out")),
            "degree": int(degrees[vertex]),
            "local_PageRank": float(pagerank[vertex]),
            "coreness": int(coreness[vertex]),
            "betweenness": float(betweenness[vertex]),
            "participation_coefficient": round(participation, 6),
            "within_module_degree_z": round(within_z, 6),
            "community": int(own),
            "community_size": int(size_of.get(own, 0)),
            "neighbour_communities": len(links_by_community),
        }
    return out


def reference_consensus(raw_edges: Mapping[str, Mapping[str, Any]],
                        core_ids: set) -> dict[str, int]:
    """How many core papers cite each paper.  Cheap and purely local."""

    counts: dict[str, int] = defaultdict(int)
    for source, record in raw_edges.items():
        if source not in core_ids:
            continue
        for node in record.get("references") or ():
            target = str(node.get("paper_id") or "")
            if target:
                counts[target] += 1
    return dict(counts)


# ------------------------------------------------------------ temporal ----


def temporal_features(meta: Mapping[str, Mapping[str, Any]],
                      raw_edges: Mapping[str, Mapping[str, Any]],
                      config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    year_now = int((config.get("temporal") or {}).get("current_year", 2026))
    out: dict[str, dict[str, Any]] = {}
    for node, info in meta.items():
        year = info.get("year")
        citations = info.get("citation_count")
        age = (year_now - int(year)) if isinstance(year, int) and year > 0 else None
        velocity = None
        if isinstance(citations, (int, float)) and age and age > 0:
            velocity = citations / age
        out[node] = {
            "year": year,
            "paper_age": age,
            "citation_count": citations,
            "citation_velocity": round(velocity, 4) if velocity is not None else None,
            "age_normalized_impact": round(math.log1p(citations) / age, 6)
            if isinstance(citations, (int, float)) and age and age > 0 else None,
        }
    return out

# ---------------------------------------------------------- communities ----


def detect_communities(graphs: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Leiden over many seeds and resolutions, then measure how stable it is.

    One partition from one seed is a random draw; the point of this function is
    to report not just a partition but how much it would move under a rerun.
    """

    import leidenalg

    composite = graphs["composite"]
    cfg = config.get("communities") or {}
    seeds = list(cfg.get("seeds") or [1, 7, 13])
    resolutions = list(cfg.get("resolutions") or [0.8, 1.0, 1.2])
    pair_sample = int(cfg.get("stability_pairs_sample", 100000))

    runs: list[dict[str, Any]] = []
    for seed in seeds:
        for resolution in resolutions:
            partition = leidenalg.find_partition(
                composite, leidenalg.RBConfigurationVertexPartition,
                weights="weight" if composite.ecount() else None,
                resolution_parameter=float(resolution), seed=int(seed))
            runs.append({
                "seed": int(seed), "resolution": float(resolution),
                "membership": list(partition.membership),
                "modularity": float(partition.modularity),
                "community_count": len(set(partition.membership)),
            })

    best = max(runs, key=lambda r: r["modularity"])
    membership = list(best["membership"])

    # stability: how often two nodes land together across all runs
    rng = np.random.default_rng(20260914)
    n = len(graphs["nodes"])
    pairs = min(pair_sample, max(1, n * (n - 1) // 2))
    left = rng.integers(0, n, size=pairs)
    right = rng.integers(0, n, size=pairs)
    keep = left != right
    left, right = left[keep], right[keep]
    agreement = np.zeros(len(left))
    for run in runs:
        member = np.asarray(run["membership"])
        agreement += (member[left] == member[right])
    agreement /= len(runs)

    per_community: dict[int, list[float]] = defaultdict(list)
    for a, b, value in zip(left, right, agreement):
        if membership[a] == membership[b]:
            per_community[int(membership[a])].append(float(value))

    sizes: dict[int, int] = defaultdict(int)
    for community in membership:
        sizes[int(community)] += 1

    stability = {c: round(float(np.mean(v)), 4) if v else None
                 for c, v in per_community.items()}
    return {
        "runs": [{k: v for k, v in run.items() if k != "membership"} for run in runs],
        "best_run": {k: v for k, v in best.items() if k != "membership"},
        "membership": membership,
        "modularity": best["modularity"],
        "community_count": len(sizes),
        "sizes": {int(k): int(v) for k, v in sizes.items()},
        "stability": stability,
        "mean_stability": round(float(np.mean(list(stability.values()))), 4)
        if stability else None,
        "pair_agreement_mean": round(float(np.mean(agreement)), 4) if len(agreement) else None,
    }


def community_profiles(graphs: Mapping[str, Any],
                       communities: Mapping[str, Any],
                       meta: Mapping[str, Mapping[str, Any]],
                       temporal: Mapping[str, Mapping[str, Any]],
                       graph_feat: Mapping[str, Mapping[str, Any]],
                       raw_edges: Mapping[str, Mapping[str, Any]],
                       core_ids: set,
                       config: Mapping[str, Any]) -> dict[str, Any]:
    """Per-community year distribution, density and representatives."""

    year_now = int((config.get("temporal") or {}).get("current_year", 2026))
    nodes = graphs["nodes"]
    membership = communities["membership"]
    groups: dict[int, list[str]] = defaultdict(list)
    for vertex, node in enumerate(nodes):
        groups[int(membership[vertex])].append(node)

    composite = graphs["composite"]
    out: dict[str, Any] = {}
    for community, members in sorted(groups.items()):
        years = [temporal.get(m, {}).get("year") for m in members]
        years = [y for y in years if isinstance(y, int)]
        by_year: dict[int, int] = defaultdict(int)
        for year in years:
            by_year[year] += 1
        recent = [y for y in years if y >= year_now - 3]
        first_half = [y for y in years if y < year_now - 3]
        representatives = sorted(
            members,
            key=lambda m: -(graph_feat.get(m, {}).get("local_PageRank") or 0.0))[:5]
        vertices = [graphs["index"][m] for m in members]
        sub = composite.subgraph(vertices)
        density = (2 * sub.ecount() / (len(members) * (len(members) - 1))
                   if len(members) > 1 else 0.0)
        out[str(community)] = {
            "community_id": community,
            "size": len(members),
            "core_members": sum(1 for m in members if m in core_ids),
            "first_year": min(years) if years else None,
            "papers_per_year": {str(k): v for k, v in sorted(by_year.items())},
            "recent_share": round(len(recent) / len(years), 4) if years else None,
            "recent_growth": round(len(recent) / max(len(first_half), 1), 4) if years else None,
            "internal_density": round(float(density), 6),
            "stability": communities["stability"].get(community),
            "representative_papers": representatives,
            "representative_titles": [meta.get(m, {}).get("title", "")[:110]
                                      for m in representatives],
        }
    return out


# ---------------------------------------------------------- cross facet ----


def cross_facet(facet_feature: Mapping[str, Any],
                core_ids: set) -> dict[str, dict[str, Any]]:
    """Which facets a paper serves, and how strong a connector it is."""

    features = facet_feature["features"]
    facets = facet_feature["facets"]
    strength: dict[str, dict[str, float]] = {}
    for paper, by_facet in features.items():
        for facet, row in by_facet.items():
            strength.setdefault(paper, {})[facet] = float(row["RRF_score"])

    out: dict[str, dict[str, Any]] = {}
    for paper, by_facet in strength.items():
        top = max(by_facet.values()) or 1.0
        normalised = {f: v / top for f, v in by_facet.items()}
        present = sorted(normalised)
        spread = len(present) - 1
        balance = (1.0 - (max(normalised.values()) - min(normalised.values()))) if spread else 0.0
        out[paper] = {
            "facet_membership_vector": {f: round(v, 4) for f, v in sorted(normalised.items())},
            "cross_facet_count": len(present),
            "cross_facet_bridge_score": round(len(present) * balance, 4) if present else 0.0,
            "facets": present,
        }
    return {"papers": out, "facet_count": len(facets)}

# ------------------------------------------------------------- roles -------

#: Title markers of a paper that synthesises the literature rather than reporting a
#: result of its own.  Meta-analyses and systematic reviews belong here: they are
#: evidence about the literature, and treating them as direct evidence for a
#: mechanism question is the confusion the role split exists to prevent.
REVIEW_WORDS = ("review", "survey", "overview", "perspective", "tutorial",
                "roadmap", "progress", "advances", "meta-analysis", "meta analysis",
                "systematic", "scoping", "narrative", "state of the art", "current status")


def _percentiles(values: Mapping[str, float]) -> dict[str, float]:
    return percentile_rank(values)


def facet_scopes(
    graphs: Mapping[str, Any],
    edge_set: EdgeSet,
    facet_feature: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Per-facet induced subgraph.

    One edge collection serves every facet: the local networks are induced from
    the global graph rather than fetched again per facet, which would multiply
    the provider cost by the number of facets for the same edges.
    """

    features = facet_feature["features"]
    facets = facet_feature["facets"]
    adjacency: dict[str, set] = defaultdict(set)
    for a, b in graphs["composite"].get_edgelist():
        na, nb = graphs["nodes"][a], graphs["nodes"][b]
        adjacency[na].add(nb)
        adjacency[nb].add(na)

    scopes: dict[str, dict[str, Any]] = {}
    for facet in facets:
        core = {p for p, by_facet in features.items() if facet in by_facet}
        touched = set(core)
        for paper in core:
            touched |= adjacency.get(paper, set())
        scopes[facet] = {"core": core, "nodes": touched}
    return scopes


def graph_features_on(graphs: Mapping[str, Any], nodes: Sequence[str],
                      communities: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Structural features restricted to one facet's induced subgraph."""

    composite = graphs["composite"]
    vertex_of = graphs["index"]
    subset = [vertex_of[n] for n in nodes if n in vertex_of]
    if len(subset) < 3:
        return {n: {"local_in_degree": 0, "local_out_degree": 0, "degree": 0,
                    "local_PageRank": 0.0, "coreness": 0, "betweenness": 0.0,
                    "participation_coefficient": 0.0, "within_module_degree_z": 0.0,
                    "community": communities["membership"][vertex_of[n]] if n in vertex_of else -1,
                    "community_size": 0, "neighbour_communities": 0} for n in nodes}
    sub = composite.subgraph(subset)
    names = [graphs["nodes"][v] for v in subset]
    sub_communities = [communities["membership"][v] for v in subset]
    fake = {"composite": sub, "nodes": names, "index": {n: i for i, n in enumerate(names)}}
    return graph_features(fake, sub_communities)


def role_candidates(
    graphs: Mapping[str, Any],
    edge_set: EdgeSet,
    facet_feature: Mapping[str, Any],
    scopes: Mapping[str, Any],
    global_feat: Mapping[str, Mapping[str, Any]],
    facet_feat: Mapping[str, Mapping[str, Mapping[str, Any]]],
    temporal: Mapping[str, Mapping[str, Any]],
    cross: Mapping[str, Any],
    communities: Mapping[str, Any],
    consensus: Mapping[str, int],
    meta: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
    blocked: Any = (),
    direct_evidence_ids: Any = None,
    reading: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ten independent role rankings.  No shared total score anywhere.

    Two roles are kept apart on purpose: a review synthesises the literature and a
    direct evidence paper reports a result of its own.  Query coverage alone cannot
    tell them apart, which is how a review took the top direct-evidence slot in a
    mechanism question.  `direct_evidence_ids` is the set the domain gate judged to
    be evidence about the question itself.
    """

    roles_cfg = config.get("roles") or {}
    weights = roles_cfg.get("role_weights") or {}
    top_n = int(roles_cfg.get("top_n_per_role_per_facet", 10))
    year_now = int((config.get("temporal") or {}).get("current_year", 2026))
    community_profiles = communities.get("profiles") or {}

    context_stats: dict[str, dict[str, Any]] = {}
    for row in edge_set.contextual:
        bucket = context_stats.setdefault(str(row["target"]), {
            "mentions": 0, "sources": set(), "queries": set(), "facets": set()})
        bucket["mentions"] += 1
        bucket["sources"].add(str(row["source"]))
        bucket["queries"].add(str(row["query_id"]))
        bucket["facets"].add(str(row["facet_id"]))

    # Domain gate.  A paper the domain engine rejected must never be promoted by
    # graph centrality into Foundational / Landmark / Bridge / Frontier: the
    # graph amplifies whatever it is handed, so the gate has to come first.
    blocked = set(blocked)
    result: dict[str, Any] = {"by_facet": {}, "weights": weights,
                              "blocked_from_roles": len(blocked)}

    for facet, scope in scopes.items():
        core = scope["core"]
        nodes = sorted(n for n in scope["nodes"] if n not in blocked)
        # facet_feature indexes RETRIEVAL rows as {paper: {facet: row}};
        # facet_feat indexes GRAPH rows as {facet: {paper: row}}.  The two were
        # swapped here, so every retrieval and every per-facet graph signal read
        # as zero for every paper -- which meant the role rankings were arbitrary
        # and the raw zeros were spread across percentiles by sort position.
        retrieval_of_paper = facet_feature.get("features") or {}
        graph_of_facet = facet_feat.get(facet) or {}
        population = {n: (retrieval_of_paper.get(n) or {}).get(facet, {}) for n in core}

        rrf = {n: float((population[n] or {}).get("RRF_score") or 0.0) for n in core}
        coverage = {n: float((population[n] or {}).get("query_coverage") or 0.0) for n in core}
        # Diagnostics only: recorded, never scored.
        snippet_n = {n: float((population[n] or {}).get("snippet_hit_count") or 0) for n in core}
        snippet_s = {n: float((population[n] or {}).get("max_snippet_score") or 0.0) for n in core}
        topical_fit = {n: float((reading or {}).get(n, {}).get("topical_fit") or 0.0) for n in core}
        consensus_v = {n: float(consensus.get(n, 0)) for n in nodes}
        pr = {n: float((graph_of_facet.get(n) or {}).get("local_PageRank") or 0.0)
              for n in nodes}
        coreness = {n: float((graph_of_facet.get(n) or {}).get("coreness") or 0)
                    for n in nodes}
        between = {n: float((graph_of_facet.get(n) or {}).get("betweenness") or 0.0)
                   for n in nodes}
        partic = {n: float((graph_of_facet.get(n) or {}).get("participation_coefficient") or 0.0)
                  for n in nodes}
        within_z = {n: float((graph_of_facet.get(n) or {}).get("within_module_degree_z") or 0.0)
                    for n in nodes}
        indeg = {n: float((graph_of_facet.get(n) or {}).get("local_in_degree") or 0)
                 for n in nodes}
        year = {n: (temporal.get(n, {}).get("year") or 0) for n in nodes}
        velocity = {n: float(temporal.get(n, {}).get("citation_velocity") or 0.0) for n in nodes}
        impact = {n: float(temporal.get(n, {}).get("age_normalized_impact") or 0.0) for n in nodes}
        age = {n: (temporal.get(n, {}).get("year") and (year_now - temporal[n]["year"])) or 99
               for n in nodes}
        lifetime = {n: float(temporal.get(n, {}).get("citation_count") or 0) for n in nodes}
        cros = {n: float((cross.get("papers", {}).get(n) or {}).get("cross_facet_bridge_score") or 0.0)
                for n in nodes}
        cros_n = {n: float((cross.get("papers", {}).get(n) or {}).get("cross_facet_count") or 0)
                  for n in nodes}
        ctx = {n: float((context_stats.get(n) or {}).get("mentions") or 0) for n in nodes}
        ctx_src = {n: float(len((context_stats.get(n) or {}).get("sources") or ())) for n in nodes}
        ctx_q = {n: float(len((context_stats.get(n) or {}).get("queries") or ())) for n in nodes}

        p_rrf = _percentiles(rrf)
        p_snippet_n = _percentiles(snippet_n)
        p_pr = _percentiles(pr)
        p_consensus = _percentiles(consensus_v)
        p_coreness = _percentiles(coreness)
        p_between = _percentiles(between)
        p_partic = _percentiles(partic)
        p_within = _percentiles(within_z)
        p_indeg = _percentiles(indeg)
        p_velocity = _percentiles(velocity)
        p_impact = _percentiles(impact)
        p_cross = _percentiles(cros)
        p_crossn = _percentiles(cros_n)
        p_ctx = _percentiles(ctx)
        p_ctxs = _percentiles(ctx_src)
        p_ctxq = _percentiles(ctx_q)
        p_lifetime = _percentiles(lifetime)
        earliness = {n: (1.0 - min(max(year[n] or year_now, 1990), year_now) - 1990)
                     / max(1, year_now - 1990) if year[n] else 0.0 for n in nodes}
        recency = {n: max(0.0, 1.0 - age[n] / 12.0) for n in nodes}
        p_earl = _percentiles(earliness)
        p_recent = _percentiles(recency)
        community_growth = {n: float((community_profiles.get(
            str((graph_of_facet.get(n) or {}).get("community", -1)), {})
            or {}).get("recent_growth") or 0.0) for n in nodes}
        p_cgrowth = _percentiles(community_growth)
        community_size = {n: float((graph_of_facet.get(n) or {}).get("community_size") or 0)
                          for n in nodes}
        smallness = {n: 1.0 / (1.0 + community_size[n]) for n in nodes}
        p_small = _percentiles(smallness)
        nb_comm = {n: float((graph_of_facet.get(n) or {}).get("neighbour_communities") or 0)
                   for n in nodes}
        p_nbcomm = _percentiles(nb_comm)
        is_review = {n: 1.0 if any(w in (meta.get(n, {}).get("title") or "").casefold()
                                   for w in REVIEW_WORDS) else 0.0 for n in nodes}

        def score(parts: Mapping[str, float], source: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
            out = {}
            for n in nodes:
                components = {k: round(float(source[k].get(n, 0.0)), 4) for k in parts}
                out[n] = {"score": round(sum(parts[k] * components[k] for k in parts), 6),
                          "components": components}
            return out

        # Snippet count and snippet score are NOT in this definition.  How many
        # passages a query returned is a property of the paper's length and of how
        # the provider splits text, so a long review collected more of them than a
        # short primary report; the count is kept as a diagnostic and as a hint
        # that the paper may be worth a look, never as evidence of relevance.
        # topical_fit is the reading judgement's own signal when one exists, and
        # zero when the paper has not been read yet.
        defs = {
            "direct_evidence": {"query_coverage": coverage,
                                "rrf_percentile": p_rrf,
                                "topical_fit": topical_fit},
            "foundational": {"earliness": p_earl, "reference_consensus": p_consensus,
                             "pagerank": p_pr, "coreness": p_coreness},
            "landmark": {"pagerank": p_pr, "local_in_degree": p_indeg,
                         "growth": p_velocity, "cross_community": p_nbcomm},
            "bridge_breakthrough": {"betweenness": p_between, "participation": p_partic,
                                    "bridge_communities": p_nbcomm, "emergence": p_cgrowth},
            "frontier": {"recency": p_recent, "citation_velocity": p_velocity,
                         "age_normalized_impact": p_impact, "community_growth": p_cgrowth},
            "community_representative": {"within_module_z": p_within, "local_pagerank": p_pr},
            "review_synthesis": {"is_review": is_review, "community_breadth": p_nbcomm,
                                 "citation_breadth": p_velocity, "facet_relevance": coverage},
            "context_gateway": {"contextual_consensus": p_ctx, "distinct_sources": p_ctxs,
                                "distinct_queries": p_ctxq},
            "cross_facet_connector": {"cross_facet_bridge": p_cross, "cross_facet_count": p_crossn},
            "emerging_niche": {"community_smallness": p_small, "community_growth": p_cgrowth,
                               "recency": p_recent, "low_lifetime_citation": {n: 1.0 - p_lifetime[n]
                                                                              for n in nodes}},
        }

        facet_roles: dict[str, Any] = {}
        for role, source in defs.items():
            parts = (weights.get(role) or {})
            scored = score(parts or {k: 1.0 / len(source) for k in source}, source)
            if role == "direct_evidence":
                # A review or meta-analysis reports no result of its own, and a
                # paper the scope gate did not accept as evidence about the
                # question is background, not direct evidence.
                allowed = None if direct_evidence_ids is None else set(direct_evidence_ids)
                scored = {
                    paper: payload for paper, payload in scored.items()
                    if not is_review.get(paper)
                    and (allowed is None or paper in allowed)
                }
            ranked = sorted(scored.items(), key=lambda kv: -kv[1]["score"])[:top_n]
            facet_roles[role] = [{
                "paper_id": paper,
                "title": (meta.get(paper, {}).get("title") or "")[:150],
                "year": meta.get(paper, {}).get("year"),
                "score": payload["score"],
                "components": payload["components"],
                "in_core": paper in core,
            } for paper, payload in ranked if payload["score"] > 0]
        result["by_facet"][facet] = facet_roles

    return result

# ---------------------------------------------------------- portfolio ------


def build_portfolio(
    roles: Mapping[str, Any],
    cross: Mapping[str, Any],
    graph_feat: Mapping[str, Mapping[str, Any]],
    temporal: Mapping[str, Mapping[str, Any]],
    facet_feature: Mapping[str, Any],
    graphs: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Greedy submodular selection over coverage, with a redundancy penalty.

    Explicitly not a Top-K: the objective rewards covering facets, queries,
    communities, roles and years, and penalises picking another paper that is
    already well connected to the chosen ones.
    """

    cfg = config.get("portfolio") or {}
    target = int(cfg.get("target_size", 30))
    low, high = (cfg.get("size_range") or [20, 40])[:2]
    lam = float(cfg.get("greedy_lambda_redundancy", 0.45))
    cover_w = cfg.get("coverage_weights") or {}

    candidates: dict[str, set] = {}
    candidate_roles: dict[str, set] = {}
    for facet, by_role in (roles.get("by_facet") or {}).items():
        for role, rows in by_role.items():
            for row in rows:
                paper = row["paper_id"]
                candidate_roles.setdefault(paper, set()).add(role)
                candidates.setdefault(paper, set()).add("role:%s" % role)
    if not candidates:
        return {"selected": [], "candidate_pool": 0}

    for paper in list(candidates):
        memberships = (cross.get("papers", {}).get(paper) or {})
        for facet in memberships.get("facets") or ():
            candidates[paper].add("facet:%s" % facet)
        feature = facet_feature["features"].get(paper) or {}
        for facet, row in feature.items():
            for query_id in (facet_feature["queries_per_facet"].get(facet) or ()):
                if row["query_coverage"] > 0:
                    candidates[paper].add("query:%s" % query_id)
        community = (graph_feat.get(paper) or {}).get("community")
        if community is not None:
            candidates[paper].add("community:%s" % community)
        year = (temporal.get(paper) or {}).get("year")
        if isinstance(year, int):
            candidates[paper].add("decade:%d" % (year // 10 * 10))
        if paper in graphs["core_ids"]:
            candidates[paper].add("tier:core")
        else:
            candidates[paper].add("tier:boundary")

    aspect_weight: dict[str, float] = {}
    for aspect in {a for s in candidates.values() for a in s}:
        kind = aspect.split(":", 1)[0]
        aspect_weight[aspect] = float(cover_w.get(kind, 0.05))

    neighbours = graphs["composite"].get_adjlist()
    vertex_of = graphs["index"]

    def similarity(a: str, b: str) -> float:
        va, vb = vertex_of.get(a), vertex_of.get(b)
        if va is None or vb is None or va not in range(len(neighbours)):
            return 0.0
        if vb in neighbours[va]:
            return 1.0
        return 0.0

    selected: list[str] = []
    covered: set = set()
    remaining = dict(candidates)
    target = max(int(low), min(target, int(high)))
    while remaining and len(selected) < target:
        best_paper, best_gain = None, -1e9
        for paper, aspects in remaining.items():
            gain = sum(aspect_weight.get(a, 0.0) for a in aspects - covered)
            redundancy = max((similarity(paper, s) for s in selected), default=0.0)
            value = gain - lam * redundancy
            if value > best_gain:
                best_paper, best_gain = paper, value
        if best_paper is None:
            break
        selected.append(best_paper)
        covered |= remaining.pop(best_paper)

    return {
        "selection_method": "greedy submodular with redundancy penalty",
        "lambda_redundancy": lam,
        "target_size": target,
        "size_range": [int(low), int(high)],
        "candidate_pool": len(candidates),
        "coverage_weights": cover_w,
        "selected": selected,
        "selected_roles": {p: sorted(candidate_roles.get(p, ())) for p in selected},
    }


def explain(
    paper: str,
    facet_feature: Mapping[str, Any],
    graph_feat: Mapping[str, Mapping[str, Any]],
    temporal: Mapping[str, Mapping[str, Any]],
    cross: Mapping[str, Any],
    roles: Mapping[str, Any],
    consensus: Mapping[str, int],
    edge_set: EdgeSet,
    communities: Mapping[str, Any],
    meta: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """A structured reason built only from data the pipeline actually tracked."""

    context = {"mentions": 0, "sources": set(), "queries": set()}
    for row in edge_set.contextual:
        if str(row["target"]) == paper:
            context["mentions"] += 1
            context["sources"].add(str(row["source"]))
            context["queries"].add(str(row["query_id"]))

    role_hits: dict[str, dict[str, Any]] = {}
    for facet, by_role in (roles.get("by_facet") or {}).items():
        for role, rows in by_role.items():
            for row in rows:
                if row["paper_id"] == paper:
                    role_hits.setdefault(role, {"facets": [], "best_score": 0.0,
                                                "components": {}})
                    role_hits[role]["facets"].append(facet)
                    if row["score"] > role_hits[role]["best_score"]:
                        role_hits[role]["best_score"] = row["score"]
                        role_hits[role]["components"] = row["components"]

    feature = facet_feature["features"].get(paper) or {}
    structure = graph_feat.get(paper) or {}
    memberships = cross.get("papers", {}).get(paper) or {}
    years = temporal.get(paper) or {}
    community = structure.get("community")
    profile = (communities.get("profiles") or {}).get(str(community)) or {}

    why: list[str] = []
    if structure.get("local_in_degree"):
        why.append("被 %d 篇本地论文引用" % structure["local_in_degree"])
    if context["mentions"]:
        why.append("在 %d 个回答该 facet 的正文片段中被 %d 篇不同论文指向"
                   % (context["mentions"], len(context["sources"])))
    if consensus.get(paper):
        why.append("%d 篇核心论文引用它" % consensus[paper])
    if memberships.get("cross_facet_count", 0) > 1:
        why.append("同时属于 %d 个 facet" % memberships["cross_facet_count"])
    if profile.get("recent_growth") and profile["recent_growth"] > 1.2:
        why.append("所在社区近年增长 %.2f" % profile["recent_growth"])
    if not why:
        why.append("由检索与图结构共同推入候选，原因需下一阶段正文证据确认")

    return {
        "paper_id": paper,
        "title": (meta.get(paper, {}).get("title") or "")[:200],
        "retrieval": feature,
        "topology": {
            "local_in_degree": structure.get("local_in_degree"),
            "local_out_degree": structure.get("local_out_degree"),
            "local_PageRank": structure.get("local_PageRank"),
            "coreness": structure.get("coreness"),
            "betweenness": structure.get("betweenness"),
            "participation_coefficient": structure.get("participation_coefficient"),
            "within_module_degree_z": structure.get("within_module_degree_z"),
        },
        "reference_consensus": consensus.get(paper, 0),
        "community": {"community_id": community, "size": profile.get("size"),
                      "stability": profile.get("stability"),
                      "recent_growth": profile.get("recent_growth")},
        "context": {"contextual_mentions": context["mentions"],
                    "distinct_source_papers": len(context["sources"]),
                    "distinct_queries": len(context["queries"])},
        "temporal": years,
        "cross_facet": memberships,
        "candidate_roles": {role: {"score": round(v["best_score"], 4),
                                   "facets": sorted(set(v["facets"])),
                                   "components": v["components"]}
                            for role, v in sorted(role_hits.items())},
        "why_investigate": why,
    }

# ------------------------------------------------------------ orchestrator --


def _meta_from(corpus: Mapping[str, Any],
               raw_edges: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Identity metadata for every node, from the corpus and the edge payloads."""

    meta: dict[str, dict[str, Any]] = {}
    for paper in corpus.get("papers") or ():
        pid = str(paper.get("paper_id") or "")
        if pid:
            meta[pid] = {"title": paper.get("title") or "", "year": paper.get("year"),
                         "citation_count": None, "core": True}
    for record in raw_edges.values():
        for relation in ("references", "citations"):
            for node in record.get(relation) or ():
                pid = str(node.get("paper_id") or "")
                if not pid:
                    continue
                info = meta.setdefault(pid, {"title": "", "year": None,
                                             "citation_count": None, "core": False})
                if node.get("title") and not info.get("title"):
                    info["title"] = node["title"]
                if node.get("year") and not info.get("year"):
                    info["year"] = node["year"]
                if node.get("citation_count") is not None and info.get("citation_count") is None:
                    info["citation_count"] = node["citation_count"]
    return meta


def run_skeleton(corpus_path: Path, edge_path: Path, out_dir: Path,
                 config: Mapping[str, Any], *, dump_edges: bool = True) -> dict[str, Any]:
    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[1/9] facet membership", flush=True)
    ff = facet_features(corpus, config)

    print("[2/9] edges -> four layers + citation backbone", flush=True)
    raw_edges = load_raw_edges(edge_path)
    edge_set = build_layers(raw_edges, corpus, ff, config)
    print("      ", json.dumps(edge_set.statistics, ensure_ascii=False), flush=True)

    print("[3/9] multiplex graph", flush=True)
    graphs = build_graphs(edge_set, corpus, config)
    meta = _meta_from(corpus, raw_edges)

    print("[4/9] communities", flush=True)
    communities = detect_communities(graphs, config)
    print("       communities:", communities["community_count"],
          "modularity:", round(communities["modularity"], 4),
          "mean stability:", communities["mean_stability"], flush=True)

    print("[5/9] graph features", flush=True)
    global_feat = graph_features(graphs, communities["membership"])
    temporal = temporal_features(meta, raw_edges, config)
    communities["profiles"] = community_profiles(
        graphs, communities, meta, temporal, global_feat, raw_edges,
        graphs["core_ids"], config)

    print("[6/9] per-facet scopes and features", flush=True)
    scopes = facet_scopes(graphs, edge_set, ff)
    facet_feat: dict[str, dict[str, Mapping[str, Any]]] = {}
    for facet, scope in scopes.items():
        facet_feat[facet] = graph_features_on(graphs, sorted(scope["nodes"]), communities)

    print("[7/9] cross-facet structure", flush=True)
    cross = cross_facet(ff, graphs["core_ids"])

    print("[8/9] role candidates", flush=True)
    consensus = reference_consensus(raw_edges, graphs["core_ids"])
    roles = role_candidates(graphs, edge_set, ff, scopes, global_feat, facet_feat,
                            temporal, cross, communities, consensus, meta, config)

    print("[9/9] portfolio", flush=True)
    portfolio = build_portfolio(roles, cross, global_feat, temporal, ff, graphs, config)
    portfolio["explanations"] = [
        explain(p, ff, global_feat, temporal, cross, roles, consensus, edge_set,
                communities, meta)
        for p in portfolio["selected"]]

    skeleton = {
        "schema_version": "optomind.scholarly_skeleton.v0",
        "labelled_as": "Facet-conditioned Multiplex Scholarly Skeleton -- experimental v0",
        "configuration": config,
        "facets": ff["facets"],
        "paper_facet_features": ff["features"],
        "papers": [{"paper_id": p, "core": True, **{k: meta.get(p, {}).get(k)
                                                     for k in ("title", "year")}}
                   for p in sorted(graphs["core_ids"])],
        "boundary_papers": sorted(edge_set.boundary.values(),
                                  key=lambda r: r["paper_id"]),
        "edge_statistics": edge_set.statistics,
        "communities": {"summary": {k: v for k, v in communities.items()
                                    if k not in ("membership", "profiles")},
                        "profiles": communities["profiles"]},
        "candidate_roles": roles,
        "portfolio": portfolio,
        "audit": {
            "boundary_admission_thresholds": edge_set.statistics.get("admission_thresholds"),
            "graph_nodes": len(graphs["nodes"]),
            "reference_consensus_targets": len(consensus),
            "contextual_targets": len({r["target"] for r in edge_set.contextual}),
        },
    }
    (out_dir / "SCHOLARLY_SKELETON.json").write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "COMMUNITIES.json").write_text(
        json.dumps(communities["profiles"], ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "INVESTIGATION_PORTFOLIO.json").write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "VOCABULARY_AUDIT.json").write_text(
        json.dumps(domain["vocabulary_audit"], ensure_ascii=False, indent=1),
        encoding="utf-8")
    (out_dir / "SELECTION_TRACE.json").write_text(
        json.dumps({"trace": portfolio["trace"],
                    "priority_portfolio": portfolio["priority_portfolio"],
                    "deep_analysis_corpus": portfolio["deep_analysis_corpus"]},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    if dump_edges:
        import pandas as pd
        rows = []
        for name, graph in graphs["layers"].items():
            for edge, weight in zip(graph.get_edgelist(), graph.es["weight"]):
                rows.append({"layer": name, "source": graphs["nodes"][edge[0]],
                             "target": graphs["nodes"][edge[1]], "weight": weight})
        pd.DataFrame(rows).to_parquet(out_dir / "GRAPH_EDGES.parquet", index=False)
        facet_rows = []
        for paper, by_facet in ff["features"].items():
            for facet, row in by_facet.items():
                facet_rows.append({"paper_id": paper, "facet_id": facet, **row})
        pd.DataFrame(facet_rows).to_parquet(out_dir / "PAPER_FACET_FEATURES.parquet",
                                            index=False)

    return skeleton


# ------------------------------------------------- three-tier pools -------


def build_pools(domain, graphs, facet_feature, config):
    """Graph Support Pool -> Investigation Eligible Pool.

    The support pool keeps every node the graph needs, including general tools,
    because removing them would change the topology.  The eligible pool is what
    a human could actually be asked to read.
    """

    eligible_classes = set((config.get("pools") or {}).get(
        "eligible_classes") or ["IN_DOMAIN", "ADJACENT", "UNCERTAIN"])
    support = set(graphs["nodes"])
    # Eligibility follows the PAPER-level verdict.  Letting it follow the most
    # lenient facet put a 1950 Brier-score paper and a 1989 universal-
    # approximation paper into the portfolio while labelling them generic.
    eligible = {p for p, row in (domain.get("aggregate") or {}).items()
                if row["class"] in eligible_classes}
    # Reading-candidate admission, and the separate question of what may be
    # offered as a confirmed pick.  A paper kept in the investigation pool is not
    # thereby confirmed as relevant: when the material to judge it was not there,
    # or a reading step has not yet looked at it, it stays out of the priority
    # tier and is listed for verification instead.
    reading = dict(domain.get("reading") or {})
    hints = {p: str((row or {}).get("pre_reading_hint") or "")
             for p, row in (domain.get("aggregate") or {}).items()}
    row_usage = {p: str((row or {}).get("usage_label") or "")
                 for p, row in (domain.get("aggregate") or {}).items()}
    priority_eligible = set()
    for paper in eligible:
        label = str((reading.get(paper) or {}).get("label") or "")
        if label == "directly_relevant":
            priority_eligible.add(paper)
        elif label == "background_or_method" and (reading.get(paper) or {}).get("use"):
            priority_eligible.add(paper)
        elif (not reading and hints.get(paper) == "likely_answers_this_facet"
              and not (row_usage.get(paper) or "")):
            # The lexical fallback only applies to a topic that has NOT been read
            # at all.  Once a reading pass exists, an unread or unplaced paper is
            # an open question, not a confirmed pick, and is offered for
            # verification instead of being counted as one.
            #
            # A widely-used paper (the instrument-like usage label) is demoted here
            # rather than vetoed: it keeps its place in the graph and in the
            # investigation pool and can still be picked by the deep corpus, but it
            # does not enter the priority tier on lexical evidence alone.  Before
            # this, a general review of deep learning entered the priority list of a
            # diffractive-optics topic because the usage label had stopped blocking,
            # and that topic has no reading pass to catch it on content.
            priority_eligible.add(paper)
    per_facet = {}
    for facet, rows in (domain.get("by_facet") or {}).items():
        facet_eligible = {p for p, row in rows.items() if row["class"] in eligible_classes}
        per_facet[facet] = {
            "graph_support": len(support),
            "facet_considered": len(rows),
            "eligible": len(facet_eligible),
            "in_domain": sum(1 for row in rows.values() if row["class"] == "IN_DOMAIN"),
            "adjacent": sum(1 for row in rows.values() if row["class"] == "ADJACENT"),
            "uncertain": sum(1 for row in rows.values() if row["class"] == "UNCERTAIN"),
            "generic_tool": sum(1 for row in rows.values() if row["class"] == "GENERIC_TOOL"),
            "off_topic": sum(1 for row in rows.values() if row["class"] == "OFF_TOPIC"),
        }
    return {
        "graph_support_pool": sorted(support),
        "priority_eligible_pool": sorted(priority_eligible),
        "eligible_pool": sorted(eligible),
        "excluded": sorted(support - eligible),
        "sizes": {"graph_support": len(support), "eligible": len(eligible),
                  "priority_eligible": len(priority_eligible),
                  "excluded_from_eligibility": len(support - eligible)},
        "per_facet": per_facet,
    }


def _percentiles_of(values):
    return percentile_rank({k: float(v or 0.0) for k, v in values.items()})


def community_representative_ranks(graphs, graph_feat, communities):
    """Where a paper sits inside its own community's representative ranking."""

    groups = defaultdict(list)
    for paper, row in graph_feat.items():
        groups[int(row.get("community", -1))].append(paper)
    ranks = {}
    for community, members in groups.items():
        ordered = sorted(members, key=lambda m: -(graph_feat[m].get("local_PageRank") or 0.0))
        for index, paper in enumerate(ordered, start=1):
            ranks[paper] = index
    return ranks


def build_cards(papers, domain, graphs, graph_feat, facet_feat, temporal, roles,
                cross, communities, consensus, edge_set, meta, reps,
                domain_index, priority, selection=None):
    """Structured investigation cards, built only from tracked features.

    Every sentence in why_investigate / why_not_higher is generated from a named
    field, and the two lists are built from disjoint evidence so they cannot
    contradict each other.  An earlier version reported six contextual mentions
    and, in the same card, that the paper had no snippet support at all.
    """

    selection = selection or {}
    trace = {row["paper_id"]: row for row in selection.get("trace") or ()}
    priority_set = set(selection.get("priority_portfolio") or ())
    deep_set = set(selection.get("deep_analysis_corpus") or ())

    pr_pct = _percentiles_of({p: (graph_feat.get(p) or {}).get("local_PageRank")
                              for p in graph_feat})
    core_pct = _percentiles_of({p: (graph_feat.get(p) or {}).get("coreness")
                                for p in graph_feat})
    bet_pct = _percentiles_of({p: (graph_feat.get(p) or {}).get("betweenness")
                               for p in graph_feat})

    cards = {}
    for paper in papers:
        structure = graph_feat.get(paper) or {}
        community = structure.get("community")
        profile = (communities.get("profiles") or {}).get(str(community)) or {}
        info = meta.get(paper) or {}
        domain_rows = {f: r for f, r in (domain_index.get(paper) or {}).items()
                       if not f.startswith("__")}
        aggregate = (domain_index.get(paper) or {}).get("__aggregate__") or {}
        best_facet, best_row = None, None
        for facet, row in domain_rows.items():
            if best_row is None or row["retrieval_score"] > best_row["retrieval_score"]:
                best_facet, best_row = facet, row
        best_row = best_row or {}

        direct_snippets = best_row.get("direct_snippet_hits")
        contextual = best_row.get("contextual_mentions_from_other_papers") or 0
        contextual_sources = best_row.get("contextual_distinct_sources") or 0
        semantic = best_row.get("semantic_relevance")
        share = best_row.get("field_share")
        domain_class = aggregate.get("class") or best_row.get("class")

        role_rows = {}
        for facet, by_role in (roles.get("by_facet") or {}).items():
            for role, rows in by_role.items():
                for row in rows:
                    if row["paper_id"] != paper:
                        continue
                    if role not in role_rows or row["score"] > role_rows[role]["score"]:
                        role_rows[role] = {"score": row["score"],
                                           "components": row["components"],
                                           "facet": facet}

        why, why_not = [], []
        if contextual:
            why.append("被 %d 篇其他论文在回答该 facet 的片段中指向，共 %d 次"
                       % (contextual_sources, contextual))
        else:
            why_not.append("没有任何其他论文在片段中指向它")
        if direct_snippets:
            why.append("被该 facet 的查询以直接片段命中 %d 次" % direct_snippets)
        else:
            why_not.append("无直接片段命中（它靠图结构或边界路径进入）")
        if semantic is not None:
            if semantic >= 0.7:
                why.append("与 facet 语义原型接近度 %.3f" % semantic)
            else:
                why_not.append("语义接近度 %.3f 未达域内高水平" % semantic)
        else:
            why_not.append("没有可用的语义向量，语义证据缺失")
        if share is not None:
            if share >= 0.05:
                why.append("已知被引中 %.1f%% 来自本领域" % (share * 100))
            else:
                why_not.append("已知被引中仅 %.2f%% 来自本领域" % (share * 100))
        if best_row.get("vocabulary_hits"):
            why.append("命中 facet 区分性短语: %s" % ", ".join(best_row["vocabulary_hits"][:3]))
        if structure.get("local_in_degree"):
            why.append("在图中被 %d 篇论文引用" % structure["local_in_degree"])
        if domain_class in ("ADJACENT", "UNCERTAIN"):
            why_not.append("领域分类为 %s，属于风险项，需正文证据确认"
                           % domain_class)
        if domain_class in ("GENERIC_TOOL", "OFF_TOPIC"):
            why_not.append("领域分类为 %s，禁止进入高价值角色候选" % domain_class)
        if profile.get("stability") is not None and profile["stability"] < 0.5:
            why_not.append("所属社区稳定性仅 %s" % profile["stability"])

        trace_row = trace.get(paper)
        if paper in deep_set:
            if trace_row and trace_row.get("reason") == "community_representative_seed":
                reason = "作为社区代表直接播种进入（社区 C%s）" % community
            elif trace_row:
                reason = "按边际知识增量入选：%s（增益 %s）" % (
                    trace_row.get("reason"), trace_row.get("marginal_gain"))
            else:
                reason = "入选 Deep-analysis Corpus"
            selection_reason = reason
        else:
            selection_reason = "未入选；仍在 Investigation Eligible Pool 中"

        cards[paper] = {
            "paper_identity": {
                "paper_id": paper,
                "title": info.get("title", ""),
                "doi": info.get("doi", ""),
                "corpus_id": info.get("corpus_id", ""),
                "venue": info.get("venue", ""),
                "publication_types": info.get("publication_types", []),
                "tier": "core" if paper in graphs["core_ids"] else "boundary",
            },
            "facet_relevance": domain_rows,
            "primary_facet": best_facet,
            "evidence_summary": {
                "retrieval_evidence": {
                    "query_coverage": best_row.get("query_coverage"),
                    "RRF_score": best_row.get("RRF_score"),
                    "direct_retrieval_snippet_hits": direct_snippets,
                    "max_snippet_score": best_row.get("max_snippet_score"),
                },
                "semantic_evidence": {"semantic_alignment": semantic,
                                      "embedding_available": semantic is not None},
                "domain_evidence": {
                    "class": domain_class, "why": aggregate.get("why"),
                    "risk_flag": bool(aggregate.get("risk_flag")),
                    "field_share": share,
                    "independent_signals": best_row.get("independent_signals"),
                    "signals": best_row.get("signals"),
                    # The three-way judgement, so a reader of the card can see why
                    # a paper is in the skeleton without re-running the gate.
                    "topic_relevance": best_row.get("topic_relevance"),
                    "scope_relevance": best_row.get("scope_relevance"),
                    "facet_relevance": best_row.get("facet_relevance"),
                    "direct_question_evidence": best_row.get("direct_question_evidence"),
                    "object_coverage": best_row.get("object_coverage"),
                    "semantic_evidence_missing": aggregate.get("semantic_evidence_missing"),
                    "excluded_terms_present": best_row.get("excluded_terms_present"),
                },
                "contextual_evidence": {
                    "contextual_mentions_from_other_papers": contextual,
                    "contextual_distinct_sources": contextual_sources,
                },
                "graph_evidence": {
                    "local_in_degree": structure.get("local_in_degree"),
                    "local_out_degree": structure.get("local_out_degree"),
                    "PageRank_percentile": round(pr_pct.get(paper, 0.0), 4),
                    "coreness_percentile": round(core_pct.get(paper, 0.0), 4),
                    "betweenness_percentile": round(bet_pct.get(paper, 0.0), 4),
                    "betweenness": structure.get("betweenness"),
                    "participation_coefficient": structure.get("participation_coefficient"),
                    "within_module_degree_z": structure.get("within_module_degree_z"),
                    "reference_consensus": consensus.get(paper, 0),
                },
            },
            "community": {
                "community_id": community,
                "size": profile.get("size"),
                "representative_rank": reps.get(paper),
                "stability": profile.get("stability"),
                "recent_growth": profile.get("recent_growth"),
            },
            "temporal_features": temporal.get(paper) or {},
            "candidate_roles": role_rows,
            "cross_facet": (cross.get("papers", {}).get(paper) or {}),
            "deep_analysis_priority": (round(float(trace_row["marginal_gain"]), 6)
                                       if trace_row and trace_row.get("marginal_gain") is not None
                                       else None),
            "selected_for_priority_portfolio": paper in priority_set,
            "selected_for_deep_analysis_corpus": paper in deep_set,
            "selection_reason": selection_reason,
            "why_investigate": why or ["无正向证据，仅因覆盖需要保留"],
            "why_not_higher": why_not or ["无明显减分项"],
        }
    return cards
def build_eligible_portfolio(roles, cross, graph_feat, temporal, facet_feature,
                             graphs, eligible, domain_index, config,
                             community_reps=None):
    """Greedy submodular selection over the ELIGIBLE pool only.

    Two changes from the earlier version.  The ground set is restricted to
    papers whose domain class allows reading, so a generic tool can still hold
    the graph together without occupying an investigation slot.  And the size is
    decided by marginal gain rather than by a fixed target: selection stops as
    soon as another paper no longer adds appreciable facet, community, role,
    temporal or contextual coverage.
    """

    cfg = config.get("portfolio") or {}
    stop = (config.get("pools") or {}).get("portfolio_stopping") or {}
    lam = float(cfg.get("greedy_lambda_redundancy", 0.45))
    cover_w = cfg.get("coverage_weights") or {}
    min_gain = float(stop.get("min_marginal_gain", 0.0008))
    min_fraction = float(stop.get("min_gain_fraction_of_first", 0.02))
    max_size = int(stop.get("max_size", 60))
    min_size = int(stop.get("min_size", 8))
    must_cover = (config.get("pools") or {}).get("must_cover_communities", True)
    value_weight = float(cfg.get("paper_value_weight", 0.35))

    # Pure coverage gain selects papers that fill a marginal aspect (a spare
    # year, an extra contextual tier) while the papers that actually matter --
    # the 258-context-mention anchor, the community representatives -- add no
    # NEW aspect and are never picked.  The objective therefore also carries a
    # per-paper value term, so coverage decides the shape of the portfolio and
    # value decides which papers fill it.
    paper_value = {}
    for facet, by_role in (roles.get("by_facet") or {}).items():
        for role, rows in by_role.items():
            for row in rows:
                paper = row["paper_id"]
                paper_value[paper] = max(paper_value.get(paper, 0.0), row["score"])
    for paper, rows in (domain_index or {}).items():
        best = max((r for f, r in rows.items() if not f.startswith("__")),
                   key=lambda r: r.get("semantic_relevance") or -1.0, default=None)
        if not best:
            continue
        bonus = 0.0
        if best.get("contextual_mentions"):
            bonus += min(0.6, 0.08 * best["contextual_mentions"])
        if best.get("semantic_relevance"):
            bonus += 0.4 * max(0.0, best["semantic_relevance"] - 0.7)
        if best.get("query_coverage"):
            bonus += 0.3 * best["query_coverage"]
        paper_value[paper] = paper_value.get(paper, 0.0) + bonus

    candidates = {}
    candidate_roles = {}
    for facet, by_role in (roles.get("by_facet") or {}).items():
        for role, rows in by_role.items():
            for row in rows:
                paper = row["paper_id"]
                if paper not in eligible:
                    continue
                candidate_roles.setdefault(paper, set()).add(role)
                # Role coverage is tracked per facet as well.  A coarse
                # role-only aspect list saturates after a handful of papers and
                # the stopping rule then fires far too early.
                candidates.setdefault(paper, set()).add("role:%s@%s" % (role, facet))
                candidates[paper].add("roleany:%s" % role)
    for paper in eligible:
        # Every eligible paper is a legitimate candidate.  Drawing the ground set
        # only from role top-N left 65 candidates out of 1313, which made the
        # coverage objective saturate almost immediately and pushed the community
        # representatives out of the portfolio entirely.
        candidates.setdefault(paper, set())
        memberships = (cross.get("papers", {}).get(paper) or {})
        for facet in memberships.get("facets") or ():
            candidates[paper].add("facet:%s" % facet)
        for facet, row in (facet_feature["features"].get(paper) or {}).items():
            if row["query_coverage"] > 0:
                for query_id in (facet_feature["queries_per_facet"].get(facet) or ()):
                    candidates[paper].add("query:%s" % query_id)
        community = (graph_feat.get(paper) or {}).get("community")
        if community is not None:
            candidates[paper].add("community:%s" % community)
        year = (temporal.get(paper) or {}).get("year")
        if isinstance(year, int):
            candidates[paper].add("year:%d" % year)
            candidates[paper].add("decade:%d" % (year // 10 * 10))
        for facet, row in (domain_index.get(paper) or {}).items():
            if facet.startswith("__"):
                continue
            if not row.get("contextual_mentions"):
                continue
            mentions = row["contextual_mentions"]
            tier = "high" if mentions >= 10 else ("med" if mentions >= 3 else "low")
            candidates[paper].add("contextual:%s@%s" % (tier, facet))
        candidates[paper].add("tier:core" if paper in graphs["core_ids"] else "tier:boundary")

    aspect_weight = {}
    for aspect in {a for s in candidates.values() for a in s}:
        kind = aspect.split(":", 1)[0]
        aspect_weight[aspect] = float(cover_w.get(kind, cover_w.get("default", 0.05)))

    neighbours = graphs["composite"].get_adjlist()
    vertex_of = graphs["index"]
    size_n = len(graphs["nodes"])

    def similarity(a, b):
        va, vb = vertex_of.get(a), vertex_of.get(b)
        if va is None or vb is None or va >= size_n or vb >= size_n:
            return 0.0
        return 1.0 if vb in neighbours[va] else 0.0

    all_communities = {"community:%s" % (graph_feat.get(p) or {}).get("community")
                       for p in candidates}
    all_communities.discard("community:None")

    # The work order requires every community to own a representative and asks
    # whether the portfolio covers all of them.  Coverage gain alone did not
    # deliver that: the field's own foundational paper, rank 1 of its community
    # with 258 contextual mentions, added no NEW aspect and was never chosen.
    # Seeding the representatives is a coverage guarantee, not a hand-picked
    # paper list -- it is computed from the ranking the pipeline itself produced.
    selected = []
    gains = {}
    covered = set()
    remaining = dict(candidates)
    seeds = []
    if (config.get("pools") or {}).get("seed_community_representatives", True):
        by_community = {}
        for paper in remaining:
            community = (graph_feat.get(paper) or {}).get("community")
            rank = (community_reps.get(paper) if community_reps else None)
            if community is None or rank != 1:
                continue
            if community not in by_community:
                by_community[community] = paper
        seeds = [by_community[c] for c in sorted(by_community)]
    for paper in seeds:
        if paper in remaining:
            selected.append(paper)
            gains[paper] = 0.0
            covered |= remaining.pop(paper)
    first_gain = None
    while remaining and len(selected) < max_size:
        best_paper, best_gain, best_value = None, -1e9, -1e9
        for paper, aspects in remaining.items():
            gain = sum(aspect_weight.get(a, 0.0) for a in aspects - covered)
            redundancy = max((similarity(paper, s) for s in selected), default=0.0)
            value = (gain + value_weight * paper_value.get(paper, 0.0)
                     - lam * redundancy)
            if value > best_value:
                best_paper, best_gain, best_value = paper, gain, value
        if best_paper is None:
            break
        if first_gain is None:
            first_gain = best_gain
        # A fixed absolute threshold stops after a handful of papers because the
        # aspect weights are small; scaling the threshold to the first gain makes
        # the rule independent of how the weights happen to be calibrated.
        threshold = max(min_gain, (first_gain or 0.0) * min_fraction)
        uncovered = all_communities - covered
        if len(selected) >= min_size and best_gain < threshold and not (
                must_cover and uncovered):
            break
        selected.append(best_paper)
        gains[best_paper] = round(best_gain, 5)
        covered |= remaining.pop(best_paper)

    return {
        "selection_method": "greedy submodular over the eligible pool, coverage-based stop",
        "lambda_redundancy": lam,
        "paper_value_weight": value_weight,
        "selection_objective": "coverage_gain + value_weight * paper_value - lambda * redundancy",
        "stopping": {"min_marginal_gain": min_gain,
                     "min_gain_fraction_of_first": min_fraction,
                     "min_size": min_size, "max_size": max_size,
                     "must_cover_communities": must_cover,
                     "communities_covered": len({c for c in covered
                                                 if str(c).startswith("community:")}),
                     "communities_total": len(all_communities)},
        "stopped_because": ("no_further_gain" if remaining else "candidate_pool_exhausted"),
        "eligible_candidates": len(candidates),
        "selected": selected,
        "marginal_gain_at_selection": gains,
        "selected_roles": {p: sorted(candidate_roles.get(p, ())) for p in selected},
        "coverage_weights": cover_w,
    }


# ------------------------------------------------- orchestrator v2 ---------


def _load_enrichment(out_dir):
    """Authoritative metadata and embeddings, when the enrich step has run."""

    meta_path = Path(out_dir) / "NODE_META.json"
    vec_path = Path(out_dir) / "NODE_EMBEDDINGS.npz"
    if not meta_path.exists() or not vec_path.exists():
        return {}, {}
    import numpy as np

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    data = np.load(vec_path, allow_pickle=True)
    ids = [str(x) for x in data["ids"]]
    matrix = data["matrix"]
    vectors = {pid: matrix[i] for i, pid in enumerate(ids)}
    return meta, vectors


def _merge_abstract_fill(out_dir, meta):
    """Fill missing abstracts from the recorded external lookups, with provenance.

    The file is written by the material-fill step.  Nothing is invented here, and
    every filled abstract keeps the source that supplied it so a reader can weigh it.
    """

    path = Path(out_dir) / "ABSTRACT_FILL.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return 0
    abstracts = payload.get("abstracts") or {}
    provenance = payload.get("provenance") or {}
    filled = 0
    for paper, text in abstracts.items():
        row = meta.setdefault(str(paper), {})
        if str(row.get("abstract") or "").strip():
            continue
        row["abstract"] = str(text)
        row["abstract_source"] = str((provenance.get(paper) or {}).get("source") or "fill")
        filled += 1
    return filled

def _material_text_by_paper(out_dir, corpus, edge_set=None, *, max_chars=4000):
    own, mentions = _snippet_text_by_paper(out_dir, corpus, edge_set, max_chars=max_chars,
                                           split_mentions=True)
    return own, mentions


def _snippet_text_by_paper(out_dir, corpus, edge_set=None, *, max_chars=4000,
                           split_mentions=False):
    """The body text each paper contributed to this review own queries.

    Snippet text lives in a side store keyed by hit id, because the corpus JSON
    carries only a locator and a hash.  The scope judgement reads it: for a paper
    with no abstract it is the only body text there is.
    """

    empty: Any = ({}, {}) if split_mentions else {}
    path = Path(out_dir) / "SNIPPET_TEXT.sqlite"
    if not path.exists():
        # A run without a snippet store has no body text; the caller still needs
        # the shape it asked for, which is why this returns a pair when asked.
        return empty
    by_hit = {}
    try:
        connection = sqlite3.connect(str(path))
        for hit_id, text in connection.execute(
                "SELECT hit_id, raw_snippet_text FROM snippet_text"):
            by_hit[str(hit_id)] = str(text or "")
        connection.close()
    except Exception:  # noqa: BLE001
        return empty
    out: dict[str, list[str]] = {}
    for hit in corpus.get("retrieval_hits") or ():
        paper = str(hit.get("paper_id") or "")
        text = by_hit.get(str(hit.get("hit_id"))) or ""
        if paper and text:
            out.setdefault(paper, []).append(text)
    # The contextual layer is the other place a paper appears in this review's own
    # text: another paper's passage names it.  Only the SENTENCE that names it is
    # evidence about the target -- the rest of that passage is about the citing
    # paper, and quoting it would judge the target by someone else's topic.
    mention_out: dict[str, list[str]] = {}
    locator_of = {str(hit.get("hit_id")): (hit.get("snippet_locator") or {})
                  for hit in corpus.get("retrieval_hits") or ()}
    for row in (edge_set.contextual if edge_set is not None else []) or ():
        target = str(row.get("target") or "")
        hit_id = str(row.get("hit_id") or "")
        raw = by_hit.get(hit_id) or ""
        if not target or not raw:
            continue
        locator = locator_of.get(hit_id) or {}
        wanted = str(row.get("matched_paper_corpus_id") or "")
        start = None
        for mention in locator.get("ref_mentions") or ():
            if str(mention.get("matched_paper_corpus_id") or "") == wanted:
                start = mention.get("start")
                break
        sentence = ""
        if start is not None:
            for span in locator.get("sentence_offsets") or ():
                if span["start"] <= int(start) < span["end"]:
                    sentence = raw[int(span["start"]):int(span["end"])]
                    break
        if sentence:
            mention_out.setdefault(target, []).append(sentence)
    own = {paper: " ".join(parts)[:max_chars] for paper, parts in out.items()}
    mentions = {paper: " ".join(parts)[:max_chars] for paper, parts in mention_out.items()}
    if split_mentions:
        return own, mentions
    merged = dict(own)
    for paper, text in mentions.items():
        merged[paper] = (merged.get(paper, "") + " " + text).strip()[:max_chars]
    return merged


def classify_domain_relevance_unused():
    pass


def run_skeleton_v2(corpus_path, edge_path, out_dir, config, *, dump_edges=True,
                    allow_degraded=False):
    """Facet-conditioned skeleton with domain relevance, pools and cards.

    A corpus built from a degraded plan carries that fact in ``input_state``.  The
    skeleton refuses such a corpus unless the caller asks for it, because every
    downstream count -- graph support, eligible, priority -- is a statement about
    the retrieval that produced it, and a missing channel makes those counts
    incomparable with a complete run.
    """

    from optomind_research.runtime.upgrade3 import domain_relevance as DR

    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    input_state = dict(corpus.get("input_state") or {})
    if corpus.get("degraded") and not allow_degraded:
        raise DegradedCorpusError(input_state)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[1/11] facet membership", flush=True)
    ff = facet_features(corpus, config)

    print("[2/11] edges -> layers + backbone", flush=True)
    raw_edges = load_raw_edges(edge_path)
    # The enrichment file is read here as well as at step 6: node identity needs
    # the titles, years and DOIs of the boundary papers before the graph exists.
    try:
        identity_meta, _early_vectors = _load_enrichment(out_dir)
    except Exception:  # noqa: BLE001
        identity_meta = {}
    edge_set = build_layers(raw_edges, corpus, ff, config, identity_meta=identity_meta)
    (out_dir / "WORK_IDENTITY.json").write_text(
        json.dumps({"summary": {k: v for k, v in edge_set.identity.items() if k != "registry"},
                    "registry": edge_set.identity.get("registry") or []},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    print("[3/11] multiplex graph", flush=True)
    graphs = build_graphs(edge_set, corpus, config)

    print("[4/11] communities", flush=True)
    communities = detect_communities(graphs, config)
    print("       communities=%d modularity=%.4f stability=%s" % (
        communities["community_count"], communities["modularity"],
        communities["mean_stability"]), flush=True)

    print("[5/11] graph features", flush=True)
    global_feat = graph_features(graphs, communities["membership"])

    print("[6/11] metadata + embeddings", flush=True)
    meta, vectors = _load_enrichment(out_dir)
    fallback = _meta_from(corpus, raw_edges)
    for pid, info in fallback.items():
        merged = meta.setdefault(pid, {})
        for key in ("title", "year", "citation_count"):
            if merged.get(key) in (None, "") and info.get(key) not in (None, ""):
                merged[key] = info[key]
        merged.setdefault("is_core", pid in graphs["core_ids"])
    for paper in corpus.get("papers") or ():
        pid = str(paper.get("paper_id") or "")
        if pid:
            merged = meta.setdefault(pid, {})
            merged.setdefault("title", paper.get("title") or "")
            merged.setdefault("year", paper.get("year"))
            merged["is_core"] = True
    filled = _merge_abstract_fill(out_dir, meta)
    print("       meta=%d vectors=%d abstract_fill=%d" % (
        len(meta), len(vectors), filled), flush=True)

    temporal = temporal_features(meta, raw_edges, config)
    communities["profiles"] = community_profiles(
        graphs, communities, meta, temporal, global_feat, raw_edges,
        graphs["core_ids"], config)

    print("[7/11] per-facet scopes", flush=True)
    scopes = facet_scopes(graphs, edge_set, ff)
    facet_feat = {facet: graph_features_on(graphs, sorted(scope["nodes"]), communities)
                  for facet, scope in scopes.items()}

    print("[8/11] cross-facet", flush=True)
    cross = cross_facet(ff, graphs["core_ids"])
    consensus = reference_consensus(raw_edges, graphs["core_ids"])

    # Domain judgement comes BEFORE role judgement on purpose.  The graph
    # amplifies whatever it is given, so a paper the domain engine rejected must
    # never be handed to it in the first place.
    print("[9/11] domain relevance engine", flush=True)
    own_text, mention_text = _material_text_by_paper(out_dir, corpus, edge_set)
    # A bounded pre-reading pass may have run on this topic.  When it has, its
    # labels are evidence about relevance that the lexical rulers do not have, and
    # they are allowed to move a paper up as well as down.
    reading_path = Path(out_dir) / "READING.json"
    reading = {}
    if reading_path.exists():
        try:
            reading = json.loads(reading_path.read_text(encoding="utf-8")).get("judgements") or {}
        except ValueError:
            reading = {}
        print("       reading labels available for %d papers" % len(reading), flush=True)
    domain = DR.classify_domain_relevance(ff, meta, vectors, edge_set, raw_edges,
                                          graphs["core_ids"], config, corpus=corpus,
                                          extra_text=own_text, mention_text=mention_text,
                                          reading=reading)
    print("       " + json.dumps(domain["counts"], ensure_ascii=False), flush=True)
    print("       documents_with_text=%d vocabulary=%s" % (
        domain["documents_with_text"],
        json.dumps(domain["vocabulary_sizes"], ensure_ascii=False)), flush=True)

    print("[10/11] pools + gated role candidates", flush=True)
    pools = build_pools(domain, graphs, ff, config)
    print("       " + json.dumps(pools["sizes"], ensure_ascii=False), flush=True)
    blocked = {p for p, row in domain["aggregate"].items()
               if row["class"] in DR.BLOCKED_FROM_ROLES}
    # Who may be offered as direct evidence.  A reading judgement that the paper
    # studies the question is the strongest signal available before full text; the
    # lexical hint is the fallback when no reading pass has run.  A paper whose
    # material was missing is in neither set.
    direct_evidence_ids = {
        paper for paper, row in domain["aggregate"].items()
        if row.get("reading_label") == "directly_relevant"
        or (not reading and row.get("pre_reading_hint") == "likely_answers_this_facet")}
    roles = role_candidates(graphs, edge_set, ff, scopes, global_feat, facet_feat,
                            temporal, cross, communities, consensus, meta, config,
                            blocked=blocked, direct_evidence_ids=direct_evidence_ids)
    print("       blocked_from_roles=%d direct_evidence_eligible=%d" % (
        len(blocked), len(direct_evidence_ids)), flush=True)

    domain_index = {}
    for facet, rows in domain["by_facet"].items():
        for paper, row in rows.items():
            domain_index.setdefault(paper, {})[facet] = row
    for paper, row in (domain.get("aggregate") or {}).items():
        domain_index.setdefault(paper, {})["__aggregate__"] = row

    print("[11/11] two-tier selection + cards", flush=True)
    reps = community_representative_ranks(graphs, global_feat, communities)
    clusters = semantic_clusters(vectors, sorted(pools["eligible_pool"]), config)
    portfolio = build_two_tier_selection(roles, cross, global_feat, temporal, ff,
                                         graphs, set(pools["eligible_pool"]),
                                         domain_index, config, community_reps=reps,
                                         clusters=clusters)
    # Admission to the priority tier is a separate decision from being kept in the
    # investigation pool.  A paper whose relevance has not been judged on real
    # material is offered for verification instead of being counted as a confirmed
    # pick, and nothing is deleted: it stays in the deep corpus and in the graph.
    priority_admitted = set(pools.get("priority_eligible_pool") or ())
    offered = list(portfolio["priority_portfolio"])
    portfolio["priority_portfolio"] = [p for p in offered if p in priority_admitted]
    portfolio["pending_verification_items"] = [
        {"paper_id": p,
         "reason": ((domain_index.get(p, {}).get("__aggregate__") or {}).get("reading_label")
                    or "not_read_yet"),
         "pre_reading_hint": (domain_index.get(p, {}).get("__aggregate__") or {}).get(
             "pre_reading_hint") or "",
         "material_sources": (domain_index.get(p, {}).get("__aggregate__") or {}).get(
             "material_sources") or []}
        for p in offered if p not in priority_admitted]
    print("       semantic clusters=%d" % len(set(clusters.values())), flush=True)
    print("       priority=%d (offered %d, held for verification %d) deep_analysis=%d "
          "stopped=%s final_gain=%s" % (
              len(portfolio["priority_portfolio"]), len(offered),
              len(portfolio["pending_verification_items"]),
              len(portfolio["deep_analysis_corpus"]),
              portfolio["stopped_because"], portfolio.get("final_gain")), flush=True)
    priority = {row["paper_id"]: (row.get("marginal_gain") or 0.0)
                for row in portfolio.get("trace") or ()}
    cards = build_cards(pools["eligible_pool"], domain, graphs, global_feat, facet_feat,
                        temporal, roles, cross, communities, consensus, edge_set,
                        meta, reps, domain_index, priority, selection=portfolio)

    skeleton = {
        "schema_version": "optomind.scholarly_skeleton.v1",
        "labelled_as": "Facet-conditioned Multiplex Scholarly Skeleton -- v1 with domain relevance",
        "configuration": config,
        # Every count below is a statement about the retrieval that produced it.
        "input_state": dict(input_state or {"plan_state": "complete", "degraded": False}),
        "degraded": bool(corpus.get("degraded")),
        "facets": ff["facets"],
        "paper_facet_features": ff["features"],
        "pools": {"sizes": pools["sizes"], "per_facet": pools["per_facet"],
                  "graph_support_pool": pools["graph_support_pool"],
                  "eligible_pool": pools["eligible_pool"],
                  "excluded": pools["excluded"]},
        # One work, one slot: the registry maps every provider record of a work to
        # the canonical node the pools use, with the rule that merged it.
        "work_identity": {"registry_file": "WORK_IDENTITY.json",
                          "merged_works": (edge_set.identity or {}).get("merged_works", 0),
                          "aliases": len((edge_set.identity or {}).get("alias") or {}),
                          "nodes_considered": (edge_set.identity or {}).get("nodes_considered", 0)},
        "domain_relevance": {
            "counts": domain["counts"], "thresholds": domain["thresholds"],
            "vocabulary_sizes": domain["vocabulary_sizes"],
            "vocabulary_audit_file": "VOCABULARY_AUDIT.json",
            "documents_with_text": domain["documents_with_text"],
        },
        "edge_statistics": edge_set.statistics,
        "communities": {"summary": {k: v for k, v in communities.items()
                                    if k not in ("membership", "profiles")},
                        "profiles": communities["profiles"]},
        "candidate_roles": roles,
        "selection": portfolio,
        "audit": {
            "graph_nodes": len(graphs["nodes"]),
            "enriched_nodes": len(meta),
            "embedded_nodes": len(vectors),
            "admission_thresholds": edge_set.statistics.get("admission_thresholds"),
        },
    }
    (out_dir / "SCHOLARLY_SKELETON.json").write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "COMMUNITIES.json").write_text(
        json.dumps(communities["profiles"], ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "INVESTIGATION_PORTFOLIO.json").write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "VOCABULARY_AUDIT.json").write_text(
        json.dumps(domain["vocabulary_audit"], ensure_ascii=False, indent=1),
        encoding="utf-8")
    (out_dir / "SELECTION_TRACE.json").write_text(
        json.dumps({"trace": portfolio["trace"],
                    "priority_portfolio": portfolio["priority_portfolio"],
                    "deep_analysis_corpus": portfolio["deep_analysis_corpus"]},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "INVESTIGATION_CARDS.json").write_text(
        json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "DOMAIN_RELEVANCE.json").write_text(
        json.dumps({p: rows for p, rows in domain_index.items()},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    if dump_edges:
        import pandas as pd
        rows = []
        for name, graph in graphs["layers"].items():
            for edge, weight in zip(graph.get_edgelist(), graph.es["weight"]):
                rows.append({"layer": name, "source": graphs["nodes"][edge[0]],
                             "target": graphs["nodes"][edge[1]], "weight": weight})
        pd.DataFrame(rows).to_parquet(out_dir / "GRAPH_EDGES.parquet", index=False)
        facet_rows = []
        for paper, by_facet in ff["features"].items():
            for facet, row in by_facet.items():
                facet_rows.append({"paper_id": paper, "facet_id": facet, **row})
        pd.DataFrame(facet_rows).to_parquet(out_dir / "PAPER_FACET_FEATURES.parquet",
                                            index=False)
        domain_rows = []
        for paper, by_facet in domain_index.items():
            for facet, row in by_facet.items():
                flat = {k: v for k, v in row.items() if not isinstance(v, (list, dict))}
                domain_rows.append({"paper_id": paper, "facet_id": facet, **flat})
        pd.DataFrame(domain_rows).to_parquet(out_dir / "DOMAIN_RELEVANCE.parquet",
                                             index=False)

    return skeleton, pools, cards, portfolio


# ------------------------------------------- two-tier continuous selection --


def _paper_dimensions(paper, roles, cross, graph_feat, temporal, facet_feature,
                      domain_index, graphs):
    """Discrete knowledge dimensions a paper contributes to."""

    dims = {}
    for facet, by_role in (roles.get("by_facet") or {}).items():
        for role, rows in by_role.items():
            for row in rows:
                if row["paper_id"] == paper:
                    dims["role@facet:%s@%s" % (role, facet)] = 1.0
                    dims["roleany:%s" % role] = 1.0
    memberships = (cross.get("papers", {}).get(paper) or {})
    for facet in memberships.get("facets") or ():
        dims["facet:%s" % facet] = 1.0
    for facet, row in (facet_feature["features"].get(paper) or {}).items():
        if row["query_coverage"] > 0:
            for query_id in (facet_feature["queries_per_facet"].get(facet) or ()):
                dims["query:%s" % query_id] = 1.0
    community = (graph_feat.get(paper) or {}).get("community")
    if community is not None:
        dims["community:%s" % community] = 1.0
    year = (temporal.get(paper) or {}).get("year")
    if isinstance(year, int):
        dims["year:%d" % year] = 1.0
    for facet, row in (domain_index.get(paper) or {}).items():
        if facet.startswith("__") or not row.get("contextual_mentions_from_other_papers"):
            continue
        mentions = row["contextual_mentions_from_other_papers"]
        tier = "high" if mentions >= 10 else ("med" if mentions >= 3 else "low")
        dims["contextual:%s@%s" % (tier, facet)] = 1.0
    classes = {r["class"] for f, r in (domain_index.get(paper) or {}).items()
               if not f.startswith("__")}
    for klass in classes:
        dims["domain:%s" % klass] = 1.0
    dims["tier:core" if paper in graphs["core_ids"] else "tier:boundary"] = 1.0
    return dims


def _dimension_kind(name):
    return name.split(":", 1)[0]


def semantic_clusters(vectors, papers, config):
    """Group the pool by embedding so diversity can be a submodular term."""

    import numpy as np
    from scipy.cluster.vq import kmeans2

    cfg = config.get("portfolio") or {}
    k = int(cfg.get("semantic_cluster_count", 40))
    usable = [p for p in papers if p in vectors]
    if len(usable) < k * 3:
        k = max(2, len(usable) // 3)
    if len(usable) < 4:
        return {p: 0 for p in papers}
    matrix = np.asarray([vectors[p] for p in usable], dtype=np.float64)
    matrix = matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)
    try:
        _, labels = kmeans2(matrix, k, minit="++", seed=20260914, iter=40)
    except Exception:  # noqa: BLE001
        labels = np.zeros(len(usable), dtype=int)
    out = {p: int(label) for p, label in zip(usable, labels)}
    for paper in papers:
        out.setdefault(paper, -1)
    return out


def build_two_tier_selection(roles, cross, graph_feat, temporal, facet_feature,
                             graphs, eligible, domain_index, config,
                             community_reps=None, clusters=None):
    """Continuous-coverage selection producing a Priority Portfolio and a
    Deep-analysis Corpus.

    Binary coverage made "this community already has one representative" mean
    "this community is understood", which stopped the selection at 14 papers.
    Coverage here saturates instead of flipping: the first paper in a community
    is worth a lot, the fifth is worth less but is NOT worth nothing, and a
    paper that is semantically far from everything already chosen keeps earning
    its place.  Selection therefore stops when new papers really stop adding
    knowledge, not when every bucket has been ticked once.
    """

    from math import exp

    cfg = config.get("portfolio") or {}
    stop = (config.get("pools") or {}).get("portfolio_stopping") or {}
    weights = cfg.get("coverage_weights") or {}
    default_w = float(weights.get("default", 0.03))
    lam = float(cfg.get("greedy_lambda_redundancy", 0.45))
    value_weight = float(cfg.get("paper_value_weight", 0.35))
    novelty_weight = float(weights.get("novelty", 0.35))
    floor_gain = float(stop.get("min_marginal_gain", 0.02))
    # The objective is scale free in practice only if the stop rule is too: the
    # absolute floor is a safety net, and the real criterion is that the gain has
    # decayed to a small fraction of the first pick.
    stop_fraction = float(stop.get("stop_gain_fraction_of_first", 0.15))
    priority_fraction = float(stop.get("priority_gain_fraction_of_first", 0.35))
    max_size = int(stop.get("max_size", 400))
    scales = cfg.get("saturation_scales") or {}

    paper_value = {}
    for facet, by_role in (roles.get("by_facet") or {}).items():
        for role, rows in by_role.items():
            for row in rows:
                paper_value[row["paper_id"]] = max(
                    paper_value.get(row["paper_id"], 0.0), row["score"])
    for paper, rows in (domain_index or {}).items():
        best = max((r for f, r in rows.items() if not f.startswith("__")),
                   key=lambda r: r.get("semantic_relevance") or -1.0, default=None)
        if not best:
            continue
        bonus = 0.0
        mentions = best.get("contextual_mentions_from_other_papers") or 0
        if mentions:
            bonus += min(0.6, 0.08 * mentions)
        if best.get("semantic_relevance"):
            bonus += 0.4 * max(0.0, best["semantic_relevance"] - 0.7)
        if best.get("query_coverage"):
            bonus += 0.3 * best["query_coverage"]
        paper_value[paper] = paper_value.get(paper, 0.0) + bonus

    pool = {p: _paper_dimensions(p, roles, cross, graph_feat, temporal,
                                 facet_feature, domain_index, graphs)
            for p in eligible}
    if not pool:
        return {"priority_portfolio": [], "deep_analysis_corpus": [],
                "trace": [], "stopped_because": "empty_pool"}

    counts = {}
    selected, trace = [], []
    priority_cut = None
    remaining = dict(pool)
    first_gain = None
    seeds = []
    if (config.get("pools") or {}).get("seed_community_representatives", True) and community_reps:
        best_per_community = {}
        for paper in remaining:
            community = (graph_feat.get(paper) or {}).get("community")
            if community is None or community_reps.get(paper) != 1:
                continue
            best_per_community.setdefault(community, paper)
        seeds = [best_per_community[c] for c in sorted(best_per_community)]

    # Novelty rewards distance, and distance alone promoted a quantised language
    # model, a genomics alignment accelerator and a face-recognition paper: each
    # was far from every paper already chosen, purely because it belonged to a
    # different subject.  Novelty is therefore scaled by how strongly the domain
    # engine vouches for the paper, so a distant paper from another field earns
    # little while a distant paper inside the field still earns its place.
    class_weight = cfg.get("domain_class_weight") or {
        "IN_DOMAIN": 1.0, "ADJACENT": 0.45, "UNCERTAIN": 0.2}

    def domain_weight(paper):
        classes = {r["class"] for f, r in (domain_index.get(paper) or {}).items()
                   if not f.startswith("__")}
        if not classes:
            return float(class_weight.get("UNCERTAIN", 0.2))
        return max(float(class_weight.get(c, 0.2)) for c in classes)

    # Diversity used to be "1 - adjacency to whatever is selected SO FAR", which
    # is path dependent: the same candidate scored differently depending on when
    # it was considered, so the greedy was not maximising a set function at all.
    # It is now coverage of semantic clusters, a monotone submodular term: the
    # first paper from a cluster pays in full, later ones from the same cluster
    # pay progressively less, and the maximum marginal gain is non-increasing.
    clusters = clusters or {p: 0 for p in eligible}
    cluster_scale = float(cfg.get("semantic_cluster_scale", 3.0))

    def gain_of(paper, dims):
        total = 0.0
        parts = {}
        for name in dims:
            kind = _dimension_kind(name)
            weight = float(weights.get(kind, default_w))
            scale = float(scales.get(kind, 2.0))
            seen = counts.get(name, 0.0)
            contribution = weight * (1.0 - exp(-1.0 / max(scale, 0.5))) * exp(-seen / max(scale, 0.5))
            parts[kind] = parts.get(kind, 0.0) + contribution
            total += contribution
        cluster = clusters.get(paper, -1)
        if cluster >= 0:
            seen = counts.get("cluster:%d" % cluster, 0.0)
            contribution = (novelty_weight * domain_weight(paper)
                            * (1.0 - exp(-1.0 / cluster_scale))
                            * exp(-seen / cluster_scale))
        else:
            contribution = novelty_weight * domain_weight(paper) * 0.1
        parts["semantic_cluster_coverage"] = contribution
        total += contribution
        return total, parts

    def mark_selected(paper, dims):
        for name in dims:
            counts[name] = counts.get(name, 0.0) + 1.0
        cluster = clusters.get(paper, -1)
        if cluster >= 0:
            counts["cluster:%d" % cluster] = counts.get("cluster:%d" % cluster, 0.0) + 1.0

    for paper in seeds:
        if paper in remaining:
            dims = remaining.pop(paper)
            mark_selected(paper, dims)
            selected.append(paper)
            trace.append({"rank": len(selected), "paper_id": paper,
                          "marginal_gain": None, "reason": "community_representative_seed"})

    while remaining and len(selected) < max_size:
        best_paper, best_value, best_gain, best_parts = None, -1e9, 0.0, {}
        for paper, dims in remaining.items():
            gain, parts = gain_of(paper, dims)
            value = gain + value_weight * paper_value.get(paper, 0.0)
            if value > best_value:
                best_paper, best_value, best_gain, best_parts = paper, value, gain, parts
        if best_paper is None:
            break
        if first_gain is None:
            first_gain = best_value
        # Stop on the quantity the greedy actually maximises.  Previously the
        # comparison used the objective while the recorded value and the stop
        # test used the coverage part, so the trace was not the trace of any
        # greedy maximisation and rose on 57 of 123 picks.
        threshold = max(floor_gain, (first_gain or 0.0) * stop_fraction)
        if best_value < threshold and len(selected) >= int(stop.get("min_size", 8)):
            break
        dims = remaining.pop(best_paper)
        mark_selected(best_paper, dims)
        selected.append(best_paper)
        top_parts = sorted(best_parts.items(), key=lambda kv: -kv[1])[:3]
        trace.append({"rank": len(selected), "paper_id": best_paper,
                      "marginal_gain": round(best_value, 6),
                      "coverage_gain": round(best_gain, 6),
                      "paper_value_term": round(best_value - best_gain, 6),
                      "reason": "coverage:" + ",".join(k for k, _ in top_parts),
                      "components": {k: round(v, 5) for k, v in best_parts.items()}})
        # The cut is computed from the trace at the end, so it can never land
        # inside the seeded representatives.

    deep = list(selected)
    # Where the steep part of the gain curve ends.  Derived from the recorded
    # trace rather than tracked mid-loop, because a mid-loop cut landed before
    # the seeded representatives and dropped three communities out of Priority.
    coverage_gains = [row["marginal_gain"] for row in trace
                      if row["marginal_gain"] is not None]
    seed_count = len(trace) - len(coverage_gains)
    if coverage_gains and first_gain:
        threshold = first_gain * priority_fraction
        cut = next((i for i, g in enumerate(coverage_gains) if g < threshold),
                   len(coverage_gains))
        priority_cut = seed_count + cut
    else:
        priority_cut = len(deep)
    # The Priority Portfolio is deliberately high precision: only papers the
    # domain engine placed firmly in the field.  ADJACENT and UNCERTAIN remain in
    # the Deep-analysis Corpus and in the Eligible Pool.
    priority_classes = set((config.get("pools") or {}).get("priority_classes")
                           or ["IN_DOMAIN"])
    def in_priority(paper):
        classes = {r["class"] for f, r in (domain_index.get(paper) or {}).items()
                   if not f.startswith("__")}
        return bool(classes) and classes <= priority_classes
    priority = [p for p in (deep[:priority_cut] if priority_cut else deep)
                if in_priority(p)]
    return {
        "priority_portfolio": priority,
        "deep_analysis_corpus": deep,
        "trace": trace,
        "first_gain": round(first_gain, 6) if first_gain else None,
        "final_gain": round(trace[-1]["marginal_gain"], 6) if trace and trace[-1]["marginal_gain"] else None,
        "stopped_because": ("gain_below_floor" if remaining else "candidate_pool_exhausted"),
        "priority_gain_fraction_of_first": priority_fraction,
        "min_marginal_gain": floor_gain,
        "stop_gain_fraction_of_first": stop_fraction,
        "effective_stop_threshold": round(max(floor_gain,
                                              (first_gain or 0.0) * stop_fraction), 6),
        "max_size": max_size,
        "paper_value_weight": value_weight,
        "saturation_scales": {k: float(v) for k, v in scales.items()},
        "semantic_cluster_count": int(cfg.get("semantic_cluster_count", 40)),
        "stopping_uses": "the same quantity the greedy maximises (gain + value term)",
        "selection_method": "greedy over saturating coverage + semantic novelty",
    }
