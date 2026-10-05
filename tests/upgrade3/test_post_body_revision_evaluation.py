"""SYNTHETIC fixtures only: no domain answer key or scientific success claims."""
import hashlib
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from optomind_research.runtime.upgrade3.post_body_revision_contracts import normalize_case
from optomind_research.runtime.upgrade3.post_body_revision_evaluation import (
    prepare, report, fingerprint, evidence_fingerprint, read, validate_judgment,
)


class BlindedEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = normalize_case({"case_id": "SYNTHETIC", "draft_text": "A sound sentence.\n",
            "materials": {"M1": {"text": "Synthetic supporting text."}}, "outline": "Synthetic outline"})
        self.cp = self.root / "case.json"
        self.cp.write_text(json.dumps(self.case))
        self.runs = []
        for variant in "ABC":
            folder = self.root / variant
            folder.mkdir()
            (folder / "baseline.md").write_bytes(self.case["draft_text"].encode("utf-8"))
            (folder / "candidate.md").write_bytes(self.case["draft_text"].encode("utf-8"))
            value = {"variant": variant, "case_id": "SYNTHETIC", "base_sha256": self.case["base_sha256"],
                     "case_fingerprint": fingerprint(self.case), "evidence_fingerprint": evidence_fingerprint(self.case),
                     "baseline_path": "baseline.md", "candidate_path": "candidate.md", "run_completed": True,
                     "generation_mode": "live", "usage": {"cost_cny": 1}, "latency_seconds": 2,
                     "call_records": [{"usage": {"cost_cny": 1}}], "applied_patches": [],
                     "model": "SECRET-MODEL", "reason": "SECRET-REASON", "status": "quality_pass"}
            contract = {"case": self.case, "config": {"variant": variant}}
            value["fingerprint"] = fingerprint(contract)
            value["candidate_sha256"] = hashlib.sha256(self.case["draft_text"].encode()).hexdigest()
            (folder / "manifest.json").write_text(json.dumps({"fingerprint": fingerprint(contract), **contract}))
            (folder / "report.json").write_text(json.dumps(value))
            self.runs.append(folder)
        self.out = self.root / "evaluation"

    def judgments(self):
        rows = []
        for path in sorted((self.out / "packets").glob("*.json")):
            packet = read(path)
            rows.append({**{k: packet[k] for k in ("case_id", "pair_id", "pair_input_sha256")},
                "assessment_state": "complete", "winner": "tie", "reason": "Both preserve the same supported sentence.",
                "knowledge_preservation": "preserved", "resolved_issues": [], "regressions": [], "remaining_issues": []})
        target = self.root / "judgments.json"
        target.write_text(json.dumps(rows))
        return target, rows

    def test_blinded_packets_and_resume_hash_guards(self):
        manifest = prepare(self.cp, self.runs, self.out, seed=7, swap_fraction=1)
        self.assertEqual(len(list((self.out / "packets").glob("*.json"))), 6)
        for p in (self.out / "packets").glob("*.json"):
            raw = p.read_text()
            for secret in ("SECRET-MODEL", "SECRET-REASON", "generation_cost", "variant", "candidate_side"):
                self.assertNotIn(secret, raw)
        self.assertEqual(manifest, prepare(self.cp, self.runs, self.out, seed=7, swap_fraction=1, resume=True))
        with self.assertRaisesRegex(ValueError, "experiment_mismatch"):
            prepare(self.cp, self.runs, self.out, seed=8, resume=True)
        p = next((self.out / "packets").glob("*.json"))
        p.write_text(p.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            prepare(self.cp, self.runs, self.out, seed=7, swap_fraction=1, resume=True)

    def test_same_case_and_evidence_required(self):
        rp = self.runs[1] / "report.json"
        value = read(rp)
        value["case_fingerprint"] = "different-outline"
        rp.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "case_fingerprint_mismatch"):
            prepare(self.cp, self.runs, self.out)
        self.assertFalse(self.out.exists())

    def test_missing_invalid_and_uncertain_never_pass(self):
        prepare(self.cp, self.runs, self.out)
        path, rows = self.judgments()
        rows[0]["pair_input_sha256"] = "stale"
        rows[1]["assessment_state"] = "insufficient_evidence"
        rows.pop()
        path.write_text(json.dumps(rows))
        result = report(self.out, path)
        self.assertEqual(result["qualified_pareto_variants"], [])
        self.assertEqual({r["state"] for r in result["pairs"]}, {"invalid", "missing", "inconclusive"})
        self.assertIsNone(result["evaluation_cost_cny"])

    def test_recording_and_unknown_or_partial_cost_excluded(self):
        for index, mode in ((0, "recording"), (1, "live"), (2, "live")):
            rp = self.runs[index] / "report.json"
            value = read(rp)
            value["generation_mode"] = mode
            if index == 1: value["usage"] = {}
            if index == 2: value["call_records"].append({"usage": {}})
            rp.write_text(json.dumps(value))
        prepare(self.cp, self.runs, self.out)
        path, _ = self.judgments()
        result = report(self.out, path)
        self.assertEqual(result["qualified_pareto_variants"], [])
        self.assertIn("not_real_generation", result["methods"][0]["exclusion_reasons"])
        self.assertIsNone(result["methods"][1]["generation_cost_cny"])
        self.assertIsNone(result["methods"][2]["generation_cost_cny"])

    def test_program_guards_independent_of_report_status(self):
        (self.runs[0] / "candidate.md").write_text("Unauthorized unrelated rewrite.")
        rp = self.runs[0] / "report.json"
        value = read(rp)
        value["candidate_sha256"] = hashlib.sha256((self.runs[0] / "candidate.md").read_bytes()).hexdigest()
        rp.write_text(json.dumps(value))
        prepare(self.cp, self.runs, self.out)
        path, _ = self.judgments()
        result = report(self.out, path)
        self.assertIn("program_guard_not_verified", result["methods"][0]["exclusion_reasons"])

    def test_position_disagreement_inconclusive(self):
        prepare(self.cp, self.runs, self.out, swap_fraction=1)
        path, rows = self.judgments()
        decode = read(self.out / "decode_key.json")["pairs"]
        for j in rows:
            if decode[j["pair_id"]]["variant"] == "A":
                j["winner"] = "left"  # Fixed position wins in both opposite orderings.
        path.write_text(json.dumps(rows))
        result = report(self.out, path)
        self.assertTrue(result["methods"][0]["position_disagreement"])
        self.assertEqual(result["methods"][0]["candidate_outcome"], "inconclusive")
        self.assertNotIn("A", result["qualified_pareto_variants"])

    def test_strict_schema_and_actual_quote_evidence(self):
        prepare(self.cp, self.runs, self.out)
        _, rows = self.judgments()
        j = rows[0]
        packet = read(self.out / "packets" / (j["pair_id"] + ".json"))
        item = {"kind": "editorial", "affected_side": "left", "target_quote": "A sound sentence.", "evidence_ids": ["M1"],
                "reason": "Synthetic comparison only.", "severity": "minor", "knowledge_loss": False}
        j["remaining_issues"] = [item]
        validate_judgment(j, packet)
        item["evidence_ids"] = ["M999"]
        with self.assertRaisesRegex(ValueError, "unknown_evidence"):
            validate_judgment(j, packet)
        item["evidence_ids"] = []
        item["target_quote"] = "Invented quotation"
        with self.assertRaisesRegex(ValueError, "quote_not_in_text"):
            validate_judgment(j, packet)

    def test_scientific_findings_require_evidence_and_severe_harm_excluded(self):
        prepare(self.cp, self.runs, self.out)
        path, rows = self.judgments()
        j = rows[0]
        packet = read(self.out / "packets" / (j["pair_id"] + ".json"))
        side = read(self.out / "decode_key.json")["pairs"][j["pair_id"]]["candidate_side"]
        item = {"kind": "scientific", "affected_side": side, "target_quote": "A sound sentence.",
                "evidence_ids": [], "reason": "Synthetic harmful change example.", "severity": "critical", "knowledge_loss": True}
        j["knowledge_preservation"] = "loss"
        j["regressions"] = [item]
        with self.assertRaisesRegex(ValueError, "requires_evidence"):
            validate_judgment(j, packet)
        item["evidence_ids"] = ["M1"]
        path.write_text(json.dumps(rows))
        result = report(self.out, path)
        affected = next(m for m in result["methods"] if m["exclusion_reasons"])
        self.assertIn("critical_regression_requires_review", affected["exclusion_reasons"])
        self.assertIn("material_knowledge_loss_requires_review", affected["exclusion_reasons"])
        self.assertFalse(affected["pareto_eligible"])

    def test_unsettled_calls_override_claimed_complete_cost(self):
        rp = self.runs[0] / "report.json"
        value = read(rp)
        value.update(cost_complete=True, cost_cny=1)
        value["call_records"].append({"status": "started"})
        rp.write_text(json.dumps(value))
        prepare(self.cp, self.runs, self.out)
        path, _ = self.judgments()
        result = report(self.out, path)
        self.assertIsNone(result["methods"][0]["generation_cost_cny"])

    def test_recorded_cli_end_to_end_without_fabricated_assessment(self):
        root = Path(__file__).resolve().parents[2]
        fixture = root / "tests/fixtures/post_body_revision"
        output = self.root / "recorded"
        for variant in "ABC":
            result = subprocess.run([sys.executable, str(root / "scripts/upgrade3/post_body_revision.py"),
                "run", "--case", str(fixture / "case.json"), "--variant", variant,
                "--recordings", str(fixture / "recordings.json"), "--output-root", str(output)],
                capture_output=True, text=True, cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
        cli = root / "scripts/upgrade3/evaluate_post_body_revision.py"
        result = subprocess.run([sys.executable, str(cli), "prepare", "--case", str(fixture / "case.json"),
            "--runs", *[str(output / variant / "recording") for variant in "ABC"],
            "--output-dir", str(self.out), "--seed", "11"], capture_output=True, text=True, cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        empty = self.root / "empty-judgments.json"
        empty.write_text("[]")
        result = subprocess.run([sys.executable, str(cli), "report", "--evaluation-dir", str(self.out),
            "--judgments", str(empty)], capture_output=True, text=True, cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["qualified_pareto_variants"], [])
        self.assertTrue(all(row["state"] == "missing" for row in value["pairs"]))

    def test_report_cli_utf8_output_idempotent_and_input_safe(self):
        prepare(self.cp, self.runs, self.out)
        judgments, rows = self.judgments()
        rows[0]["reason"] = "保留原有证据，不强行判优。"
        judgments.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        root = Path(__file__).resolve().parents[2]
        cli = root / "scripts/upgrade3/evaluate_post_body_revision.py"
        destination = self.out / "assessment-report.json"
        command = [sys.executable, str(cli), "report", "--evaluation-dir", str(self.out),
                   "--judgments", str(judgments), "--output"]
        def invoke(target):
            return subprocess.run([*command, str(target)], capture_output=True, text=True, cwd=root)
        result = invoke(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_bytes(), (result.stdout).encode("utf-8"))
        self.assertIn("保留原有证据".encode("utf-8"), destination.read_bytes())
        self.assertEqual(invoke(destination).returncode, 0)
        for target in (self.cp, judgments, self.out / "manifest.json", self.out / "decode_key.json",
                       next((self.out / "packets").glob("*.json"))):
            before = target.read_bytes()
            failed = invoke(target)
            self.assertEqual(failed.returncode, 2, failed.stdout)
            self.assertEqual(target.read_bytes(), before)
        destination.write_bytes(b"different experiment")
        self.assertEqual(invoke(destination).returncode, 2)
        self.assertEqual(destination.read_bytes(), b"different experiment")

    def test_no_input_overwrites(self):
        before = self.cp.read_bytes()
        with self.assertRaisesRegex(ValueError, "overlaps_inputs"):
            prepare(self.cp, self.runs, self.root)
        self.assertEqual(self.cp.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
