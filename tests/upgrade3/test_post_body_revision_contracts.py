"""Snapshot safety tests; scientific entailment belongs to the review layer."""
import json

import unittest
import tempfile
from pathlib import Path

from optomind_research.runtime.upgrade3.post_body_revision_contracts import (
    RevisionContractError, apply_patches, evidence_for_issue, load_case,
    normalize_case, resolve_target, sha256_text, validate_patch,
)


def case(text="Alpha result.\n\nBeta result.\n", **extra):
    return normalize_case({"case_id": "test", "draft_text": text, **extra})


def issue(c, original="Alpha result.", **extra):
    return {"issue_id": "I1", "kind": "editorial", "target_block_id": c["blocks"][0]["block_id"],
            "original_text": original, "operation": "replace", "evidence_ids": [], **extra}


class PostBodyRevisionContractTests(unittest.TestCase):
    def test_duplicate_across_blocks_is_safe_but_within_block_is_ambiguous(self):
        c = case("Same.\n\nSame.\n")
        p = validate_patch(c, issue(c, "Same."), {"replacement_text": "The same."})
        assert apply_patches(c, [p])["draft_text"] == "The same.\n\nSame.\n"
        with self.assertRaisesRegex(RevisionContractError, "not_unique"):
            resolve_target(case("Same. Same."), issue(c, "Same."))
        with self.assertRaisesRegex(RevisionContractError, "not_unique"):
            resolve_target(case("aaa"), issue(c, "aa"))


    def test_fence_table_unicode_and_crlf_snapshot_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            text = "# 标题\r\n\r\n```python\r\nx = 1\r\n\r\ny = 2\r\n```\r\n\r\n| A | B |\r\n| - | - |\r\n| 1 | 2 |\r\n\r\n尾句。\r\n"
            (tmp_path / "body.md").write_bytes(text.encode())
            (tmp_path / "material.txt").write_bytes(b"Evidence\r\nFull text\r\n")
            (tmp_path / "case.json").write_text(json.dumps({"draft_file": "body.md", "materials": {"M1": {"text_file": "material.txt"}}}))
            c = load_case(tmp_path / "case.json")
            assert c["draft_text"].encode() == text.encode()
            assert len(c["blocks"]) == 4
            assert "x = 1\r\n\r\ny = 2" in c["blocks"][1]["text"]
            assert c["materials"]["M1"]["text"] == "Evidence\r\nFull text\r\n"
            p = validate_patch(c, issue(c, "标题"), {"replacement_text": "新标题"})
            assert apply_patches(c, [p])["draft_text"].encode() == text.replace("标题", "新标题", 1).encode()


    def test_case_hash_and_generated_blocks_cannot_be_forged(self):
        c = case()
        with self.assertRaisesRegex(RevisionContractError, "stale"):
            normalize_case({**c, "draft_text": "Changed"})
        c["blocks"][0]["start"] = 2
        with self.assertRaisesRegex(RevisionContractError, "snapshot"):
            normalize_case(c)


    def test_scientific_and_missing_require_actual_material(self):
        for kind in ["scientific", "missing"]:
            c = case(materials={"M1": {"summary": "Summary is insufficient"}})
            with self.assertRaisesRegex(RevisionContractError, "requires_material"):
                evidence_for_issue(c, issue(c, kind=kind))
            with self.assertRaisesRegex(RevisionContractError, "empty_evidence"):
                evidence_for_issue(c, issue(c, kind=kind, evidence_ids=["M1"]))
            with self.assertRaisesRegex(RevisionContractError, "unknown_evidence"):
                evidence_for_issue(c, issue(c, kind=kind, evidence_ids=["M2"]))


    def test_evidence_is_exact_full_record_and_citation_identity_is_exact(self):
        c = case(materials={"M1": {"text": "Full material", "title": "Paper", "summary": "Short", "source_handles": ["P0001"]}},
                 source_identity_map={"P0002": {"material_id": "M1"}, "P0003": {"title": "Paper"}})
        i = issue(c, kind="scientific", evidence_ids=["M1"])
        assert evidence_for_issue(c, i)["M1"]["text"] == "Full material"
        for handle in ["P0001", "P0002"]:
            validate_patch(c, i, {"replacement_text": "New supported result [" + handle + "]."})
        for handle in ["P0003", "P9999"]:
            with self.assertRaisesRegex(RevisionContractError, "unsupported_introduced_citation"):
                validate_patch(c, i, {"replacement_text": "New result [" + handle + "]."})


    def test_editorial_allows_paraphrase_but_no_new_numbers_or_citations(self):
        c = case(materials={"M1": {"text": "Full text", "source_handles": ["P0001"]}})
        validate_patch(c, issue(c), {"replacement_text": "An editorial paraphrase."})
        with self.assertRaisesRegex(RevisionContractError, "introduces_number"):
            validate_patch(c, issue(c), {"replacement_text": "Alpha result 99."})
        with self.assertRaisesRegex(RevisionContractError, "introduces_citation"):
            validate_patch(c, issue(c, evidence_ids=["M1"]), {"replacement_text": "Alpha result [P0001]."})


    def test_removal_can_remove_citation_when_not_explicitly_preserved(self):
        c = case("Overclaim [P0001].\n")
        p = validate_patch(c, issue(c, "Overclaim [P0001].", operation="remove"), {})
        assert apply_patches(c, [p])["draft_text"] == "\n"
        with self.assertRaisesRegex(RevisionContractError, "preserve"):
            validate_patch(c, issue(c, "Overclaim [P0001].", operation="remove", preserve=["[P0001]"]), {})


    def test_nonoverlap_apply_uses_original_offsets_atomically(self):
        c = case()
        p1 = validate_patch(c, issue(c), {"replacement_text": "A much longer editorial paraphrase."})
        p2 = validate_patch(c, issue(c, "Beta result.", issue_id="I2", target_block_id="B0002"), {"replacement_text": "Shorter."})
        result = apply_patches(c, [p2, p1])
        assert result["draft_text"] == "A much longer editorial paraphrase.\n\nShorter.\n"
        assert c["draft_text"] == "Alpha result.\n\nBeta result.\n"
        assert result["skipped"] == []
        overlap = validate_patch(c, issue(c, "Alpha", issue_id="I3"), {"replacement_text": "New"})
        with self.assertRaisesRegex(RevisionContractError, "overlapping"):
            apply_patches(c, [p1, overlap])
        assert c["draft_text"] == "Alpha result.\n\nBeta result.\n"


    def test_replay_stale_bindings_unbound_and_insert_conflicts(self):
        c = case()
        i = issue(c, operation="insert_after")
        p = validate_patch(c, i, {"replacement_text": " Extra sentence."})
        result = apply_patches(c, [p])
        assert result["draft_text"].startswith("Alpha result. Extra sentence.")
        changed = case(result["draft_text"])
        with self.assertRaisesRegex(RevisionContractError, "binding_mismatch"):
            apply_patches(changed, [p])
        with self.assertRaisesRegex(RevisionContractError, "duplicate_patch"):
            apply_patches(c, [p, p])
        p2 = {**p, "issue_id": "I2"}
        with self.assertRaisesRegex(RevisionContractError, "overlapping"):
            apply_patches(c, [p, p2])
        with self.assertRaisesRegex(RevisionContractError, "unbound"):
            apply_patches(c, [{"replacement_text": "x"}])
        with self.assertRaisesRegex(RevisionContractError, "binding_mismatch"):
            validate_patch(c, i, {"replacement_text": "x", "base_sha256": sha256_text("other")})


    def test_preserve_anchor_and_unknown_operation_fail_closed(self):
        c = case()
        with self.assertRaisesRegex(RevisionContractError, "preserve"):
            validate_patch(c, issue(c, preserve=["Alpha"]), {"replacement_text": "Beta."})
        with self.assertRaisesRegex(RevisionContractError, "unknown_operation"):
            validate_patch(c, issue(c, operation="global_replace"), {"replacement_text": "Beta."})
        with self.assertRaisesRegex(RevisionContractError, "unknown_target"):
            resolve_target(c, issue(c, target_block_id="B9999"))


    def test_proposal_evidence_is_unique_subset_of_issue_selection(self):
        c = case(materials={"M1": {"text": "Full material", "source_handles": ["P123"]},
                            "M2": {"text": "Other material"}})
        i = issue(c, kind="scientific", evidence_ids=["M1"])
        for ids, message in [(["invented"], "not_selected"), (["M2"], "not_selected"),
                             (["M1", "M1"], "duplicate"), ("M1", "invalid"),
                             ([], "requires_material")]:
            with self.assertRaisesRegex(RevisionContractError, message):
                validate_patch(c, i, {"replacement_text": "Revised result.", "evidence_ids": ids})
        p = validate_patch(c, i, {"replacement_text": "Supported [P123].", "evidence_ids": ["M1"]})
        assert p["evidence_ids"] == ["M1"]
        assert apply_patches(c, [p])["draft_text"].startswith("Supported [P123].")

    def test_citation_parser_masks_code_links_and_accepts_three_digit_handles(self):
        c = case(materials={"M1": {"text": "Full material"}})
        i = issue(c, kind="scientific", evidence_ids=["M1"])
        replacements = ["Variable P9999 remains an identifier.",
                        "Code `lookup[P9999]` remains code.",
                        "[P9999](https://example.org/P9999) is a link.",
                        "```python\nlookup[P9999]\n```"]
        for replacement in replacements:
            validate_patch(c, i, {"replacement_text": replacement})
            validate_patch(c, issue(c), {"replacement_text": replacement})
        with self.assertRaisesRegex(RevisionContractError, "unsupported_introduced_citation"):
            validate_patch(c, i, {"replacement_text": "New citation [P123]."})


if __name__ == "__main__":
    unittest.main()
