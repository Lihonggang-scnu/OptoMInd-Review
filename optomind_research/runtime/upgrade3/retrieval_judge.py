"""The retrieval judge (ticket 05).  A tuning tool, not a production component.

Formal runs have no programmer watching, so the only thing that decides quality
there is what this judge taught the prompt to look for.  Two consequences:

* the judge must read what actually carries the answer -- the ABSTRACT on the
  keyword channel and the returned SENTENCE on the semantic channel -- because a
  title-only word count misfires both ways;
* every verdict must point at the paper or the sentence that decided it, because
  a number nobody can trace back is not evidence.

The model is injected.  The real one is a cheap chat call; the tests use stubs, so
the judging logic is pinned without spending anything.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

JUDGE_SCHEMA = "optomind.qp01.retrieval_judge.v1"

#: Verdicts the judge may return.  Anything else is treated as "no answer".
VERDICTS = ("relevant", "not_relevant", "partly_relevant", "unjudgeable")

#: A paper with no abstract cannot be judged, and must NOT be counted as relevant.
UNJUDGEABLE = "unjudgeable"

#: an ask covering this much of the question is the question, not a clause
ASK_COVERS_QUESTION = 0.8

#: two queries sharing this much vocabulary retrieve the same papers
PADDING_OVERLAP = 0.4

_DIRECTORY_MARKERS = (
    "review", "survey", "overview", "challenges", "prospects", "future directions",
    "recent advances", "trends", "benchmark", "state of the art",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _tokens(value: Any) -> list[str]:
    return [word for word in re.split(r"[^0-9a-z]+", _text(value).casefold()) if len(word) > 2]


# --------------------------------------------------------------------------- #
# the keyword channel: read the abstract
# --------------------------------------------------------------------------- #

def _verdict_of(judge: Callable[..., Any], subject: Mapping[str, Any],
                object_phrase: str) -> dict[str, Any]:
    """Ask the judge about one item, and never let a failure become a verdict."""

    try:
        raw = judge(subject, object_phrase)
    except Exception as exc:
        return {"verdict": UNJUDGEABLE, "evidence": "",
                "why": "judge_failed:%s" % type(exc).__name__ + ":" + str(exc)[:120]}
    if not isinstance(raw, Mapping):
        return {"verdict": UNJUDGEABLE, "evidence": "", "why": "judge_returned_no_verdict"}
    verdict = _text(raw.get("verdict")).casefold().replace(" ", "_")
    if verdict not in VERDICTS:
        return {"verdict": UNJUDGEABLE, "evidence": _text(raw.get("evidence")),
                "why": "judge_verdict_not_in_vocabulary:%s" % verdict}
    return {"verdict": verdict, "evidence": _text(raw.get("evidence")),
            "why": _text(raw.get("why"))}


def _rate_block(query: str, verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """One query's outcome, with what could not be judged kept apart.

    Three numbers, never merged: how many hits there were, how many of those the
    judge could actually read, and how many of the readable ones were relevant.
    Folding "the provider returned no abstract" into "not relevant" would blame a
    query for its provider, and folding it out of the count entirely would let a
    provider that returns titles only look as good as one that returns text.
    """

    relevant = sum(1 for row in verdicts if row["verdict"] == "relevant")
    judgeable = sum(1 for row in verdicts if row["verdict"] != UNJUDGEABLE)
    return {
        "query": query, "total": len(verdicts), "relevant": relevant,
        "judgeable": judgeable,
        "relevant_of_judgeable": (round(relevant / judgeable, 4) if judgeable else None),
        "partly_relevant": sum(1 for row in verdicts
                               if row["verdict"] == "partly_relevant"),
        "unjudgeable": sum(1 for row in verdicts if row["verdict"] == UNJUDGEABLE),
        "verdicts": verdicts,
    }


def judge_keyword_results(
    query: str,
    papers: Sequence[Mapping[str, Any]],
    *,
    object_phrase: str,
    judge: Callable[[Mapping[str, Any], str], Any],
) -> dict[str, Any]:
    """One keyword query's hits, judged on the abstract of each."""

    verdicts: list[dict[str, Any]] = []
    for paper in papers or ():
        abstract = _text(paper.get("abstract"))
        if not abstract:
            # An empty abstract is not evidence of relevance.  It is counted
            # against the query so a provider that returns titles only cannot
            # look better than one that returns abstracts.
            verdicts.append({
                "paper_id": _text(paper.get("paper_id")), "title": _text(paper.get("title")),
                "verdict": UNJUDGEABLE, "evidence": "",
                "why": "the provider returned no abstract for this paper",
            })
            continue
        row = _verdict_of(judge, paper, object_phrase)
        verdicts.append({
            "paper_id": _text(paper.get("paper_id")), "title": _text(paper.get("title")),
            **row,
        })
    return _rate_block(_text(query), verdicts)


