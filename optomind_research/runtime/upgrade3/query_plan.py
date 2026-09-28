"""The query plan: the one place where the user's words become "what to search".

Ticket 01.  The old planner returned three paragraphs a human could read and one
flat keyword list a machine could use -- and the keywords were not what retrieval
actually used: the queries were assembled downstream from a section title plus a
hard-coded role suffix.  Measured on the real provider, that produced this:

    physical diffractive computation            -> 219,837 results,  3/10 relevant
    diffractive optical neural network phase modulation linear transform
                                                ->     378 results,  9/10 relevant
    the same intent written as a question       ->       0 results
    the same question on the snippet endpoint   ->  body sentences, quotable

Three things follow, and this module is built around them.

1. What the plan says is what retrieval runs.  Nothing downstream is allowed to
   assemble a query from a title and a suffix again.
2. The plan separates four kinds of thing that must not be mixed: what the plan
   claims the question is (for a human to check), the query text (written by the
   model), the filters (SELECTED from a fixed enumeration, never invented), and
   the criteria for "does this count as a hit".
3. A format error is a repair, never an outage.  The decoding mode we can use
   guarantees valid JSON, not the right shape, so the shape is enforced locally.

The skeleton is held in code and the model fills values.  The enumeration lives
in config/query_plan_fields.json, is rendered into the prompt and read by the
validator, so there is exactly one copy of it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

QUERY_PLAN_SCHEMA = "research_harness.query_plan.v2"
FIELD_DICTIONARY_PATH = Path(__file__).resolve().parents[3] / "config" / "query_plan_fields.json"

#: Suffixes that were measured to pull in whole neighbouring fields.  A query
#: must earn its words, so a topic string plus one of these is refused.
BANNED_QUERY_SUFFIXES = (
    "causal mechanism", "recent advances", "fabrication measurement",
    "principles history", "conflicting evidence", "deployment application",
    "state of the art", "latest advances", "recent progress", "review of reviews",
)

#: Head terms that measured badly, each carrying the measurement that put it
#: here.  The list is deliberately SHORT and evidence-backed: on the very same
#: run "computational imaging" looked just as generic and measured 10/10, so a
#: term is only banned by having been tried.  A guessed banlist would have
#: thrown away the best query of the run.
#: What the mechanical query ACTUALLY returned, recorded so the model starts from
#: the observed contamination instead of from its own idea of it.  Query A was
#: "physical diffractive computation network neural causal mechanism"; these are
#: the hits it produced that have nothing to do with the object.
MEASURED_GARBAGE: dict[str, dict[str, Any]] = {
    "neural network pruning": {
        "source_query": "physical diffractive computation network neural causal mechanism",
        "measured_top10": 0,
        "note": "returned 'Causal Mechanism Reduction: Mechanism Replacement for "
                "Neural Network Pruning' -- the role suffix 'causal mechanism' "
                "retrieved the causal-inference literature",
    },
    "fault diagnosis": {
        "source_query": "physical diffractive computation network neural causal mechanism",
        "measured_top10": 0,
        "note": "returned 'A Generalized Method for Diagnosing Ground Faults in "
                "Distribution Networks'",
    },
    "ultrasonic": {
        "source_query": "physical diffractive computation network neural causal mechanism",
        "measured_top10": 0,
        "note": "returned 'All-wave computational ultrasonic fingerprint identification "
                "with metasurface' -- shares the word metasurface and nothing else",
    },
    "soft sensor": {
        "source_query": "physical diffractive computation network neural causal mechanism",
        "measured_top10": 0,
        "note": "returned 'A Soft Sensor Modeling Method ... Dynamic Causal Graph "
                "Neural Network'",
    },
}

#: Review-directory words: the vocabulary a review's table of contents uses and a
#: paper's own title does not.  Each entry carries the measurement that earned it,
#: taken from the twelve-question tuning baseline of 2026-09-11.  The list is
#: checked against the counter-examples as well: "applications" appears in a 9/10
#: query and the singular "challenge" in another, so neither is banned.  A word is
#: only here because a query containing it was measured to fail.
REVIEW_DIRECTORY_WORDS: dict[str, dict[str, Any]] = {
    "review": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": "'perovskite solar cell stability review' returned 0/10 relevant",
    },
    "limitations and": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": ("'diffractive neural network limitations and challenges' returned "
                 "0/10 even though it carries the full object phrase, while the "
                 "singular 'limitation challenge' returned 9/10"),
    },
    "challenges": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": "the plural form appeared only in 0/10 queries",
    },
    "prospects": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": "'future prospects of all-optical neural networks' returned 0/10",
    },
    "benchmark": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": "the generic 'energy efficiency benchmark' query returned 0/10",
    },
    "versus": {
        "measured_top10": 1,
        "measured_on": "2026-09-11",
        "note": "'optical neural network versus electronic implementation' returned 1/10",
    },
    "compared to": {
        "measured_top10": 1,
        "measured_on": "2026-09-11",
        "note": "'photonic neural network compared to CMOS accelerator' returned 1/10",
    },
    "survey": {
        "measured_top10": 2,
        "measured_on": "2026-09-11",
        "note": "the ticket's own T10 brief bans 'applications survey' after 2/10",
    },
    "overview": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": "review-directory vocabulary with no measured counter-example",
    },
}

GENERIC_HEAD_TERMS: dict[str, dict[str, Any]] = {
    "future directions": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": ("measured 0/10: returned fiber-optic communication trends, "
                 "co-packaged optics for AI data centres, dental implant surgery "
                 "and a bibliometric study of laser wakefield acceleration"),
    },
    "pattern recognition": {
        "measured_top10": 5,
        "measured_on": "2026-09-11",
        "note": ("measured 5/10: returned optical packet switching and a "
                 "nonlinear optical loop mirror instead of diffractive networks"),
    },
    "trends": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": "appeared in every off-topic title of the 0/10 query",
    },
    "artificial intelligence": {
        "measured_top10": 0,
        "measured_on": "2026-09-11",
        "note": ("the abbreviation AI was present in every off-topic title of the "
                 "0/10 query and named no domain by itself"),
    },
}

_INTERROGATIVE = ("how", "why", "what", "which", "when", "where", "who",
                  "does", "do", "is", "are", "can", "should")

#: words that carry no identity, so they never form an object phrase
_PHRASE_STOP_WORDS = frozenset({
    "a", "an", "and", "of", "the", "for", "in", "on", "to", "with", "their",
    "its", "review", "study",
})

_FACET_KEYS = ("id", "ask", "keyword_queries", "question_queries", "filters",
               "must_exclude")
_FILTER_KEYS = ("publication_type", "fields_of_study", "text_availability")
_TOP_KEYS = ("schema_version", "question_en", "research_object", "ambiguity",
             "facets", "seeds", "criteria", "additional_constraints")

PROMPT_TEMPLATE = """You are Query Planner.

