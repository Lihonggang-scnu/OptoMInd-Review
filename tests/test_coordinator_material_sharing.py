"""Coordinator context sharing keeps complete evidence and chapter bindings."""
import copy
import json
import unittest

from optomind_research.runtime.upgrade3.progressive_review_plan import (
    _messages_for,
    _share_exact_review_materials,
)


def resolve_shared(document):
    def resolve(value):
        if isinstance(value, dict) and set(value) == {"$ref"}:
            target = document
            for part in value["$ref"][2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[int(part)] if isinstance(target, list) else target[part]
            return resolve(target)
        if isinstance(value, dict):
            return {key: resolve(child) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child) for child in value]
        return value
    return resolve(document)


class CoordinatorMaterialSharingTest(unittest.TestCase):
    def setUp(self):
        self.material = {
            "finding": "A useful result with its experimental contrast. " * 80,
            "conditions": ["specified population", "specified timing"],
            "negative_result": "The alternative intervention did not improve the outcome.",
            "provenance": {"reported_by": "review-A", "original_reference": "R17"},
        }

    def test_all_chapter_uses_and_complementary_evidence_survive(self):
        additional = {**self.material, "conditions": ["different setting"]}
        payload = {"chapters": [
            {"chapter_id": "C1", "source_materials": [
                {"paper_id": "stable-1", "source_handle": "P0001", "A": self.material}]},
            {"chapter_id": "C2", "source_materials": [
                {"paper_id": "stable-1", "source_handle": "alias-1", "A": self.material,
                 "deep_read_material": additional}]},
        ], "tool_materials": [self.material]}
        before = copy.deepcopy(payload)
        shared, count = _share_exact_review_materials(payload)
        self.assertGreater(count, 0)
        self.assertEqual(resolve_shared(shared), before)
        self.assertEqual(payload, before)
        self.assertEqual(shared["chapters"][1]["source_materials"][0]["paper_id"], "stable-1")
        self.assertEqual(shared["chapters"][1]["source_materials"][0]["source_handle"], "alias-1")
        self.assertEqual(shared["chapters"][1]["source_materials"][0]["deep_read_material"]["conditions"],
                         ["different setting"])

    def test_different_content_is_not_merged_by_paper_id(self):
        payload = {"old": {"paper_id": "same-id", "A": self.material},
                   "new": {"paper_id": "same-id", "A": {**self.material, "negative_result": "Correction"}}}
        shared, _ = _share_exact_review_materials(payload)
        self.assertEqual(resolve_shared(shared), payload)
        self.assertNotEqual(shared["new"].get("$ref"), "#/old")

    def test_json_pointer_handles_slash_tilde_and_array(self):
        payload = {"a/b~c": [self.material], "other": self.material}
        shared, _ = _share_exact_review_materials(payload)
        self.assertEqual(shared["other"], {"$ref": "#/a~1b~0c/0"})
        self.assertEqual(resolve_shared(shared), payload)

    def test_long_supplementary_answer_keeps_its_full_text(self):
        answer = json.dumps(self.material, ensure_ascii=False)
        payload = {"usable_content": answer, "chapters": [{"usable_content": answer}]}
        shared, _ = _share_exact_review_materials(payload)
        self.assertEqual(shared["usable_content"], answer)
        self.assertEqual(resolve_shared(shared), payload)

    def test_only_coordinator_messages_share_context(self):
        payload = {"first": self.material, "second": self.material}
        detail = _messages_for("chapter_details", payload)
        self.assertIn(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), detail[1]["content"])
        coordinator = _messages_for("whole_plan_improvement", payload)
        self.assertIn('"$ref":"#/first"', coordinator[1]["content"])
        self.assertIn("不是缺失材料或外部文件", coordinator[1]["content"])
        self.assertLess(len(coordinator[1]["content"]), len(detail[1]["content"]))

    def test_normal_small_request_remains_unchanged(self):
        payload = {"chapters": [{"chapter_id": "C1", "claim": "Short complete task"}]}
        shared, count = _share_exact_review_materials(payload)
        self.assertEqual(count, 0)
        self.assertEqual(shared, payload)
        self.assertNotIn("材料共享说明", _messages_for("whole_plan_improvement", payload)[1]["content"])


if __name__ == "__main__":
    unittest.main()
