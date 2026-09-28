"""Evidence-first atom production for the formal Phase-3 P3A node.

The P3A claim pool is built from *served* material records.  Before those
records may feed any claim producer, each one has to become a canonical evidence
atom whose span, source version, owner row, scope receipt and domain contract can
all be re-verified from this generation's own objects.

This module is deliberately deterministic: it never calls a model, never reads the
R13-A offline fixture pointer, and never invents a value.  It can only copy an
anchor out of the served chunk text, or record that a field is not reported.  A
missing owner, a stale document, a foreign scope receipt or a value that is not
present in its own slice becomes a structured rejection instead of an atom.

Downstream contract: an evidence atom is *never* authorable on its own.
authorable stays False and binding_required stays True for every atom; only atoms
whose claim_input_status is eligible_for_claim_audit may be offered to the
atomic-claim producer, and even those still need a resolved claim binding before
they can reach an author.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import contracts
from . import evidence_atoms as EA
from .source_resolver import DocumentResolver

ATOM_PRODUCER_ID = "optomind_research.runtime.upgrade3.evidence_first"
ATOM_PRODUCER_FUNCTION = "produce_evidence_atoms"

MANIFEST_SCHEMA = "optomind.upgrade3.evidence_atom_manifest.v1"
PRODUCER_REJECTION_SCHEMA = "optomind.upgrade3.evidence_atom_producer_rejection.v1"
ELIGIBLE_SCHEMA = "optomind.upgrade3.evidence_atom_eligible.v1"

ATOMS_FILENAME = "EVIDENCE_ATOMS.jsonl"
REJECTIONS_FILENAME = "EVIDENCE_ATOM_REJECTIONS.jsonl"
MANIFEST_FILENAME = "EVIDENCE_ATOM_MANIFEST.json"

DEFAULT_ATOM_RECORD_LIMIT = 24

# Scope verdicts that may never become writable evidence.  out_of_scope is a hard
# domain-boundary failure; uncertain and unreviewed are absence of a decision,
# and absence of a decision is not permission.
NON_ELIGIBLE_SCOPE = {
    "out_of_scope": "scope_out_of_domain",
    "uncertain": "scope_undetermined",
    "unreviewed": "scope_not_reviewed",
    "": "scope_missing",
}

# Content depths that cannot carry a citable span.
NON_ELIGIBLE_DEPTH = {
    "metadata": "content_depth_metadata_only",
    "abstract": "content_depth_abstract_only",
    "": "content_depth_missing",
}

_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_RE = re.compile(r"\d")
_CONDITION_RE = re.compile(
    r"\b(under|at|with|using|within|below|above|across|per|during|after|before|"
    r"condition(?:s|ed)?|measured|operating|trained|based on)\b",
    re.IGNORECASE,
)
_METHOD_RE = re.compile(
    r"\b(?:we|this (?:work|paper|study)|the (?:authors|study|system|network|model))\b"
    r"[^.]{0,160}?\b(?:propose|present|introduce|demonstrate|report|develop|design|"
    r"train|employ|use|implement|fabricate|simulate|measure|perform|build)\w*\b",
    re.IGNORECASE,
)
# A limitation is stated in the literature's own words, and the first vocabulary
# here only knew the word "limitation".  Material that says a system "remains a
# major obstacle", reports an "accuracy reduction", or finds a "discrepancy
# between simulation and measurement" is stating a limitation and was read as
# stating nothing, so a controversy role could never be carried by the very
# material that argues one.  Generic connectives (however, despite) are
# deliberately absent: they mark contrast, not a limitation, and would make
# almost any paragraph a limitation.  "mean square error" is a loss function,
# not a defect, which is why bare "error" is absent too.
_LIMITATION_RE = re.compile(
    r"\b(?:limitation|limitations|obstacle|obstacles|barrier|barriers|"
    r"bottleneck|bottlenecks|shortcoming|shortcomings|drawback|drawbacks|"
    r"inabilit(?:y|ies)|unable to|trade-?offs?|"
    r"degrad(?:e|es|ed|ing)|degradation|degradations|"
    r"reduction|reductions|sensitivity|sensitivities|sensitive to|"
    r"susceptible to|toleran(?:ce|t)|misalign(?:ment|ments|ed)|"
    r"mismatch|mismatches|mismatched|deviat(?:e|es|ed|ion|ions)|"
    r"inaccura(?:cy|cies|te)|discrepanc(?:y|ies)|disagree(?:s|ment)?|"
    r"contradict(?:s|ion|ions|ory)?|inconsisten(?:t|cy|cies)|"
    r"conflicting|fail(?:s|ed|ure|ures)?|not robust|"
    r"unsolved|unresolved|remains? (?:un)?clear|future work|"
    r"not (?:yet )?(?:possible|demonstrated|shown)|only|restricted to|cannot|"
    r"does not)\b",
    re.IGNORECASE,
)

MIN_ANCHOR_CHARS = 24


class EvidenceFirstError(RuntimeError):
    """Raised when the evidence-first producer cannot produce a trustworthy manifest."""


def _canonical(value: Any) -> str:
    return contracts.canonical_json(value)


def _sha_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _fold_for_identity(value: Any) -> str:
    """Fold a text the way the canonical asset graph folds served material.

    The graph applies compatibility normalisation (so the "fi" ligature becomes
    two characters) and collapses whitespace.  Identity comparisons between a
    served record and its owner row must use the same folding; the raw bytes are
    still what get hashed as the owner text.
    """

    return EA._normalise_for_binding(str(value or ""))


def _clean(value: Any) -> str:
    """Collapse every whitespace run the way the served material records do.

    The canonical asset graph folds newlines as well as horizontal whitespace, so
    an owner row and the served record that quotes it must be compared under the
    same folding or an identical chunk would look drifted.  Only whitespace is
    touched: the characters that carry the evidence are never rewritten.
    """

    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).strip()


def _norm_scope(value: Any) -> str:
    return _text(value).casefold()


def _norm_depth(value: Any) -> str:
    return EA._normalise_source_depth(value)


def producer_code_hashes() -> dict[str, str]:
    """Hash the producer implementation so a code change invalidates the node."""

    hashes: dict[str, str] = {}
    for module in (EA,):
        path = Path(getattr(module, "__file__", "") or "")
        if path.is_file():
            hashes[module.__name__] = _sha_bytes(path.read_bytes())
    path = Path(__file__)
    if path.is_file():
        hashes[__name__] = _sha_bytes(path.read_bytes())
    return hashes


# --------------------------------------------------------------------------- #
# working-set selection
# --------------------------------------------------------------------------- #

def select_atom_working_set(
    records: Iterable[Mapping[str, Any]],
    *,
    resolver: DocumentResolver | None = None,
    source_roots: Iterable[str | os.PathLike[str]] = (),
    recorded_receipts: Mapping[str, Mapping[str, Any]] | None = None,
    scope_index: Mapping[str, Mapping[str, Any]] | None = None,
    limit: int = DEFAULT_ATOM_RECORD_LIMIT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the served material records into atom inputs and hard rejections.

    Selection is deterministic.  In-scope records are kept in input order,
    duplicate chunk ids are dropped with a receipt, and - when a resolver is
    supplied - the bounded budget is spent on records whose document version can
    actually be verified, because a record without a local source can never become
    a canonical span.  Nothing is filtered out silently: every skipped record
    yields a structured rejection row.
    """

    selected, rejections, _documents, _lookups = _select_working_set_with_documents(
        records,
        resolver=resolver,
        source_roots=source_roots,
        recorded_receipts=recorded_receipts,
        scope_index=scope_index,
        limit=limit,
    )
    return selected, rejections