You turn one user research question into a retrieval plan: a small set of
facets, each stating what evidence is needed.

QUALITY OBJECTIVE
  Treat retrieval quality as the objective. A useful first page should contain
  a clear majority of papers or snippets about this facet, with complementary
  evidence across the facet's queries. Do not accept an irrelevant net merely
  because it contains a few possible papers.

Return exactly one JSON object in EXACTLY the shape below. Fill the values.
Do not rename, add, reorder or remove any key.

{
  "schema_version": "%(schema)s",
  "question_en": "",
  "research_object": "",
  "ambiguity": {
    "is_ambiguous": false,
    "default_reading": "",
    "needs_user_input": []
  },
  "facets": [
    {
      "id": "",
      "ask": "",
      "keyword_queries": [],
      "question_queries": [],
      "filters": {
        "publication_type": [],
        "fields_of_study": [],
        "text_availability": []
      },
      "must_exclude": []
    }
  ],
  "seeds": [
    { "title": "", "hint": "", "why": "" }
  ],
  "criteria": {
    "must_include_topic": [],
    "must_exclude_domain": [],
    "synonyms": {}
  },
  "additional_constraints": []
}

# FIELD RULES

question_en
  The user's question normalised into English. Preserve every request, object,
  comparison, condition, metric, date, venue, and explicit exclusion it makes.

research_object
  The scientific object this review is about, as a noun phrase, under 12 words.
  It is the thing the whole review studies, not the topic of one chapter.

CANONICAL OBJECT CONSTRUCTION
  First choose one short canonical object noun phrase from the user's question.
  Copy that exact phrase into every keyword query and every question query.
  A query may add one concrete aspect after it, but may not drop the object or
  replace it with a broad parent category. For a comparison of two objects,
  name both compared objects and their shared class in the facet ask, then
  cover both sides across focused queries, one side per phrase where that keeps
  each phrase focused.

ambiguity.is_ambiguous
  true only if the question can reasonably be read as asking for two different
  reviews. When false, leave default_reading empty and needs_user_input empty.

facets
  Work in two steps, in this order.

  STEP 1 -- what the user's question requires you to establish.
  Read the question as a set of RELATIONS it asks about, not as a sentence to
  copy. A relation is "X affects Y", "A and B differ in M", "P is limited by Q",
  "R holds only under condition C". Write out the relations the user is asking
  you to establish, including the comparison, the condition and the outcome when
  the question names them. Then decide which of those relations are different
  enough to need different evidence. A facet is one of those relations, phrased
  as the sub-question that has to be answered for the review to be able to answer
  the user. This is not adding a research goal the user did not ask for: it is
  unfolding the relations the user's own question already contains.

  STEP 2 -- the evidence each relation needs.
  For each facet, identify the smallest set of distinct evidence needs that would
  settle its sub-question, and write the queries for those needs.

  How many facets: decided by the question, between %(facets_min)d and
  %(facets_max)d. A question that already names several relations may be served
  by one facet per relation. A question that names one relation but several
  things that could explain it is served by facets that separate those
  explanations. If the question is broad, unfold it; if it is already specific,
  do not inflate it. Do not pad, and do not merge two relations merely because
  they share an object.

  What a facet is never: the whole question copied back, or a sentence that only
  renames the object. A facet states a sub-question whose answer is one step
  towards the user's question, and it must be answerable by evidence of its own.

facets[].id
  F1, F2, F3 ... in order.

facets[].ask
  The sub-question this facet has to settle, in English, written as the relation
  from STEP 1 that it serves. Every request, comparison, condition and outcome in
  the user's question must be claimed by exactly one facet. If you cannot write
  this, the facet must not exist.
  Reusing the user's own words for the object and for the relation is correct and
  expected; what is not allowed is an ask that merely repeats the whole question,
  because then the facet has no evidence need of its own.

