from __future__ import annotations

import json
from pathlib import Path

from optomind_research.runtime.upgrade3.chapter_arrangement import (
    build_chapter_view,
    parse_arrangement_response,
    validate_arrangement,
)


def _packet(units: list[dict]) -> dict:
    return {
        "chapter": {"chapter_id": "ChX", "title": "Fixture"},
        "chapter_plan": {"units": units},
        "source_materials": [
            {
                "source_handle": "P0001",
                "paper_id": "local-paper",
                "title": "Local source",
                "year": "2024",
                "doi": "10.1/local",
                "card_path": "C:/cards/local.json",
                "study_summary_A": {"work_summary": "local material"},
            },
        ],
        "source_identity_map": {
            "P0001": {
                "source_handle": "P0001",
                "paper_id": "local-paper",
                "title": "Local source",
                "year": "2024",
                "doi": "10.1/local",
                "card_path": "C:/cards/local.json",
            },
        },
    }


def _write(tmp_path: Path, packet: dict) -> Path:
    path = tmp_path / "ChX.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path


def _paragraph(handle: str) -> list[dict]:
    return [{
        "substantive_point": "fixture point",
        "paragraph_briefs": [{
            "point": "fixture paragraph",
            "development": "fixture development",
            "source_handles": [handle],
        }],
    }]


def test_global_identity_fallback_resolves_only_used_handle(tmp_path: Path):
    packet_path = _write(tmp_path, _packet(_paragraph("P0099")))
    fallback = {
        "P0099": {
            "source_handle": "P0099",
            "paper_id": "global-paper",
            "title": "Global source",
            "year": "2025",
            "doi": "10.1/global",
            "card_path": "C:/cards/global.json",
        },
        "P0888": {
            "source_handle": "P0888",
            "paper_id": "inventory-paper",
            "title": "Unrelated inventory",
            "card_path": "C:/cards/inventory.json",
        },
    }

    view = build_chapter_view(
        packet_path,
        fallback_source_identity_map=fallback,
        id_map_path=tmp_path / "ID_MAP.json",
    )

    source = next(item for item in view.sources if item.source_handle == "P0099")
    assert source.paper_id == "global-paper"
    assert source.title == "Global source"
    assert source.card_path == "C:/cards/global.json"
    assert source.material_status == "no_material"
    assert {item.source_handle for item in view.sources} == {"P0099"}


def test_local_identity_and_material_win_but_missing_locator_is_filled(tmp_path: Path):
    packet = _packet(_paragraph("P0001"))
    local = packet["source_materials"][0]
    local.pop("card_path")
    packet["source_identity_map"]["P0001"].pop("card_path")
    packet_path = _write(tmp_path, packet)
    fallback = {
        "P0001": {
            "paper_id": "local-paper",
            "title": "Local source",
            "year": "2024",
            "doi": "10.1/local",
            "card_path": "C:/cards/fallback.json",
        },
    }

    view = build_chapter_view(
        packet_path,
        fallback_source_identity_map=fallback,
        id_map_path=tmp_path / "ID_MAP.json",
    )

    source = next(item for item in view.sources if item.source_handle == "P0001")
    assert source.paper_id == "local-paper"
    assert source.title == "Local source"
    assert source.year == "2024"
    assert source.doi == "10.1/local"
    assert source.card_path == "C:/cards/fallback.json"
    assert source.study_summary_a == {"work_summary": "local material"}


def test_conflicting_global_identity_does_not_supply_locator(tmp_path: Path):
    packet = _packet(_paragraph("P0001"))
    local = packet["source_materials"][0]
    local.pop("card_path")
    packet["source_identity_map"]["P0001"].pop("card_path")
    packet_path = _write(tmp_path, packet)

    view = build_chapter_view(
        packet_path,
        fallback_source_identity_map={
            "P0001": {
                "paper_id": "different-paper",
                "title": "Different source",
                "doi": "10.1/different",
                "card_path": "C:/cards/wrong.json",
            },
        },
        id_map_path=tmp_path / "ID_MAP.json",
    )

    source = next(item for item in view.sources if item.source_handle == "P0001")
    assert source.paper_id == "local-paper"
    assert source.doi == "10.1/local"
    assert source.card_path == ""


def test_missing_global_identity_remains_unresolvable(tmp_path: Path):
    packet_path = _write(tmp_path, _packet(_paragraph("P0099")))
    view = build_chapter_view(
        packet_path,
        fallback_source_identity_map={"P0888": {"card_path": "C:/cards/other.json"}},
        id_map_path=tmp_path / "ID_MAP.json",
    )

    source = next(item for item in view.sources if item.source_handle == "P0099")
    assert source.material_status == "unresolvable_handle"
    assert source.paper_id == ""
    assert source.card_path == ""