def _select_working_set_with_documents(
    records: Iterable[Mapping[str, Any]],
    *,
    resolver: DocumentResolver | None = None,
    source_roots: Iterable[str | os.PathLike[str]] = (),
    recorded_receipts: Mapping[str, Mapping[str, Any]] | None = None,
    scope_index: Mapping[str, Mapping[str, Any]] | None = None,
    limit: int = DEFAULT_ATOM_RECORD_LIMIT,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[int, Mapping[str, Any]],
    list[dict[str, Any]],
]:
    """Select the bounded working set and return the resolution evidence."""

    kept, rejections = _eligible_scope_records(records, scope_index=scope_index)
    cap = max(0, int(limit or 0))
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    documents: dict[int, Mapping[str, Any]] = {}
    lookups: dict[int, dict[str, Any]] = {}
    if resolver is not None:
        for record in kept:
            document, evidence = resolve_record_document(
                record,
                resolver=resolver,
                source_roots=source_roots,
                recorded_receipts=recorded_receipts,
            )
            lookups[id(record)] = {
                "chunk_id": _text(record.get("chunk_id")),
                "paper_id": _text(record.get("paper_id")),
                **dict(evidence),
            }
            if isinstance(document, Mapping):
                documents[id(record)] = document
                resolved.append(record)
            else:
                unresolved.append(record)
        ordered = [*resolved, *unresolved]
    else:
        ordered = kept
    selected = ordered[:cap]
    selected_ids = {id(record) for record in selected}
    for record in ordered:
        if id(record) in selected_ids:
            continue
        rejections.append(_producer_rejection(
            position=kept.index(record),
            record=record,
            reason="working_set_budget_exhausted",
            claim_input_status="inventory_only",
        ))
    rejections.sort(key=lambda row: (row["position"], row["reason"]))
    return (
        selected,
        rejections,
        {key: value for key, value in documents.items() if key in selected_ids},
        [lookups[id(record)] for record in selected if id(record) in lookups],
    )