facets[].keyword_queries
  ONE TO %(keyword_queries_per_facet_max)d search phrases for a keyword search engine.
  Length is not constrained: write the phrase as long as the object's own name
  plus its aspect needs, and no longer. A short object gets a short phrase; a
  long object name is not a reason to drop words from it or to split it.
  EVERY phrase must contain the exact canonical object phrase
  plus a named, concrete facet aspect noun. Keep each phrase
  focused on one aspect; do not stack several unrelated technical terms.
  Write phrases that a relevant paper's title or abstract could plausibly contain.
  Do not append generic review language or unrelated technical terms to a topic
  string; every word must help identify this facet.
  Do not use generic modifiers such as "comparison", "techniques", "variants",
  "current limitations", or "future research directions" as the only remaining
  aspect. Replace them with a concrete factor, mechanism, material, component,
  task, measurement, condition, or failure mode. This restriction applies to
  retrieval queries, not to the user's ask or to additional_constraints.

  Each additional phrase must add a distinct, useful information angle. Two
  phrases that only rename the same object or restate the same need are one
  phrase; write fewer when there is no second useful angle. Do not stack several
  unrelated technical terms into one phrase merely to make it specific.

  Example construction (illustrative only, never copy as a fixed template):
    canonical object: "sodium ion battery cathodes"
    keyword: "sodium ion battery cathodes cycling degradation"
    semantic: "How do cycling-induced structural changes affect capacity retention in sodium ion battery cathodes?"
  The exact object phrase remains present in every query; only the evidence need
  changes.

facets[].question_queries
  ONE TO %(question_queries_per_facet_max)d natural-language questions for a semantic snippet engine.
  Write complete, answerable questions or statements, each containing the
  exact canonical object phrase and one main information need (relationship, result, comparison, or
  condition). Use up to three only when they cover distinct needs and together
  cover the user's request. Do not repeat one need with different wording or
  presuppose an unknown result. This channel is intended to retrieve body
  sentences; a title-only result is not substantive evidence.

  Use two complementary forms when the facet needs two evidence angles: first a
  specific research-relation question, then a neutral statement describing the
  research content that must be established. They must address different needs,
  not repeat one another. Do not ask "which is most effective", "what has the
  highest accuracy", or broad "future developments" questions; ask about a
  mechanism, condition, measurement, trade-off, observed effect, or mitigation
  without assuming an unknown result.
  For broad limitation requests, ask about a concrete factor, its effect, and
  a possible mitigation. For methods, ask about the working mechanism or cost;
  for applications, ask about the task and reported performance or conditions.
  Do not copy review section headings such as "future directions" into a query.
  Replace broad headings with an object-specific factor, effect, and mitigation
  question; adapt the object and factor to the user's actual request.
  Example of the required transformation for this measured topic only:
    bad keyword: "optical diffractive neural networks future research directions"
    good keywords: "optical diffractive neural networks fabrication tolerance"
    and "optical diffractive neural networks reconfigurable architectures"
  This example teaches the construction; do not copy its topic into another
  question.

  Generic cross-topic example (illustrative only): for canonical object
  "sodium ion battery cathodes", a cycling-stability facet could use keyword
  "sodium ion battery cathodes cycling degradation" and the semantic pair
  "How do cycling-induced structural changes affect capacity retention in
  sodium ion battery cathodes?" / "Studies characterize electrolyte side reactions
  and interfacial degradation of sodium ion battery cathodes."

facets[].filters
  Optional narrowing. ANY FILTER MAY BE LEFT AS AN EMPTY LIST.
  AN EMPTY LIST MEANS "DO NOT FILTER". Never invent a value to avoid an empty
  list, and never choose a value you are unsure of.
  Leave every filter empty unless the user explicitly requests that exact
  metadata restriction. "Scholarly review" describes the task and does not
  mean publication_type=Review. Never infer a field of study, publication type,
  or text availability requirement.

%(enum_block)s

facets[].must_exclude
  Always return an empty array. This compatibility field is not a place for
  model-inferred exclusions. If the user explicitly excludes a field, venue,
  date, method, material, or condition, preserve that request as an English
  sentence in additional_constraints.

  Do not infer exclusions from remembered examples. Content quality is checked
  against the actual returned papers and snippets in the acceptance stage.

# QUERY QUALITY

  Aim for a first result page whose clear majority is about the facet. Queries
  should be complementary and usable for evidence review. Avoid bare topic
  strings, generic review-directory wording, invented compounds, and repeated
  paraphrases. Do not copy topic-specific exclusions from another question.

criteria.must_include_topic
  Compact topic anchors for identity and retrieval. They need not all appear
  literally in every relevant paper and must not be treated as a hard lexical
  test.

criteria.must_exclude_domain
  Always return an empty array. Do not infer hard domain exclusions. Explicit
  user exclusions belong in additional_constraints.

criteria.synonyms
  Map a canonical term only to genuine abbreviations, spelling variants, or
  field aliases. Do not claim a broader parent concept or merely related method
  as an exact synonym when uncertain.

  Cross-topic examples (illustrative only): for canonical object
  "sodium ion battery cathodes", a genuine abbreviation or spelling variant may
  be listed, but a broader "battery materials" category may not. For canonical
  object "fresnel lens arrays", a true field alias may be listed, but a generic
  "optical component" parent may not.

SELF-CHECK BEFORE RETURNING
  Confirm that every facet maps to one atomic user request, every query names
  the exact canonical object phrase, all parallel requests are
  covered exactly once, each facet has two complementary evidence needs when
  useful, filters are empty unless explicitly requested, both exclusion fields
  are empty, seeds are empty unless user-supplied, and no query is a copied
  review heading, generic modifier, or invented fact.

additional_constraints
  Anything the user asked for that the fields above cannot express: a named
  journal, a year range, a citation threshold, a required author group.
  Preserve every explicit requirement, comparison, or exclusion as a plain
  English sentence. Do not drop it, infer new constraints, or force it into a
  field with a different meaning.

seeds
  Return up to %(seeds_max)d papers only when the user supplied a title that can
  be carried forward for later verification. Otherwise return an empty list.
  Give only title and a short identifying hint; never invent a seed or a DOI.