def test_owner_text_handle_is_merged_only_from_global_identity_map(tmp_path: Path):
    packet = _packet([{
        "substantive_point": "fixture point",
        "paragraph_briefs": [{
            "point": "compare P0001",
            "development": "owner brief also names P0002中",
            "source_handles": ["P0001"],
        }],
    }])
    packet_path = _write(tmp_path, packet)
    fallback = {
        "P0002": {
            "paper_id": "global-paper-2",
            "title": "Global source 2",
            "doi": "10.1/global-2",
            "card_path": "C:/cards/global-2.json",
        },
    }

    view = build_chapter_view(
        packet_path,
        fallback_source_identity_map=fallback,
        id_map_path=tmp_path / "ID_MAP.json",
    )

    assert {item.source_handle for item in view.sources} == {"P0001", "P0002"}
    assert view.source_uses["P0002"]
    p0002 = next(item for item in view.sources if item.source_handle == "P0002")
    assert p0002.card_path == "C:/cards/global-2.json"

    payload = {
        "chapter_id": "ChX",
        "units": [{
            "unit_id": "ChX_U01",
            "paragraph_tasks": [{
                "paragraph_id": "ChX_U01_P01",
                "source_uses": [{"source_handle": "P0001", "role": "main", "use": "fixture"}],
            }],
        }],
    }
    result = validate_arrangement(payload, view, planning_revision=True)
    assert result["validation"]["ok"]
    assert {use["source_handle"] for use in result["units"][0]["paragraph_tasks"][0]["source_uses"]} == {
        "P0001", "P0002",
    }


def test_model_only_text_handle_still_fails_contract(tmp_path: Path):
    packet_path = _write(tmp_path, _packet(_paragraph("P0001")))
    view = build_chapter_view(
        packet_path,
        fallback_source_identity_map={"P0002": {"card_path": "C:/cards/known.json"}},
        id_map_path=tmp_path / "ID_MAP.json",
    )
    payload = {
        "chapter_id": "ChX",
        "units": [{
            "unit_id": "ChX_U01",
            "focus": "model text introduces P9999",
            "paragraph_tasks": [{
                "paragraph_id": "ChX_U01_P01",
                "point": "model text introduces P9999",
                "development": "P9999 is not in the owner plan",
                "source_uses": [{"source_handle": "P0001", "role": "main", "use": "fixture"}],
            }],
        }],
    }

    result = validate_arrangement(payload, view, planning_revision=True)
    assert result["validation"]["ok"] is False
    assert "source_handle_unknown:P9999" in result["validation"]["errors"]


def test_without_fallback_preserves_explicit_handle_behavior(tmp_path: Path):
    packet = _packet([{
        "substantive_point": "fixture point",
        "paragraph_briefs": [{
            "point": "compare P0001",
            "development": "legacy text also names P0002",
            "source_handles": ["P0001"],
        }],
    }])
    packet_path = _write(tmp_path, packet)
    view = build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    assert {item.source_handle for item in view.sources} == {"P0001"}


def _ch6_arrangement_payload() -> dict:
    return {
        "chapter_id": "Ch6",
        "chapter_argument": "efficacy and toxicity",
        "units": [
            {
                "unit_id": f"Ch6_U{unit_index:02d}",
                "paragraph_tasks": [
                    {
                        "paragraph_id": f"Ch6_U{unit_index:02d}_P{task_index:02d}",
                        "point": "fixture",
                        "source_uses": [],
                    }
                    for task_index in range(1, 4)
                ],
            }
            for unit_index in range(1, 5)
        ],
        "safety_table": [{"endpoint": "irAE", "interpretation": "有效且无 irAE\"状态"}],
    }


def test_inner_quote_normalization_preserves_all_units_and_safety_table():
    valid = json.dumps(_ch6_arrangement_payload(), ensure_ascii=False)
    escaped_quote = "irAE" + "\\" + '"状态'
    malformed = valid.replace(escaped_quote, 'irAE"状态', 1)

    parsed = parse_arrangement_response(malformed)

    assert [unit["unit_id"] for unit in parsed["units"]] == [
        "Ch6_U01", "Ch6_U02", "Ch6_U03", "Ch6_U04",
    ]
    assert sum(len(unit["paragraph_tasks"]) for unit in parsed["units"]) == 12
    assert parsed["safety_table"] == _ch6_arrangement_payload()["safety_table"]


def test_valid_escaped_quotes_are_unchanged():
    valid = json.dumps(_ch6_arrangement_payload(), ensure_ascii=False)

    parsed = parse_arrangement_response(valid)

    assert parsed == _ch6_arrangement_payload()


def test_incomplete_payload_still_fails_validation_without_fabrication(tmp_path: Path):
    packet = _packet(_paragraph("P0001") + _paragraph("P0001"))
    packet_path = _write(tmp_path, packet)
    view = build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    incomplete = {
        "chapter_id": "ChX",
        "units": [{
            "unit_id": "ChX_U01",
            "paragraph_tasks": [{
                "paragraph_id": "ChX_U01_P01",
                "source_uses": [{"source_handle": "P0001", "role": "main", "use": "fixture"}],
            }],
        }],
    }

    result = validate_arrangement(incomplete, view, planning_revision=True)

    assert result["validation"]["ok"] is False
    assert "units_missing:ChX_U02" in result["validation"]["errors"]
    assert len(result["units"]) == 1