def effective_scope_fit(
    record: Mapping[str, Any],
    scope_index: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """The domain verdict that decides this record, receipt first.

    A served record carries the label it was acquired under.  That label is a
    plan, not a decision: the served library labelled a hydrology paper "direct"
    while the real judge refused it, and it labels freshly acquired material
    "unreviewed" until the judge has spoken.  When the run holds a real receipt
    for the record's document, the receipt is the verdict; otherwise the label
    stands and an unknown label stays non-eligible.
    """

    index = scope_index or {}
    document_id = _document_id_for_record(record)
    receipt = index.get(document_id) if document_id else None
    if isinstance(receipt, Mapping):
        verdict = _text(receipt.get("verdict") or receipt.get("scope_verdict"))
        if verdict:
            return _canonical_scope_verdict(verdict)
    return _norm_scope(record.get("scope_fit"))


def _scope_candidate_records(
    records: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Records that could become evidence if only the domain verdict existed.

    Identical to the eligible set except for the scope test: an unknown verdict
    is a candidate, an explicit refusal is not.  This is the only way a
    freshly acquired document can ever reach the judge, because eligibility
    itself requires the verdict the judge has not given yet.
    """

    kept: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            continue
        record = dict(raw)
        chunk_id = _text(record.get("chunk_id"))
        paper_id = _text(record.get("paper_id"))
        if not chunk_id or not paper_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        scope = _norm_scope(record.get("scope_fit"))
        if scope in NON_ELIGIBLE_SCOPE and scope != "" and scope != "unreviewed" \
                and scope != "uncertain":
            rejections.append(_producer_rejection(
                position=position, record=record,
                reason=NON_ELIGIBLE_SCOPE[scope],
                claim_input_status="rejected_scope",
                detail={"scope_fit": scope},
            ))
            continue
        depth = _norm_depth(record.get("content_depth"))
        if depth in NON_ELIGIBLE_DEPTH:
            continue
        if len(_clean(record.get("normalized_text") or record.get("text"))) \
                < MIN_ANCHOR_CHARS:
            continue
        kept.append(record)
    return kept, rejections


def select_scope_judgement_candidates(
    records: Iterable[Mapping[str, Any]],
    *,
    resolver: DocumentResolver | None = None,
    source_roots: Iterable[str | os.PathLike[str]] = (),
    recorded_receipts: Mapping[str, Mapping[str, Any]] | None = None,
    scope_index: Mapping[str, Mapping[str, Any]] | None = None,
    limit: int = DEFAULT_ATOM_RECORD_LIMIT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The documents the scope judge should be asked about, bounded.

    Only records whose domain verdict is genuinely unknown are returned: a
    document that already has a receipt has been judged, and one the material
    itself calls out_of_scope is not re-litigated by a helper.
    """

    index = scope_index or {}
    candidates, rejections = _scope_candidate_records(records)
    wanted: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    for record in candidates:
        document_id = _document_id_for_record(record)
        if not document_id or document_id in index or document_id in seen_documents:
            continue
        seen_documents.add(document_id)
        wanted.append(record)
    if resolver is None:
        return wanted[: max(0, int(limit or 0))], rejections
    resolved: list[dict[str, Any]] = []
    for record in wanted:
        document, _evidence = resolve_record_document(
            record, resolver=resolver, source_roots=source_roots,
            recorded_receipts=recorded_receipts,
        )
        if isinstance(document, Mapping):
            resolved.append(record)
    return resolved[: max(0, int(limit or 0))], rejections


def _eligible_scope_records(
    records: Iterable[Mapping[str, Any]],
    *,
    scope_index: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only records whose scope and depth can support a citable span.

    Order is preserved and every dropped record produces a structured rejection,
    so the atom producer never filters the served pool silently.  The scope test
    reads the run's own receipt when one exists, so a judgement the run really
    made can admit material its acquisition label could not.
    """

    kept: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            rejections.append(_producer_rejection(
                position=position, record={}, reason="record_not_an_object",
                claim_input_status="rejected_contract",
            ))
            continue
        record = dict(raw)
        chunk_id = _text(record.get("chunk_id"))
        paper_id = _text(record.get("paper_id"))
        if not chunk_id or not paper_id:
            rejections.append(_producer_rejection(
                position=position, record=record,
                reason="record_without_chunk_id" if not chunk_id else "record_without_paper_id",
                claim_input_status="rejected_identity",
            ))
            continue
        if chunk_id in seen:
            rejections.append(_producer_rejection(
                position=position, record=record,
                reason="duplicate_chunk_id_in_working_set",
                claim_input_status="rejected_contract",
            ))
            continue
        seen.add(chunk_id)
        scope = effective_scope_fit(record, scope_index)
        if scope in NON_ELIGIBLE_SCOPE:
            rejections.append(_producer_rejection(
                position=position, record=record,
                reason=NON_ELIGIBLE_SCOPE[scope],
                claim_input_status="rejected_scope",
                detail={"scope_fit": scope,
                        "declared_scope_fit": _norm_scope(record.get("scope_fit"))},
            ))
            continue
        depth = _norm_depth(record.get("content_depth"))
        if depth in NON_ELIGIBLE_DEPTH:
            rejections.append(_producer_rejection(
                position=position, record=record,
                reason=NON_ELIGIBLE_DEPTH[depth],
                claim_input_status="rejected_permission",
                detail={"content_depth": depth},
            ))
            continue
        if len(_clean(record.get("normalized_text") or record.get("text"))) < MIN_ANCHOR_CHARS:
            rejections.append(_producer_rejection(
                position=position, record=record,
                reason="served_text_too_short_for_anchor",
                claim_input_status="rejected_contract",
            ))
            continue
        kept.append(record)
    return kept, rejections


def _producer_rejection(
    *,
    position: int,
    record: Mapping[str, Any],
    reason: str,
    claim_input_status: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": PRODUCER_REJECTION_SCHEMA,
        "stage": "evidence_first.working_set",
        "position": int(position),
        "document_id": _text(record.get("document_id")),
        "paper_id": _text(record.get("paper_id")),
        "chunk_id": _text(record.get("chunk_id")),
        "experiment_id": _text(record.get("experiment_id")),
        "field_name": "",
        "reason": reason,
        "claim_input_status": claim_input_status,
        "permission_ceiling": "discovery_only",
        "authorable": False,
        "detail": dict(detail or {}),
    }


# --------------------------------------------------------------------------- #
# deterministic cards
# --------------------------------------------------------------------------- #

# Anchors that are structurally incapable of carrying an assertion.  A reference
# list, a running head or a figure caption can contain numbers without stating a
# fact, so they are never selected as an atom field.
_REFERENCE_HEADER_RE = re.compile(r"(?im)^\s*(references|bibliography|works cited)\s*$")
_CITATION_DENSE_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}\b\s*[;,.)]?\s*){2,}|\bet al\.,?\s*(?:19|20)\d{2}\b"
)
_FIGURE_CAPTION_RE = re.compile(r"(?i)^\s*(fig(?:ure)?\.?|table|scheme)\s*\d+")
# A single bibliography ENTRY is not dense with years, so the dense-citation test
# alone let one reference through as a "result".  These structural markers catch
# the entry itself: a DOI/URL, a volume(issue), pages pattern, or an author list.
_REFERENCE_ENTRY_RE = re.compile(
    # a resolvable locator
    r"(?:https?://|doi\.org/|\bdoi:\s*10\.)"
    # journal year, volume (issue),
    r"|\b(?:19|20)\d{2}\s*,\s*\d{1,4}\s*\(\s*\d{1,4}\s*\)\s*,"
    # volume (issue), page range
    r"|\b\d{1,4}\s*\(\s*\d{1,4}\s*\)\s*,\s*[A-Za-z]?\d{1,5}\s*[-\u2013]\s*[A-Za-z]?\d{1,5}"
    # author run, including hyphenated initials: "Li, H.-Y.; Zhao, H.-T.; ..."
    r"|(?:[A-Z][A-Za-z'\u2019-]+,\s*[A-Z]\.(?:-[A-Z]\.)*\s*;\s*){2,}"
    # proceedings: "In Proceedings of ...; 2019; pp 1-8."
    r"|\b(?:In\s+)?Proceedings\s+of\b"
    r"|\b(?:Symposium|Conference|Workshop|Congress)\s+on\b"
    r"|;\s*SPIE\b"
    r"|;\s*(?:19|20)\d{2}\s*;\s*pp?\.?\s*\d"
    # a journal, a year and a bare article number with no volume
    r"|\b[A-Z][A-Za-z&\s]{3,40}\s(?:19|20)\d{2},\s*\d{5,}\."
    # a figure credit: "Adapted from Ref 138, ..."
    r"|\bAdapted\s+from\s+Ref\b"
    # a numbered bibliography entry: "(85) Kilic, V.; Tran, T."
    r"|^\s*\(\s*\d{1,4}\s*\)\s+\S"
    # a reference-list heading that shares its line with the first entry
    r"|^\s*(?i:references|bibliography|works\s+cited)\b"
    # two authors in bibliography order: "Kilic, V.; Tran, T."
    r"|(?:[A-Z][A-Za-z'\u2019-]+,\s*[A-Z]\.(?:-[A-Z]\.)*\s*;\s*"
    r"[A-Z][A-Za-z'\u2019-]+,\s*[A-Z]\.)"
    # a book: "Introduction to Fourier Optics; Roberts and Company Publishers, 2005."
    r"|;\s*[^;]{3,60}(?:Publisher|Press|Wiley|Springer|Elsevier|McGraw)"
    r"[^;]{0,40},\s*(?:19|20)\d{2}\."
)


_LOCATOR_RE = re.compile(r"https?://|doi\.org/")
_CITATION_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _looks_like_a_reference_list(text: str) -> bool:
    """True when a whole chunk is a bibliography rather than body text.

    Measured on the real R6 material: body chunks never exceed 3 resolvable
    locators and 7 citation years, while reference-list chunks reach 15 and 28.
    A chunk that crosses those bounds is a bibliography, and no line inside it is
    an assertion - including a bare paper title, which carries no marker of its
    own and cannot be caught by any per-string rule.
    """

    body = text or ""
    if len(body) < 300:
        return False
    if len(_LOCATOR_RE.findall(body)) >= 4:
        return True
    return len(_CITATION_YEAR_RE.findall(body)) >= 8


def _strip_reference_tail(text: str) -> str:
    """Drop a reference list that a PDF extraction appended to the body."""

    match = _REFERENCE_HEADER_RE.search(text or "")
    if match and match.start() > 0:
        return text[: match.start()]
    return text


def _looks_like_reference_furniture(anchor: str) -> bool:
    """True when an anchor cannot carry an assertion.

    The test is structural, not topical: a paragraph that is dense with citation
    years or volume/page patterns is a bibliography, and a caption is a label.
    Both are rejected so the claim chain can never quote a reference list as a
    result.
    """

    text = _clean(anchor)
    if not text:
        return True
    if _FIGURE_CAPTION_RE.match(text):
        return True
    if _CITATION_DENSE_RE.search(text):
        return True
    if _REFERENCE_ENTRY_RE.search(text):
        return True
    # a references heading with most of the paragraph after it
    match = _REFERENCE_HEADER_RE.search(anchor or "")
    if match and (len(anchor) - match.end()) > len(anchor) * 0.5:
        return True
    digits = sum(1 for character in text if character.isdigit())
    if len(text) >= 80 and digits / max(1, len(text)) > 0.14:
        return True
    return False


def _paragraphs(text: str) -> list[str]:
    blocks = [_clean(block) for block in _PARA_SPLIT_RE.split(text or "")]
    return [block for block in blocks if block]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(_clean(text)) if part.strip()]


def _anchor(paragraphs: Sequence[str], predicate) -> str:
    """Return the first paragraph, else the first sentence, satisfying a rule.

    This is a *selection* rule, not an extraction: the returned string is always a
    verbatim slice of the served text.  Structural furniture - reference lists,
    running heads and captions - is skipped because it can carry numbers without
    asserting a fact.
    """

    for block in paragraphs:
        if predicate(block) and not _looks_like_reference_furniture(block):
            return block
    for block in paragraphs:
        for sentence in _sentences(block):
            if predicate(sentence) and not _looks_like_reference_furniture(sentence):
                return sentence
    return ""


def _select_anchors(text: str) -> dict[str, str]:
    if _looks_like_a_reference_list(text):
        # The whole chunk is a bibliography: it has no assertion to anchor, and
        # returning nothing is the only honest answer.  Downstream this becomes a
        # rejection, never an empty claim.
        return {"method": "", "results": "", "conditions": "", "limitations": ""}
    paragraphs = _paragraphs(_strip_reference_tail(text))
    # Paragraphs are allocated to fields by SPECIFICITY, not by a fixed sequence.
    # The loose rules (any digit; any condition word) used to run first and claim
    # the paragraph before the tight ones were even considered, so a sentence
    # that stated a limitation and happened to say "with" or "below" was taken
    # by conditions and the limitation was then discarded as a duplicate - which
    # is why a controversy role could never be carried by material that argues
    # one.  The tightest rule now claims its paragraph first and the loose rules
    # take what is left, so no field is lost to a weaker claim on the same text.
    method = _anchor(
        paragraphs,
        lambda block: len(block) >= MIN_ANCHOR_CHARS and bool(_METHOD_RE.search(block)),
    )
    limitations = _anchor(
        paragraphs,
        lambda block: len(block) >= MIN_ANCHOR_CHARS
        and bool(_LIMITATION_RE.search(block))
        and block != method,
    )
    taken = {block for block in (method, limitations) if block}
    results = _anchor(
        paragraphs,
        lambda block: len(block) >= MIN_ANCHOR_CHARS
        and bool(_NUMBER_RE.search(block))
        and block not in taken,
    )
    # The conditions anchor used to be chosen by its own loop, without the
    # furniture filter the other three anchors go through.  A two-column
    # extraction interleaves the bibliography with the body and often has no
    # usable heading, so a reference paragraph full of "at 1550 nm" and numbers
    # satisfied the condition and number tests and became the anchor - from
    # there it travelled into the claims as a statement.  Every anchor now goes
    # through exactly one filter.
    conditions = ""
    for block in paragraphs:
        if (
            len(block) >= MIN_ANCHOR_CHARS
            and _CONDITION_RE.search(block)
            and _NUMBER_RE.search(block)
            and block not in taken
            and block != results
            and not _looks_like_reference_furniture(block)
        ):
            conditions = block
            break
    if not conditions:
        for block in paragraphs:
            if (
                len(block) >= MIN_ANCHOR_CHARS
                and _CONDITION_RE.search(block)
                and block not in taken
                and block != results
                and not _looks_like_reference_furniture(block)
            ):
                conditions = block
                break
    return {
        "method": method,
        "results": results,
        "conditions": conditions,
        "limitations": limitations,
    }


def build_evidence_card(
    record: Mapping[str, Any],
    *,
    document_id: str,
    experiment_id: str,
    sim_or_experiment: str = "not_reported",
    experiment_level: str = "not_reported",
    document_version_id: str = "",
    document_raw_hash: str = "",
) -> dict[str, Any]:
    """Build one deterministic card from a served record's own text.

    A field is either a verbatim anchor of the served text or explicitly
    not_reported; a model-written summary is never substituted for the source.
    """

    text = str(record.get("normalized_text") or record.get("text") or "")
    anchors = _select_anchors(text)
    fields: dict[str, Any] = {}
    for name in EA.CARD_FIELDS:
        anchor = anchors.get(name, "")
        if anchor:
            fields[name] = {
                "status": "extracted",
                "value": anchor,
                "anchor_quote": anchor,
                "span_ids": [],
                "selection_policy": "deterministic_served_text_anchor",
            }
        else:
            fields[name] = {
                "status": "not_reported",
                "value": "",
                "anchor_quote": "",
                "span_ids": [],
                "selection_policy": "deterministic_served_text_anchor",
            }
    return {
        "schema_version": "optomind.upgrade3.evidence_card.v1",
        "card_id": "card:p3a:%s" % _sha_text(
            _canonical([document_id, experiment_id, _text(record.get("chunk_id"))])
        )[:24],
        "document_id": document_id,
        "paper_id": _text(record.get("paper_id")),
        "chunk_id": _text(record.get("chunk_id")),
        "version_id": _text(document_version_id),
        "version_raw_hash": _text(document_raw_hash),
        "experiment_id": experiment_id,
        "sim_or_experiment": sim_or_experiment,
        "experiment_level": experiment_level,
        "fields": fields,
        "card_source": {
            "producer": ATOM_PRODUCER_ID,
            "selection_policy": "deterministic_served_text_anchor",
            "record_position": record.get("record_position"),
        },
    }


# --------------------------------------------------------------------------- #
# identity from this generation's own objects
# --------------------------------------------------------------------------- #

def resolve_owner_chunk(
    resolver: DocumentResolver,
    *,
    chunk_id: str,
    paper_id: str,
) -> dict[str, Any]:
    """Return the registered KB row that actually owns chunk_id."""

    try:
        result = resolver.resolve_chunk(chunk_id, paper_id)
    except Exception as exc:  # pragma: no cover - defensive
        return {"resolved": False, "reason": "owner_lookup_failed:%s" % type(exc).__name__}
    if not isinstance(result, Mapping):
        return {"resolved": False, "reason": "owner_lookup_invalid"}
    return dict(result)


def _owner_identity_fields(
    record: Mapping[str, Any],
    *,
    resolver: DocumentResolver,
) -> tuple[dict[str, Any], str | None]:
    """Resolve the owner KB row and return identity fields plus a failure reason."""

    chunk_id = _text(record.get("chunk_id"))
    paper_id = _text(record.get("paper_id"))
    lookup = resolve_owner_chunk(resolver, chunk_id=chunk_id, paper_id=paper_id)
    if not lookup.get("resolved"):
        return {}, _text(lookup.get("reason")) or "owner_chunk_unresolved"
    owner_text = lookup.get("text")
    if not isinstance(owner_text, str) or not owner_text.strip():
        return {}, "owner_text_empty"
    # The served record is produced by the canonical asset graph, which folds
    # compatibility characters such as the "fi" ligature; the owner row still holds
    # the parser's raw bytes.  Comparing both sides under the same folding keeps an
    # identical chunk from being reported as drifted, while a genuinely different
    # chunk still fails closed.  The raw owner text is what gets hashed.
    served_text = _fold_for_identity(record.get("normalized_text") or record.get("text"))
    if _fold_for_identity(owner_text) != served_text:
        # A served preview that differs from the owner row cannot anchor a
        # canonical span: the writing chain would cite text the KB does not own.
        return {}, "owner_text_differs_from_served_text"
    return {
        "owner_kb": _text(lookup.get("kb")),
        "owner_kb_path": _text(lookup.get("source_path") or lookup.get("path")),
        "owner_source_path": _text(lookup.get("source_path") or lookup.get("path")),
        "owner_source_sha256": _text(lookup.get("source_sha256")),
        "owner_object_sha256": _text(lookup.get("object_sha256")),
        "owner_text_sha256": _text(lookup.get("text_sha256")),
    }, None


#: scope labels that report the absence of a decision rather than a decision
ABSENT_SCOPE_LABELS = ("", "unreviewed")

_SCOPE_STRICTNESS = {"out_of_scope": 0, "unreviewed": 1, "uncertain": 1,
                     "contextual": 2, "adjacent": 3, "direct": 4}


def fold_scope_verdict(receipt_verdict: Any, declared_label: Any) -> str:
    """The verdict that decides a record: the receipt, tightened by a real label.

    The P3A served record carries the section-level scope decision and a record
    may never be promoted by its own copy of the verdict, so a stricter declared
    label wins.  "Unreviewed" is not a stricter verdict -- it is the absence of
    one, and it is exactly what freshly acquired material is labelled with until
    the run's own judge has spoken.  Letting that label outrank the receipt meant
    a real direct judgement could never reach the material it had just admitted.
    """

    verdict = _norm_scope(receipt_verdict)
    label = _norm_scope(declared_label)
    if label in ABSENT_SCOPE_LABELS or not verdict:
        return verdict
    if label == verdict:
        return verdict
    if _SCOPE_STRICTNESS.get(label, 0) < _SCOPE_STRICTNESS.get(verdict, 0):
        return label
    return verdict


def build_identity_record(
    record: Mapping[str, Any],
    *,
    resolver: DocumentResolver,
    domain_contract: Mapping[str, Any],
    domain_contract_source: str,
    scope_entry: Mapping[str, Any] | None,
    scope_log_path: str,
    roles: Sequence[str],
    document_id: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Assemble the identity record the atom builder requires, or reject.

    Every receipt here comes from a real object of this generation: the frozen
    domain contract, the run's scope-decision log, the served material record and
    the registered owner database row.
    """

    rejections: list[dict[str, Any]] = []

    def reject(reason: str, status: str, detail: Mapping[str, Any] | None = None):
        rejections.append({
            "schema_version": PRODUCER_REJECTION_SCHEMA,
            "stage": "evidence_first.identity",
            "document_id": _text(record.get("document_id")),
            "paper_id": _text(record.get("paper_id")),
            "chunk_id": _text(record.get("chunk_id")),
            "experiment_id": _text(record.get("experiment_id")),
            "field_name": "",
            "reason": reason,
            "claim_input_status": status,
            "permission_ceiling": "discovery_only",
            "authorable": False,
            "detail": dict(detail or {}),
        })

    resolved_document_id = _text(document_id) or _document_id_for_record(record)
    if not resolved_document_id:
        reject("record_without_document_id", "rejected_identity")
        return None, rejections
    document_id = resolved_document_id

    if not isinstance(domain_contract, Mapping) or not domain_contract:
        reject("domain_contract_unavailable", "rejected_contract")
        return None, rejections
    try:
        contract_sha = _text(
            (domain_contract.get("envelope") or {}).get("content_sha256")
        )
    except Exception:
        contract_sha = ""
    if not contract_sha:
        contract_sha = _sha_text(_canonical(domain_contract))
    contract_path = Path(_text(domain_contract_source)).expanduser()
    if not contract_path.is_file():
        reject("domain_contract_source_file_missing", "rejected_contract",
               {"domain_contract_source": _text(domain_contract_source)})
        return None, rejections
    contract_file_sha = _sha_bytes(contract_path.read_bytes())

    if not isinstance(scope_entry, Mapping):
        reject("scope_receipt_not_found_for_document", "rejected_scope")
        return None, rejections
    scope_path = Path(_text(scope_log_path)).expanduser()
    if not scope_path.is_file():
        reject("scope_receipt_log_missing", "rejected_scope")
        return None, rejections
    scope_file_sha = _sha_bytes(scope_path.read_bytes())
    scope_object_sha = _sha_text(_canonical(scope_entry))

    raw_scope_verdict = _norm_scope(
        scope_entry.get("verdict") or scope_entry.get("scope_verdict")
    )
    scope_verdict = _canonical_scope_verdict(raw_scope_verdict)
    scope_verdict = fold_scope_verdict(scope_verdict, record.get("scope_fit"))
    if not scope_verdict:
        reject("scope_receipt_verdict_missing", "rejected_scope")
        return None, rejections

    owner_fields, owner_reason = _owner_identity_fields(record, resolver=resolver)
    if owner_reason:
        reject(owner_reason, "rejected_identity")
        return None, rejections

    depth = _norm_depth(record.get("content_depth"))
    allowed = scope_entry.get("allowed_uses")
    permission_hint = scope_entry.get("permission") or scope_entry.get("allowed_uses")
    identity = {
        "document_id": document_id,
        "paper_id": _text(record.get("paper_id")),
        "chunk_id": _text(record.get("chunk_id")),
        "scope_verdict": scope_verdict,
        "source_depth": depth,
        "roles": list(roles or ()),
        "scope_receipt_hash": scope_object_sha,
        "scope_receipt_object_sha256": scope_object_sha,
        "scope_receipt_source": str(scope_path),
        "scope_receipt_source_path": str(scope_path),
        "scope_receipt_source_sha256": scope_file_sha,
        "scope_receipt_raw_verdict": raw_scope_verdict,
        "scope_receipt_permission": (
            _text(permission_hint)
            if not isinstance(permission_hint, (list, tuple))
            else ",".join(str(item) for item in permission_hint)
        ),
        "scope_receipt_source_depth": _norm_depth(
            scope_entry.get("source_depth") or depth
        ),
        "scope_receipt_paper_id": _text(scope_entry.get("paper_id")),
        "scope_receipt_chunk_id": _text(scope_entry.get("chunk_id")),
        "scope_receipt_allowed_uses": (
            list(allowed) if isinstance(allowed, (list, tuple)) else []
        ),
        "domain_contract_hash": contract_sha,
        "domain_contract_object_sha256": contract_sha,
        "domain_contract_source": _text(domain_contract_source),
        "domain_contract_source_path": str(contract_path),
        "domain_contract_source_sha256": contract_file_sha,
        # Reserved identity keys (chunk_id / paper_id / scope_verdict / ...) must
        # not be overridden from here: the atom builder validates that provenance
        # can never shadow an identity field, so only non-reserved hints travel.
        "role_provenance": {
            "source": "p3a_served_material_record",
            "served_scope_fit": _norm_scope(record.get("scope_fit")),
            "served_content_depth": depth,
            "discovery_route": _text(record.get("discovery_route")),
            "materialization_route": _text(record.get("materialization_route")),
            "record_limit_root": "evidence_first.select_atom_working_set",
        },
    }
    identity.update(owner_fields)
    return identity, rejections


def load_scope_receipts(
    scope_log_path: str | os.PathLike[str],
) -> dict[str, dict[str, Any]]:
    """Index the run's scope-decision log by document id (last decision wins)."""

    path = Path(scope_log_path)
    index: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return index
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        document_id = _text(row.get("document_id") or row.get("paper_id"))
        if not document_id:
            continue
        index[document_id] = dict(row)
    return index


# --------------------------------------------------------------------------- #
# production entry
# --------------------------------------------------------------------------- #

def _normalise_doi(value: Any) -> str:
    """Normalise a DOI or a DOI-looking filename for identity comparison."""

    text = _text(value).casefold().strip()
    if not text:
        return ""
    text = text.replace("https://doi.org/", "").replace("http://doi.org/", "")
    text = text.replace("doi:", "")
    for suffix in (".pdf", ".xml", ".html", ".txt"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _canonical_scope_verdict(value: Any) -> str:
    """Map the scope-decision vocabulary onto the canonical scope verdicts.

    The scope producer reports acquisition verdicts (reviewed_direct, adjacent,
    background, rejected, uncertain, candidate) while the contract vocabulary is
    direct / adjacent / background / out_of_scope / uncertain.  An unmapped or
    missing verdict becomes uncertain, which is not permission.
    """

    from .wiring import VERDICT_TO_SCOPE_FIT

    raw = _norm_scope(value)
    if not raw:
        return "uncertain"
    return VERDICT_TO_SCOPE_FIT.get(raw, "uncertain")


def _record_doi(record: Mapping[str, Any]) -> str:
    """Return the DOI-shaped identity of a served record, if it carries one.

    The explicit doi field is preferred over a scheme-prefixed document id so the
    value stays comparable with a downloaded file name.
    """

    locator = record.get("source_locator")
    if isinstance(locator, Mapping):
        for key in ("doi", "DOI", "document_id"):
            value = _text(locator.get(key))
            if value:
                return value
    for key in ("doi", "DOI", "document_id"):
        value = _text(record.get(key))
        if value:
            return value
    return ""


def _document_id_for_record(record: Mapping[str, Any]) -> str:
    """Return the identity a document version is registered under.

    An explicit document identity wins; otherwise the record's paper identity is
    used, because paper identity is the stable join key between the served
    material record, the scope-decision log and the document version.  Nothing is
    rewritten to make a lookup succeed.
    """

    declared = _text(record.get("document_id"))
    if declared:
        return declared
    locator = record.get("source_locator")
    if isinstance(locator, Mapping):
        declared = _text(locator.get("document_id"))
        if declared:
            return declared
    return _text(record.get("paper_id"))


def resolve_record_document(
    record: Mapping[str, Any],
    *,
    resolver: DocumentResolver,
    source_roots: Iterable[str | os.PathLike[str]] = (),
    recorded_receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve the local document version that backs one served record.

    The document is looked up by identity, never fabricated: an explicit receipt
    wins, otherwise the DOI recorded by the served record plus the owner row is
    matched against the run's own downloaded documents under source_roots.  A
    record whose document cannot be located returns an unresolved receipt; the
    caller turns that into a rejection, not into a synthetic span.
    """

    document_id = _document_id_for_record(record)
    evidence = {"document_id": document_id, "resolution": "unresolved"}
    if not document_id:
        return None, dict(evidence, reason="record_without_paper_id")
    # A document that is already registered is never re-registered here.  If its
    # bytes changed, the version identity would silently advance mid-attempt and
    # the atom would be written against a document the run never reviewed; a
    # drifted source must be an explicit refusal instead.
    if _text(record.get("document_id")) and document_id in getattr(
        resolver, "documents", {}
    ):
        current = resolver.verify_document_current(document_id)
        status = _text(current.get("status"))
        if status == "current":
            document = current.get("document")
            if isinstance(document, Mapping):
                return dict(document), dict(
                    evidence,
                    resolution="already_registered_current",
                    source_path=_text(document.get("source_path")),
                    parser=_text(document.get("parser")),
                    raw_hash=_text(document.get("raw_hash")),
                )
        return None, dict(
            evidence,
            resolution="registered_document_not_current",
            reason="document_not_current:%s" % (status or "unknown"),
            detail=_text(current.get("reason")),
        )

    recorded = (recorded_receipts or {}).get(document_id)
    if isinstance(recorded, Mapping) and _text(recorded.get("source_path")):
        path = Path(_text(recorded["source_path"])).expanduser()
        if path.is_file():
            receipt = resolver.register_document(
                document_id=document_id,
                source_path=str(path),
                legal_basis=_text(recorded.get("legal_basis")) or "run_local_document",
                parser=_text(recorded.get("parser")) or "pdftotext-layout",
            )
            if _text(receipt.get("body_status")) in {"complete", "partial"}:
                return receipt, dict(
                    evidence,
                    resolution="recorded_receipt",
                    source_path=str(path),
                    parser=_text(receipt.get("parser")),
                    raw_hash=_text(receipt.get("raw_hash")),
                )
            return None, dict(
                evidence,
                resolution="recorded_receipt_unusable",
                reason=_text(receipt.get("reason")) or "document_body_status_unusable",
                source_path=str(path),
            )
    # Held text: the generation may have read a paper without downloading its
    # PDF.  The record says which bytes back it, so the bytes are verified and
    # registered with a parser that is explicitly not a PDF parser -- a citation
    # from held text must never look like a citation from a publisher file.
    from . import material_documents as _material_documents

    held, held_reason = _material_documents.resolve_declared_document(
        record, source_roots=source_roots
    )
    if held is not None:
        receipt = resolver.register_document(
            document_id=document_id,
            source_path=str(held["path"]),
            legal_basis=_text(held.get("legal_basis"))
            or "central_material_cache_unit_text",
            parser=_text(held.get("parser"))
            or _material_documents.MATERIAL_DOCUMENT_PARSER,
        )
        if _text(receipt.get("body_status")) in {"complete", "partial"}:
            return receipt, dict(
                evidence,
                resolution="material_unit_document",
                path=str(held["path"]),
                source_path=str(held["path"]),
                parser=_text(receipt.get("parser")),
                raw_hash=_text(receipt.get("raw_hash")),
                source_kind=_text(held.get("source_kind")),
                chunk_id=_text(held.get("chunk_id")),
                held_text_locator=dict(held.get("locator") or {}),
            )
        return None, dict(
            evidence,
            resolution="material_unit_document_unusable",
            reason=_text(receipt.get("reason")) or "document_body_status_unusable",
            path=str(held["path"]),
        )
    if _material_documents.declared_document(record) is not None:
        return None, dict(
            evidence,
            resolution="declared_document_refused",
            reason=held_reason,
        )

    candidate_dois = [
        _normalise_doi(_record_doi(record)),
        _normalise_doi(
            resolve_owner_chunk(
                resolver,
                chunk_id=_text(record.get("chunk_id")),
                paper_id=_text(record.get("paper_id")),
            ).get("owner_doi")
        ),
    ]
    candidate_dois = [value for value in candidate_dois if value]
    if not candidate_dois:
        return None, dict(evidence, reason="document_identity_unknown")
    matches: list[Path] = []
    for root in source_roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.pdf")):
            if "downloads" not in {part.casefold() for part in path.parts}:
                continue
            if _normalise_doi(path.name) in candidate_dois:
                matches.append(path)
    if len(matches) != 1:
        return None, dict(
            evidence,
            reason="document_lookup_not_unique" if matches else "document_source_missing",
            matches=[str(item) for item in matches[:8]],
        )
    path = matches[0]
    receipt = resolver.register_document(
        document_id=document_id,
        source_path=str(path),
        legal_basis="run_local_document_lookup",
        parser="pdftotext-layout",
    )
    if _text(receipt.get("body_status")) not in {"complete", "partial"}:
        return None, dict(
            evidence,
            resolution="doi_lookup_unusable",
            reason=_text(receipt.get("reason")) or "document_body_status_unusable",
            source_path=str(path),
        )
    return receipt, dict(
        evidence,
        resolution="doi_lookup",
        source_path=str(path),
        parser=_text(receipt.get("parser")),
        raw_hash=_text(receipt.get("raw_hash")),
    )



def ensure_scope_receipts(
    records: Iterable[Mapping[str, Any]],
    *,
    resolver: DocumentResolver,
    domain_contract: Mapping[str, Any],
    scope_decisions_path: str | os.PathLike[str],
    source_roots: Iterable[str | os.PathLike[str]] = (),
    judge: Any,
    max_paper_chars: int = 6000,
    record_limit: int = DEFAULT_ATOM_RECORD_LIMIT,
) -> dict[str, Any]:
    """Make sure every working-set document has a real scope receipt.

    The scope producer only speaks about documents it has actually seen.  When a
    run predates the scope wiring - or a section introduced a document after the
    stage ran - the receipt is missing, and a missing receipt must never be read as
    permission.  This helper performs the bounded, real scope judgement for exactly
    the documents the atom working set is about to use, through the same
    :class:`ScopeAdmission` entry the KB stage uses, so the atom producer can
    consume a receipt that its own generation really produced.

    A ``judge`` that is missing or fails is reported, never substituted: the
    affected documents simply have no receipt and their atoms are rejected.
    """

    if judge is None:
        return {"status": "no_scope_judge", "judged": 0, "documents": []}


    path = Path(scope_decisions_path)
    existing = load_scope_receipts(path)
    # The candidates are chosen by REACHABILITY, not by eligibility: a record
    # labelled unreviewed is exactly the record the judge has not spoken about,
    # and selecting from the eligible set alone meant it never could.
    working_set, _ = select_scope_judgement_candidates(
        records,
        resolver=resolver,
        source_roots=source_roots,
        scope_index=existing,
        limit=record_limit,
    )

    ordered: dict[str, dict[str, Any]] = {}
    for record in working_set:
        document_id = _document_id_for_record(record)
        if not document_id or document_id in existing or document_id in ordered:
            continue
        ordered[document_id] = dict(record)

    appended: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for document_id, record in ordered.items():
        document, evidence = resolve_record_document(
            record, resolver=resolver, source_roots=source_roots
        )
        if not isinstance(document, Mapping):
            failures.append({"document_id": document_id,
                             "reason": _text(evidence.get("reason"))})
            continue
        try:
            paper_text = resolver.canonical_text(document_id)[: max(200, int(max_paper_chars))]
        except Exception as exc:  # pragma: no cover - defensive
            failures.append({"document_id": document_id,
                             "reason": "canonical_text_failed:%s" % type(exc).__name__})
            continue
        if not _text(paper_text):
            failures.append({"document_id": document_id, "reason": "document_text_empty"})
            continue
        paper = {
            "document_id": document_id,
            "paper_id": _text(record.get("paper_id")),
            "title": _text(record.get("paper_title") or record.get("title")),
            "text": paper_text,
        }
        logical_call_id = "scope_receipt:%s" % _sha_text(document_id)[:16]
        try:
            decision = judge.decide(paper, logical_call_id)
        except Exception as exc:
            failures.append({"document_id": document_id,
                             "reason": "scope_judge_failed:%s" % type(exc).__name__})
            continue
        if not isinstance(decision, Mapping):
            failures.append({"document_id": document_id, "reason": "scope_judge_invalid"})
            continue
        row = dict(decision)
        row.setdefault("document_id", document_id)
        row["paper_id"] = _text(record.get("paper_id"))
        row["source_depth"] = _norm_depth(
            _text(document.get("body_status")) == "complete" and "fulltext" or "partial_fulltext"
        )
        row["source_receipt"] = {
            "source_path": _text(document.get("source_path")),
            "raw_hash": _text(document.get("raw_hash")),
            "version_id": _text(document.get("version_id")),
            "parser": _text(document.get("parser")),
        }
        _append_scope_receipt(path, row)
        existing[document_id] = row
        appended.append({"document_id": document_id,
                         "verdict": _text(row.get("verdict")),
                         "reused": bool(row.get("reused"))})
    return {
        "status": "completed",
        "path": str(path),
        "existing_receipts": len(load_scope_receipts(path)) - len(appended),
        "judged": len(appended),
        "documents": appended,
        "failures": failures,
    }


def _append_scope_receipt(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical(dict(row)))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical(dict(row)))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def produce_evidence_atoms(
    *,
    output_dir: str | os.PathLike[str],
    records: Iterable[Mapping[str, Any]],
    resolver: DocumentResolver,
    domain_contract: Mapping[str, Any],
    domain_contract_source: str,
    scope_decisions_path: str | os.PathLike[str],
    run_id: str,
    generation_id: str,
    attempt_id: str,
    policy_sha256: str,
    material_snapshot_hash: str,
    roles: Sequence[str] = (),
    role_by_paper: Mapping[str, Sequence[str]] | None = None,
    record_limit: int = DEFAULT_ATOM_RECORD_LIMIT,
    source_roots: Iterable[str | os.PathLike[str]] = (),
    recorded_document_receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Produce the P3A evidence-atom artifacts for this generation.

    Returns the manifest.  The manifest is the only object downstream producers
    may read: it lists the eligible atom ids, the rejection count and the hashes of
    every artifact it describes.
    """

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    scope_index = load_scope_receipts(scope_decisions_path)

    # The atom working set must not waste its bounded budget on records that can
    # never produce a canonical span.  Resolution is therefore attempted for every
    # in-scope record first, and the budget is then spent on resolvable records in
    # their original order.  This changes only which records the atom producer
    # inspects - never what it is allowed to accept - because eligibility still
    # requires a verified document, a real owner row and a scope receipt.
    working_set, working_rejections, _working_documents, document_lookup = (
        _select_working_set_with_documents(
            records,
            resolver=resolver,
            source_roots=source_roots,
            recorded_receipts=recorded_document_receipts,
            scope_index=scope_index,
            limit=record_limit,
        )
    )

    cards: list[dict[str, Any]] = []
    identities: dict[str, dict[str, Any]] = {}
    producer_rejections: list[dict[str, Any]] = list(working_rejections)
    registration_rejections: list[dict[str, Any]] = []
    for position, record in enumerate(working_set):
        # The document identity is the join key between the served material record,
        # the scope-decision log and the registered document version.  A record that
        # declares one keeps it; otherwise the paper identity is used, because that
        # is what the scope producer keys its receipts by.
        document_id = _document_id_for_record(record)

        def _registration_reject(reason: str, status: str) -> None:
            registration_rejections.append({
                "schema_version": PRODUCER_REJECTION_SCHEMA,
                "stage": "evidence_first.document_registration",
                "document_id": document_id,
                "paper_id": _text(record.get("paper_id")),
                "chunk_id": _text(record.get("chunk_id")),
                "experiment_id": _text(record.get("experiment_id")),
                "field_name": "",
                "reason": reason,
                "claim_input_status": status,
                "permission_ceiling": "discovery_only",
                "authorable": False,
                "detail": {"source_path": _text(record.get("document_source_path"))},
            })

        if not document_id:
            _registration_reject("record_without_document_id", "rejected_identity")
            continue
        document = _working_documents.get(id(record))
        document_evidence = next(
            (
                row for row in document_lookup
                if row.get("chunk_id") == _text(record.get("chunk_id"))
            ),
            {"document_id": document_id, "resolution": "unresolved"},
        )
        if not isinstance(document, Mapping):
            _registration_reject(
                _text(document_evidence.get("reason"))
                or "document_source_not_registered",
                "rejected_identity",
            )
            continue
        identity, identity_rejections = build_identity_record(
            record,
            resolver=resolver,
            domain_contract=domain_contract,
            domain_contract_source=domain_contract_source,
            scope_entry=scope_index.get(document_id),
            scope_log_path=str(scope_decisions_path),
            roles=tuple(
                roles
                or (role_by_paper or {}).get(_text(record.get("paper_id")))
                or ()
            ),
            document_id=document_id,
        )
        if identity is None:
            registration_rejections.extend(identity_rejections)
            continue
        card = build_evidence_card(
            dict(record, record_position=position),
            document_id=document_id,
            experiment_id=_text(record.get("experiment_id"))
            or "%s:%s" % (_text(record.get("paper_id")) or "paper", position),
            sim_or_experiment=_text(document.get("sim_or_experiment")) or "not_reported",
            experiment_level=_text(document.get("experiment_level")) or "not_reported",
            document_version_id=_text(document.get("version_id")),
            document_raw_hash=_text(document.get("raw_hash")),
        )
        cards.append(card)
        identities[_text(card.get("card_id"))] = identity

    result = EA.materialize_evidence_atoms(
        cards,
        identity_records=identities,
        resolver=resolver,
        run_id=run_id,
        generation_id=generation_id,
        attempt_id=attempt_id,
        policy_sha256=policy_sha256,
        material_snapshot_hash=material_snapshot_hash,
    )

    atoms = list(result.get("atoms") or [])
    atom_rejections = list(result.get("rejections") or [])
    eligible = [
        atom for atom in atoms
        if _text(atom.get("claim_input_status")) == "eligible_for_claim_audit"
    ]
    not_eligible = [
        atom for atom in atoms
        if _text(atom.get("claim_input_status")) != "eligible_for_claim_audit"
    ]

    # Hard invariant: an atom never carries writing authority on its own.
    for atom in atoms:
        atom["authorable"] = False
        atom["binding_required"] = True

    all_rejections = [
        *producer_rejections,
        *registration_rejections,
        *atom_rejections,
        *not_eligible,
    ]
    all_rejections.sort(key=lambda row: (
        _text(row.get("document_id")),
        _text(row.get("chunk_id")),
        _text(row.get("field_name")),
        _text(row.get("reason")),
    ))

    atoms_path = _write_jsonl(out / ATOMS_FILENAME, atoms)
    rejections_path = _write_jsonl(out / REJECTIONS_FILENAME, all_rejections)

    eligible_view = [
        {
            "schema_version": ELIGIBLE_SCHEMA,
            "atom_id": _text(atom.get("atom_id")),
            "document_id": _text(
                (atom.get("source_provenance") or {}).get("document_id")
            ),
            "paper_id": _text((atom.get("span") or {}).get("paper_id")),
            "chunk_id": _text((atom.get("span") or {}).get("chunk_id")),
            "field_name": _text((atom.get("field") or {}).get("name")),
            "experiment_id": _text(
                (atom.get("experiment") or {}).get("experiment_id")
            ),
            "span_id": _text((atom.get("span") or {}).get("span_id")),
            "permission_ceiling": _text(atom.get("permission_ceiling")),
            "authorable": False,
            "binding_required": True,
        }
        for atom in eligible
    ]

    scope_verdicts: dict[str, int] = {}
    for atom in atoms:
        verdict = _text((atom.get("role_provenance") or {}).get("scope_verdict"))
        scope_verdicts[verdict] = scope_verdicts.get(verdict, 0) + 1

    contract_path = Path(_text(domain_contract_source)).expanduser()
    scope_path = Path(str(scope_decisions_path)).expanduser()
    manifest_body = {
        "schema_version": MANIFEST_SCHEMA,
        "producer": {
            "module": ATOM_PRODUCER_ID,
            "function": ATOM_PRODUCER_FUNCTION,
            "code_sha256": producer_code_hashes(),
            "model_calls": 0,
            "deterministic": True,
        },
        "run_id": str(run_id),
        "generation_id": str(generation_id),
        "attempt_id": str(attempt_id),
        "policy_sha256": str(policy_sha256),
        "material_snapshot_hash": str(material_snapshot_hash),
        "domain_contract": {
            "source": _text(domain_contract_source),
            "source_sha256": (
                _sha_bytes(contract_path.read_bytes()) if contract_path.is_file() else ""
            ),
            "contract_hash": _text(
                (domain_contract.get("envelope") or {}).get("content_sha256")
            ) or _sha_text(_canonical(domain_contract)),
        },
        "scope_receipts": {
            "path": str(scope_decisions_path),
            "source_sha256": (
                _sha_bytes(scope_path.read_bytes()) if scope_path.is_file() else ""
            ),
            "documents_indexed": len(scope_index),
        },
        "document_resolution": {
            "source_roots": [str(item) for item in source_roots],
            "resolved": sum(
                1 for row in document_lookup if _text(row.get("resolution"))
                in {"doi_lookup", "recorded_receipt"}
            ),
            "unresolved": sum(
                1 for row in document_lookup if _text(row.get("resolution"))
                not in {"doi_lookup", "recorded_receipt"}
            ),
            "lookups": document_lookup,
        },
        "record_limit": int(record_limit),
        "counts": {
            "atom_records_considered": len(working_set) + len(working_rejections),
            "working_set": len(working_set),
            "cards": len(cards),
            "atoms": len(atoms),
            "eligible_for_claim_audit": len(eligible),
            "rejected": len(all_rejections),
            "working_set_rejections": len(working_rejections),
            "document_or_identity_rejections": len(registration_rejections),
            "atom_level_rejections": len(atom_rejections) + len(not_eligible),
        },
        "scope_verdicts": scope_verdicts,
        "artifacts": {
            ATOMS_FILENAME: {
                "filename": ATOMS_FILENAME,
                "sha256": _sha_bytes(atoms_path.read_bytes()),
                "rows": len(atoms),
            },
            REJECTIONS_FILENAME: {
                "filename": REJECTIONS_FILENAME,
                "sha256": _sha_bytes(rejections_path.read_bytes()),
                "rows": len(all_rejections),
            },
        },
        "eligible_atoms": eligible_view,
        "authorable_atoms": 0,
        "binding_required": True,
        "created_at": _now(),
    }
    manifest = dict(manifest_body)
    manifest["manifest_body_sha256"] = _sha_text(_canonical(manifest_body))
    (out / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


# --------------------------------------------------------------------------- #
# consumer helpers
# --------------------------------------------------------------------------- #

def load_atom_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and integrity-check the manifest.  Never returns a partial object."""

    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise EvidenceFirstError("evidence_atom_manifest_missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceFirstError("evidence_atom_manifest_unparseable") from exc
    if not isinstance(manifest, Mapping):
        raise EvidenceFirstError("evidence_atom_manifest_not_an_object")
    if _text(manifest.get("schema_version")) != MANIFEST_SCHEMA:
        raise EvidenceFirstError("evidence_atom_manifest_schema_mismatch")
    body = {key: value for key, value in manifest.items()
            if key != "manifest_body_sha256"}
    if _sha_text(_canonical(body)) != _text(manifest.get("manifest_body_sha256")):
        raise EvidenceFirstError("evidence_atom_manifest_hash_mismatch")
    if int(manifest.get("authorable_atoms") or 0) != 0:
        raise EvidenceFirstError("evidence_atom_manifest_claims_authorable_atom")
    base = manifest_path.parent
    for name, record in (manifest.get("artifacts") or {}).items():
        target = base / _text(record.get("filename") or name)
        if not target.is_file():
            raise EvidenceFirstError("evidence_atom_artifact_missing:%s" % name)
        if _sha_bytes(target.read_bytes()) != _text(record.get("sha256")):
            raise EvidenceFirstError("evidence_atom_artifact_hash_mismatch:%s" % name)
    return dict(manifest)


def eligible_atom_ids(path: str | os.PathLike[str]) -> list[str]:
    """Return the only atom ids a downstream claim producer may consume."""

    manifest = load_atom_manifest(path)
    return [
        _text(row.get("atom_id"))
        for row in (manifest.get("eligible_atoms") or [])
        if _text(row.get("atom_id"))
    ]


__all__ = [
    "ATOMS_FILENAME",
    "ATOM_PRODUCER_ID",
    "ELIGIBLE_SCHEMA",
    "EvidenceFirstError",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "PRODUCER_REJECTION_SCHEMA",
    "REJECTIONS_FILENAME",
    "build_evidence_card",
    "build_identity_record",
    "eligible_atom_ids",
    "ensure_scope_receipts",
    "load_atom_manifest",
    "load_scope_receipts",
    "produce_evidence_atoms",
    "producer_code_hashes",
    "resolve_owner_chunk",
    "resolve_record_document",
    "select_atom_working_set",
]