"""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _sha_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def load_field_dictionary(path: str | Path | None = None) -> dict[str, Any]:
    """The one definition of the enumerations, the limits and the spellings."""

    target = Path(path) if path else FIELD_DICTIONARY_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or "enums" not in payload:
        raise ValueError("query field dictionary is unusable: %s" % target)
    return dict(payload)


def _render_enum_block(fields: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for name in _FILTER_KEYS:
        block = (fields.get("enums") or {}).get(name) or {}
        allowed = ", ".join(str(item) for item in block.get("allowed") or ())
        lines.append("    %s   one or more of: %s" % (name, allowed))
    lines.append("")
    for name in _FILTER_KEYS:
        block = (fields.get("enums") or {}).get(name) or {}
        note = _text(block.get("spelling_note"))
        if note:
            lines.append("  " + note)
    return chr(10).join(lines)


def render_prompt(
    fields: Mapping[str, Any] | None = None,
    *,
    user_question: str = "",
) -> dict[str, Any]:
    """The system prompt, rendered from the dictionary rather than retyped."""

    payload = dict(fields or load_field_dictionary())
    limits = payload.get("limits") or {}
    body = PROMPT_TEMPLATE % {
        "schema": QUERY_PLAN_SCHEMA,
        "facets_min": limits.get("facets_min", 1),
        "facets_max": limits.get("facets_max", 10),
        "keyword_queries_per_facet_max": limits.get("keyword_queries_per_facet_max", 3),
        "question_queries_per_facet_max": limits.get("question_queries_per_facet_max", 3),
        "seeds_max": limits.get("seeds_max", 3),
        "banned_examples": ", ".join(BANNED_QUERY_SUFFIXES[:3]),
        "enum_block": _render_enum_block(payload),
        "garbage_block": chr(10).join(
            "    - %s  (%s)" % (term, evidence.get("note", ""))
            for term, evidence in MEASURED_GARBAGE.items()),
        "directory_block": chr(10).join(
            "    - %s  (measured %s/10: %s)"
            % (word, evidence.get("measured_top10"), evidence.get("note", ""))
            for word, evidence in REVIEW_DIRECTORY_WORDS.items()),
    }
    payload_out = {
        "user_question": _text(user_question),
        "generation_checklist": [
            "Choose one exact canonical object noun phrase before writing queries.",
            "Keep that exact object phrase in every keyword and semantic query.",
            "Split independently comparable user requests into separate atomic facets.",
            "Use two complementary evidence needs per broad facet when possible; add a third only if required.",
            "Always leave must_exclude and must_exclude_domain empty; preserve explicit exclusions in additional_constraints. Populate metadata filters only when explicitly requested.",
            "Use an empty seeds list unless the user supplied a verifiable paper title.",
            "Preserve every explicit user requirement or exclusion in additional_constraints.",
        ],
    }
    return {
        "system_prompt": body,
        "user_payload": payload_out,
        "prompt_hash": _sha_text(body),
        "execution_prompt_hash": _sha_text(
            body + "\0" + json.dumps(payload_out, ensure_ascii=False, sort_keys=True)
        ),
        "field_dictionary_version": _text(payload.get("version")),
    }


# --------------------------------------------------------------------------- #
# local normalisation (the first rung of the ladder)
# --------------------------------------------------------------------------- #

def _fold(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).casefold())


def _enum_lookup(fields: Mapping[str, Any], name: str) -> dict[str, str]:
    block = (fields.get("enums") or {}).get(name) or {}
    lookup: dict[str, str] = {}
    for item in block.get("allowed") or ():
        key = _fold(item)
        if key:
            lookup.setdefault(key, str(item))
    return lookup


def _word_count(phrase: str) -> int:
    return len([word for word in _text(phrase).split() if word])


def _looks_like_a_sentence(phrase: str) -> bool:
    text = _text(phrase)
    if text.endswith("?"):
        return True
    first = (text.split() or [""])[0].casefold()
    return first in _INTERROGATIVE


def _banned_suffix(phrase: str) -> str:
    folded = " " + " ".join(_text(phrase).casefold().split()) + " "
    for suffix in BANNED_QUERY_SUFFIXES:
        if (" " + suffix + " ") in folded:
            return suffix
    return ""


def object_phrases(plan: Mapping[str, Any]) -> list[str]:
    """The adjacent word pairs that name the object in the object's own order.

    "diffractive optical neural network" yields diffractive optical, optical
    neural, neural network.  A query that carries one of these is written the way
    a paper's own title is written, which is the whole finding.
    """

    phrases: list[str] = []
    for words in _object_word_lists(plan):
        for index in range(len(words) - 1):
            phrase = "%s %s" % (words[index], words[index + 1])
            if phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _object_word_lists(plan: Mapping[str, Any]) -> list[list[str]]:
    sources: list[str] = [_text((plan or {}).get("research_object"))]
    criteria = (plan or {}).get("criteria") or {}
    sources.extend(_text(item) for item in criteria.get("must_include_topic") or ())
    sources.extend(_text(item) for item in (criteria.get("synonyms") or {}).keys())
    lists: list[list[str]] = []
    for source in sources:
        words = [word for word in re.split(r"[^0-9A-Za-z\u4e00-\u9fff-]+", source.casefold())
                 if len(word) >= 3 and word not in _PHRASE_STOP_WORDS]
        if words:
            lists.append(words)
    return lists


def object_phrases(plan: Mapping[str, Any]) -> list[str]:
    """The adjacent word pairs that name the object in the object's own order.

    "diffractive optical neural network" yields diffractive optical, optical
    neural, neural network.  A query that carries one of these is written the way
    a paper's own title is written, which is the whole finding.
    """

    phrases: list[str] = []
    for words in _object_word_lists(plan):
        for index in range(len(words) - 1):
            phrase = "%s %s" % (words[index], words[index + 1])
            if phrase not in phrases:
                phrases.append(phrase)
    return phrases


def object_content_words(plan: Mapping[str, Any]) -> list[str]:
    """The content words that name the review's object."""

    words: list[str] = []
    for row in _object_word_lists(plan):
        for word in row:
            if word not in words:
                words.append(word)
    return words


