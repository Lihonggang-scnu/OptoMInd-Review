"""Evidence-first atomic claims built from eligible Evidence Atoms (SM02).

The legacy path generated claims freely and then searched the whole material pool
for something that happened to support them.  This module inverts that: a claim can
only exist when it inherits an already verified evidence atom, and it must carry the
atom's own span, conditions and experiment identity with it.

Two layers:

* a deterministic builder that never invents wording - it composes the statement
  from the atom's own source slice and its condition anchors, so no number, unit,
  object or direction can be lost;
* an optional bounded atomiser that may rewrite the statement, but only against the
  atoms it was given, and only if the result still contains every pinned token.
  Anything else is a structured rejection, never a silently repaired claim.

A claim is still not authorable.  It is a binding candidate: P3B has to resolve its
support before any writer may see it.
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
from . import evidence_first as EF

CLAIM_SCHEMA = "optomind.upgrade3.atomic_claim.v1"
MANIFEST_SCHEMA = "optomind.upgrade3.atomic_claim_manifest.v1"
REJECTION_SCHEMA = "optomind.upgrade3.atomic_claim_rejection.v1"

CLAIMS_FILENAME = "ATOMIC_CLAIMS.jsonl"
REJECTIONS_FILENAME = "ATOMIC_CLAIM_REJECTIONS.jsonl"
MANIFEST_FILENAME = "ATOMIC_CLAIM_MANIFEST.json"

ATOM_PRODUCER_ID = "optomind_research.runtime.upgrade3.atomic_claims"
DEFAULT_SECTION_TARGET = (16, 32)
DEFAULT_BATCH_SIZE = 4
DEFAULT_MAX_ATOMISER_CALLS = 3

#: The atomiser budget is a real cost guard, but a guard that cannot reach the
#: contract's own target is a ceiling on quality, not a guard.  Every run of the
#: three-section slice reported exactly twelve verified claims because three calls
#: of four atoms covered twelve atoms for the whole attempt, while the contract
#: asks each section for sixteen to thirty-two.  The budget is therefore derived
#: from the target and the supply, and only then capped.
ATOMISER_HARD_CALL_CAP = 48


def plan_atomiser_calls(
    *,
    eligible_atoms: int,
    sections: int,
    target_range: Sequence[int] = DEFAULT_SECTION_TARGET,
    batch_size: int = DEFAULT_BATCH_SIZE,
    hard_cap: int = ATOMISER_HARD_CALL_CAP,
) -> int:
    """How many atomiser calls this attempt needs to be able to meet its target."""

    size = max(1, int(batch_size or 1))
    floor = int(target_range[0]) if list(target_range or ()) else 1
    wanted_atoms = max(1, int(floor)) * max(1, int(sections))
    supply = max(0, int(eligible_atoms or 0))
    wanted_atoms = min(wanted_atoms, supply) or min(wanted_atoms, max(supply, 1))
    calls = -(-max(0, wanted_atoms) // size)
    calls = max(calls, DEFAULT_MAX_ATOMISER_CALLS)
    return max(1, min(int(hard_cap), calls))

CLAIM_STATUSES = ("candidate_for_binding", "rejected")

# A raw atom slice can be a whole paragraph - and a paragraph lifted out of a
# two-column PDF carries page furniture, running heads and hyphenated line breaks.
# Such a slice is never presented as an atomic claim: it either gets a validated
# bounded rewrite, or it is rejected.  The limit is deliberately generous, so an
# honest single-sentence result still passes untouched.
MAX_RAW_STATEMENT_CHARS = 700
MAX_ATOMISER_SLICE_CHARS = 1800

# Fields that may carry a load-bearing statement.  limitations is a real boundary
# fact; method/conditions inform the statement and are pinned as conditions
# instead of becoming a claim of their own.
LOAD_BEARING_FIELDS = ("results", "limitations")
CONDITION_FIELDS = ("conditions", "method")

_WS_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMBER_TOKEN_RE = re.compile(
    # Thousands separators are part of the value ("1,252" is one number, not two),
    # so a comma or space between digit groups stays inside the token.
    r"(?<![A-Za-z0-9])[-+]?\d+(?:[,\u00a0 ]\d{3})*(?:\.\d+)?"
    r"(?:\s*[x*\u00d7]\s*10\s*(?:\^|\*\*)?\s*[-+]?\d+)?"
    r"(?:\s*%|\s*\u00b0[CF]?)?",
)
_UNIT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[a-zA-Z\u00b5\u03bc\u03a9]{1,6})(?![A-Za-z0-9])",
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|cannot|can't|does not|do not|did not|"
    r"is not|are not|was not|were not|fails? to|unable to|neither|nor)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "by", "for",
    "with", "as", "is", "are", "was", "were", "be", "been", "being", "that",
    "this", "these", "those", "it", "its", "we", "our", "their", "from", "into",
    "than", "then", "which", "while", "when", "where", "who", "whom", "whose",
    "also", "such", "can", "may", "might", "must", "shall", "should", "will",
    "would", "has", "have", "had", "do", "does", "did", "but", "so", "if",
}


_UNIT_VOCAB = {
    # Keep the unit pin deliberately small: a false positive here blocks a
    # legitimate rewrite, so only tokens that really denote a unit are pinned.
    "hz", "khz", "mhz", "ghz", "thz", "w", "kw", "mw", "uw", "nw", "pw", "gw",
    "wh", "kwh", "j", "kj", "mj", "nj", "pj", "v", "kv", "mv", "a", "ma", "ua",
    "na", "pa", "kpa", "mpa", "gpa", "bar", "atm", "nm", "um", "mm", "cm", "km",
    "pm", "fm", "m", "kg", "mg", "ug", "ng", "pg", "g", "s", "ms", "us", "ns",
    "ps", "fs", "min", "h", "day", "days", "k", "degc", "degf", "c", "f",
    "tops", "teraops", "flops", "gflops", "tflops", "ops", "fps", "db", "bit",
    "bits", "byte", "bytes", "b", "kb", "mb", "gb", "tb", "px", "dpi", "mol",
    "mm2", "cm2", "m2", "nm2", "lx", "lm", "cd", "rad", "sr", "ev", "mev", "kev",
    "ohm", "kohm", "mohm", "f", "uf", "pf", "nf", "t", "mt", "kt", "wtpct",
    # spelled-out forms, because a rewrite may keep the unit as a word
    "watt", "watts", "kilowatt", "kilowatts", "milliwatt", "milliwatts",
    "volt", "volts", "ampere", "amperes", "amp", "amps", "joule", "joules",
    "hertz", "second", "seconds", "minute", "minutes", "hour", "hours",
    "metre", "metres", "meter", "meters", "nanometre", "nanometres",
    "nanometer", "nanometers", "micrometre", "micrometres", "micrometer",
    "micrometers", "millimetre", "millimetres", "millimeter", "millimeters",
    "centimetre", "centimetres", "centimeter", "centimeters", "gram", "grams",
    "kilogram", "kilograms", "pascal", "pascals", "joulepersecond", "percent",
    "percentage", "decibel", "decibels", "bit", "bits", "byte", "bytes",
}


def _known_unit_tokens(text: str) -> list[str]:
    """Unit-like tokens that really are units."""

    found: list[str] = []
    for match in _UNIT_TOKEN_RE.finditer(_clean(text)):
        token = _clean(match.group(0)).casefold()
        if token in _UNIT_VOCAB and token not in found:
            found.append(token)
    return found


class AtomicClaimError(RuntimeError):
    """Raised when the atomic-claim producer cannot produce a trustworthy manifest."""


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


def _clean(value: Any) -> str:
    return _WS_RE.sub(" ", "" if value is None else str(value)).strip()


def producer_code_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for module in (EF,):
        path = Path(getattr(module, "__file__", "") or "")
        if path.is_file():
            hashes[module.__name__] = _sha_bytes(path.read_bytes())
    path = Path(__file__)
    if path.is_file():
        hashes[__name__] = _sha_bytes(path.read_bytes())
    return hashes


# --------------------------------------------------------------------------- #
# token pinning
# --------------------------------------------------------------------------- #

def pinned_tokens(text: str) -> dict[str, Any]:
    """Numbers, units and the negation polarity that must survive a rewrite."""

    folded = _clean(text)
    numbers = []
    for match in _NUMBER_TOKEN_RE.finditer(folded):
        token = _clean(match.group(0))
        if token and token not in numbers:
            numbers.append(token)
    return {
        "numbers": numbers,
        "units": _known_unit_tokens(folded),
        "negated": bool(_NEGATION_RE.search(folded)),
    }


def _token_present(token: str, haystack: str) -> bool:
    if not token:
        return True
    if token in haystack:
        return True
    # A model may reformat spacing around a value ("1,252" vs "1252"); compare on
    # the digits-and-letters skeleton as a fallback.
    skeleton = re.sub(r"[^0-9A-Za-z]+", "", token).casefold()
    return bool(skeleton) and skeleton in re.sub(
        r"[^0-9A-Za-z]+", "", haystack
    ).casefold()


# --------------------------------------------------------------------------- #
# deterministic candidates
# --------------------------------------------------------------------------- #

def _atom_field(atom: Mapping[str, Any]) -> str:
    return _text((atom.get("field") or {}).get("name"))


def _atom_slice(atom: Mapping[str, Any]) -> str:
    field = atom.get("field") or {}
    value = _clean(field.get("value") or field.get("source_value"))
    if value:
        return value
    return _clean((atom.get("span") or {}).get("quote"))


def _atom_span_id(atom: Mapping[str, Any]) -> str:
    return _text((atom.get("span") or {}).get("span_id"))


def _compose_statement(slice_text: str, condition_anchors: Sequence[str]) -> str:
    """Compose a self-contained statement without inventing any wording.

    The atom slice is quoted verbatim; conditions are prepended as an explicit
    "Under ..." clause using their own verbatim anchors.  Nothing is paraphrased,
    so no number, unit, object or direction can be introduced or lost.
    """

    body = _clean(slice_text)
    if body and body[-1] not in ".!?":
        body = body + "."
    conditions = [_clean(item) for item in condition_anchors if _clean(item)]
    if not conditions:
        return body
    clause = "; ".join(conditions)
    if clause and clause[-1] not in ".!?":
        clause = clause + "."
    return "Under %s %s" % (clause, body)


def _strip_reference_dense_tail(text: str) -> str:
    """Cut a trailing region that is mostly citation furniture.

    A two-column PDF extraction can interleave a reference list with the body, so
    a simple "References" heading is not always present.  The tail is walked window
    by window and the first window whose citation density crosses the threshold
    truncates the text; nothing is rewritten, only cut.
    """

    cleaned = _clean(text)
    if len(cleaned) < 400:
        return cleaned
    window = 400
    for start in range(0, max(1, len(cleaned) - window), 200):
        segment = cleaned[start:start + window]
        years = len(re.findall(r"\b(?:19|20)\d{2}\b", segment))
        volumes = len(re.findall(r"\b\d{1,4}\s*,\s*\d{3,7}\b", segment))
        markers = len(re.findall(r"\b\d{1,3}\.\s", segment))
        if (years + volumes + markers) >= 8:
            return cleaned[:start].strip() or cleaned
    return cleaned

_FURNITURE_RE = re.compile(
    r"(?i)\b(conflict of interest|competing interests|author contributions|"
    r"supplementary information|data availability|acknowledg(e)?ments?)\b"
)


def atomic_slice(slice_text: str, *, budget: int = MAX_RAW_STATEMENT_CHARS) -> str:
    """The atomic part of an evidence slice.

    A slice can be a whole extracted page.  When it is, the assertion is the
    sentence that actually carries the evidence: the one with the most numbers and
    units is chosen, and every sentence that carries numbers or units is kept if it
    still fits the budget.  The result is always a verbatim excerpt - nothing is
    paraphrased - so no value can be invented or lost, and a page of furniture is
    never presented as one atomic claim.
    """

    text = _strip_reference_dense_tail(_clean(slice_text))
    # A back-matter heading ends the substantive body; anything after it is
    # metadata, not an assertion.
    furniture = _FURNITURE_RE.search(text)
    if furniture and furniture.start() > 0:
        text = text[: furniture.start()].strip()
    if not text:
        text = _clean(slice_text)
    # Furniture is rejected sentence by sentence, before the budget shortcut.
    # The paragraph-level filter cannot see a reference entry that shares a
    # paragraph with prose, and a short slice used to be returned verbatim - so
    # a two-column extraction with no usable heading could hand a bibliography
    # line straight to the statement.  A slice that is furniture end to end
    # yields nothing, and the claim is then refused by the minimum-length rule
    # instead of quoting a reference list.
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if sentences:
        dropped = {index for index, part in enumerate(sentences)
                   if EF._looks_like_reference_furniture(part)}
        kept = [part for index, part in enumerate(sentences) if index not in dropped]
        if dropped:
            # The slice is contaminated by furniture, so what survives has to earn
            # its place: only a sentence that carries a value is an evidence
            # sentence.  This is what removes a bare paper title, which sits
            # inside a reference list but carries no marker of its own.
            kept = [part for part in kept
                    if pinned_tokens(part)["numbers"] or pinned_tokens(part)["units"]]
        if not kept:
            return ""
        if dropped:
            text = " ".join(kept)
            sentences = kept
    if not text:
        return ""
    if len(text) <= budget:
        return text
    if not sentences:
        return text[:budget]
    scored = []
    for index, sentence in enumerate(sentences):
        pinned = pinned_tokens(sentence)
        scored.append((
            len(pinned["numbers"]) * 2 + len(pinned["units"]),
            len(sentence),
            index,
            sentence,
        ))
    best = max(scored, key=lambda row: (row[0], row[1], -row[2]))
    keep_indexes = {best[2]}
    length = len(best[3])
    for score, size, index, sentence in sorted(scored, key=lambda row: (-row[0], row[2])):
        if index in keep_indexes:
            continue
        if score == 0:
            continue
        if length + size + 1 > budget:
            continue
        keep_indexes.add(index)
        length += size + 1
    excerpt = " ".join(sentences[index] for index in sorted(keep_indexes))
    if len(excerpt) <= budget:
        return excerpt
    return best[3][:budget].strip()


def build_deterministic_candidate(
    atom: Mapping[str, Any],
    *,
    conditions: Sequence[Mapping[str, Any]],
    generation_id: str,
    policy_sha256: str,
    material_snapshot_hash: str,
) -> dict[str, Any]:
    """Turn one eligible atom into a quote-preserving claim candidate."""

    slice_text = atomic_slice(_atom_slice(atom))
    condition_anchors = [atomic_slice(_atom_slice(item)) for item in conditions]
    statement = _compose_statement(slice_text, condition_anchors)
    return {
        "atomic_statement": statement,
        "statement_source": "deterministic_atom_slice",
        "support_slice": slice_text,
        "condition_anchors": condition_anchors,
        "condition_atom_ids": [_text(item.get("atom_id")) for item in conditions],
        "pinned": pinned_tokens(" ".join([statement, *condition_anchors])),
        "generation_id": generation_id,
        "policy_sha256": policy_sha256,
        "material_snapshot_hash": material_snapshot_hash,
    }


def stable_claim_id(
    *,
    statement: str,
    atom_ids: Sequence[str],
    span_ids: Sequence[str],
    condition_atom_ids: Sequence[str],
    policy_sha256: str,
) -> str:
    """Stable id over the normalised statement, its evidence and the policy.

    Time, attempt id and input order are deliberately excluded, so re-running an
    unchanged input reproduces the same ids and a shuffled input reproduces the
    same set.
    """

    basis = {
        "statement": _clean(statement),
        "atom_ids": sorted(_text(item) for item in atom_ids),
        "span_ids": sorted(_text(item) for item in span_ids),
        "condition_atom_ids": sorted(_text(item) for item in condition_atom_ids),
        "policy_sha256": _text(policy_sha256),
    }
    return "claim_%s" % _sha_text(_canonical(basis))[:24]


def build_claim_object(
    atom: Mapping[str, Any],
    *,
    conditions: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    importance: str,
    evidence_type: str,
    roles: Sequence[str],
    generation_id: str,
    attempt_id: str,
    policy_sha256: str,
    material_snapshot_hash: str,
    prompt_manifest_hash: str,
    model_manifest_hash: str,
) -> dict[str, Any]:
    span = atom.get("span") or {}
    experiment = atom.get("experiment") or {}
    permission = atom.get("permission_receipt") or {}
    role_provenance = atom.get("role_provenance") or {}
    atom_id = _text(atom.get("atom_id"))
    condition_ids: list[str] = []
    for item in conditions:
        for value in (item.get("field") or {}).get("condition_ids") or ():
            if value and str(value) not in condition_ids:
                condition_ids.append(str(value))
    condition_atom_ids = [_text(item.get("atom_id")) for item in conditions]
    statement = _text(candidate.get("atomic_statement"))
    claim_id = stable_claim_id(
        statement=statement,
        atom_ids=[atom_id],
        span_ids=[_atom_span_id(atom)],
        condition_atom_ids=condition_atom_ids,
        policy_sha256=policy_sha256,
    )
    body = {
        "claim_id": claim_id,
        "atomic_statement": statement,
        "atom_ids": [atom_id],
        "span_ids": [_atom_span_id(atom)],
        "condition_atom_ids": condition_atom_ids,
        "condition_ids": condition_ids,
        "field_name": _atom_field(atom),
        "paper_id": _text(span.get("paper_id")),
    }
    return {
        "schema_version": CLAIM_SCHEMA,
        "claim_id": claim_id,
        "revision": 1,
        "atomic_statement": statement,
        "statement_source": _text(candidate.get("statement_source")),
        "importance": importance,
        "evidence_type": evidence_type,
        "authorable": False,
        "status": "candidate_for_binding",
        "atom_ids": [atom_id],
        "span_ids": [_atom_span_id(atom)],
        "support_span_ids": [_atom_span_id(atom)],
        "context_span_ids": [],
        "condition_atom_ids": condition_atom_ids,
        "condition_anchors": list(candidate.get("condition_anchors") or []),
        "condition_ids": condition_ids,
        "paper_id": _text(span.get("paper_id")),
        "document_id": _text((span.get("source_provenance") or {}).get("document_id")),
        "chunk_id": _text(span.get("chunk_id")),
        "field_name": _atom_field(atom),
        "experiment": {
            "experiment_id": _text(experiment.get("experiment_id")),
            "sim_or_experiment": _text(experiment.get("sim_or_experiment")),
            "experiment_level": _text(experiment.get("experiment_level")),
        },
        "scope_verdict": _text(role_provenance.get("scope_verdict")),
        "permission_ceiling": _text(atom.get("permission_ceiling")),
        "permission_receipt_hash": (
            _sha_text(_canonical(permission)) if permission else ""
        ),
        "role_provenance": {
            # The claim's roles are its own atom's roles.  The run-level list is
            # only a fallback: the producer used to pass none, so every claim lost
            # the role its evidence actually carries and no section could ever
            # cover a required role.
            "roles": (
                [str(item) for item in (role_provenance.get("roles") or [])
                 if str(item)]
                or list(roles or ())
            ),
            "atom_producer": EF.ATOM_PRODUCER_ID,
            "scope_receipt_hash": _text(role_provenance.get("scope_receipt_hash")),
            "domain_contract_hash": _text(role_provenance.get("domain_contract_hash")),
            "owner_kb": _text(role_provenance.get("owner_kb")),
        },
        "pinned": dict(candidate.get("pinned") or {}),
        "envelope": {
            "run_id": _text((atom.get("envelope") or {}).get("run_id")),
            "generation_id": str(generation_id),
            "attempt_id": str(attempt_id),
            "artifact_id": claim_id,
            "producer": ATOM_PRODUCER_ID,
            "parent_artifact_hashes": [atom_id, _text(atom.get("content_sha256"))],
            "created_at": _now(),
            "content_sha256": _sha_text(_canonical(body)),
            "policy_sha256": str(policy_sha256),
            "prompt_manifest_hash": str(prompt_manifest_hash or "not_applicable"),
            "model_manifest_hash": str(model_manifest_hash or "not_applicable"),
            "material_snapshot_hash": str(material_snapshot_hash),
        },
    }


def _rejection(
    *,
    atom: Mapping[str, Any] | None,
    reason: str,
    status: str = "rejected",
    detail: Mapping[str, Any] | None = None,
    candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": REJECTION_SCHEMA,
        "atom_id": _text((atom or {}).get("atom_id")),
        "paper_id": _text(((atom or {}).get("span") or {}).get("paper_id")),
        "chunk_id": _text(((atom or {}).get("span") or {}).get("chunk_id")),
        "field_name": _atom_field(atom or {}),
        "reason": reason,
        "status": status,
        "authorable": False,
        "statement_attempt": _text((candidate or {}).get("atomic_statement")),
        "detail": dict(detail or {}),
    }


# --------------------------------------------------------------------------- #
# the atomiser contract
# --------------------------------------------------------------------------- #

ATOMISER_SYSTEM = (
    "You rewrite a claim statement so that it is a single self-contained atomic "
    "assertion. Rules: use ONLY the supplied source slice and condition anchors; "
    "introduce no new fact, number, unit, object, direction or source; keep every "
    "number and unit exactly as written; keep the negation polarity; never remove a "
    "condition; return the input atom_id unchanged; if you cannot do this, return "
    "skip=true. Output JSON only."
)


def build_atomiser_prompt(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build the bounded, per-batch atomiser prompt."""

    payload_items = []
    for item in items:
        payload_items.append({
            "atom_id": _text(item.get("atom_id")),
            "field": _text(item.get("field")),
            "condition_anchors": list(item.get("condition_anchors") or []),
            # The slice is bounded: an atom anchor can be a whole PDF page, and
            # the model has to spend its budget writing the atomic assertion, not
            # reading page furniture.
            "source_slice": _text(item.get("support_slice"))[:MAX_ATOMISER_SLICE_CHARS],
            "current_statement": _text(item.get("atomic_statement"))[:MAX_ATOMISER_SLICE_CHARS],
        })
    return {
        "system_prompt": ATOMISER_SYSTEM,
        "user_payload": {
            "task": "atomise_claims",
            "items": payload_items,
            "output_schema": {
                "claims": [{
                    "atom_id": "str",
                    "statement": "str",
                    "skip": "bool",
                }]
            },
        },
        "prompt_hash": _sha_text(_canonical({
            "system": ATOMISER_SYSTEM,
            "atom_ids": [item["atom_id"] for item in payload_items],
        })),
    }


