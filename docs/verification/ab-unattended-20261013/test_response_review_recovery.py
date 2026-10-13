"""Offline checks for additive root format review recovery; fake client only."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


SUPERVISOR = Path(r"F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\preparation\supervisor")
TEST_ROOT = Path(r"F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny\preparation\ab_unattended_20261013")
sys.path.insert(0, str(SUPERVISOR))

from supervised_client import (  # noqa: E402
    SupervisedClient,
    SupervisionError,
    call_directory,
    decide,
    review_response,
)


PROFILE = {
    "model": "fake-only",
    "max_output_tokens": 64,
    "thinking_budget": 0,
    "max_retries": 0,
    "max_keys": 1,
}
MESSAGES = [{"role": "user", "content": "format-review-fake"}]


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("bounded_fake_test_wait_expired")


def rejected_gate(root):
    stage, call_id = "format-review-test", "rejected"
    directory = call_directory(root, stage, call_id)
    calls, errors = [], []

    def provider(messages, **kwargs):
        calls.append((messages, kwargs))
        return {"content": "saved-response", "complete": True, "finish_reason": "stop"}

    adapter = SupervisedClient(
        provider, root=root, stage=stage, call_id=call_id,
        profile=PROFILE, poll_seconds=0.01,
    )

    def run():
        try:
            adapter(MESSAGES, model="fake-only")
        except BaseException as error:  # the response rejection is expected
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    wait_for(lambda: (directory / "REQUEST.json").exists())
    decide(root=root, stage=stage, call_id=call_id, phase="request", decision="approve")
    wait_for(lambda: (directory / "RESPONSE.json").exists())
    decide(root=root, stage=stage, call_id=call_id, phase="response", decision="reject")
    thread.join(5)
    if thread.is_alive():
        raise AssertionError("response_gate_thread_did_not_finish")
    if len(errors) != 1 or str(errors[0]) != "response_rejected":
        raise AssertionError(f"unexpected_initial_gate_error:{errors!r}")
    if len(calls) != 1:
        raise AssertionError(f"initial_provider_calls:{len(calls)}")
    return stage, call_id, directory, calls


class ResponseReviewRecoveryTests(unittest.TestCase):
    def test_review_replays_saved_response_once_and_preserves_old_decision(self):
        with tempfile.TemporaryDirectory(prefix="format-review-", dir=TEST_ROOT) as temp:
            root = Path(temp)
            stage, call_id, directory, calls = rejected_gate(root)
            decision_path = directory / "RESPONSE_DECISION.json"
            old_decision = decision_path.read_bytes()
            review = review_response(
                root=root, stage=stage, call_id=call_id, reviewer="root",
                decision="approve", reason="root-format-reparse: native parser accepted saved raw",
            )
            self.assertEqual(review["decision"], "approve")
            self.assertEqual(old_decision, decision_path.read_bytes())

            replay_calls = []

            def no_provider(*args, **kwargs):
                replay_calls.append((args, kwargs))
                raise AssertionError("format_review_replay_called_provider")

            result = SupervisedClient(
                no_provider, root=root, stage=stage, call_id=call_id,
                profile=PROFILE, poll_seconds=0.01,
            )(MESSAGES, model="fake-only")
            self.assertEqual(result["content"], "saved-response")
            self.assertEqual(len(calls), 1)
            self.assertEqual(replay_calls, [])
            self.assertEqual(old_decision, decision_path.read_bytes())

    def test_unreviewed_reject_and_mutations_remain_rejected(self):
        mutations = ("raw", "request", "decision", "unreviewed")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory(prefix=f"format-review-{mutation}-", dir=TEST_ROOT) as temp:
                    root = Path(temp)
                    stage, call_id, directory, calls = rejected_gate(root)
                    if mutation != "unreviewed":
                        review_response(
                            root=root, stage=stage, call_id=call_id, reviewer="root",
                            decision="approve", reason="root-format-reparse: offline mutation test",
                        )
                    if mutation == "raw":
                        path = directory / "RESPONSE.json"
                        record = json.loads(path.read_bytes())
                        record["result"]["content"] = "tampered-response"
                        path.write_text(json.dumps(record), encoding="utf-8")
                        expected = "response_integrity_failure"
                    elif mutation == "request":
                        path = directory / "REQUEST.json"
                        record = json.loads(path.read_bytes())
                        record["messages"][0]["content"] = "tampered-request"
                        path.write_text(json.dumps(record), encoding="utf-8")
                        expected = "immutable_artifact_conflict"
                    elif mutation == "decision":
                        path = directory / "RESPONSE_DECISION.json"
                        record = json.loads(path.read_bytes())
                        record["decision"] = "approve"
                        path.write_text(json.dumps(record), encoding="utf-8")
                        expected = "response_review_integrity_failure"
                    else:
                        expected = "response_rejected"
                        self.assertFalse((directory / "RESPONSE_REVIEW.json").exists())

                    replay_calls = []

                    def no_provider(*args, **kwargs):
                        replay_calls.append((args, kwargs))
                        raise AssertionError("rejected_replay_called_provider")

                    adapter = SupervisedClient(
                        no_provider, root=root, stage=stage, call_id=call_id,
                        profile=PROFILE, poll_seconds=0.01,
                    )
                    with self.assertRaisesRegex(SupervisionError, expected):
                        adapter(MESSAGES, model="fake-only")
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(replay_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