def _missing_object_word(phrase: str, wanted: Sequence[str]) -> bool:
    """True when a keyword query never names the object in the object's own words.

    The rule is: carry one of the object's adjacent word pairs, OR one of the
    field's own aliases.  Measured on the real provider, the queries that carried
    one scored 9-10/10 and the ones that carried neither scored 2-4/10.  The alias
    half is not decoration: the query that scored 10/10 and was refused by a
    phrase-only version of this rule used "diffractive deep learning", which is
    what the literature calls the object.
    """

    folded = " " + " ".join(_text(phrase).casefold().split()) + " "
    for alias in OBJECT_ALIASES:
        if (" " + alias + " ") in folded:
            return False
    phrases = [item for item in wanted if item]
    if not phrases:
        return False
    return not any((" " + item + " ") in folded for item in phrases)


def review_directory_word(phrase: str) -> str:
    """The review-directory word a keyword query used, if any."""

    folded = " " + " ".join(_text(phrase).casefold().split()) + " "
    for word in REVIEW_DIRECTORY_WORDS:
        if (" " + word + " ") in folded:
            return word
    return ""


def _generic_head_term(phrase: str) -> str:
    folded = " " + " ".join(_text(phrase).casefold().split()) + " "
    for term in GENERIC_HEAD_TERMS:
        if (" " + term + " ") in folded:
            return term
    if re.search(r"\bai\b", folded):
        return "artificial intelligence"
    return ""


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def fingerprint_of(source: str) -> str:
    """Content identity of a validator implementation."""

    return _sha_text(source)


def validator_fingerprint() -> str:
    """The hash of the validator that produced a verdict.

    Without it a stored verdict cannot be attributed: the same plan can be valid
    under one rule set and invalid under the next, and a tuning baseline that
    cannot say which rules produced it is not a baseline.
    """

    try:
        return fingerprint_of(Path(__file__).read_text(encoding="utf-8"))
    except OSError:  # pragma: no cover - defensive
        return ""


def _report(
    ok: bool,
    plan: Mapping[str, Any] | None,
    errors: list[dict[str, Any]],
    repairs: list[dict[str, Any]],
    *,
    raw_plan: Mapping[str, Any] | None = None,
    refused_queries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "plan": dict(plan or {}),
        # The plan as it was RECEIVED, kept beside the cleaned one.  Storing only
        # the cleaned plan made a past verdict unreproducible: the refused queries
        # had already been dropped, so re-validating always passed and the reason
        # the plan failed at the time was gone for good.
        "raw_plan": dict(raw_plan or {}),
        "errors": errors,
        "refused_queries": list(refused_queries or []),
        "repairs": repairs,
        "survived_format_errors": bool(repairs and ok),
        "checked_contract": QUERY_PLAN_SCHEMA,
        "validator_fingerprint": validator_fingerprint(),
    }


def _error(code: str, path: str, detail: str = "") -> dict[str, Any]:
    return {"code": code, "path": path, "detail": detail}