def validate_atomised_statement(
    statement: str,
    *,
    candidate: Mapping[str, Any],
    cited_atom_ids: Sequence[str],
    allowed_atom_ids: Sequence[str],
) -> str | None:
    """Return a rejection reason, or None when the rewrite is acceptable."""

    text = _clean(statement)
    if not text:
        return "atomised_statement_empty"
    allowed = {_text(item) for item in allowed_atom_ids}
    cited = [_text(item) for item in cited_atom_ids]
    if not cited:
        return "atomised_claim_cites_no_atom"
    if not set(cited).intersection(allowed):
        return "atomised_claim_cites_unknown_atom"
    unknown = [item for item in cited if item not in allowed]
    if unknown:
        return "atomised_claim_cites_unknown_atom:extra"
    pinned = candidate.get("pinned") or {}
    # Polarity first: a flipped denial is the most dangerous rewrite, and it must
    # not be reported as a coincidental missing token.
    if bool(pinned.get("negated")) != bool(_NEGATION_RE.search(text)):
        return "atomised_statement_polarity_flipped"
    for token in pinned.get("numbers") or ():
        if not _token_present(_text(token), text):
            return "atomised_statement_lost_number:%s" % token
    for token in pinned.get("units") or ():
        if not _token_present(_text(token), text):
            return "atomised_statement_lost_unit:%s" % token
    for anchor in candidate.get("condition_anchors") or ():
        head = " ".join(_clean(anchor).split()[:6])
        if head and not _token_present(head, text):
            return "atomised_statement_lost_condition"
    return None

