"""Contract validation; fixtures are editorial examples, not scientific evidence."""
import json
from pathlib import Path
import pytest
from optomind_research.runtime.upgrade3.manuscript_parts import (
    ManuscriptPartsError, boundary_projection, validate_body_tasks,
    validate_manuscript_parts_plan,
)


def card():
    return {"context": "Tutorial for readers who know probability but not optimization.",
            **{name: {"purpose": "Explain when uncertainty-guided search is useful.",
                      "focus": ["Separate sample efficiency from computational cost."],
                      "boundary": ["Detailed acquisition-function derivation belongs to BODY."],
                      "placement": {"mode": "standalone", "anchor": "article boundary"},
                      "finalize_from": ["actual BODY", "actual scope"]}
               for name in ("abstract", "introduction", "conclusion")}}


def test_round_trip_and_detached_projection():
    original = card()
    result = validate_manuscript_parts_plan(original)
    projection = boundary_projection(result)
    assert set(projection) == {"introduction", "conclusion"}
    assert set(projection['introduction']) == {"purpose", "focus", "boundary"}
    projection['introduction']['focus'].append('changed')
    assert result == original


@pytest.mark.parametrize('mode', ['standalone', 'embedded', 'distributed'])
def test_all_placement_modes_valid(mode):
    value = card(); value['conclusion']['placement']['mode'] = mode
    assert validate_manuscript_parts_plan(value) == value


@pytest.mark.parametrize('field', ['units', 'cases', 'paragraph_briefs', 'evidence_matrix'])
def test_no_second_planner(field):
    value = card(); value['introduction'][field] = []
    with pytest.raises(ManuscriptPartsError, match='unexpected'):
        validate_manuscript_parts_plan(value)


@pytest.mark.parametrize('mutate', [
    lambda p: p.pop('conclusion'),
    lambda p: p.update(context=''),
    lambda p: p['abstract'].update(focus='topic'),
    lambda p: p['abstract'].update(focus=[{}]),
    lambda p: p['abstract']['placement'].update(mode='omitted'),
    lambda p: p['abstract']['placement'].update(offset=10),
])
def test_invalid_shape(mutate):
    value = card(); mutate(value)
    with pytest.raises(ManuscriptPartsError): validate_manuscript_parts_plan(value)


def test_body_is_not_selected_by_heading():
    validate_body_tasks([{'chapter_id': 'CH01', 'title': 'Introduction',
                          'purpose': 'Derive posterior and acquisition functions.'},
                         {'chapter_id': 'CH02', 'title': 'Conclusion and Perspectives'}])
    with pytest.raises(ManuscriptPartsError): validate_body_tasks([card()['introduction']])
    with pytest.raises(ManuscriptPartsError):
        validate_body_tasks([{'chapter_id': 'CH01', 'task_type': 'manuscript_part'}])


def test_schema_matches_validator_on_examples():
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((Path(__file__).resolve().parents[2] /
                        'schemas/upgrade3/manuscript_parts_plan.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    for mode in ('standalone', 'embedded', 'distributed'):
        value = card(); value['conclusion']['placement']['mode'] = mode
        jsonschema.validate(value, schema)
        assert validate_manuscript_parts_plan(value) == value


def test_cli_preflight_counts_new_contract_without_model_calls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.upgrade3 import progressive_review_plan as cli
    from optomind_research.runtime.upgrade3.progressive_review_plan import ProgressivePlannerConfig
    pool, plan = tmp_path / 'pool.jsonl', tmp_path / 'PLAN.json'
    pool.write_text(json.dumps({'paper_id': 'p1', 'planning_view': {'planning_summary': 'A technical tutorial'}}) + '\n')
    plan.write_text(json.dumps({'question': 'Which assumptions matter?', 'facets': [{'facet_id': 'F1'}]}))
    seen = []
    def counter(_path):
        def count(_raw, messages):
            seen.extend(messages)
            return 100
        return count
    monkeypatch.setattr(cli.planning, 'qwen_local_token_counter', counter)
    cfg = ProgressivePlannerConfig(topic_id='offline', pool_path=pool, plan_path=plan,
        output_dir=tmp_path / 'out', planning_revision_enabled=True)
    result = cli.preflight(cfg, SimpleNamespace(budget_ledger=None))
    assert result['network_calls'] == 0
    assert 'manuscript_parts_plan' in seen[0]['content']
    text = seen[1]['content']
    payload, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])
    assert payload['planning_revision_mode'] is True