def validate_plan(
    raw: Any,
    *,
    fields: Mapping[str, Any] | None = None,
    declared_requests: int | None = None,
    field_repairer: Callable[[str, Any, Sequence[str]], Any] | None = None,
) -> dict[str, Any]:
    """Check the plan's shape AND its content, and repair rather than raise."""

    payload = dict(fields or load_field_dictionary())
    limits = payload.get("limits") or {}
    errors: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    refused_queries: list[dict[str, Any]] = []
    raw_plan: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}

    if not isinstance(raw, Mapping):
        return _report(False, None, [_error("plan_not_an_object", "$")], repairs,
                       raw_plan=raw_plan, refused_queries=refused_queries)

    # --- top level ------------------------------------------------------
    plan: dict[str, Any] = {}
    for key in _TOP_KEYS:
        if key not in raw:
            errors.append(_error("plan_key_missing", "$." + key))
    # A field nobody defined is the hallucination this validator exists for: the
    # downstream cannot parse it and nothing else will notice.
    for key in raw:
        if key not in _TOP_KEYS:
            errors.append(_error("plan_key_undefined", "$." + _text(key)))
    if _text(raw.get("schema_version")) not in ("", QUERY_PLAN_SCHEMA):
        errors.append(_error("plan_schema_version", "$.schema_version",
                             _text(raw.get("schema_version"))))
    plan["schema_version"] = QUERY_PLAN_SCHEMA
    plan["question_en"] = _text(raw.get("question_en"))
    if not plan["question_en"]:
        errors.append(_error("question_en_empty", "$.question_en"))
    plan["research_object"] = _text(raw.get("research_object"))
    if not plan["research_object"]:
        errors.append(_error("research_object_empty", "$.research_object"))

    ambiguity = raw.get("ambiguity") if isinstance(raw.get("ambiguity"), Mapping) else {}
    plan["ambiguity"] = {
        "is_ambiguous": bool(ambiguity.get("is_ambiguous")),
        "default_reading": _text(ambiguity.get("default_reading")),
        "needs_user_input": [_text(item) for item in ambiguity.get("needs_user_input") or ()
                             if _text(item)],
    }

    # --- facets ---------------------------------------------------------
    for key in ("question_en", "research_object"):
        if key in raw and not isinstance(raw.get(key), str):
            errors.append(_error("plan_field_not_a_string", "$." + key,
                                 type(raw.get(key)).__name__))
    if "facets" in raw and not isinstance(raw.get("facets"), list):
        errors.append(_error("facet_list_not_a_list", "$.facets",
                             type(raw.get("facets")).__name__))
    if "additional_constraints" in raw and not isinstance(
            raw.get("additional_constraints"), (list, str)):
        errors.append(_error("constraints_not_a_list", "$.additional_constraints",
                             type(raw.get("additional_constraints")).__name__))
    raw_facets = raw.get("facets")
    if not isinstance(raw_facets, list):
        errors.append(_error("facet_list_missing", "$.facets"))
        raw_facets = []
    facets: list[dict[str, Any]] = []
    for index, item in enumerate(raw_facets):
        path = "$.facets[%d]" % index
        if not isinstance(item, Mapping):
            errors.append(_error("facet_not_an_object", path))
            continue
        missing = [key for key in _FACET_KEYS if key not in item]
        if missing:
            errors.append(_error("facet_keys", path, ",".join(missing)))
            continue
        for key in item:
            if key not in _FACET_KEYS:
                errors.append(_error("facet_key_undefined",
                                     "%s.%s" % (path, _text(key))))
        facet: dict[str, Any] = {"id": _text(item.get("id"))}
        if facet["id"] != "F%d" % (len(facets) + 1):
            errors.append(_error("facet_id", path + ".id", facet["id"]))
        facet["ask"] = _text(item.get("ask"))
        if not facet["ask"]:
            errors.append(_error("facet_without_ask", path + ".ask"))

        keyword_queries: list[str] = []

        def _refuse(code: str, detail: str) -> None:
            # A refused phrase is kept with its facet and its reason: it is the
            # only record of why this plan was not a first hit.
            refused_queries.append({
                "facet_index": index, "facet_id": facet["id"],
                "query": phrase, "code": code, "detail": detail,
            })

        # THE VALIDATOR CHECKS FORMAT, NEVER QUALITY.
        #
        # It exists for one reason: the model may hallucinate a field or a value
        # the downstream cannot parse.  Whether a query is GOOD is not its
        # business.  It used to refuse a query for naming no object, for using a
        # review-directory word, and for carrying a generic head term; measured on
        # twelve cases that was 23 refusals out of 23, every one of them a quality
        # judgement and not one a format problem, and it drove a 67% revision rate
        # on plans whose format was clean.  It also refused a query that had
        # measured 10/10.  Those three rules now live in the prompt and in the
        # dashboard, where they can inform the model instead of blocking it.
        #
        # The rule that stayed is the one whose violation means the net comes back
        # EMPTY, which is the one outcome a first pass cannot recover from: a
        # sentence handed to a keyword engine returns nothing at all.
        #
        # The word-count rule was REMOVED, not retuned.  It refused phrases over 8
        # words because the frozen instance's object phrase is four words and left
        # room for an aspect; on a topic whose object phrase is itself six words
        # every honest phrase is over the limit, and the cross-domain run lost the
        # whole keyword channel to it (six refusals, an empty keyword list per
        # facet, and a corpus with zero keyword hits).  A word budget is a
        # property of the object's name, not of the retrieval channel, so the
        # prompt describes the phrase contract and the validator no longer
        # measures its length.
        for phrase in item.get("keyword_queries") or ():
            phrase = " ".join(_text(phrase).split())
            if not phrase:
                continue
            if _looks_like_a_sentence(phrase):
                _refuse("keyword_query_not_a_phrase", phrase)
                errors.append(_error("keyword_query_not_a_phrase",
                                     path + ".keyword_queries", phrase))
                continue
            keyword_queries.append(phrase)
        facet["keyword_queries"] = keyword_queries[
            : int(limits.get("keyword_queries_per_facet_max", 3))]
        if not facet["keyword_queries"]:
            errors.append(_error("keyword_queries_empty", path + ".keyword_queries"))

        facet["question_queries"] = [
            " ".join(_text(item2).split())
            for item2 in (item.get("question_queries") or ()) if _text(item2)
        ][: int(limits.get("question_queries_per_facet_max", 3))]
        if not facet["question_queries"]:
            # The semantic endpoint is the one that returns quotable sentences, and
            # the real run left it switched on but called it once per chapter.  A
            # facet with no question is a channel left shut.
            errors.append(_error("question_queries_empty", path + ".question_queries"))
        facet["must_exclude"] = [_text(item2) for item2
                                 in (item.get("must_exclude") or ()) if _text(item2)]

        raw_filters = item.get("filters") if isinstance(item.get("filters"), Mapping) else {}
        for key in raw_filters:
            if key not in _FILTER_KEYS:
                errors.append(_error("filter_key_undefined",
                                     "%s.filters.%s" % (path, _text(key))))
        filters: dict[str, list[str]] = {}
        for name in _FILTER_KEYS:
            lookup = _enum_lookup(payload, name)
            allowed = list((payload.get("enums") or {}).get(name, {}).get("allowed") or ())
            values: list[str] = []
            for value in raw_filters.get(name) or ():
                if not _text(value):
                    continue
                canonical = lookup.get(_fold(value))
                if canonical:
                    if canonical != _text(value):
                        repairs.append({
                            "tier": 1, "path": "%s.filters.%s" % (path, name),
                            "action": "normalised_locally",
                            "before": _text(value), "after": canonical,
                        })
                    if canonical not in values:
                        values.append(canonical)
                    continue
                # rung two: one cheap, bounded question about one field
                fixed = ""
                if field_repairer is not None:
                    try:
                        fixed = _text(field_repairer(
                            "%s.filters.%s" % (path, name), value, allowed))
                    except Exception:
                        fixed = ""
                canonical = lookup.get(_fold(fixed)) if fixed else None
                if canonical:
                    repairs.append({
                        "tier": 2, "path": "%s.filters.%s" % (path, name),
                        "action": "repaired_by_model",
                        "before": _text(value), "after": canonical,
                    })
                    if canonical not in values:
                        values.append(canonical)
                    continue
                # rung three: empty, logged, and the process continues
                repairs.append({
                    "tier": 3, "path": "%s.filters.%s" % (path, name),
                    "action": "emptied_and_logged",
                    "before": _text(value), "after": "",
                })
            filters[name] = values
        facet["filters"] = filters
        facets.append(facet)

    count = len(facets)
    if count < int(limits.get("facets_min", 1)) or count > int(limits.get("facets_max", 10)):
        errors.append(_error("facet_count", "$.facets", str(count)))

    key_sets = {tuple(sorted(row)) for row in facets}
    if len(key_sets) > 1:
        errors.append(_error("facet_keys_not_identical", "$.facets"))

    asks = [_fold(row.get("ask")) for row in facets if _fold(row.get("ask"))]
    if len(asks) != len(set(asks)):
        errors.append(_error("duplicate_ask", "$.facets"))
    if declared_requests is not None and int(declared_requests) != count:
        errors.append(_error("request_coverage", "$.facets",
                             "declared=%s facets=%d" % (declared_requests, count)))
    plan["facets"] = facets

    # --- seeds ----------------------------------------------------------
    seeds: list[dict[str, Any]] = []
    for item in raw.get("seeds") or ():
        if not isinstance(item, Mapping):
            continue
        title = _text(item.get("title"))
        if not title:
            continue
        seeds.append({"title": title, "hint": _text(item.get("hint")),
                      "why": _text(item.get("why"))})
    plan["seeds"] = seeds[: int(limits.get("seeds_max", 3))]

    # --- criteria and the overflow bucket --------------------------------
    criteria = raw.get("criteria") if isinstance(raw.get("criteria"), Mapping) else {}
    synonyms = criteria.get("synonyms") if isinstance(criteria.get("synonyms"), Mapping) else {}
    plan["criteria"] = {
        "must_include_topic": [_text(x) for x in criteria.get("must_include_topic") or ()
                               if _text(x)],
        "must_exclude_domain": [_text(x) for x in criteria.get("must_exclude_domain") or ()
                                if _text(x)],
        "synonyms": {_text(k): [_text(v) for v in (values or ()) if _text(v)]
                     for k, values in synonyms.items() if _text(k)},
    }
    constraints = raw.get("additional_constraints")
    if isinstance(constraints, str):
        constraints = [constraints]
    plan["additional_constraints"] = [_text(item) for item in (constraints or ())
                                      if _text(item)]
    return _report(not errors, plan, errors, repairs,
                   raw_plan=raw_plan, refused_queries=refused_queries)


