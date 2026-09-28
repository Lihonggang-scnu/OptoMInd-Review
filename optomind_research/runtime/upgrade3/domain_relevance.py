"""Facet Domain Relevance Engine.

Answers one question per paper and facet: does this paper belong to the
literature this facet is about, or is it a general-purpose instrument our
papers happen to cite.

Two rules keep the engine honest, and both were added after measuring real
failures:

* the vocabulary signal is built from PHRASES scored against a background of
  documents that actually carry text.  An earlier version scored against the
  whole node set, most of which has no abstract, so ordinary academic wording
  looked distinctive and a paper about imaging through diffusers was admitted
  as in-domain on those words alone;
* when the embedding is missing, vocabulary alone may not decide the class.
  One signal is not evidence, so the paper falls to ADJACENT or UNCERTAIN and
  carries a risk flag instead.

Nothing here deletes a paper.  ADJACENT and UNCERTAIN are kept by construction.
No domain term, paper title or facet id is written into this file.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

import numpy as np

IN_DOMAIN = "IN_DOMAIN"
ADJACENT = "ADJACENT"
GENERIC_TOOL = "GENERIC_TOOL"
OFF_TOPIC = "OFF_TOPIC"
UNCERTAIN = "UNCERTAIN"
CLASSES = (IN_DOMAIN, ADJACENT, GENERIC_TOOL, OFF_TOPIC, UNCERTAIN)

#: Classes the graph may not promote into Foundational / Landmark / Bridge /
#: Frontier candidates, no matter how central they are structurally.
#: Only a paper the judgement actually placed outside the question is blocked from
#: roles.  A widely used tool is a usage label, not a verdict: a paper can be cited
#: across fields and still be the paper that answers this question, so it keeps its
#: place in the content judgement and is only marked as instrument-like.
BLOCKED_FROM_ROLES = (OFF_TOPIC,)
#: Kept, but every card carries a risk flag.
RISK_FLAGGED = (ADJACENT, UNCERTAIN)

_WORD = re.compile(r"[a-z][a-z\-]{2,}")
_SENTENCE_SPLIT = re.compile(r"[.;:!?()\[\]]+")

#: Function words only.  A stoplist of this kind is language plumbing, not
#: domain knowledge: it contains no subject term and is identical for every
#: research object.
_STOP = set("""the of and for with from that this these those using used use into via
based towards toward their there where which while been have has had were was are
can could may might will would shall should not but also such than then them they
our its it is be by as at on in to a or we you it's results result study present
presents paper article here show shows shown propose proposed new novel method
methods approach approaches data model models using two three one however moreover
furthermore thus therefore between among during within without about over under
more most less least other others same different high low large small""".split())


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(str(text or "").casefold()) if w not in _STOP]


def phrases(text: str, max_n: int = 3) -> list[str]:
    """Contiguous n-grams of content words, so multi-word terms survive."""

    out: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(str(text or "").casefold()):
        words = [w for w in _WORD.findall(sentence) if w not in _STOP]
        for size in range(1, max_n + 1):
            for start in range(len(words) - size + 1):
                out.append(" ".join(words[start:start + size]))
    return out


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


# ------------------------------------------------------- semantic layer ---


def semantic_prototypes(facet_features, vectors, config):
    """One prototype vector per facet, from its most confident papers."""

    cfg = config.get("domain_relevance") or {}
    seed_count = int(cfg.get("prototype_seed_papers", 12))
    require_snippet = bool(cfg.get("prototype_requires_snippet", True))

    prototypes = {}
    for facet, by_paper in _facet_index(facet_features).items():
        ranked = sorted(by_paper.items(),
                        key=lambda kv: (-float(kv[1].get("RRF_score") or 0.0),
                                        -(kv[1].get("snippet_hit_count") or 0)))
        chosen = []
        for paper, row in ranked:
            if paper not in vectors:
                continue
            if require_snippet and not (row.get("snippet_hit_count") or 0):
                continue
            chosen.append(paper)
            if len(chosen) >= seed_count:
                break
        if not chosen:
            for paper, _ in ranked:
                if paper in vectors:
                    chosen.append(paper)
                if len(chosen) >= seed_count:
                    break
        if chosen:
            prototypes[facet] = _unit(np.mean([vectors[p] for p in chosen], axis=0))
    return prototypes


def _facet_index(facet_features):
    out = defaultdict(dict)
    for paper, by_facet in (facet_features.get("features") or {}).items():
        for facet, row in by_facet.items():
            out[facet][paper] = row
    return out


def semantic_scores(prototypes, vectors):
    out = defaultdict(dict)
    for facet, prototype in prototypes.items():
        for paper, vector in vectors.items():
            out[paper][facet] = float(np.dot(_unit(vector), prototype))
    return out


# --------------------------------------------------- contrastive vocab ----


#: Words that mark an affiliation block or a person, not a subject.  This is not
#: domain knowledge: the list is identical for every research object, and the
#: person names themselves are learned from the corpus at run time.
_AFFILIATION_MARKERS = ("university", "universit", "institute", "institut",
                        "department", "college", "laboratory", "school", "faculty",
                        "academy", "center", "centre", "hospital", "corporation",
                        "gmbh", "inc", "ltd")


def strip_affiliation_block(text):
    """Drop a leading author/affiliation header.

    Abstracts fetched from the provider often begin with the affiliation block,
    which is how place names such as a city or a state ended up among the
    highest-weighted "domain terms".
    """

    text = str(text or "")
    head = text.split(".", 1)[0]
    if len(head) < 80:
        return text
    if head.count(",") < 3:
        return text
    if not any(ch.isdigit() for ch in head):
        return text
    if not any(marker in head.casefold() for marker in _AFFILIATION_MARKERS):
        # Requiring an actual affiliation word keeps ordinary opening sentences,
        # which also contain commas and numbers, from being cut away.
        return text
    return text[len(head) + 1:].strip()


def always_capitalised_tokens(texts):
    """Tokens that never once appear in lower case anywhere in this corpus.

    A person, a place or an institution is written capitalised every time; a
    subject term is not.  This needs no list of names and no list of places, and
    it is what removes a city name from the discriminative vocabulary without
    touching the term that actually names the research object.
    """

    import re as _re

    lower = set()
    anycase = set()
    for text in texts:
        for token in _re.findall(r"[A-Za-z][A-Za-z\-]{2,}", str(text or "")):
            anycase.add(token.casefold())
            if token.islower():
                lower.add(token)
    return anycase - lower


def person_name_tokens(meta):
    """Every token that appears in any author name in this corpus."""

    out = set()
    for info in meta.values():
        for author in info.get("authors") or ():
            name = author if isinstance(author, str) else str(author.get("name") or "")
            for token in _WORD.findall(name.casefold()):
                if len(token) > 2:
                    out.add(token)
    return out


def looks_like_entity(phrase, people):
    words = phrase.split()
    if any(w in people for w in words):
        return True
    return any(w in _AFFILIATION_MARKERS for w in words)


def contrastive_vocabulary(facet_features, meta, config):
    """Phrases that mark a facet, measured against documents that have text.

    Scoring against the full node set was the bug: most nodes carry no title
    and no abstract, so the background document frequency was far too low and
    mundane wording won.  Only documents with real text form the background.
    """

    cfg = config.get("domain_relevance") or {}
    seed_count = int(cfg.get("vocabulary_seed_papers", 30))
    min_df = int(cfg.get("vocabulary_min_count", 3))
    max_n = int(cfg.get("vocabulary_max_ngram", 3))
    top_n = int(cfg.get("vocabulary_top_n", 30))

    people = person_name_tokens(meta)
    documents = {}
    raw_texts = [(str(i.get("title") or "") + ". " + str(i.get("abstract") or ""))
                 for i in meta.values()]
    proper_tokens = always_capitalised_tokens(raw_texts)
    for paper, info in meta.items():
        abstract = strip_affiliation_block(info.get("abstract") or "")
        text = (str(info.get("title") or "") + ". " + str(abstract)).strip()
        if len(text) > 40:
            documents[paper] = text
    background = Counter()
    for text in documents.values():
        background.update(set(phrases(text, max_n)))
    document_count = max(len(documents), 1)

    vocabulary = {}
    audit = {}
    for facet, by_paper in _facet_index(facet_features).items():
        ranked = sorted(by_paper.items(),
                        key=lambda kv: -float(kv[1].get("RRF_score") or 0.0))[:seed_count]
        inside = Counter()
        seen_docs = 0
        for paper, _ in ranked:
            text = documents.get(paper)
            if not text:
                continue
            seen_docs += 1
            inside.update(set(phrases(text, max_n)))
        if not seen_docs:
            vocabulary[facet] = {}
            audit[facet] = {"top": [], "downweighted": [], "seed_documents": 0}
            continue
        min_single_support = float(cfg.get("single_token_min_support", 0.4))
        weights = {}
        dropped = []
        for phrase, count in inside.items():
            if count < min_df:
                continue
            if looks_like_entity(phrase, people):
                dropped.append(phrase)
                continue
            if any(w in proper_tokens for w in phrase.split()):
                # A token that is capitalised every single time in this corpus is
                # a person, a place or an institution, never a subject term.
                dropped.append(phrase)
                continue
            if " " not in phrase and count / max(seen_docs, 1) < min_single_support:
                # A single word only counts as a facet term if it really is
                # pervasive across the facet, which removes stray boilerplate
                # while keeping the one word that names the object.
                dropped.append(phrase)
                continue
            p_in = count / seen_docs
            p_bg = background.get(phrase, 0) / document_count
            lift = (p_in + 1e-9) / (p_bg + 1e-9)
            if lift <= 1.0:
                continue
            weights[phrase] = round(math.log(lift) * math.log1p(count), 4)
        vocabulary[facet] = weights
        ordered = sorted(weights.items(), key=lambda kv: -kv[1])
        audit[facet] = {
            "seed_documents": seen_docs,
            "top": [{"phrase": p, "weight": w, "in_facet": inside[p],
                     "in_background": background.get(p, 0)} for p, w in ordered[:top_n]],
            "downweighted": [{"phrase": p, "in_facet": inside[p],
                              "in_background": background.get(p, 0)}
                             for p, _ in sorted(inside.items(),
                                                key=lambda kv: -background.get(kv[0], 0))[:20]],
            "removed_as_entity_or_boilerplate": sorted(dropped)[:40],
            "person_tokens": len(people),
        }
    return vocabulary, audit


def vocabulary_score(title, abstract, weights):
    """Weighted phrase overlap, with longer phrases worth more."""

    if not weights:
        return 0.0, []
    hits = [(phrase, weights[phrase])
            for phrase in set(phrases(str(title or "") + ". " + str(abstract or "")))
            if phrase in weights]
    if not hits:
        return 0.0, []
    hits.sort(key=lambda kv: (-kv[1], -len(kv[0])))
    top = hits[:12]
    score = sum(w * (1.0 + 0.35 * (len(p.split()) - 1)) for p, w in top) / len(top)
    return round(score, 4), [p for p, _ in top[:6]]


# ------------------------------------------------------------- engine -----


# ------------------------------------------------------------ scope layer --

#: Function words and the most generic research words.  A word on this list
#: carries no scope: it says that a paper is a study, not what the study is
#: about.  The list is deliberately short and entirely domain-neutral.
GENERIC_QUERY_WORDS = {
    "about", "after", "against", "also", "among", "analysis", "approach", "approaches",
    "are", "based", "between", "both", "can", "compared", "comparison", "does", "during",
    "effect", "effects", "evaluate", "evaluation", "for", "from", "how", "impact", "impacts",
    "into", "its", "method", "methods", "more", "most", "new", "not", "novel", "of", "on",
    "other", "our", "paper", "papers", "recent", "research", "results", "review", "role",
    "study", "studies", "such", "than", "that", "the", "their", "these", "this", "through",
    "under", "use", "used", "using", "via", "was", "were", "what", "when", "which", "while",
    "with", "within", "without",
}


def scope_vocabulary(corpus: Mapping[str, Any]) -> dict[str, Any]:
    """The specifying language the plan itself used, split into object and scope.

    The object phrase names what the review studies; the facet queries name the
    conditions the user asked about.  Both are read from the artefacts the corpus
    already carries -- the research object and the query texts -- so no domain
    word list is involved and nothing has to be maintained per topic.
    """

    def content(text: Any) -> list[str]:
        words = []
        for word in re.split(r"[^0-9A-Za-z\u4e00-\u9fff-]+", str(text or "").casefold()):
            if len(word) >= 3 and word not in GENERIC_QUERY_WORDS and word not in words:
                words.append(word)
        return words

    object_terms = content(corpus.get("research_object"))
    object_set = set(object_terms)
    by_facet: dict[str, list[str]] = defaultdict(list)
    rows = list(corpus.get("query_audit") or [])
    for hit in corpus.get("retrieval_hits") or ():
        rows.append(hit)
    for row in rows:
        facet = str((row or {}).get("facet_id") or "")
        if not facet:
            continue
        for word in content((row or {}).get("query_text")):
            if word not in object_set and word not in by_facet[facet]:
                by_facet[facet].append(word)
    exclusions = [str(item).strip() for item in (corpus.get("exclusions") or [])
                  if str(item).strip()]
    return {"object_terms": object_terms, "scope_terms_by_facet": dict(by_facet),
            "exclusions": exclusions}


#: How many leading characters two words must share to be the same word in another
#: inflection: microbiome/microbiota, modulation/modulates, inhibitor/inhibitors.
#: Six is long enough that unrelated words rarely collide.
TERM_PREFIX = 6

#: Below this many characters of title plus abstract the scope judgement has no
#: evidence to read, and "no evidence" must not be spent as "out of scope".
MIN_TEXT_FOR_SCOPE = 200


def term_coverage(text: str, terms: Sequence[str]) -> tuple[float, list[str]]:
    """Share of the plan terms a paper text actually carries, and which ones.

    A term counts when the text contains it, or contains a word that shares its
    opening characters: an inflection of the same word is the same word for this
    purpose, and exact matching alone demoted papers whose abstracts say
    "microbiota" where the plan says "microbiome".
    """

    wanted = [t for t in terms if t]
    if not wanted:
        return 1.0, []
    words = [w for w in re.split(r"[^0-9A-Za-z\u4e00-\u9fff-]+",
                                 str(text or "").casefold()) if w]
    joined = " " + " ".join(words) + " "
    hits = []
    for term in wanted:
        if (" " + term) in joined:
            hits.append(term)
            continue
        stem = term[:TERM_PREFIX]
        if len(stem) >= TERM_PREFIX and any(word.startswith(stem) for word in words):
            hits.append(term)
    return round(len(hits) / len(wanted), 4), hits


def classify_domain_relevance(facet_features, meta, vectors, edge_set, raw_edges,
                              core_ids, config, corpus=None, extra_text=None,
                              mention_text=None, reading=None):
    """Per paper x facet domain relevance, with an explicit class and risk flag.

    Three judgements are kept apart instead of collapsed into one class:

      topic_relevance  -- is this paper in the field at all
      scope_relevance  -- does it match the specifying language the plan used
      facet_relevance  -- does it carry this facet's own evidence

    A paper can be on topic and still not answer the question: a methane study in
    a producing gas field is in the methane literature and is not evidence about
    Arctic permafrost thaw.  Such papers stay in the skeleton and stop being
    counted as direct evidence for the question.
    """

    cfg = config.get("domain_relevance") or {}
    scope_vocab = scope_vocabulary(corpus or {})
    scope_min = float(cfg.get("scope_min_object_coverage", 0.34))
    prototypes = semantic_prototypes(facet_features, vectors, config)
    semantics = semantic_scores(prototypes, vectors)
    vocabulary, vocabulary_audit = contrastive_vocabulary(facet_features, meta, config)

    generic_share = float(cfg.get("generic_tool_max_field_share", 0.02))
    generic_citations = int(cfg.get("generic_tool_min_citations", 800))
    strong_share = float(cfg.get("strong_field_share", 0.05))
    min_core_citers = int(cfg.get("min_core_papers_citing", 2))
    required_with_semantic = int(cfg.get("signals_required_with_semantic", 1))
    rescue_min_sources = int(cfg.get("contextual_rescue_min_sources", 2))
    retrieval_strong = float(cfg.get("retrieval_strong_threshold", 0.25))

    cited_by_core = defaultdict(int)
    for source, record in raw_edges.items():
        if source not in core_ids:
            continue
        for node in record.get("references") or ():
            target = str(node.get("paper_id") or "")
            if target:
                cited_by_core[target] += 1

    context_stats = defaultdict(lambda: {"mentions": 0, "sources": set(), "facets": set()})
    for row in edge_set.contextual:
        bucket = context_stats[str(row["target"])]
        bucket["mentions"] += 1
        bucket["sources"].add(str(row["source"]))
        bucket["facets"].add(str(row["facet_id"]))

    facets = list(facet_features.get("facets") or [])
    by_facet = {f: {} for f in facets}
    counts = Counter()

    def _cut(values, pct, fallback):
        clean = sorted(v for v in values if v is not None)
        if len(clean) < 20:
            return fallback
        return clean[min(len(clean) - 1, max(0, int(round(pct * (len(clean) - 1)))))]

    all_semantic, all_vocab = [], []
    precomputed = {}
    for paper, info in meta.items():
        abstract = str(info.get("abstract") or "")
        for facet in facets:
            score, hits = vocabulary_score(info.get("title", ""), abstract,
                                           vocabulary.get(facet) or {})
            precomputed[(paper, facet)] = (score, hits)
            all_vocab.append(score)
            value = semantics.get(paper, {}).get(facet)
            if value is not None:
                all_semantic.append(value)

    semantic_high = _cut(all_semantic, float(cfg.get("semantic_high_percentile", 0.70)),
                         float(cfg.get("semantic_high", 0.80)))
    semantic_low = _cut(all_semantic, float(cfg.get("semantic_low_percentile", 0.25)),
                        float(cfg.get("semantic_low", 0.65)))
    vocabulary_high = _cut(all_vocab, float(cfg.get("vocabulary_high_percentile", 0.85)),
                           float(cfg.get("vocabulary_high", 1.45)))
    off_semantic = _cut(all_semantic,
                        float(cfg.get("off_topic_max_semantic_percentile", 0.20)),
                        float(cfg.get("off_topic_max_semantic", 0.62)))

    for paper, info in meta.items():
        abstract = str(info.get("abstract") or "")
        for facet in facets:
            retrieval = (facet_features["features"].get(paper) or {}).get(facet)
            is_core = paper in core_ids
            context = context_stats.get(paper) or {}
            context_facet = facet in (context.get("facets") or set())
            context_mentions = int(context.get("mentions") or 0) if context_facet else 0
            if not is_core and retrieval is None and not context_facet:
                continue

            semantic = semantics.get(paper, {}).get(facet)
            vocab, vocab_hits = precomputed[(paper, facet)]
            citations = int(info.get("citation_count") or 0)
            core_citers = cited_by_core.get(paper, 0)
            share = core_citers / max(citations, core_citers, 1)

            retrieval_score = 0.0
            if retrieval:
                # The snippet-count term was removed.  How many passages a query
                # returned depends on the paper's length and on how the provider
                # splits it, so it must not add relevance.
                retrieval_score = round(
                    0.65 * float(retrieval.get("query_coverage") or 0.0)
                    + 0.35 * min(1.0, float(retrieval.get("RRF_score") or 0.0) / 0.4), 4)

            signals = {
                "retrieval": retrieval_score > 0.0,
                "contextual": context_mentions > 0,
                "field_share": share >= strong_share,
                "core_citations": core_citers >= min_core_citers,
                "vocabulary": vocab >= vocabulary_high,
                "semantic": semantic is not None and semantic >= semantic_high,
            }
            # Which material the judgement actually had, recorded per paper so a
            # reader can see what a label rests on.  The three body sources are
            # kept apart because they are not equally strong: the paper's own
            # abstract is its account of its work, its own passage is direct local
            # material, and a sentence in which someone else cites it is an
            # external clue and not the paper's own conclusion.
            own_snippet = str((extra_text or {}).get(paper) or "")
            mention = str((mention_text or {}).get(paper) or "")
            material_sources = [name for name, present in (
                ("title", bool(str(info.get("title") or "").strip())),
                ("abstract", bool(abstract.strip())),
                ("own_snippet", bool(own_snippet.strip())),
                ("mention_sentence", bool(mention.strip())),
            ) if present]
            text = " ".join(part for part in (info.get("title") or "", abstract,
                                              own_snippet, mention) if part)
            object_coverage, object_hits = term_coverage(
                text, scope_vocab.get("object_terms") or [])
            facet_terms = (scope_vocab.get("scope_terms_by_facet") or {}).get(facet) or []
            term_coverage_value, _facet_hits = term_coverage(text, facet_terms)
            independent = sum(1 for name, value in signals.items()
                              if value and name != "vocabulary")
            generic_like = share < generic_share and citations >= generic_citations

            # A usage label, not a verdict: it is recorded and shown, and it does
            # not decide the class or block the content judgement.
            usage_label = ("widely_cited_outside_field"
                           if generic_like else "")
            if semantic is not None and semantic >= semantic_high:
                klass, because = IN_DOMAIN, "semantic"
            elif (context_mentions
                  and len(context.get("sources") or ()) >= rescue_min_sources
                  and independent >= 1):
                # A single mention is a coincidence, not a rescue.  Requiring
                # several independent source papers is what keeps this route from
                # admitting a boundary paper on one stray citation.
                klass, because = IN_DOMAIN, "contextual_rescue"
            elif retrieval_score >= retrieval_strong and independent >= required_with_semantic:
                # Being returned by a facet query is real evidence, but a weak
                # rank is not.  A weak hit needs a second independent signal.
                klass, because = IN_DOMAIN, "retrieved_by_facet_query"
            elif independent >= max(2, required_with_semantic):
                klass, because = IN_DOMAIN, "multiple_independent_signals"
            elif semantic is None and signals["vocabulary"] and independent == 0:
                # One signal is not evidence.  This is the exact path that let a
                # paper through on ordinary academic wording alone.
                klass, because = UNCERTAIN, "vocabulary_only_and_no_embedding"
            elif semantic is None and independent >= 1:
                klass, because = ADJACENT, "partial_signals_without_embedding"
            elif retrieval_score > 0.0 and independent >= 1 and \
                    retrieval_score < retrieval_strong:
                klass, because = ADJACENT, "weak_retrieval_hit_only"
            elif (semantic is not None and semantic <= off_semantic) and independent == 0 \
                    and not signals["vocabulary"]:
                klass, because = OFF_TOPIC, "no_domain_signal_at_all"
            elif semantic is not None and semantic >= semantic_low:
                klass, because = ADJACENT, "semantic_below_in_domain_cut"
            elif signals["vocabulary"] or independent >= 1:
                klass, because = ADJACENT, "weak_but_real_domain_signal"
            else:
                klass, because = UNCERTAIN, "signals_disagree_or_missing"

            # topic is judged first: the scope gate may only demote a paper that
            # is in the field, never promote one that is not.
            topic_relevance = klass not in (OFF_TOPIC, GENERIC_TOOL)
            # A paper whose abstract is missing cannot be judged on scope, and the
            # judgement is not allowed to read "no text" as "wrong scope": 235 of
            # 250 scope demotions in one reworked run were papers with no abstract.
            scope_evidence_missing = len(text.strip()) < MIN_TEXT_FOR_SCOPE
            scope_ok = object_coverage >= scope_min
            excluded_hits = [term for term in (scope_vocab.get("exclusions") or [])
                             if (" " + term.casefold()) in (" " + text.casefold())]
            if excluded_hits:
                scope_ok = False
                because = "excluded_term_present"
            if klass == IN_DOMAIN and not scope_ok and not scope_evidence_missing:
                klass, because = ADJACENT, "topic_matched_scope_missing"
            # The third ruler ("does this piece carry evidence for this facet") is
            # gone: that is a judgement about the science, and nothing here has
            # read the paper.  What remains is a pre-reading hint that only ever
            # says "worth reading", never "confirmed", and it is explicitly
            # withheld when the material to judge on is not there.
            # A reading judgement, when one exists, outranks the lexical rulers in
            # both directions: it is the only signal here that read the material.
            label = str(((reading or {}).get(paper) or {}).get("label") or "")
            if label == "directly_relevant":
                klass, because = IN_DOMAIN, "reading_directly_relevant"
            elif label == "clearly_irrelevant":
                klass, because = OFF_TOPIC, "reading_clearly_irrelevant"
            if scope_evidence_missing and label not in ("directly_relevant",
                                                        "clearly_irrelevant"):
                pre_reading_hint = "unjudged_material_missing"
            elif klass == IN_DOMAIN and scope_ok:
                pre_reading_hint = "likely_answers_this_facet"
            elif topic_relevance:
                pre_reading_hint = "related_but_other_scope"
            else:
                pre_reading_hint = "outside_the_question"

            counts[klass] += 1
            by_facet[facet][paper] = {
                "paper_id": paper, "facet_id": facet, "class": klass, "why": because,
                "risk_flag": klass in RISK_FLAGGED,
                "is_core": is_core,
                "signals": signals,
                "topic_relevance": topic_relevance,
                # None means unjudged: the text to judge it on was not there.
                "scope_relevance": None if (scope_evidence_missing and not excluded_hits)
                                   else scope_ok,
                "scope_evidence_missing": scope_evidence_missing,
                "pre_reading_hint": pre_reading_hint,
                "reading_label": label,
                "reading_evidence": str(((reading or {}).get(paper) or {}).get("evidence") or "")[:300],
                "reading_sub_question": str(((reading or {}).get(paper) or {}).get("sub_question") or "")[:200],
                "reading_uncertain": str(((reading or {}).get(paper) or {}).get("uncertain") or "")[:200],
                "usage_label": usage_label or (label if label == "background_or_method" else ""),
                "material_sources": material_sources,
                "object_coverage": object_coverage,
                "object_terms_matched": object_hits,
                "facet_scope_coverage": term_coverage_value,
                "semantic_evidence_missing": semantic is None,
                "excluded_terms_present": excluded_hits,
                "independent_signals": independent,
                "retrieval_score": retrieval_score,
                "query_coverage": (retrieval or {}).get("query_coverage"),
                "RRF_score": (retrieval or {}).get("RRF_score"),
                "direct_snippet_hits": (retrieval or {}).get("snippet_hit_count"),
                "max_snippet_score": (retrieval or {}).get("max_snippet_score"),
                "semantic_relevance": round(semantic, 4) if semantic is not None else None,
                "vocabulary_score": vocab,
                "vocabulary_hits": vocab_hits,
                "field_share": round(share, 4),
                "citation_count": citations,
                "core_papers_citing_it": core_citers,
                # Deliberately distinct from direct snippet hits: these are OTHER
                # papers' passages pointing at this one.
                "contextual_mentions_from_other_papers": context_mentions,
                "contextual_distinct_sources": len(context.get("sources") or ())
                if context_facet else 0,
            }

    aggregate = {}
    for facet, rows in by_facet.items():
        for paper, row in rows.items():
            current = aggregate.get(paper)
            # A missing embedding is missing, not zero: rows that have one are
            # preferred, and only rows that both have one are compared by it.
            def rank(item):
                has = item.get("semantic_relevance") is not None
                return (1 if has else 0,
                        float(item.get("semantic_relevance") or 0.0),
                        float(item.get("retrieval_score") or 0.0),
                        1 if item.get("direct_question_evidence") else 0)
            if current is None or rank(row) > rank(current):
                aggregate[paper] = dict(row)
    # The strongest pre-reading hint any facet produced, plus the union of the
    # material the facets had.  No facet reading "confirmed" is possible here.
    order = ["likely_answers_this_facet", "related_but_other_scope",
             "unjudged_material_missing", "outside_the_question"]
    for paper, row in aggregate.items():
        hints = [(by_facet[f].get(paper) or {}).get("pre_reading_hint")
                 for f in facets if by_facet[f].get(paper)]
        row["pre_reading_hint"] = next((h for h in order if h in hints),
                                       "unjudged_material_missing")
        sources: list[str] = []
        for f in facets:
            for name in ((by_facet[f].get(paper) or {}).get("material_sources") or []):
                if name not in sources:
                    sources.append(name)
        row["material_sources"] = sources
    for paper, row in aggregate.items():
        classes = {r["class"] for r in (by_facet[f].get(paper) for f in facets) if r}
        if classes == {GENERIC_TOOL}:
            row["class"] = GENERIC_TOOL
            row["why"] = "generic_in_every_facet"
        row["risk_flag"] = row["class"] in RISK_FLAGGED
        row["facet_classes"] = {f: by_facet[f][paper]["class"]
                                for f in facets if paper in by_facet[f]}

    aggregate_counts = Counter(row["class"] for row in aggregate.values())
    return {
        "by_facet": by_facet,
        "aggregate": aggregate,
        # The reading judgements travel with the domain result: the admission
        # decision is theirs, not the lexical rulers'.
        "reading": dict(reading or {}),
        "aggregate_counts": {k: aggregate_counts.get(k, 0) for k in CLASSES},
        "counts": {k: counts.get(k, 0) for k in CLASSES},
        "thresholds": {
            "calibrated_from_population": True,
            "semantic_high": round(semantic_high, 4),
            "semantic_low": round(semantic_low, 4),
            "vocabulary_high": round(vocabulary_high, 4),
            "off_topic_max_semantic": round(off_semantic, 4),
            "generic_tool_max_field_share": generic_share,
            "generic_tool_min_citations": generic_citations,
            "strong_field_share": strong_share,
            "min_core_papers_citing": min_core_citers,
            "signals_required_with_semantic": required_with_semantic,
            "contextual_rescue_min_sources": rescue_min_sources,
            "retrieval_strong_threshold": retrieval_strong,
            "scope_min_object_coverage": scope_min,
            "scope_object_terms": scope_vocab.get("object_terms") or [],
        },
        "vocabulary_audit": vocabulary_audit,
        "vocabulary_sizes": {f: len(v) for f, v in vocabulary.items()},
        "documents_with_text": sum(
            1 for info in meta.values()
            if len(str(info.get("title") or "") + str(info.get("abstract") or "")) > 40),
    }