# --------------------------------------------------------------------------- #
# the semantic channel: read the returned sentence
# --------------------------------------------------------------------------- #

def judge_snippet_results(
    query: str,
    snippets: Sequence[Mapping[str, Any]],
    *,
    object_phrase: str,
    judge: Callable[[Mapping[str, Any], str], Any],
) -> dict[str, Any]:
    """One semantic query's hits, judged on the sentence each returned."""

    verdicts: list[dict[str, Any]] = []
    for snippet in snippets or ():
        text = _text(snippet.get("text"))
        if not text:
            verdicts.append({
                "paper_id": _text(snippet.get("paper_id")),
                "title": _text(snippet.get("title")),
                "verdict": UNJUDGEABLE, "evidence": "",
                "why": "the provider returned no sentence for this hit",
            })
            continue
        row = _verdict_of(judge, snippet, object_phrase)
        verdicts.append({
            "paper_id": _text(snippet.get("paper_id")),
            "title": _text(snippet.get("title")), **row,
        })
    return _rate_block(_text(query), verdicts)


# --------------------------------------------------------------------------- #
# the format layer: read the plan
# --------------------------------------------------------------------------- #

_STOP_WORDS = frozenset({
    "what", "which", "how", "why", "when", "where", "who", "is", "are", "was",
    "were", "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with",
    "does", "do", "did", "be", "been", "that", "this", "these", "those", "it",
    "its", "their", "there", "as", "at", "by", "from", "into", "than", "then",
    "current", "state", "research", "study", "review", "about",
})


def _content_words(value: Any) -> list[str]:
    return [word for word in _tokens(value) if word not in _STOP_WORDS]


def _finding(code: str, *, facet_id: str = "", detail: str = "",
             evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "facet_id": facet_id, "detail": detail,
            "evidence": dict(evidence or {})}


