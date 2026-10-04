"""CJK and explicit English citation prefixes stay consumable as handles."""

import importlib.util
from pathlib import Path

from optomind_research.runtime.upgrade3 import review_unit_writer as writer
from scripts.upgrade3 import full_review_draft as assembler


ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / "docs/workorders/body-chain-20261003/records/body06/writer/capture.py"
_spec = importlib.util.spec_from_file_location("citation_prefix_fixture", CAPTURE)
_fixture = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_fixture)


def test_prefixed_handles_are_consumed_but_identifier_substrings_are_rejected():
    cases = {
        "[P0602]": ["P0602"],
        "[参考P0602]": ["P0602"],
        "[ref P0602]": ["P0602"],
        "[AP0602]": [],
        "[P0602suffix]": [],
        "[12345]": [],
    }
    for body, expected in cases.items():
        assert writer.citations_in(body) == expected
        assert assembler.citation_handles(body) == expected


def test_adjacent_prefixed_handles_are_consumed_and_links_stay_protected():
    body = "事实[参考P0602][参考P0564][参考P0576]。"
    assert writer.citations_in(body) == ["P0602", "P0564", "P0576"]
    assert assembler.citation_handles(body) == ["P0602", "P0564", "P0576"]
    assert writer.citations_in("[参考P0602](https://example.invalid)") == []
    assert writer.citations_in("[参考P0602][1]\n\n[1]: https://example.invalid") == []


def test_unknown_prefixed_handle_is_reported_and_body_is_unchanged(tmp_path):
    body = "事实[参考P9999]，数学12345，标识AP0602与P0602suffix。"
    report = writer.write_unit_output(
        _fixture.view(), body, tmp_path,
        model="offline-synthetic", language="zh", mode="live",
        used_messages=[], estimate={}, issues=[],
    )
    assert report["unknown_citations"] == ["P9999"]
    assert report["used_source_handles"] == ["P9999"]
    assert (tmp_path / "UNIT_BODY.md").read_text(encoding="utf-8") == body + "\n"


def test_assembly_prefixes_preserve_first_appearance_and_unknowns():
    text = "[参考P0602] [ref P0190] [参考P0602] [参考P9999]"
    assert assembler.citation_handles(text) == ["P0602", "P0190", "P9999"]


def test_known_prefixed_handles_render_as_bare_numbers_and_unknown_stays():
    identity = assembler.IdentityIndex([{
        "P0602": {"paper_id": "p0602"},
        "P0190": {"paper_id": "p0190"},
    }])
    numbered = assembler.replace_citations_numbered(
        "[参考P0602][ref P0190][参考P9999]",
        identity,
        {"P0602": 1, "P0190": 2},
    )
    assert numbered == "[1][2][参考P9999]"
