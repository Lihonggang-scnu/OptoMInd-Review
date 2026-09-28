"""Candidate Corpus, layer 1: schema -> one deduplicated, traceable paper set.

Two independent things are modelled here and they are never collapsed into one
another:

* papers[]          -- one object per real paper, identity fields only;
* retrieval_hits[]  -- one object per real retrieval hit, never deduplicated.

A hit is not a property of a paper.  The same paper reached by the same question
through three different body snippets is three hits, because several passages of
one paper answering one question is itself a signal the later layers will want.
Deduplication applies to papers only.

Snippet text is deliberately kept OUT of the corpus JSON: the JSON carries a
locator and a hash, and the raw text lives in a side store keyed by hit_id.

This layer does not download full text, parse bodies, walk the citation graph,
read citation contexts, judge a paper's role, or analyse relations.  It turns a
schema into a corpus, nothing more.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "optomind.candidate_corpus.v2"

#: Two budgets, deliberately not one.
#:
#: The EVIDENCE budget is every hit the provider returned for a query: passages
#: of one paper answer different parts of a facet, so no hit is ever folded into
#: another.  The DISCOVERY budget is how many distinct papers a single query
#: should put on the table.  The cross-domain run showed why they must be
#: separate: one query returned 100 snippet hits covering 12 papers, one of them
#: twelve times, so the net was narrow even though the evidence was rich.
#: A query that reaches the evidence budget but misses the discovery target is
#: fetched one step deeper rather than accepted as a narrow net.
SNIPPET_DISCOVERY_TARGET = 60

#: The one deeper request is bounded by this factor of the starting limit, so a
#: pathological query cannot buy unbounded provider calls.
SNIPPET_DISCOVERY_MAX_FACTOR = 3

#: Hard safety ceiling.  One abnormally broad or outright wrong query must not be
#: able to pull thousands of papers into the corpus in a single sweep.
MAX_RESULTS_PER_QUERY = 400

#: Pages of this size are requested until the per-query limit is reached.
DEFAULT_PAGE_SIZE = 100

#: Engineering target for the current topic, used to choose the per-query stop
#: point.  NOT a validation rule: the real size is whatever the queries return.
CORPUS_TARGET_LOW = 500
CORPUS_TARGET_HIGH = 800

KEYWORD_SOURCE = "paper_search"
QUESTION_SOURCE = "snippet_search"


class DegradedPlanError(RuntimeError):
    """Raised when a plan with an empty retrieval channel is handed in as whole.

    An invalid plan used to travel into the corpus with nothing in the artefacts
    saying a channel was missing: the run looked healthy and simply had no
    keyword hits.  Consuming a degraded plan is now an explicit decision.
    """

    def __init__(self, state):
        self.state = dict(state or {})
        super().__init__(
            "planner input is degraded (%s): missing %s"
            % (self.state.get("plan_state") or "unknown",
               ", ".join(self.state.get("missing_channels") or ["?"])))


def channel_state(plan):
    """Which retrieval channels the plan actually carries.

    Derived from the plan itself, not from a caller's promise, so a caller that
    forgets to pass the planner state still cannot consume an empty channel in
    silence.
    """

    facets = (plan or {}).get("facets") or []
    keyword = sum(len((f or {}).get("keyword_queries") or []) for f in facets
                  if isinstance(f, Mapping))
    question = sum(len((f or {}).get("question_queries") or []) for f in facets
                   if isinstance(f, Mapping))
    missing = []
    if not facets:
        missing.append("facets")
    else:
        if keyword == 0:
            missing.append("keyword")
        if question == 0:
            missing.append("question")
    return {"facets": len(facets), "keyword_queries": keyword,
            "question_queries": question, "missing_channels": missing}

PAPER_FIELDS = ("paper_id", "corpus_id", "doi", "title", "authors", "year",
                "venue", "abstract")


@dataclass(frozen=True)
class QuerySpec:
    """One query exactly as the schema wrote it, with a stable id."""

    query_id: str
    facet_id: str
    query_type: str          # "keyword" | "question"
    query_text: str
    retrieval_source: str    # the endpoint this query is executed against


@dataclass
class Hit:
    """One retrieval hit.  Never merged with another hit."""

    identifiers: dict
    row: dict
    raw_snippet_text: str = ""
    fields: dict = field(default_factory=dict)


def plan_queries(plan):
    """Flatten the schema into the exact list of queries, each with an id."""

    specs = []
    for facet in plan.get("facets") or ():
        if not isinstance(facet, Mapping):
            continue
        facet_id = str(facet.get("id") or "")
        for index, text in enumerate(facet.get("keyword_queries") or (), start=1):
            if str(text or "").strip():
                specs.append(QuerySpec("%s_KQ_%02d" % (facet_id, index), facet_id,
                                       "keyword", str(text).strip(), KEYWORD_SOURCE))
        for index, text in enumerate(facet.get("question_queries") or (), start=1):
            if str(text or "").strip():
                specs.append(QuerySpec("%s_QQ_%02d" % (facet_id, index), facet_id,
                                       "question", str(text).strip(), QUESTION_SOURCE))
    return specs


# --------------------------------------------------------------------- ids --


def _identifiers(*, paper_id="", doi="", corpus_id=None):
    """Every stable handle we got, keyed by kind.

    A paper is kept when ANY handle exists; nothing here requires all of them.
    """

    out = {}
    if str(paper_id or "").strip():
        out["s2"] = str(paper_id).strip()
    if str(doi or "").strip():
        out["doi"] = str(doi).strip().casefold()
    if corpus_id not in (None, ""):
        out["corpus"] = str(corpus_id).strip()
    return out


def _handle(kind, value):
    text = str(value or "").strip()
    if not text:
        return ""
    return "%s:%s" % (kind, text.casefold() if kind == "doi" else text)


class _Union:
    """Union-find over every handle seen, so one paper cannot appear twice.

    Two queries can legitimately report the same paper through different handles
    (one gives the S2 id, the other only a corpus id).  Keying on a single handle
    would keep both; this merges them.
    """

    def __init__(self):
        self.parent = {}

    def find(self, node):
        self.parent.setdefault(node, node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def union(self, left, right):
        a, b = self.find(left), self.find(right)
        if a != b:
            first, second = sorted((a, b))
            self.parent[second] = first


# ------------------------------------------------------------------ snippet --


def _locator(item):
    """Everything needed to find this passage again in the source paper."""

    snippet = item.get("snippet") if isinstance(item.get("snippet"), Mapping) else {}
    annotations = snippet.get("annotations") if isinstance(snippet.get("annotations"), Mapping) else {}
    offset = snippet.get("snippetOffset") if isinstance(snippet.get("snippetOffset"), Mapping) else {}

    def spans(value):
        out = []
        for entry in value or ():
            if isinstance(entry, Mapping):
                out.append({"start": entry.get("start"), "end": entry.get("end")})
        return out

    ref_mentions = []
    for entry in annotations.get("refMentions") or ():
        if not isinstance(entry, Mapping):
            continue
        ref_mentions.append({
            "start": entry.get("start"),
            "end": entry.get("end"),
            # Kept because the relation layer will want to follow
            # question -> passage -> the paper the passage names.
            "matched_paper_corpus_id": entry.get("matchedPaperCorpusId"),
        })

    return {
        "section": snippet.get("section"),
        "snippet_kind": snippet.get("snippetKind"),
        "start": offset.get("start"),
        "end": offset.get("end"),
        "sentence_offsets": spans(annotations.get("sentences")),
        "ref_mentions": ref_mentions,
    }


def _text_hash(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- harvest --


def _snippet_hits(items, spec, *, rank_offset=0, request_index=1):
    """Turn one batch of snippet items into hits, keeping every passage.

    Ranks are offset by the batches before this one, so that a hit keeps a unique
    (rank, request) provenance pair even when the same query had to be fetched
    twice to widen the net.
    """

    out = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        paper = item.get("paper") if isinstance(item.get("paper"), Mapping) else {}
        snippet = item.get("snippet") if isinstance(item.get("snippet"), Mapping) else {}
        text = str(snippet.get("text") or "")
        out.append(Hit(
            identifiers=_identifiers(corpus_id=paper.get("corpusId")),
            fields={"title": str(paper.get("title") or ""),
                    "authors": list(paper.get("authors") or [])},
            raw_snippet_text=text,
            row={
                "facet_id": spec.facet_id, "query_id": spec.query_id,
                "query_type": spec.query_type, "query_text": spec.query_text,
                "retrieval_source": spec.retrieval_source,
                "result_rank": rank_offset + position, "score": item.get("score"),
                "request_index": request_index,
                "snippet_locator": _locator(item),
                "text_hash": _text_hash(text) if text else None,
            }))
    return out


def _passage_key(hit):
    """Identity of one passage: the paper it belongs to and the bytes themselves."""

    return (str(hit.identifiers.get("corpus") or ""), str(hit.row.get("text_hash") or ""))


def _diversity_order(hits):
    """First passage of each paper in provider order, then the rest, same order."""

    seen = set()
    first, rest = [], []
    for hit in hits:
        key = str(hit.identifiers.get("corpus") or "") or str(id(hit))
        if key in seen:
            rest.append(hit)
        else:
            seen.add(key)
            first.append(hit)
    return first + rest


def harvest_query(gateway, spec, *, per_query_limit, discovery_target=0):
    """Run ONE query and keep every hit it produced."""

    limit = max(1, min(int(per_query_limit), MAX_RESULTS_PER_QUERY))
    target = max(0, int(discovery_target or 0))
    hits = []
    audit = {
        "query_id": spec.query_id, "facet_id": spec.facet_id,
        "query_type": spec.query_type, "query_text": spec.query_text,
        "retrieval_source": spec.retrieval_source,
        "requested_limit": limit, "returned": 0, "status": None,
    }

    if spec.retrieval_source == QUESTION_SOURCE:
        # The evidence budget is everything the provider returns; the discovery
        # budget decides how wide the net has to be before we stop asking.  A
        # query whose first page is dominated by one paper is fetched one step
        # deeper, and the deeper passages are kept as evidence too: this is not
        # the (query, paper) fold that throws evidence away, it is a wider net.
        request_limit = limit
        if target:
            request_limit = min(MAX_RESULTS_PER_QUERY,
                                max(limit, min(target * 2, limit * SNIPPET_DISCOVERY_MAX_FACTOR)))
        items, response = gateway.search_snippets(spec.query_text, limit=request_limit)
        audit["status"] = getattr(response, "status_code", None)
        audit["requested_limits"] = [request_limit]
        hits = _snippet_hits(items, spec)
        unique = len({h.identifiers.get("corpus") for h in hits if h.identifiers.get("corpus")})
        exhausted = len(items) < request_limit
        while (target and unique < target and not exhausted
               and request_limit < MAX_RESULTS_PER_QUERY):
            deeper = min(MAX_RESULTS_PER_QUERY, max(request_limit + 1,
                                                   request_limit * SNIPPET_DISCOVERY_MAX_FACTOR))
            more, response = gateway.search_snippets(spec.query_text, limit=deeper)
            audit["requested_limits"].append(deeper)
            audit["status"] = getattr(response, "status_code", None)
            seen = {_passage_key(h) for h in hits}
            deeper_hits = _snippet_hits(more, spec, rank_offset=len(hits),
                                        request_index=len(audit["requested_limits"]))
            extra = [h for h in deeper_hits if _passage_key(h) not in seen]
            collapsed = len(more) - len(extra)
            audit["identical_passage_collapses"] = (
                int(audit.get("identical_passage_collapses") or 0) + max(0, collapsed))
            hits.extend(extra)
            unique = len({h.identifiers.get("corpus") for h in hits
                          if h.identifiers.get("corpus")})
            exhausted = len(more) < deeper
            request_limit = deeper
        # The provider ranks by relevance; a consumer that reads the first K hits
        # of a query should meet K papers before it meets a second passage of one.
        hits = _diversity_order(hits)
        for position, hit in enumerate(hits, start=1):
            hit.row["diversity_rank"] = position
        per_paper = {}
        for hit in hits:
            key = str(hit.identifiers.get("corpus") or "")
            per_paper[key] = per_paper.get(key, 0) + 1
        audit.update({
            "raw_snippet_hits": len(hits),
            "unique_papers": len(per_paper),
            "max_snippets_per_paper": max(per_paper.values()) if per_paper else 0,
            "discovery_target": target,
            "requests": len(audit["requested_limits"]),
        })
        audit["returned"] = len(hits)
        return hits, audit
    else:
        papers, response = gateway.search_papers(spec.query_text, limit=limit)
        audit["status"] = getattr(response, "status_code", None)
        for rank, record in enumerate(papers[:limit], start=1):
            hits.append(Hit(
                identifiers=_identifiers(
                    paper_id=str(getattr(record, "paper_id", "") or ""),
                    doi=str(getattr(record, "doi", "") or ""),
                    corpus_id=getattr(record, "corpus_id", None)),
                fields={"title": str(getattr(record, "title", "") or ""),
                        "authors": list(getattr(record, "authors", []) or []),
                        "year": getattr(record, "year", None),
                        "venue": str(getattr(record, "venue", "") or ""),
                        "abstract": str(getattr(record, "abstract", "") or "")},
                row={
                    "facet_id": spec.facet_id, "query_id": spec.query_id,
                    "query_type": spec.query_type, "query_text": spec.query_text,
                    "retrieval_source": spec.retrieval_source,
                    "result_rank": rank, "score": None,
                    "snippet_locator": None, "text_hash": None,
                }))

    audit["returned"] = len(hits)
    return hits, audit


# ------------------------------------------------------------------- papers --


def _first(identifiers, kind):
    """Primary handle of a kind.

    Callers hand this two shapes: raw harvest identifiers map a kind to a single
    string, while merged entries map a kind to an ordered list.  Treating the
    string form as a list silently returned its first CHARACTER, which truncated
    every id to one letter.
    """

    value = identifiers.get(kind)
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _paper_from(identifiers, fields):
    return {
        "paper_id": _first(identifiers, "s2"),
        "corpus_id": _first(identifiers, "corpus"),
        "doi": _first(identifiers, "doi"),
        "title": fields.get("title", ""),
        "authors": list(fields.get("authors") or []),
        "year": fields.get("year"),
        "venue": fields.get("venue", ""),
        "abstract": fields.get("abstract", ""),
    }


def _merge_papers(papers, union):
    """Fold papers that share any handle into one, keeping every field."""

    for paper in papers:
        handles = [h for h in (_handle("s2", paper.get("paper_id")),
                               _handle("doi", paper.get("doi")),
                               _handle("corpus", paper.get("corpus_id"))) if h]
        for handle in handles[1:]:
            union.union(handles[0], handle)

    merged = {}
    for paper in papers:
        anchor = ""
        for kind, value in (("s2", paper.get("paper_id")), ("doi", paper.get("doi")),
                            ("corpus", paper.get("corpus_id"))):
            handle = _handle(kind, value)
            if handle:
                anchor = handle
                break
        if not anchor:
            continue
        entry = merged.setdefault(union.find(anchor),
                                  {"identifiers": {}, "fields": {}})
        # EVERY handle is kept, not just one per kind.  Semantic Scholar holds
        # duplicate records for the same paper -- two different paper ids, two
        # different corpus ids, one shared DOI.  Keeping only the first id of
        # each kind made the losers unresolvable, and the hits that had reached
        # the paper through a losing id could no longer be linked to it.
        for kind, value in (("s2", paper.get("paper_id")), ("doi", paper.get("doi")),
                            ("corpus", paper.get("corpus_id"))):
            if value in (None, ""):
                continue
            bucket = entry["identifiers"].setdefault(kind, [])
            text = str(value)
            if text not in bucket:
                bucket.append(text)
        for key in ("title", "venue", "abstract"):
            if paper.get(key) not in (None, "") and not entry["fields"].get(key):
                entry["fields"][key] = paper.get(key)
        if paper.get("year") not in (None, "") and not entry["fields"].get("year"):
            entry["fields"]["year"] = paper.get("year")
        if paper.get("authors") and not entry["fields"].get("authors"):
            entry["fields"]["authors"] = list(paper.get("authors") or [])

    papers = []
    index = {}
    merges = []
    for _, entry in sorted(merged.items()):
        paper = _paper_from(entry["identifiers"], entry["fields"])
        papers.append(paper)
        primary = paper.get("paper_id") or _handle("corpus", paper.get("corpus_id"))
        for kind, values in entry["identifiers"].items():
            for value in values:
                index[_handle(kind, value)] = primary
        absorbed = {kind: values for kind, values in entry["identifiers"].items()
                    if len(values) > 1}
        if absorbed:
            merges.append({"paper_id": paper.get("paper_id"),
                           "corpus_id": paper.get("corpus_id"),
                           "absorbed": absorbed})
    return papers, index, merges


def enrich_papers(gateway, papers):
    """Fill author/year/venue/abstract/DOI for the deduplicated set.

    One batch call per 500 papers, so cost follows the corpus size rather than
    the number of queries.
    """

    by_handle = {}
    request_ids = []
    for paper in papers:
        if paper.get("paper_id"):
            by_handle[_handle("s2", paper["paper_id"])] = paper
            request_ids.append(str(paper["paper_id"]))
        elif paper.get("corpus_id"):
            request_ids.append("CorpusId:%s" % paper["corpus_id"])
        if paper.get("corpus_id"):
            by_handle.setdefault(_handle("corpus", paper["corpus_id"]), paper)

    for start in range(0, len(request_ids), 500):
        chunk = request_ids[start:start + 500]
        try:
            records, _ = gateway.batch_papers(chunk)
        except Exception:  # noqa: BLE001 - enrichment is best effort
            continue
        for record in records:
            paper_id = str(getattr(record, "paper_id", "") or "")
            corpus_id = getattr(record, "corpus_id", None)
            target = by_handle.get(_handle("s2", paper_id)) if paper_id else None
            if target is None and corpus_id not in (None, ""):
                target = by_handle.get(_handle("corpus", corpus_id))
            if target is None:
                continue
            if paper_id and not target.get("paper_id"):
                target["paper_id"] = paper_id
            if corpus_id not in (None, "") and not target.get("corpus_id"):
                target["corpus_id"] = str(corpus_id)
            doi = str(getattr(record, "doi", "") or "")
            if doi and not target.get("doi"):
                target["doi"] = doi
            for key, value in (("title", getattr(record, "title", "")),
                               ("venue", getattr(record, "venue", "")),
                               ("abstract", getattr(record, "abstract", "")),
                               ("year", getattr(record, "year", None))):
                if value not in (None, "") and not target.get(key):
                    target[key] = value
            authors = list(getattr(record, "authors", []) or [])
            if authors and not target.get("authors"):
                target["authors"] = authors
    return papers


# --------------------------------------------------------------- assemble --


def build_candidate_corpus(plan, *, gateway, per_query_limit=DEFAULT_PAGE_SIZE,
                           enrich=True, snippet_store=None, planner_state=None,
                           allow_degraded=False,
                           snippet_discovery_target=SNIPPET_DISCOVERY_TARGET):
    """Execute every query, deduplicate papers, and keep every hit.

    ``planner_state`` is the planner's own verdict on the plan.  When the plan
    carries an empty retrieval channel the corpus refuses it unless the caller
    asks for ``allow_degraded=True``, and the refusal or the acceptance is
    recorded in the corpus either way.
    """

    channels = channel_state(plan)
    supplied = dict(planner_state or {})
    supplied_degradation = dict(supplied.get("degradation") or {})
    degraded = bool(channels["missing_channels"]) or bool(supplied_degradation.get("degraded"))
    input_state = {
        "planner_status": str(supplied.get("status") or ""),
        "plan_state": str(supplied_degradation.get("plan_state")
                          or ("degraded" if channels["missing_channels"] else "complete")),
        "degraded": degraded,
        "missing_channels": list(channels["missing_channels"]),
        "channels": channels,
        "reasons": list(supplied_degradation.get("reasons") or []),
        "allowed_degraded": bool(allow_degraded),
    }
    if degraded and not allow_degraded:
        raise DegradedPlanError(input_state)

    specs = plan_queries(plan)
    union = _Union()
    hits = []
    audits = []

    for spec in specs:
        found, audit = harvest_query(gateway, spec, per_query_limit=per_query_limit,
                                     discovery_target=snippet_discovery_target)
        hits.extend(found)
        audits.append(audit)

    provisional = [_paper_from(hit.identifiers, hit.fields)
                   for hit in hits if hit.identifiers]
    papers, lookup, merges = _merge_papers(provisional, union)

    if enrich:
        papers = enrich_papers(gateway, papers)
        # Enrichment is what teaches a snippet-only paper its S2 id, so the merge
        # has to run AGAIN afterwards.  Without the second pass one paper stays
        # two objects -- one holding the S2 id, one holding only a corpus id.
        papers, lookup, merges = _merge_papers(papers, union)

    retrieval_hits = []
    for index, hit in enumerate(hits, start=1):
        handle = ""
        for kind in ("s2", "doi", "corpus"):
            value = hit.identifiers.get(kind)
            if value:
                handle = _handle(kind, value)
                break
        row = dict(hit.row)
        retrieval_hits.append({
            "hit_id": "H%06d" % index,
            "paper_id": lookup.get(handle, ""),
            "corpus_id": hit.identifiers.get("corpus", ""),
            "facet_id": row["facet_id"],
            "query_id": row["query_id"],
            "query_type": row["query_type"],
            "query_text": row["query_text"],
            "retrieval_source": row["retrieval_source"],
            "result_rank": row["result_rank"],
            "diversity_rank": row.get("diversity_rank"),
            "request_index": row.get("request_index", 1),
            "score": row["score"],
            "snippet_locator": row["snippet_locator"],
            "text_hash": row["text_hash"],
        })

    # Raw snippet text goes to the side store, keyed by hit id.  The corpus JSON
    # keeps only the locator and the hash.
    if snippet_store is not None:
        for hit, row in zip(hits, retrieval_hits):
            if hit.raw_snippet_text:
                snippet_store.add(row["hit_id"], hit.raw_snippet_text)

    exclusions: list[str] = []
    for facet in (plan.get("facets") or []):
        for item in (facet or {}).get("must_exclude") or ():
            text = str(item or "").strip()
            if text and text not in exclusions:
                exclusions.append(text)
    for item in plan.get("additional_constraints") or ():
        text = str(item or "").strip()
        if text and text not in exclusions:
            exclusions.append(text)

    return {
        "schema_version": SCHEMA_VERSION,
        "research_object": plan.get("research_object", ""),
        # The plan's own exclusions, kept with the corpus so the scope judgement can
        # use the user's words instead of a maintained list.
        "exclusions": exclusions,
        "degraded": degraded,
        "input_state": input_state,
        "configuration": {
            "per_query_limit": per_query_limit,
            "max_results_per_query": MAX_RESULTS_PER_QUERY,
            "corpus_target": [CORPUS_TARGET_LOW, CORPUS_TARGET_HIGH],
            "corpus_target_is_a_design_target_only": True,
            "snippet_discovery_target": snippet_discovery_target,
            "evidence_budget_is_every_returned_hit": True,
        },
        "query_audit": audits,
        "papers": papers,
        "retrieval_hits": retrieval_hits,
        "statistics": _statistics(papers, retrieval_hits, merges),
    }


def _statistics(papers, hits, merges=()):
    """Derived roll-ups.  These never replace the raw hits."""

    per_paper = {}
    for hit in hits:
        key = str(hit.get("paper_id") or hit.get("corpus_id") or "")
        if not key:
            continue
        row = per_paper.setdefault(key, {"paper_id": hit.get("paper_id", ""),
                                         "corpus_id": hit.get("corpus_id", ""),
                                         "queries": set(), "facets": set(), "total": 0})
        row["total"] += 1
        row["queries"].add(str(hit.get("query_id")))
        row["facets"].add(str(hit.get("facet_id")))

    per_query = {}
    for hit in hits:
        key = str(hit.get("query_id"))
        row = per_query.setdefault(key, {"query_id": key, "hits": 0, "snippet_hits": 0,
                                         "best_rank": None, "best_score": None})
        row["hits"] += 1
        if hit.get("retrieval_source") == QUESTION_SOURCE:
            row["snippet_hits"] += 1
        rank = hit.get("result_rank")
        if isinstance(rank, int) and (row["best_rank"] is None or rank < row["best_rank"]):
            row["best_rank"] = rank
        score = hit.get("score")
        if isinstance(score, (int, float)) and (row["best_score"] is None
                                                or score > row["best_score"]):
            row["best_score"] = score

    # The two budgets are reported separately, per query and as a roll-up: the
    # evidence budget is every hit kept, the discovery budget is how many distinct
    # papers those hits actually reached.  Collapsing them into one number is what
    # hid a query whose 100 hits came from 12 papers.
    snippet_queries = [row for row in per_query.values() if row["snippet_hits"]]
    unique_per_query = []
    per_paper_per_query = []
    for row in sorted(snippet_queries, key=lambda item: item["query_id"]):
        rows = [h for h in hits if str(h.get("query_id")) == row["query_id"]]
        counts = {}
        for hit in rows:
            key = str(hit.get("paper_id") or hit.get("corpus_id") or "")
            if key:
                counts[key] = counts.get(key, 0) + 1
        unique_per_query.append({"query_id": row["query_id"],
                                 "raw_snippet_hits": len(rows),
                                 "unique_papers": len(counts),
                                 "max_snippets_per_paper": max(counts.values()) if counts else 0,
                                 "snippets_per_paper_mean": (round(len(rows) / len(counts), 3)
                                                             if counts else 0)})
        per_paper_per_query.extend(sorted(counts.values(), reverse=True))
    return {
        "raw_hits": len(hits),
        "raw_snippet_hits": sum(1 for h in hits
                                if h.get("retrieval_source") == QUESTION_SOURCE),
        "distinct_papers": len(papers),
        "snippet_hits": sum(1 for h in hits if h.get("retrieval_source") == QUESTION_SOURCE),
        "paper_search_hits": sum(1 for h in hits if h.get("retrieval_source") == KEYWORD_SOURCE),
        "unique_papers_per_query": unique_per_query,
        "snippets_per_paper_per_query": {
            "max": max(per_paper_per_query) if per_paper_per_query else 0,
            "mean": (round(sum(per_paper_per_query) / len(per_paper_per_query), 3)
                     if per_paper_per_query else 0),
            "distribution": per_paper_per_query[:50],
        },
        "hits_with_unresolved_paper": sum(1 for h in hits if not h.get("paper_id")),
        # Papers that absorbed more than one handle of a kind.  Semantic Scholar
        # serves duplicate records for the same paper, so this is expected; it is
        # recorded so the identity unification stays auditable.
        "identity_merges": list(merges),
        "per_paper": [
            {"paper_id": row["paper_id"], "corpus_id": row["corpus_id"],
             "hit_summary": {"total_hits": row["total"],
                             "distinct_queries": len(row["queries"]),
                             "distinct_facets": len(row["facets"])}}
            for _, row in sorted(per_paper.items())],
        "per_query": [per_query[key] for key in sorted(per_query)],
    }


# ------------------------------------------------------------ side storage --


class SnippetTextStore:
    """Raw snippet text, kept out of the corpus JSON and keyed by hit id."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS snippet_text ("
            "hit_id TEXT PRIMARY KEY, raw_snippet_text TEXT NOT NULL)")
        self.connection.commit()

    def add(self, hit_id, text):
        self.connection.execute(
            "INSERT OR REPLACE INTO snippet_text (hit_id, raw_snippet_text) VALUES (?, ?)",
            (str(hit_id), str(text)))

    def get(self, hit_id):
        row = self.connection.execute(
            "SELECT raw_snippet_text FROM snippet_text WHERE hit_id = ?",
            (str(hit_id),)).fetchone()
        return str(row[0]) if row else ""

    def count(self):
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM snippet_text").fetchone()[0])

    def close(self):
        self.connection.commit()
        self.connection.close()


def write_corpus(path, corpus):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