def judge_plan_format(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Is the plan clean, padded, or self-contradictory?  Pure code, traceable."""

    findings: list[dict[str, Any]] = []
    question = _text((plan or {}).get("question_en"))
    facets = list((plan or {}).get("facets") or ())
    for facet in facets:
        facet_id = _text(facet.get("id"))
        ask = _text(facet.get("ask"))
        keywords = [_text(item) for item in facet.get("keyword_queries") or () if _text(item)]
        questions = [_text(item) for item in facet.get("question_queries") or ()
                     if _text(item)]

        if not questions:
            findings.append(_finding("facet_without_question", facet_id=facet_id,
                                     detail="the semantic channel is left shut for this facet"))
        # Content words only: read on real output, the ask was "current state of
        # the perovskite solar cell stability research" against "What is the
        # current state of ... research?", and an exact token-set comparison
        # missed it over the words what and is.
        # An ask that covers most of the question is the question.  A subset test
        # was too weak: read on real output, the facet asks "training methods" and
        # "imaging and recognition applications" are plainly clauses, yet every one
        # of their words appears in the question too, so all four were flagged.
        ask_words = set(_content_words(ask))
        question_words = set(_content_words(question))
        if ask_words and question_words:
            covered = len(ask_words & question_words) / len(question_words)
            if covered >= ASK_COVERS_QUESTION:
                findings.append(_finding(
                    "ask_restates_the_question", facet_id=facet_id,
                    detail=("%.0f%% of the question's own words are in the ask, so the "
                            "ask names no clause of its own" % (100 * covered)),
                    evidence={"ask": ask, "coverage": round(covered, 4)}))

        # Padding by alias swap: the same phrase with the object swapped for
        # another of its names.  Measured on the real run: four facets each
        # received the identical query three times over, once per alias.
        signatures: dict[str, list[str]] = {}
        for keyword in keywords:
            words = _tokens(keyword)
            signature = " ".join(sorted(words))
            signatures.setdefault(signature, []).append(keyword)
        for signature, group in signatures.items():
            if len(group) > 1:
                findings.append(_finding(
                    "alias_swap_padding", facet_id=facet_id,
                    detail=("%d queries are the same words in a different order"
                            % len(group)),
                    evidence={"queries": group}))
        # The same sentence with the object swapped for another of its names.  An
        # exact-skeleton test missed it: read on real output, T05 wrote "layer
        # architecture design", "diffractive layer structure" and "architecture
        # variations" for one facet.  Overlap catches what a fixed skeleton cannot,
        # and the number is recorded so a reader can check the call.
        for first in range(len(keywords)):
            for second in range(first + 1, len(keywords)):
                left, right = set(_tokens(keywords[first])), set(_tokens(keywords[second]))
                if not left or not right:
                    continue
                overlap = len(left & right) / len(left | right)
                if overlap >= PADDING_OVERLAP:
                    findings.append(_finding(
                        "alias_swap_padding", facet_id=facet_id,
                        detail=("%.0f%% of the words are shared, so the second query adds "
                                "no new search" % (100 * overlap)),
                        evidence={"queries": [keywords[first], keywords[second]],
                                  "overlap": round(overlap, 4)}))

        for keyword in keywords:
            lowered = " " + keyword.casefold() + " "
            directory = [word for word in _DIRECTORY_MARKERS
                         if (" " + word + " ") in lowered]
            if directory and len(_tokens(keyword)) <= 6:
                findings.append(_finding(
                    "query_is_object_plus_directory_word", facet_id=facet_id,
                    detail="the query is the object plus a review word and nothing else",
                    evidence={"query": keyword, "directory_words": directory}))
    # a plan with no facets at all
    if not facets:
        findings.append(_finding("plan_without_facets", detail="no facet to retrieve with"))
    return findings


# --------------------------------------------------------------------------- #
# reporting: three numbers, never merged
# --------------------------------------------------------------------------- #

def summarise(
    *,
    format_rows: Sequence[Mapping[str, Any]],
    retrieval_rows: Sequence[Mapping[str, Any]],
    quality_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The three rates, each on its own, with no combined figure."""

    def rate(rows, predicate):
        rows = list(rows or ())
        if not rows:
            return None
        return round(sum(1 for row in rows if predicate(row)) / len(rows), 4)

    return {
        "schema_version": JUDGE_SCHEMA,
        "format_first_pass_rate": rate(format_rows, lambda row: bool(row.get("ok"))),
        "retrieval_pass_rate": rate(retrieval_rows, lambda row: bool(row.get("ok"))),
        "quality_pass_rate": rate(
            quality_rows,
            lambda row: bool(row.get("total")) and (row.get("relevant") or 0) >= 9),
        "counts": {
            "format": len(list(format_rows or ())),
            "retrieval": len(list(retrieval_rows or ())),
            "quality": len(list(quality_rows or ())),
        },
        "note": ("the three rates are reported separately on purpose; a single "
                 "combined figure would let a revision, or a lenient layer, hide the "
                 "quality of the plan itself"),
    }


BATCH_JUDGE_SYSTEM_PROMPT = """You judge whether each retrieved item answers one \
research query.  You are given the query, the object the review is about, and a \
numbered list of items; each item is an abstract, or a sentence taken from a paper \
body, together with its title.

Answer with one JSON object:

{"verdicts": [{"index": 1, "verdict": "relevant" | "not_relevant" | "partly_relevant",
               "evidence": "the exact words from that item that decided it",
               "why": "one short sentence"}]}

Give one entry per item, in order.  Rules:
- Judge the TEXT of the item, not the title and not what the title suggests.  A \
title can name the object while the text is about a different field.
- "relevant" means the text addresses the query's subject.  Sharing vocabulary is \
not enough: a paper about optical communication networks is NOT relevant to a \
query about diffractive neural networks, and a paper that computes with light IS \
relevant even if it never uses the words diffractive or neural.
- "partly_relevant" is for a text that addresses a neighbouring aspect.
- evidence must be copied verbatim from the item.  If you cannot copy a span, \
answer not_relevant.
"""


def parse_batch_verdicts(payload: Any, count: int) -> list[dict[str, Any]]:
    """The batch reply as one verdict per item, never fewer."""

    rows = payload.get("verdicts") if isinstance(payload, Mapping) else None
    by_index: dict[int, dict[str, Any]] = {}
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        verdict = _text(row.get("verdict")).casefold().replace(" ", "_")
        by_index[index] = {
            "verdict": verdict if verdict in VERDICTS else UNJUDGEABLE,
            "evidence": _text(row.get("evidence")), "why": _text(row.get("why")),
        }
    return normalise_verdicts(
        [by_index.get(index) for index in range(1, int(count) + 1)], count)


def normalise_verdicts(raw_rows: Any, count: int) -> list[dict[str, Any]]:
    """Whatever the judge returned, as one acceptable verdict per item.

    This runs on EVERY path, including an injected judge, so the rule that a
    verdict must cite a span cannot be bypassed by the caller.  It was bypassed
    once: the injected judge in a test returned not_relevant with no evidence and
    the caller took it at face value.
    """

    incoming = list(raw_rows or ())
    rows: list[dict[str, Any]] = []
    for index in range(1, int(count) + 1):
        row = incoming[index - 1] if index - 1 < len(incoming) else None
        if not isinstance(row, Mapping):
            rows.append({"verdict": UNJUDGEABLE, "evidence": "",
                         "why": "the judge did not answer for this item"})
            continue
        verdict = _text(row.get("verdict")).casefold().replace(" ", "_")
        row = {"verdict": verdict if verdict in VERDICTS else UNJUDGEABLE,
               "evidence": _text(row.get("evidence")), "why": _text(row.get("why"))}
        if row["verdict"] in ("relevant", "partly_relevant", "not_relevant") \
                and not row["evidence"]:
            # The ticket's hard requirement: a verdict must point at the words
            # that decided it.  A verdict with no span is an answer the reader
            # cannot check, so it is not accepted as a verdict at all.
            rows.append({"verdict": UNJUDGEABLE, "evidence": "",
                         "why": "the judge gave no span, so the verdict cannot be traced"})
            continue
        rows.append(row)
    return rows


def build_batch_judge(*, model_tier: str = "", call_fn: Callable[..., Any] | None = None):
    """One call per QUERY instead of one per item."""

    def _judge_batch(query: str, items: Sequence[Mapping[str, Any]],
                     object_phrase: str) -> list[dict[str, Any]]:
        payload = {
            "query": _text(query), "object": _text(object_phrase),
            "items": [{"index": index, "title": _text(item.get("title")),
                       "text": (_text(item.get("abstract")) or _text(item.get("text")))[:2500]}
                      for index, item in enumerate(items, start=1)],
        }
        if call_fn is not None:
            return parse_batch_verdicts(call_fn(payload), len(items))
        from llm.qwen_chat_client import call_qwen_chat

        response = call_qwen_chat(
            "QueryPlanJudge",
            [{"role": "system", "content": BATCH_JUDGE_SYSTEM_PROMPT},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            model_tier=model_tier or "c_model",
            max_retries=1,
            temperature=0.0,
            force_mock=False,
            max_tokens=2200,
            response_format={"type": "json_object"},
        )
        body = _text(response.get("content"))
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return parse_batch_verdicts({}, len(items))
        try:
            return parse_batch_verdicts(json.loads(body[start:end + 1]), len(items))
        except ValueError:
            return parse_batch_verdicts({}, len(items))

    return _judge_batch


def judge_keyword_batch(query, papers, *, object_phrase, batch_judge):
    """The keyword channel, judged in one call for the whole result page."""

    items = [{"title": _text(p.get("title")), "abstract": _text(p.get("abstract"))}
             for p in papers or ()]
    verdicts = normalise_verdicts(batch_judge(query, items, object_phrase), len(items))
    rows = []
    for paper, verdict in zip(papers or (), verdicts):
        row = dict(verdict)
        if not _text(paper.get("abstract")):
            row = {"verdict": UNJUDGEABLE, "evidence": "",
                   "why": "the provider returned no abstract for this paper"}
        rows.append({"paper_id": _text(paper.get("paper_id")),
                     "title": _text(paper.get("title")), **row})
    return _rate_block(_text(query), rows)


def _carries_a_sentence(snippet: Mapping[str, Any]) -> bool:
    """A returned sentence, not just the paper's own title repeated back."""

    text = _text(snippet.get("text"))
    if not text:
        return False
    title = _text(snippet.get("title"))
    return bool(title) is False or text.casefold().strip() != title.casefold().strip()


def judge_snippet_batch(query, snippets, *, object_phrase, batch_judge):
    items = [{"title": _text(s.get("title")), "text": _text(s.get("text"))}
             for s in snippets or ()]
    verdicts = normalise_verdicts(batch_judge(query, items, object_phrase), len(items))
    rows = []
    for snippet, verdict in zip(snippets or (), verdicts):
        row = dict(verdict)
        if not _carries_a_sentence(snippet):
            # Read on real output: several semantic hits came back as the paper's
            # title with an empty snippet, the judge answered not_relevant with no
            # evidence, and plainly on-topic papers were scored as failures.
            row = {"verdict": UNJUDGEABLE, "evidence": "",
                   "why": "the provider returned no sentence for this hit"}
        rows.append({"paper_id": _text(snippet.get("paper_id")),
                     "title": _text(snippet.get("title")), **row})
    return _rate_block(_text(query), rows)


# --------------------------------------------------------------------------- #
# the real judge
# --------------------------------------------------------------------------- #

JUDGE_SYSTEM_PROMPT = """You judge whether one retrieved item answers one research query. You are given the query, the object the review is about, and the item's own text (an abstract, or a sentence taken from a paper body).

Answer with one JSON object:

{"verdict": "relevant" | "not_relevant" | "partly_relevant",
 "evidence": "the exact words from the item that decided your verdict",
 "why": "one short sentence"}

Rules:
- Judge the TEXT you are given, not the title and not what the title suggests. A title can name the object while the text is about a different field.
- "relevant" means the text addresses the query's subject. Sharing vocabulary is not enough: a paper about optical communication networks is NOT relevant to a query about diffractive neural networks, and a paper that computes with light IS relevant even if it never uses the words diffractive or neural.
- "partly_relevant" is for a text that addresses a neighbouring aspect of the same object.
- evidence must be copied verbatim from the text you were given. If the text is \
only the paper's title, or you cannot copy a span from it, answer "unjudgeable" \
-- an item with no readable text is not evidence either way.
"""


def build_real_judge(*, model_tier: str = "", call_fn: Callable[..., Any] | None = None):
    """The judge that spends money: one cheap call per item."""

    def _judge(subject: Mapping[str, Any], object_phrase: str) -> dict[str, Any]:
        text = _text(subject.get("abstract")) or _text(subject.get("text"))
        query = _text(subject.get("__query__")) or ""
        payload = {
            "query": query,
            "object": object_phrase,
            "title": _text(subject.get("title")),
            "text": text[:4000],
        }
        if call_fn is not None:
            return call_fn(payload)
        from llm.qwen_chat_client import call_qwen_chat

        response = call_qwen_chat(
            "QueryPlanJudge",
            [{"role": "system", "content": JUDGE_SYSTEM_PROMPT},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            model_tier=model_tier or "c_model",
            max_retries=1,
            temperature=0.0,
            force_mock=False,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        body = _text(response.get("content"))
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return {"verdict": UNJUDGEABLE, "evidence": "", "why": "no JSON in the reply"}
        return json.loads(body[start:end + 1])

    return _judge