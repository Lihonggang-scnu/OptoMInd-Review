"""Bounded advisory review contracts; shape/quotation checks are not truth tests."""
from copy import deepcopy
import json
from collections.abc import Mapping

from .guided_body_contracts import validate_guide, _canonical_source
from .json_format_recovery import recover_json_format
from .writer_candidates_contracts import CandidateError

MAX_FINDINGS = 6
MAX_TEXT = 1200
MAX_REVIEW_BYTES = 20000


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise CandidateError(label + '_must_be_nonempty_bounded_text')
    return value


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _quoted(excerpt, value):
    return any(excerpt in text for text in _strings(value))


def decode_response(response):
    value, transport, audit = response, True, None
    # Inspect every envelope, including nested finish reasons. Direct fixtures
    # are also supported but never override explicit provider truncation.
    while isinstance(value, Mapping):
        if value.get('finish_reason') in {'length', 'max_tokens', 'max_output_tokens', 'content_filter', 'error', 'cancelled', 'timeout'} or value.get('error') or value.get('complete') is False:
            transport = False
        if 'findings' in value or 'guide' in value:
            break
        if isinstance(value.get('choices'), list) and value['choices']:
            value = value['choices'][0]
        else:
            nested = next((value[k] for k in ('content', 'response', 'message') if isinstance(value.get(k), (str, Mapping))), None)
            if nested is None:
                break
            value = nested
    if isinstance(value, str):
        value, audit = recover_json_format(value)
    if not isinstance(value, Mapping):
        raise CandidateError('review_response_not_object')
    return value, transport, audit


def parse_review(response, payload):
    value, transport, audit = decode_response(response)
    if set(value) != {'findings', 'limitations'}:
        raise CandidateError('review_response_invalid_keys')
    if len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_REVIEW_BYTES:
        raise CandidateError('review_total_feedback_exceeds_bound')
    findings = value['findings']
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise CandidateError('review_findings_exceed_bound')
    ids = set()
    for row in findings:
        if not isinstance(row, Mapping) or set(row) != {'id', 'location', 'guide_excerpt', 'concern', 'proposed_change', 'evidence_basis', 'evidence_excerpt'}:
            raise CandidateError('review_finding_invalid_keys')
        for key in row:
            _text(row[key], 'finding_' + key)
        if row['id'] in ids or not row['id'].startswith('F'):
            raise CandidateError('review_finding_id_invalid')
        ids.add(row['id'])
        if row['location'] not in ['manuscript_guide'] + [c['chapter_id'] for c in payload['original_guide']['chapters']]:
            raise CandidateError('review_location_unknown')
        target = payload['original_guide']['manuscript_guide'] if row['location'] == 'manuscript_guide' else next(c for c in payload['original_guide']['chapters'] if c['chapter_id'] == row['location'])
        if not _quoted(row['guide_excerpt'], target):
            raise CandidateError('review_guide_excerpt_not_found')
        basis = {'guide': payload['original_guide'], 'outline': payload['full_outline'], 'materials': payload['materials']}.get(row['evidence_basis'])
        if basis is None or not _quoted(row['evidence_excerpt'], basis):
            raise CandidateError('review_evidence_excerpt_not_found')
    limitations = value['limitations']
    if not isinstance(limitations, list) or len(limitations) > 8:
        raise CandidateError('review_limitations_exceed_bound')
    for item in limitations:
        _text(item, 'review_limitation')
    result = dict(findings=deepcopy(findings), limitations=deepcopy(limitations), complete=transport,
                  provider_transport_complete=transport)
    if audit:
        result['format_recovery'] = audit
    return result


def parse_revision(response, book, bundle, review, original_guide=None):
    value, transport, audit = decode_response(response)
    if set(value) != {'guide', 'decisions'}:
        raise CandidateError('revision_response_invalid_keys')
    guide = validate_guide(value['guide'], book)
    for chapter in guide['chapters']:
        for handle in chapter.get('source_handles', []):
            _canonical_source(bundle['science_archive'], handle)
    if not review['findings'] and original_guide is not None and guide != original_guide:
        raise CandidateError('revision_without_findings_must_preserve_guide')
    decisions = value['decisions']
    if not isinstance(decisions, list) or len(decisions) != len(review['findings']):
        raise CandidateError('revision_decisions_missing')
    expected, seen = {r['id'] for r in review['findings']}, set()
    for row in decisions:
        if not isinstance(row, Mapping) or set(row) != {'finding_id', 'decision', 'rationale'}:
            raise CandidateError('revision_decision_invalid_keys')
        if row['finding_id'] not in expected or row['finding_id'] in seen:
            raise CandidateError('revision_decision_id_invalid')
        seen.add(row['finding_id'])
        if row['decision'] not in {'accepted', 'partially_accepted', 'rejected'}:
            raise CandidateError('revision_decision_unknown')
        _text(row['rationale'], 'revision_rationale')
    result = dict(guide=guide, decisions=deepcopy(decisions), complete=transport,
                  provider_transport_complete=transport)
    if audit:
        result['format_recovery'] = audit
    return result