REVISION_TEMPLATE = """Your previous plan was rejected by the local validator.

Each item below names the exact value that was refused and the measurement that
refuses it. Rewrite ONLY those values. Keep every other value exactly as it was.

Return the complete plan again as one JSON object, in the same shape as the plan
you were given, with no prose and no markdown fence around it. The JSON object is
the whole reply: every key of the plan, including the keys you did not change.

%(items)s

If you cannot write a keyword phrase that survives the rule, write fewer phrases
for that facet rather than a phrase you are unsure of."""


#: A keyword phrase that returns almost nothing cannot serve its facet, and a
#: phrase that returns hundreds of thousands is being answered by other fields.
#: Both were measured on the real provider: an invented compound phrase returned
#: 28 results and 2/10 relevant, while a bare topic string returned 219,837 and
#: was answered by superconductivity and chiral scattering.  The plan cannot know
#: either number when it is written, so the numbers come back to it.
QUERY_RESULT_FLOOR = 200
QUERY_RESULT_CEILING = 150000


def provider_feedback(
    measurements: Sequence[Mapping[str, Any]],
    *,
    floor: int = QUERY_RESULT_FLOOR,
    ceiling: int = QUERY_RESULT_CEILING,
) -> list[dict[str, Any]]:
    """The one hard failure a first pass cannot recover from: an empty net.

    This is a CHECK, not a tuning loop.  Running the search, feeding the numbers
    back and asking the model to rewrite is explicitly out of scope: retrieval is
    the expensive part and iterating on it to perfect a phrase is buying the box
    and returning the pearl.  A phrase that returns nothing at all is the single
    case worth a second call, because the alternative is a facet with no material.
    """

    rows: list[dict[str, Any]] = []
    for row in measurements or ():
        phrase = _text(row.get("query"))
        total = row.get("provider_total")
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = -1
        facet = _text(row.get("facet")) or "?"
        if total >= 0 and total < int(floor):
            rows.append(_error(
                "query_returns_too_little",
                "$.facets[%s].keyword_queries" % facet,
                "%s returned %d results -- write this facet's phrase in the words "
                "a paper's own title uses, not in words invented for the plan"
                % (phrase, total)))
        elif total > int(ceiling):
            rows.append(_error(
                "query_returns_too_much",
                "$.facets[%s].keyword_queries" % facet,
                "%s returned %d results -- the query is not narrowed enough to "
                "be about this object; every phrase must name the object"
                % (phrase, total)))
    return rows

def render_revision_prompt(
    fields: Mapping[str, Any] | None,
    plan: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    *,
    user_question: str = "",
) -> dict[str, Any]:
    """The second round: the refused values, with the evidence, and nothing else."""

    payload = dict(fields or load_field_dictionary())
    items: list[str] = []
    for row in errors or ():
        items.append("- %s at %s: %s" % (_text(row.get("code")),
                                          _text(row.get("path")),
                                          _text(row.get("detail")) or "refused"))
    body = REVISION_TEMPLATE % {"items": chr(10).join(items) or "- (none)"}
    return {
        "system_prompt": body,
        "user_payload": {"user_question": _text(user_question),
                         "previous_plan": dict(plan or {}),
                         "refused": items},
        "prompt_hash": _sha_text(body),
        "field_dictionary_version": _text(payload.get("version")),
    }