# --------------------------------------------------------------------------- #
# section-level production entry
# --------------------------------------------------------------------------- #

def load_atoms_from_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read the atom rows the P3A producer committed for this attempt."""

    atoms_path = Path(path)
    if not atoms_path.is_file():
        raise AtomicClaimError("evidence_atoms_jsonl_missing")
    rows: list[dict[str, Any]] = []
    for line in atoms_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AtomicClaimError("evidence_atoms_jsonl_unparseable") from exc
        if isinstance(row, Mapping):
            rows.append(dict(row))
    return rows


def _permission_rank(value: str) -> int:
    try:
        from .evidence_permission import PERMISSION_STRENGTH

        return int(PERMISSION_STRENGTH.get(_text(value), -1))
    except Exception:  # pragma: no cover - defensive
        return -1


MIN_CLAIM_PERMISSION = "qualified_support"


def select_claim_inputs(
    atoms: Iterable[Mapping[str, Any]],
    *,
    eligible_atom_ids: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick the atoms that may become claims, and reject the rest with reasons.

    Only atoms the manifest lists as eligible are considered; a load-bearing
    statement additionally needs at least qualified support, so an abstract or
    adjacent atom can never carry one.
    """

    allowed = {_text(item) for item in eligible_atom_ids}
    by_document: dict[str, list[dict[str, Any]]] = {}
    accepted: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(atom) for atom in atoms),
        key=lambda atom: _text(atom.get("atom_id")),
    )
    for atom in ordered:
        document_id = _text(
            ((atom.get("span") or {}).get("source_provenance") or {}).get("document_id")
        )
        by_document.setdefault(document_id, []).append(atom)

    for atom in ordered:
        atom_id = _text(atom.get("atom_id"))
        if atom_id not in allowed:
            rejections.append(_rejection(
                atom=atom,
                reason="atom_not_listed_as_eligible",
                status="rejected_not_eligible",
            ))
            continue
        if _text(atom.get("claim_input_status")) != "eligible_for_claim_audit":
            rejections.append(_rejection(
                atom=atom,
                reason="atom_claim_input_status_not_eligible",
                status="rejected_not_eligible",
                detail={"claim_input_status": _text(atom.get("claim_input_status"))},
            ))
            continue
        field_name = _atom_field(atom)
        if field_name in CONDITION_FIELDS:
            rejections.append(_rejection(
                atom=atom,
                reason="condition_atom_is_pinned_not_a_claim",
                status="rejected_field_role",
            ))
            continue
        if field_name not in LOAD_BEARING_FIELDS:
            rejections.append(_rejection(
                atom=atom,
                reason="field_cannot_carry_a_load_bearing_claim",
                status="rejected_field_role",
                detail={"field_name": field_name},
            ))
            continue
        if not _atom_slice(atom):
            rejections.append(_rejection(
                atom=atom,
                reason="atom_has_no_source_slice",
            ))
            continue
        scope_verdict = _text((atom.get("role_provenance") or {}).get("scope_verdict"))
        if scope_verdict in {"adjacent", "background", "uncertain", "out_of_scope", ""}:
            rejections.append(_rejection(
                atom=atom,
                reason="load_bearing_claim_needs_direct_scope",
                detail={"scope_verdict": scope_verdict},
            ))
            continue
        if _permission_rank(_text(atom.get("permission_ceiling"))) < _permission_rank(
            MIN_CLAIM_PERMISSION
        ):
            rejections.append(_rejection(
                atom=atom,
                reason="load_bearing_claim_needs_qualified_support",
                detail={"permission_ceiling": _text(atom.get("permission_ceiling"))},
            ))
            continue
        accepted.append(atom)
    return accepted, rejections


