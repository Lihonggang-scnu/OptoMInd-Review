"""Deterministic source-role boundaries, with no provider or ledger."""
import json

import pytest

from optomind_research.runtime.upgrade3.document_snapshot import (
    build_snapshot_from_bytes, classify_source_material_roles, validate_snapshot,
)
from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
from optomind_research.runtime.upgrade3.material_reading import build_reading_policy
from optomind_research.runtime.upgrade3 import directed_reading as directed


def snapshot(tmp_path, opening, *, filename="article.xml", role="main", tail=""):
    xml = f'<article><front><article-meta><title-group><article-title>Controlled study</article-title></title-group></article-meta></front><body><sec><p>{opening}</p><p>Measured method with wavelength 820 nm and bandwidth 10 nm.</p>{tail}</sec></body></article>'.encode()
    manifest = build_snapshot_from_bytes(
        xml, tmp_path, canonical_paper_id="controlled-paper", source_name=filename,
        source_role=role, material_depth_override="fulltext",
        producer_fulltext_claim_override={"claimed": True},
    )
    root = tmp_path / manifest["snapshot_id"]
    validate_snapshot(root)
    return PreparedSnapshotProvider(root).load()


@pytest.mark.parametrize("opening", [
    "Supporting Information for a metasurface objective",
    "Supplementary material for a population health study",
])
def test_opening_declaration_preserves_useful_supplement_and_blocks_main_claim(tmp_path, opening):
    prepared = snapshot(tmp_path, opening)
    assert prepared.manifest["material_inventory"]["main_document"] == "missing"
    assert prepared.manifest["material_inventory"]["supplementary_material"] == "available"
    assert prepared.manifest["producer_fulltext_claim"]["claimed"] is False
    assert prepared.content_depth == "supplementary_only"
    packet = prepared.build_reading_packet()
    assert packet["eligible"] and packet["material_scope"] == "supplementary_only"
    assert packet["declared_content_depth"] == "supplementary"
    assert [row["text"] for row in packet["observations"]] == [opening, "Measured method with wavelength 820 nm and bandwidth 10 nm."]
    assert all(row["source_kind"] == "supplementary_body" for row in packet["observations"])
    source_packet, audit = directed.build_prompt_source_packet(prepared)
    assert source_packet["material_boundary"]["supplementary_only"]
    assert set(source_packet["supplementary_source_handles"]) == set(audit["body_source_handles"])
    catalog, _ = directed.build_source_selection_catalog(prepared)
    messages, _ = directed.build_source_selection_messages(snapshot=prepared, request={}, task={})
    assert json.loads(messages[-1]["content"])["selector_catalog"]["material_boundary"]["supplementary_only"]
    selection = {"status": "selected", "selected_candidate_ids": [row["candidate_id"] for row in catalog["candidates"]]}
    selected, _ = directed.build_selected_source_packet(prepared, catalog=catalog, selection=selection)
    assert selected["material_boundary"] == source_packet["material_boundary"]
    _, practical_messages, material, *_ = directed._practical_reading_plan(request={}, paper={"title": "Controlled study"}, snapshot_dir=prepared.root, task={}, max_input_tokens=100000)
    assert "supplementary_only" in practical_messages[-1]["content"]
    assert "wavelength 820 nm and bandwidth 10 nm" in material["body"]


def test_filename_and_later_citations_or_appended_si_do_not_relabel_main(tmp_path):
    prepared = snapshot(tmp_path, "We study the main article question.", filename="study_si_main.xml", tail='<p>Supporting Information for another study is cited here.</p><sec><title>Supporting Information</title><p>Appended supporting controls.</p></sec>')
    assert prepared.content_depth == "fulltext"
    assert "material_role_policy" not in prepared.manifest
    assert "material_boundary" not in prepared.reading_policy()
    assert "material_boundary" not in directed.build_prompt_source_packet(prepared)[0]


def test_explicit_supplementary_role_is_sufficient_without_filename_or_title(tmp_path):
    prepared = snapshot(tmp_path, "Experimental controls and extra methods.", role="supplementary_material")
    boundary = prepared.reading_policy()["material_boundary"]
    assert boundary["evidence"][0]["basis"] == "explicit_source_role"
    assert prepared.content_depth == "supplementary_only"


def test_separate_main_and_si_sources_keep_main_scope_and_source_roles():
    manifest = {"source_documents": [{"source_document_id": "main", "role": "main"}, {"source_document_id": "extra", "role": "supplementary_material"}], "producer_fulltext_claim": {"claimed": True, "content_depth": "fulltext"}}
    blocks = [{"source_document_id": source, "block_type": "paragraph", "text_normalized": text, "locator": {"xml_path": "/article[1]/body[1]/p[1]"}} for source, text in [("main", "Main article findings."), ("extra", "Extra controls.")]]
    roles = classify_source_material_roles(manifest, blocks)
    assert roles["supplementary_source_ids"] == ["extra"] and not roles["supplementary_only"]
    policy = build_reading_policy(manifest, blocks)
    assert policy["material_scope"] == "fulltext" and policy["fulltext_claimed"]
    assert not any(gap.get("status") == "supplementary_only" for gap in policy["known_gaps"])