#: The field's own names for the object, each with the reason it is on the list.
#: A query that names the object by an alias is naming the object.  This is what
#: rescues the query that scored 10/10 and a naive phrase-only rule refused:
#: "computational imaging diffractive deep learning" uses the alias the
#: literature actually uses, so the rule is phrase OR alias, not phrase alone.
OBJECT_ALIASES: dict[str, dict[str, str]] = {
    "diffractive deep learning": {
        "why": ("the alias the field uses in its own titles; measured 9-10/10 "
                "as the head of a keyword query"),
    },
    "diffractive deep neural network": {
        "why": "the expansion of the D2NN acronym, used verbatim in titles",
    },
    "d2nn": {"why": "the standard acronym, used verbatim in titles"},
    "donn": {"why": "the acronym used for diffractive optical neural networks"},
    "optical diffractive neural network": {
        "why": "the object phrase with its two adjectives in the other order",
    },
}

#: One revision per facet.  The first pass is the target and the revision is a
#: fallback: a high revision rate is the prompt failing, not the loop working.
MAX_REVISION_ROUNDS_PER_FACET = 1


def _facet_index(path: Any) -> int | None:
    match = re.search(r"facets\[(\d+)\]", _text(path))
    return int(match.group(1)) if match else None


def revision_budget(
    errors: Sequence[Mapping[str, Any]],
    *,
    rounds_used: int,
    cap: int = MAX_REVISION_ROUNDS_PER_FACET,
) -> dict[str, Any]:
    """Whether a revision round is allowed, and for which facets.

    A plan-level error cannot be repaired facet by facet, and a facet that has
    already used its round is not retried: without this the loop oscillates
    between returning too little and returning too much, which costs a call each
    time and never converges.
    """

    if int(rounds_used) >= int(cap):
        return {"allowed": False, "reason": "revision_cap_reached", "facets": []}
    facets = sorted({index for index in (_facet_index(row.get("path"))
                                         for row in errors or ())
                     if index is not None})
    if not facets:
        return {"allowed": False, "reason": "no_revisable_facet", "facets": []}
    return {"allowed": True, "reason": "", "facets": facets}


def legacy_projection(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The retired shape, DERIVED from the v2 plan for consumers not yet moved.

    Eight modules read the old input/output nesting.  Changing all of them in one
    step is a large risk taken for no quality gain, and keeping the old planner
    alive as a second producer would be a second source of truth.  So the old
    shape is projected from the plan that was really produced, it says which
    contract it was derived from, and it carries the v2 plan itself so a
    migrated consumer never has to guess.
    """

    payload = dict(plan or {})
    question = _text(payload.get("question_en"))
    facets = list(payload.get("facets") or ())
    constraints = [_text(item) for item in payload.get("additional_constraints") or ()
                   if _text(item)]
    return {
        "derived_from": QUERY_PLAN_SCHEMA,
        "input": {"user_query": question},
        "output": {
            "problem_understanding": question,
            "scope_definition": {
                "main_scope": _text(payload.get("research_object")),
                "scope_items": [_text(facet.get("ask")) for facet in facets
                                 if _text(facet.get("ask"))],
            },
            "keyword_decomposition": {"keywords": keyword_queries(payload)},
            "extra_notes": chr(10).join(constraints),
        },
        "query_plan_v2": payload,
    }


def retrieval_plan(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One route per facet, with the two channels kept structurally apart.

    The keyword channel is a keyword engine: it needs 4-8 word phrases and it
    returns zero for a sentence.  The snippet channel is a semantic engine: it
    needs a question and it returns the sentences themselves.  Handing either
    channel the other one's text is the mistake this function exists to prevent.
    """

    routes: list[dict[str, Any]] = []
    for facet in (plan or {}).get("facets") or ():
        routes.append({
            "facet": _text(facet.get("id")),
            "ask": _text(facet.get("ask")),
            "paper_queries": [_text(item) for item in facet.get("keyword_queries") or ()
                              if _text(item)],
            "snippet_queries": [_text(item) for item in facet.get("question_queries") or ()
                                if _text(item)],
            "filters": dict(facet.get("filters") or {}),
            "must_exclude": list(facet.get("must_exclude") or ()),
        })
    return routes

def plan_identity(plan: Mapping[str, Any]) -> str:
    """Content identity of a plan, stable for the same content."""

    return _sha_text(_canonical(dict(plan or {})))


def keyword_queries(plan: Mapping[str, Any]) -> list[str]:
    rows: list[str] = []
    for facet in (plan or {}).get("facets") or ():
        for phrase in facet.get("keyword_queries") or ():
            if _text(phrase) and phrase not in rows:
                rows.append(phrase)
    return rows


def question_queries(plan: Mapping[str, Any]) -> list[str]:
    rows: list[str] = []
    for facet in (plan or {}).get("facets") or ():
        for phrase in facet.get("question_queries") or ():
            if _text(phrase) and phrase not in rows:
                rows.append(phrase)
    return rows


def filter_plan(plan: Mapping[str, Any]) -> dict[str, list[str]]:
    """The union of the facets' filters, ready to hand to a provider."""

    merged: dict[str, list[str]] = {name: [] for name in _FILTER_KEYS}
    for facet in (plan or {}).get("facets") or ():
        for name in _FILTER_KEYS:
            for value in (facet.get("filters") or {}).get(name) or ():
                if value not in merged[name]:
                    merged[name].append(value)
    return merged