def _conditions_for(atom: Mapping[str, Any], atoms: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    wanted = {_text(item) for item in (atom.get("field") or {}).get("condition_atom_ids") or ()}
    if not wanted:
        return []
    return [
        dict(item) for item in atoms
        if _text(item.get("atom_id")) in wanted
    ]


def _importance_for(field_name: str, statement: str) -> str:
    if field_name == "limitations":
        return "supporting"
    if re.search(r"\d", statement):
        return "load_bearing"
    return "supporting"


def _evidence_type_for(field_name: str, atom: Mapping[str, Any]) -> str:
    experiment = atom.get("experiment") or {}
    kind = _text(experiment.get("sim_or_experiment")) or "not_reported"
    return "%s:%s" % (field_name, kind)


def _score(claim: Mapping[str, Any]) -> tuple:
    """Deterministic complementarity ordering for the per-section shortlist."""

    pinned = claim.get("pinned") or {}
    return (
        0 if claim.get("field_name") == "results" else 1,
        -len(pinned.get("numbers") or ()),
        -len(claim.get("condition_atom_ids") or ()),
        _text(claim.get("claim_id")),
    )

def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(_canonical(dict(row)))
            handle.write(chr(10))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def _run_atomiser(
    items: Sequence[Mapping[str, Any]],
    *,
    atomiser: Any,
    batch_size: int,
    max_calls: int,
    task_id: str,
    generation_id: str,
    attempts: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ask the bounded atomiser for rewrites and keep every raw receipt.

    One call per batch, hard-capped.  A call that fails leaves its items on the
    deterministic statement; it never produces a claim without a source.

    Rows are returned in the order the model emitted them, each still carrying the
    atom id it declared.  Deciding which candidate a row answers for is the
    caller's job, so a misattributed row can never disappear into a dictionary.
    """

    results: list[dict[str, Any]] = []
    call_receipts: list[dict[str, Any]] = []
    calls = 0
    size = max(1, int(batch_size or 1))
    cap = max(0, int(max_calls or 0))
    for start in range(0, len(items), size):
        batch = list(items[start:start + size])
        if calls >= cap:
            call_receipts.append({
                "batch": calls + 1,
                "status": "skipped_call_budget_exhausted",
                "atom_ids": [_text(item.get("atom_id")) for item in batch],
            })
            continue
        calls += 1
        prompt = build_atomiser_prompt(batch)
        receipt: dict[str, Any] = {
            "batch": calls,
            "atom_ids": [_text(item.get("atom_id")) for item in batch],
            "prompt_hash": prompt["prompt_hash"],
        }
        payload = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                payload = atomiser(
                    prompt["system_prompt"],
                    prompt["user_payload"],
                    task_id=task_id,
                    logical_call_id="atomise:%d" % calls,
                    generation_id=generation_id,
                    attempt=attempt,
                )
                receipt["status"] = "completed"
                break
            except Exception as exc:
                receipt["status"] = "failed"
                receipt["error"] = "%s:%s" % (type(exc).__name__, str(exc)[:200])
                payload = None
        if not isinstance(payload, Mapping):
            call_receipts.append(receipt)
            continue
        receipt["response_hash"] = _sha_text(_canonical(dict(payload)))
        for key in ("receipt", "usage", "actual_model", "cost_receipt"):
            if key in payload:
                receipt[key] = payload[key]
        rows = payload.get("claims")
        if not isinstance(rows, list):
            receipt["status"] = "unparseable_response"
            call_receipts.append(receipt)
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            results.append(dict(row))
        call_receipts.append(receipt)
    return results, call_receipts


def _claimed_answer(
    atomised: Sequence[Mapping[str, Any]], atom_id: str
) -> Mapping[str, Any] | None:
    """The atomiser row that declares this atom id, if the model produced one.

    A row is matched by the id it carries.  A row that declares a different id is
    never returned here, so the caller sees the misattribution instead of silently
    accepting the deterministic fallback.
    """

    for row in atomised:
        if isinstance(row, Mapping) and _text(row.get("atom_id")) == atom_id:
            return row
    return None


def build_atomic_claims(
    *,
    output_dir: str | os.PathLike[str],
    atoms: Iterable[Mapping[str, Any]],
    eligible_atom_ids: Iterable[str],
    generation_id: str,
    attempt_id: str,
    run_id: str,
    policy_sha256: str,
    material_snapshot_hash: str,
    roles: Sequence[str] = (),
    target_range: Sequence[int] = DEFAULT_SECTION_TARGET,
    atomiser: Any = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_atomiser_calls: int = DEFAULT_MAX_ATOMISER_CALLS,
    atomiser_task_id: str = "",
    atomiser_generation_id: str = "",
    atomiser_attempts: int = 1,
) -> dict[str, Any]:
    """Produce the atomic-claim artifacts for one section.

    The manifest is the consumer contract: it lists the shortlisted claim ids that
    P3B may bind and the unselected ids that stay available, plus the sha256 of both
    JSONL artifacts and the atomiser call receipts.
    """

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    atom_rows = [dict(atom) for atom in atoms]
    by_id = {_text(atom.get("atom_id")): atom for atom in atom_rows}

    accepted, rejections = select_claim_inputs(
        atom_rows, eligible_atom_ids=eligible_atom_ids
    )

    candidates: list[dict[str, Any]] = []
    for atom in accepted:
        conditions = _conditions_for(atom, atom_rows)
        candidate = build_deterministic_candidate(
            atom,
            conditions=conditions,
            generation_id=generation_id,
            policy_sha256=policy_sha256,
            material_snapshot_hash=material_snapshot_hash,
        )
        pinned = pinned_tokens(candidate["support_slice"])
        if not pinned["numbers"] and not re.search(r"[A-Za-z]{4}", candidate["support_slice"]):
            rejections.append(_rejection(
                atom=atom,
                reason="atom_slice_has_no_assertable_content",
                candidate=candidate,
            ))
            continue
        if not _token_present(
            " ".join(candidate["condition_anchors"]),
            candidate["atomic_statement"],
        ):
            rejections.append(_rejection(
                atom=atom,
                reason="condition_anchor_lost_while_composing",
                candidate=candidate,
            ))
            continue
        if len(_clean(candidate.get("support_slice"))) > MAX_RAW_STATEMENT_CHARS:
            # Without a bounded rewriter there is no honest way to turn a page of
            # extracted text into one atomic assertion, so no claim is produced.
            # (atomic_slice already narrowed the slice; this is the residual guard.)
            rejections.append(_rejection(
                atom=atom,
                reason="raw_atom_slice_exceeds_atomic_statement_budget",
                status="rejected_slice_budget",
                detail={"length": len(_clean(candidate.get("support_slice")))},
                candidate=candidate,
            ))
            continue
        candidate["atom"] = atom
        candidate["atom_id"] = _text(atom.get("atom_id"))
        candidate["field"] = _atom_field(atom)
        candidates.append(candidate)

    atomised: list[dict[str, Any]] = []
    call_receipts: list[dict[str, Any]] = []
    if atomiser is not None and candidates:
        atomised, call_receipts = _run_atomiser(
            candidates,
            atomiser=atomiser,
            batch_size=batch_size,
            max_calls=max_atomiser_calls,
            task_id=atomiser_task_id or "031",
            generation_id=atomiser_generation_id or generation_id,
            attempts=atomiser_attempts,
        )

    claims: list[dict[str, Any]] = []
    model_hashes: set[str] = set()
    prompt_hashes: set[str] = set()
    for candidate in candidates:
        atom = candidate["atom"]
        atom_id = candidate["atom_id"]
        # A batch can answer for many atoms; only the row that names THIS atom may
        # rewrite it.  A misattributed row is a rejection, never a silent fallback,
        # so the producer cannot quietly lose an atomiser error.
        statement = _text(candidate.get("atomic_statement"))
        statement_source = _text(candidate.get("statement_source"))
        if atomised:
            answer = _claimed_answer(atomised, atom_id)
            if answer is None:
                # The model answered for this batch but not for this atom: it either
                # omitted the atom or attributed its rewrite to another id.  Both are
                # rejections, so an atomiser mistake can never look like success.
                rejections.append(_rejection(
                    atom=atom,
                    reason="atomiser_did_not_answer_for_atom",
                    status="rejected_atomiser_output",
                    detail={
                        "declared_atom_ids": sorted({
                            _text(row.get("atom_id"))
                            for row in atomised if isinstance(row, Mapping)
                        }),
                    },
                    candidate=candidate,
                ))
                continue
        else:
            answer = None
        if isinstance(answer, Mapping) and not bool(answer.get("skip")):
            proposed = _text(answer.get("statement"))
            _ = proposed
            cited = answer.get("atom_ids")
            cited_ids = (
                [_text(item) for item in cited]
                if isinstance(cited, (list, tuple))
                else [atom_id]
            )
            reason = validate_atomised_statement(
                proposed,
                candidate=candidate,
                cited_atom_ids=cited_ids,
                allowed_atom_ids=[atom_id],
            )
            if reason is None and len(proposed) <= MAX_RAW_STATEMENT_CHARS:
                statement = proposed
                statement_source = "bounded_atomiser"
            elif reason is None:
                rejections.append(_rejection(
                    atom=atom,
                    reason="atomised_statement_still_oversized",
                    status="rejected_atomiser_output",
                    detail={"length": len(proposed)},
                    candidate=candidate,
                ))
                continue
            else:
                rejections.append(_rejection(
                    atom=atom,
                    reason=reason,
                    status="rejected_atomiser_output",
                    detail={"proposed_statement": proposed, "cited_atom_ids": cited_ids},
                    candidate=candidate,
                ))
                continue
        candidates[candidates.index(candidate)] = dict(candidate,
                                                       atomic_statement=statement,
                                                       statement_source=statement_source)
        claims.append(build_claim_object(
            atom,
            conditions=_conditions_for(atom, atom_rows),
            candidate=dict(candidate, atomic_statement=statement,
                           statement_source=statement_source),
            importance=_importance_for(_atom_field(atom), statement),
            evidence_type=_evidence_type_for(_atom_field(atom), atom),
            roles=roles,
            generation_id=generation_id,
            attempt_id=attempt_id,
            policy_sha256=policy_sha256,
            material_snapshot_hash=material_snapshot_hash,
            prompt_manifest_hash="",
            model_manifest_hash="",
        ))

    # A claim must reference a real span and a real atom, or it does not exist.
    verified: list[dict[str, Any]] = []
    for claim in sorted(claims, key=lambda item: _text(item.get("claim_id"))):
        missing = [
            atom_id for atom_id in claim.get("atom_ids") or ()
            if _text(atom_id) not in by_id
        ]
        if missing or not claim.get("support_span_ids"):
            rejections.append(_rejection(
                atom=by_id.get(_text((claim.get("atom_ids") or [""])[0])),
                reason="claim_without_source_atom_or_span",
                status="rejected_no_source",
                detail={"missing_atom_ids": missing},
            ))
            continue
        verified.append(claim)

    for claim in verified:
        envelope = claim.get("envelope") or {}
        model_hashes.add(_text(envelope.get("model_manifest_hash")))
        prompt_hashes.add(_text(envelope.get("prompt_manifest_hash")))
        _ = claim

    low = max(1, int(target_range[0]))
    high = max(low, int(target_range[1]))
    ordered = sorted(verified, key=_score)
    selected = ordered[:high]
    unselected = ordered[high:]
    if len(selected) < low:
        selection_status = "section_supply_below_target"
    else:
        selection_status = "section_target_met"

    rejections.sort(key=lambda row: (
        _text(row.get("atom_id")), _text(row.get("reason"))
    ))
    claims_path = _write_jsonl(out / CLAIMS_FILENAME, selected)
    rejections_path = _write_jsonl(out / REJECTIONS_FILENAME, rejections)

    manifest_body = {
        "schema_version": MANIFEST_SCHEMA,
        "producer": {
            "module": ATOM_PRODUCER_ID,
            "function": "build_atomic_claims",
            "code_sha256": producer_code_hashes(),
            "atomiser_enabled": atomiser is not None,
            "model_calls": len([row for row in call_receipts if row.get("status") == "completed"]),
        },
        "run_id": str(run_id),
        "generation_id": str(generation_id),
        "attempt_id": str(attempt_id),
        "policy_sha256": str(policy_sha256),
        "material_snapshot_hash": str(material_snapshot_hash),
        "target_range": [low, high],
        "selection_status": selection_status,
        "counts": {
            "atoms_considered": len(atom_rows),
            "eligible_atoms": len({_text(item) for item in eligible_atom_ids}),
            "accepted_atoms": len(accepted),
            "candidate_claims": len(candidates),
            "verified_claims": len(verified),
            "selected_claims": len(selected),
            "unselected_claims": len(unselected),
            "rejections": len(rejections),
        },
        "atomiser": {
            "calls": call_receipts,
            "prompt_manifest_hash": _sha_text(_canonical(sorted(prompt_hashes))) if prompt_hashes else "not_applicable",
            "model_manifest_hash": _sha_text(_canonical(sorted(model_hashes))) if model_hashes else "not_applicable",
        },
        "artifacts": {
            CLAIMS_FILENAME: {
                "filename": CLAIMS_FILENAME,
                "sha256": _sha_bytes(claims_path.read_bytes()),
                "rows": len(selected),
            },
            REJECTIONS_FILENAME: {
                "filename": REJECTIONS_FILENAME,
                "sha256": _sha_bytes(rejections_path.read_bytes()),
                "rows": len(rejections),
            },
        },
        "selected_claims": [
            {
                "claim_id": _text(claim.get("claim_id")),
                "atom_ids": list(claim.get("atom_ids") or ()),
                "span_ids": list(claim.get("support_span_ids") or ()),
                "importance": _text(claim.get("importance")),
                "field_name": _text(claim.get("field_name")),
                "authorable": False,
            }
            for claim in selected
        ],
        "unselected_claims": [
            {
                "claim_id": _text(claim.get("claim_id")),
                "atom_ids": list(claim.get("atom_ids") or ()),
                "reason": "section_shortlist_cap",
            }
            for claim in unselected
        ],
        "authorable_claims": 0,
        "binding_required": True,
        "created_at": _now(),
    }
    manifest = dict(manifest_body)
    manifest["manifest_body_sha256"] = _sha_text(_canonical(manifest_body))
    (out / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + chr(10),
        encoding="utf-8",
    )
    return manifest


def load_claim_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and integrity-check the atomic-claim manifest."""

    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise AtomicClaimError("atomic_claim_manifest_missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AtomicClaimError("atomic_claim_manifest_unparseable") from exc
    if not isinstance(manifest, Mapping):
        raise AtomicClaimError("atomic_claim_manifest_not_an_object")
    if _text(manifest.get("schema_version")) != MANIFEST_SCHEMA:
        raise AtomicClaimError("atomic_claim_manifest_schema_mismatch")
    body = {key: value for key, value in manifest.items()
            if key != "manifest_body_sha256"}
    if _sha_text(_canonical(body)) != _text(manifest.get("manifest_body_sha256")):
        raise AtomicClaimError("atomic_claim_manifest_hash_mismatch")
    if int(manifest.get("authorable_claims") or 0) != 0:
        raise AtomicClaimError("atomic_claim_manifest_claims_authorable")
    base = manifest_path.parent
    for name, record in (manifest.get("artifacts") or {}).items():
        target = base / _text(record.get("filename") or name)
        if not target.is_file():
            raise AtomicClaimError("atomic_claim_artifact_missing:%s" % name)
        if _sha_bytes(target.read_bytes()) != _text(record.get("sha256")):
            raise AtomicClaimError("atomic_claim_artifact_hash_mismatch:%s" % name)
    return dict(manifest)


def selected_claim_ids(path: str | os.PathLike[str]) -> list[str]:
    """The only claim ids P3B may bind for this section."""

    manifest = load_claim_manifest(path)
    return [
        _text(row.get("claim_id"))
        for row in (manifest.get("selected_claims") or [])
        if _text(row.get("claim_id"))
    ]


__all__ = [
    "ATOM_PRODUCER_ID",
    "CLAIMS_FILENAME",
    "CLAIM_SCHEMA",
    "DEFAULT_SECTION_TARGET",
    "AtomicClaimError",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "REJECTIONS_FILENAME",
    "build_atomic_claims",
    "build_atomiser_prompt",
    "build_claim_object",
    "build_deterministic_candidate",
    "load_atoms_from_jsonl",
    "load_claim_manifest",
    "pinned_tokens",
    "producer_code_hashes",
    "select_claim_inputs",
    "selected_claim_ids",
    "stable_claim_id",
    "validate_atomised_statement",
]
