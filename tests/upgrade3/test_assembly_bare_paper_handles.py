"""Offline display projection: explicit known handles, no source mutation."""
import json
from pathlib import Path

import pytest

from scripts.upgrade3 import full_review_draft as assembly


def identity():
    return assembly.IdentityIndex([{
        "P0096": {"paper_id": "offline-study-a", "title": "Synthetic study A", "aliases": ["P0996"]},
        "P0123": {"doi": "10.1234/offline-b", "title": "Synthetic study B"},
    }])


@pytest.mark.parametrize("body, expected, count", [
    ("研究P0096提示；P0123研究与别名P0996均可定位。", "研究[P0096]提示；[P0123]研究与别名[P0996]均可定位。", 3),
    ("[P0096] [P0096,P0123] [参考P0096] [11] [P0096][P0123]", "[P0096] [P0096,P0123] [参考P0096] [11] [P0096][P0123]", 0),
    ("P9999未知 P53蛋白 AP0096 P0096suffix 7P0096 P0096_foo _P0096 P00967", "P9999未知 P53蛋白 AP0096 P0096suffix 7P0096 P0096_foo _P0096 P00967", 0),
])
def test_known_bare_handles_and_ascii_identifier_boundaries(body, expected, count):
    output, repairs = assembly.normalize_bare_paper_handles(body, identity())
    assert output == expected and len(repairs) == count
    for repair in repairs:
        assert body[repair["start"]:repair["end"]] == repair["original"]
        assert repair["replacement"] == "[" + repair["original"] + "]"


def test_code_links_images_urls_reference_links_and_tables_stay_unchanged():
    protected = ("`P0096`\n```python\nP0096\n```\n~~~\nP0096\n~~~\n"
                 "    P0096\n[P0096 study](https://example.invalid/P0096) "
                 "![P0096](P0096.png) [P0096 study][label]\n"
                 "[label]: https://example.invalid/P0096\n"
                 "https://example.invalid/P0096 ftp://example.invalid/P0096 www.example.invalid/P0096\n"
                 "|来源|结果|\n|---|---|\n|P0096|P0123|\n")
    output, repairs = assembly.normalize_bare_paper_handles(protected, identity())
    assert output == protected and repairs == []


def test_repairs_use_original_unicode_offsets_and_are_idempotent():
    body = "标题\r\n研究P0096支持观察。\r\n研究P0123支持边界。"
    output, repairs = assembly.normalize_bare_paper_handles(body, identity())
    assert [(r["line"], r["column"]) for r in repairs] == [(2, 3), (3, 3)]
    assert output == body.replace("P0096", "[P0096]").replace("P0123", "[P0123]")
    assert assembly.normalize_bare_paper_handles(output, identity()) == (output, [])


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_final_numbering_references_and_table_path_preserve_original_files(tmp_path):
    body = ("研究P0096支持观察，研究P0123给出边界。[P0096,P0123]\n\n"
            "`P0096` https://example.invalid/P0096 P9999未知 P53蛋白 [11]\n\n"
            "|来源|结果|\n|---|---|\n|P0123|合并观察|\n")
    source = tmp_path / "source"
    source.mkdir()
    body_path = source / "UNIT_BODY.md"
    body_path.write_bytes(body.encode("utf-8"))
    result_path = put(source / "UNIT_RESULT.json", {"chapter_id": "Ch1", "unit_id": "U1",
        "body_markdown": body, "body_path": str(body_path), "complete": True, "mode": "run", "issues": []})
    raw_path = put(source / "RAW_RESPONSE.json", {"content": body, "complete": True, "finish_reason": "stop"})
    arrangement_path = put(tmp_path / "arrangement.json", {"chapter_id": "Ch1", "title": "Offline projection",
        "source_catalog": identity().entries,
        "units": [{"unit_id": "U1", "focus": "Synthetic evidence", "table_tasks": []}]})
    manifest = put(tmp_path / "manifest.json", {"title": "Offline projection", "planning_status": "complete",
        "arrangement_status": "complete", "chapters": [{"chapter_id": "Ch1", "arrangement_path": str(arrangement_path)}]})
    batch = tmp_path / "batch"
    put(batch / "BATCH_JOBS.json", [{"chapter_id": "Ch1", "unit_id": "U1", "arrangement": str(arrangement_path),
        "reused_result": str(result_path), "output": str(source)}])
    before = {path: path.read_bytes() for path in (body_path, result_path, raw_path, arrangement_path)}
    output = tmp_path / "assembled"
    assert assembly.main(["--manifest", str(manifest), "--batch-root", str(batch), "--output-root", str(output)]) == 0
    summary = json.loads((output / "ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    refs = json.loads((output / "REFERENCES.json").read_text(encoding="utf-8"))
    numeric = (output / "REVIEW_DRAFT.md").read_text(encoding="utf-8")
    handles = (output / "REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")
    assert "研究[1]支持观察，研究[2]给出边界。[1,2]" in numeric
    assert "研究[P0096]支持观察，研究[P0123]给出边界。" in handles
    assert "`P0096` https://example.invalid/P0096 P9999未知 P53蛋白 [11]" in numeric
    assert "|[2]|合并观察|" in numeric and "[[2]]" not in numeric
    assert summary["bare_handle_replacements"] == 2 and summary["table_handle_replacements"] == 1
    assert refs["handle_to_reference"]["P0096"] == 1 and refs["handle_to_reference"]["P0123"] == 2
    assert [r["used_handles_in_text"] for r in refs["references"]] == [["P0096"], ["P0123"]]
    assert summary["bare_handle_repairs"][0]["result_path"] == str(result_path)
    assert all(path.read_bytes() == content for path, content in before.items())
