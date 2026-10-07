"""Offline wire-payload checks: no network, real credentials, or model calls.

Load the stdlib-only transport directly so these checks can also run with
``python tests/upgrade3/test_qwen_effective_token_transport.py`` without the
optional AgentScope runtime or a pytest installation.
"""
from __future__ import annotations

import importlib.util
import io
import json
import gc
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

_RUNTIME_PATH = Path(__file__).resolve().parents[2] / "optomind_research/runtime/upgrade3/module4/runtime.py"
_SPEC = importlib.util.spec_from_file_location("_offline_qwen_token_transport", _RUNTIME_PATH)
runtime = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runtime
_SPEC.loader.exec_module(runtime)


class FakeResponse:
    status = 200
    headers = {"x-request-id": "offline-request"}

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class FakeStreamResponse:
    status = 200
    headers = {"x-request-id": "offline-stream-request", "content-type": "text/event-stream"}

    def __init__(self, lines):
        self.lines = []
        for line in lines:
            self.lines.extend(line.splitlines(keepends=True))

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class CaptureOpener:
    def __init__(self, *, usage=None, finish_reason="stop", error=None):
        self.bodies = []
        self.usage = usage
        self.finish_reason = finish_reason
        self.error = error

    def open(self, request, timeout):
        body = json.loads(request.data)
        self.bodies.append(body)
        if self.error:
            raise self.error
        return FakeResponse({
            "id": "offline-response",
            "model": body["model"],
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": self.finish_reason}],
            "usage": self.usage if self.usage is not None else {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        })


class StreamOpener:
    def __init__(self, lines):
        self.lines = list(lines)
        self.bodies = []

    def open(self, request, timeout):
        self.bodies.append(json.loads(request.data))
        return FakeStreamResponse(self.lines)


class EffectiveTokenTransportTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(runtime.os.environ, {"OPTOMIND_ECONOMY_TEXT_CEILING": "0"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.key_patch = patch.object(runtime.QwenDirectClient, "_keys", return_value=["offline-placeholder"])
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def invoke(self, client, *, opener=None, **kwargs):
        opener = opener or CaptureOpener()
        with patch.object(runtime.urllib.request, "build_opener", return_value=opener):
            result = client([{"role": "user", "content": "Return JSON."}], **kwargs)
        return opener.bodies[-1], result

    def test_supported_models_send_combined_completion_budget(self):
        for model, answer, thinking in (
            ("qwen3.5-plus", 32_768, 16_384),
            ("qwen3.7-flash", 32_768, 16_384),
            ("qwen3.8-max", 65_536, 32_768),
        ):
            with self.subTest(model=model):
                client = runtime.QwenDirectClient(
                    model=model, max_output_tokens=answer, thinking_budget=thinking,
                    json_mode=model != "qwen3.5-plus",
                )
                body, result = self.invoke(client)
                self.assertEqual(body["max_completion_tokens"], answer + thinking)
                self.assertNotIn("max_tokens", body)
                self.assertNotIn("reasoning_effort", body)
                self.assertTrue(body["enable_thinking"])
                self.assertEqual(body["thinking_budget"], thinking)
                effective = result["effective_request"]
                self.assertEqual(effective["model"], model)
                self.assertEqual(effective["answer_tokens"], answer)
                self.assertEqual(effective["thinking_budget"], thinking)
                self.assertEqual(effective["max_completion_tokens"], body["max_completion_tokens"])
                self.assertIsNone(effective["max_tokens"])
                self.assertTrue(result["complete"])
                self.assertFalse(body["stream"])
                self.assertNotIn("stream_options", body)

    def test_opt_in_stream_preserves_answer_reasoning_usage_and_phase_audit(self):
        lines = [
            b'data: {"id":"stream-1","model":"qwen3.8-max","choices":[{"delta":{"reasoning_content":"check "},"finish_reason":null}]}\r\n\r\n',
            b'data: {"id":"stream-1","choices":[{"delta":{"content":"{\\"ok\\":"},"finish_reason":null}]}\r\n\r\n',
            b'data: {"id":"stream-1","choices":[{"delta":{"content":"true}"},"finish_reason":"stop"}]}\r\n\r\n',
            b'data: {"id":"stream-1","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":9,"completion_tokens_details":{"reasoning_tokens":2}}}\r\n\r\n',
            b'data: [DONE]\r\n\r\n',
        ]
        # Keep the second event visibly valid after constructing it with a
        # JSON round trip; the line above remains CRLF to exercise SSE parsing.
        lines[1] = (b'data: ' + json.dumps({
            "id": "stream-1", "choices": [{"delta": {"content": '{"ok":'}, "finish_reason": None}],
        }, separators=(",", ":")).encode() + b"\r\n\r\n")
        opener = StreamOpener(lines)
        with tempfile.TemporaryDirectory() as temp:
            events = []
            client = runtime.QwenDirectClient(
                model="qwen3.8-max", max_output_tokens=32768, thinking_budget=32768,
                json_mode=False, raw_response_dir=Path(temp), timeout_seconds=30,
            )
            body, result = self.invoke(client, opener=opener, stream=True, transport_observer=events, call_id="stream-test")
            self.assertTrue(body["stream"])
            self.assertEqual(body["stream_options"], {"include_usage": True})
            self.assertEqual(result["content"], '{"ok":true}')
            self.assertEqual(result["reasoning_content"], "check ")
            self.assertEqual(result["usage"]["prompt_tokens"], 12)
            self.assertTrue(result["complete"])
            self.assertEqual(result["finish_reason"], "stop")
            self.assertEqual(result["normalized_response"]["choices"][0]["message"]["content"], '{"ok":true}')
            self.assertTrue(any(event["stage"] == "response_opened" for event in events))
            self.assertTrue(any(event["stage"] == "stream_first_byte" for event in events))
            self.assertTrue(any(event["stage"] == "stream_complete" for event in events))
            self.assertTrue(Path(result["raw_stream_path"]).exists())

    def test_stream_error_event_preserves_partial_evidence_and_does_not_adopt(self):
        opener = StreamOpener([
            b'data: {"id":"stream-error","choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
            b'data: {"error":{"code":"provider_busy"}}\n\n',
        ])
        with tempfile.TemporaryDirectory() as temp:
            ledger = runtime.GlobalBudgetLedger(limit_cny=10)
            client = runtime.QwenDirectClient(
                model="qwen3.8-max", max_output_tokens=32768, thinking_budget=32768,
                json_mode=False, raw_response_dir=Path(temp), budget_ledger=ledger,
                prompt_token_counter=lambda *_: 100, max_retries=0,
            )
            with self.assertRaisesRegex(runtime.QwenTransportError, "qwen_stream_error_event") as caught:
                self.invoke(client, opener=opener, stream=True, call_id="stream-error")
            self.assertEqual(caught.exception.record["transport_phase"], "stream_event")
            self.assertGreater(caught.exception.record["partial_content_bytes"], 0)
            row = ledger.as_dict()["reservations"][0]
            self.assertEqual(row["status"], "uncertain")
            self.assertTrue(list(Path(temp).glob("*.sse.partial")))

    def test_stream_socket_timeout_records_read_phase(self):
        class TimeoutResponse(FakeStreamResponse):
            def readline(self):
                raise TimeoutError("offline timeout")

        class TimeoutOpener(StreamOpener):
            def open(self, request, timeout):
                self.bodies.append(json.loads(request.data))
                return TimeoutResponse([])

        opener = TimeoutOpener([])
        client = runtime.QwenDirectClient(model="qwen3.8-max", json_mode=False, max_retries=0)
        with self.assertRaisesRegex(runtime.QwenTransportError, "qwen_stream_read_timeout") as caught:
            self.invoke(client, opener=opener, stream=True, call_id="stream-timeout")
        self.assertEqual(caught.exception.record["transport_phase"], "stream_read")

    def test_stream_malformed_event_is_rejected_without_adoption(self):
        opener = StreamOpener([b"data: {not-json}\r\n\r\n"])
        with tempfile.TemporaryDirectory() as temp:
            client = runtime.QwenDirectClient(
                model="qwen3.8-max", json_mode=False, raw_response_dir=Path(temp), max_retries=0,
            )
            with self.assertRaisesRegex(runtime.QwenTransportError, "qwen_stream_event_not_json"):
                self.invoke(client, opener=opener, stream=True, call_id="stream-malformed")
            self.assertTrue(list(Path(temp).glob("*.sse.partial")))

    def test_stream_observer_failure_does_not_fail_complete_response(self):
        lines = [
            b'data: {"id":"observer-ok","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
            b'data: {"id":"observer-ok","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n',
            b"data: [DONE]\n\n",
        ]
        opener = StreamOpener(lines)
        def failing_observer(_event):
            raise RuntimeError("audit sink unavailable")
        client = runtime.QwenDirectClient(model="qwen3.8-max", json_mode=False, max_retries=0)
        _, result = self.invoke(client, opener=opener, stream=True, transport_observer=failing_observer)
        self.assertEqual(result["content"], "ok")
        self.assertTrue(result["complete"])

    def test_stream_audit_keeps_periodic_event_boundary(self):
        lines = [
            (b'data: ' + json.dumps({
                "id": "periodic", "choices": [{"delta": {"content": "x"}, "finish_reason": None}],
            }, separators=(",", ":")).encode() + b"\n\n")
            for _ in range(130)
        ]
        lines.extend([
            b'data: {"id":"periodic","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b'data: {"id":"periodic","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":131}}\n\n',
            b"data: [DONE]\n\n",
        ])
        opener = StreamOpener(lines)
        events = []
        client = runtime.QwenDirectClient(model="qwen3.8-max", json_mode=False, max_retries=0)
        _, result = self.invoke(client, opener=opener, stream=True, transport_observer=events)
        self.assertEqual(len(result["content"]), 130)
        self.assertTrue(any(event.get("event_count") == 128 for event in events))
        self.assertTrue(any(event["stage"] == "stream_complete" for event in events))

    def test_max_strong_profile_is_not_artificially_capped_at_32768(self):
        client = runtime.QwenDirectClient(model="qwen3.8-max", max_output_tokens=65_536, thinking_budget=32_768)
        body, _ = self.invoke(client)
        self.assertEqual(body["max_completion_tokens"], 98_304)
        self.assertEqual(runtime.model_pricing("qwen3.8-max")["max_output_tokens"], 131_072)

    def test_shared_wire_meter_matches_live_reservation_with_and_without_counter(self):
        messages = [{"role": "user", "content": "Return JSON with the supplied condition."}]
        model = "qwen3.8-max"
        answer = 256
        thinking = 128
        wire, _ = runtime.build_qwen_wire_body(
            messages, model=model, max_output_tokens=answer,
            thinking=True, thinking_budget=thinking, json_mode=True, stream=False,
        )
        for counter in (lambda *_: 100, None):
            with self.subTest(counter=counter is not None):
                ledger = runtime.GlobalBudgetLedger(limit_cny=100)
                client = runtime.QwenDirectClient(
                    model=model, max_output_tokens=answer, thinking_budget=thinking,
                    budget_ledger=ledger, prompt_token_counter=counter, max_retries=0,
                )
                with patch.object(runtime.urllib.request, "build_opener", return_value=CaptureOpener()):
                    client(messages, call_id="meter-match")
                if counter is None:
                    meter = runtime.estimate_prompt_tokens(wire, messages)
                else:
                    meter = runtime.estimate_prompt_tokens(wire, messages, prompt_token_counter=counter)
                expected = runtime.estimated_cost_cny(
                    {"prompt_tokens": meter["prompt_tokens"], "completion_tokens": answer + thinking},
                    model=model, conservative=True,
                )
                row = ledger.as_dict()["reservations"][0]
                self.assertAlmostEqual(float(row["amount_cny"]), expected, places=9)

    def test_per_call_thinking_overrides_constructor_both_directions(self):
        client = runtime.QwenDirectClient(thinking=False, thinking_budget=16_384)
        body, result = self.invoke(client, thinking=True, max_output_tokens=32_768)
        self.assertTrue(body["enable_thinking"])
        self.assertEqual(body["thinking_budget"], 16_384)
        self.assertEqual(body["max_completion_tokens"], 49_152)
        self.assertTrue(result["effective_request"]["enable_thinking"])
        body, result = self.invoke(client, thinking=True, thinking_budget=32_768, max_output_tokens=65_536)
        self.assertEqual(body["max_completion_tokens"], 98_304)
        self.assertEqual(result["effective_request"]["thinking_budget"], 32_768)
        # Per-call settings do not mutate the constructor's default.
        body, result = self.invoke(client)
        self.assertFalse(body["enable_thinking"])
        self.assertNotIn("thinking_budget", body)
        self.assertEqual(result["effective_request"]["thinking_budget"], 0)
        client = runtime.QwenDirectClient(thinking=True)
        body, result = self.invoke(client, thinking=False, thinking_budget=65_536)
        self.assertFalse(body["enable_thinking"])
        self.assertNotIn("thinking_budget", body)
        self.assertEqual(body["max_completion_tokens"], 32_768)
        self.assertEqual(result["effective_request"]["thinking_budget"], 0)

    def test_json_thinking_compatibility_checked_after_per_call_override(self):
        client = runtime.QwenDirectClient(model="qwen3.5-plus", thinking=False)
        with self.assertRaisesRegex(runtime.QwenTransportError, "thinking_json_unsupported_for_model"):
            self.invoke(client, thinking=True)

    def test_published_total_output_limit_is_enforced_before_network(self):
        with self.assertRaisesRegex(runtime.QwenTransportError, "model_total_output_exceeded"):
            runtime.QwenDirectClient(model="qwen3.8-max", max_output_tokens=100_000, thinking_budget=32_768)
        client = runtime.QwenDirectClient(model="qwen3.8-max", thinking=False)
        opener = CaptureOpener()
        with self.assertRaisesRegex(runtime.QwenTransportError, "model_total_output_exceeded"):
            self.invoke(client, opener=opener, thinking=True, thinking_budget=100_000)
        self.assertEqual(opener.bodies, [])

    def test_qwen38_effort_mapping_is_metadata_not_a_second_wire_parameter(self):
        client = runtime.QwenDirectClient(model="qwen3.8-max")
        for budget, effort in ((0, "low"), (4096, "low"), (8192, "medium"), (16384, "medium"), (32768, "xhigh")):
            with self.subTest(budget=budget):
                body, result = self.invoke(client, thinking_budget=budget)
                self.assertEqual(result["effective_request"]["mapped_reasoning_effort"], effort)
                self.assertNotIn("reasoning_effort", body)
                self.assertEqual(body["thinking_budget"], budget)

    def test_legacy_registered_models_keep_max_tokens_compatibility(self):
        legacy = {**runtime.MODEL_PRICING_CNY["qwen3.7-flash"]}
        legacy.pop("completion_token_parameter")
        with patch.dict(runtime.MODEL_PRICING_CNY, {"legacy-test-model": legacy}):
            client = runtime.QwenDirectClient(model="legacy-test-model", max_output_tokens=4096, thinking_budget=8192)
            body, result = self.invoke(client)
        self.assertEqual(body["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", body)
        self.assertIsNone(result["effective_request"]["max_completion_tokens"])
        self.assertEqual(result["effective_request"]["total_output_tokens"], 12_288)

    def test_actual_request_and_pressure_persist_across_ledger_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "budget.sqlite"
            ledger = runtime.GlobalBudgetLedger(path=path, limit_cny=10)
            client = runtime.QwenDirectClient(
                model="qwen3.8-max", max_output_tokens=65_536, thinking_budget=32_768,
                budget_ledger=ledger, prompt_token_counter=lambda *_: 100,
            )
            _, result = self.invoke(client)
            reopened = runtime.GlobalBudgetLedger(path=path)
            row = reopened.as_dict()["reservations"][0]
            self.assertAlmostEqual(row["amount_cny"], (100 * 12 + 98_304 * 36) / 1_000_000)
            self.assertEqual(row["telemetry"]["effective_request"], result["effective_request"])
            self.assertEqual(row["telemetry"]["cap_pressure"], result["cap_pressure"])
            self.assertEqual(row["status"], "settled")
            self.assertNotIn("offline-placeholder", json.dumps(row))
            del reopened
            gc.collect()

    def test_missing_usage_keeps_full_reservation_and_request_metadata(self):
        ledger = runtime.GlobalBudgetLedger(limit_cny=10)
        client = runtime.QwenDirectClient(budget_ledger=ledger)
        _, result = self.invoke(client, opener=CaptureOpener(usage={}))
        row = ledger.as_dict()["reservations"][0]
        self.assertEqual(row["status"], "uncertain")
        self.assertEqual(row["telemetry"]["effective_request"], result["effective_request"])
        self.assertGreater(ledger.reserved_cny, 0)
        self.assertIsNone(result["cap_pressure"]["reasoning_tokens"])

    def test_shared_spend_cap_still_blocks_before_dispatch(self):
        ledger = runtime.GlobalBudgetLedger(limit_cny=1)
        client = runtime.QwenDirectClient(
            model="qwen3.8-max", max_output_tokens=65_536, thinking_budget=32_768,
            budget_ledger=ledger, prompt_token_counter=lambda *_: 100,
        )
        opener = CaptureOpener()
        with self.assertRaisesRegex(runtime.QwenTransportError, "global_budget_exceeded"):
            self.invoke(client, opener=opener)
        self.assertEqual(opener.bodies, [])
        self.assertEqual(ledger.as_dict()["reservations"], [])

    def test_near_cap_stop_is_informative_and_not_fatal(self):
        client = runtime.QwenDirectClient(max_output_tokens=1000, thinking_budget=1000)
        usage = {"prompt_tokens": 100, "completion_tokens": 1995,
                 "completion_tokens_details": {"reasoning_tokens": 998}}
        _, result = self.invoke(client, opener=CaptureOpener(usage=usage))
        self.assertTrue(result["complete"])
        self.assertTrue(result["cap_pressure"]["near_limit"])
        self.assertEqual(result["cap_pressure"]["near_limits"], ["completion", "thinking", "answer_allocation"])
        self.assertEqual(result["cap_pressure"]["answer_tokens"], 997)

    def test_length_response_remains_incomplete_with_useful_metadata(self):
        client = runtime.QwenDirectClient()
        with self.assertRaisesRegex(runtime.QwenTransportError, "qwen_incomplete_response") as caught:
            self.invoke(client, opener=CaptureOpener(finish_reason="length"))
        self.assertFalse(caught.exception.record["complete"])
        self.assertTrue(caught.exception.record["cap_pressure"]["finish_reason_length"])
        self.assertEqual(caught.exception.record["effective_request"]["max_completion_tokens"], 40_960)

    def test_http_failure_retains_request_metadata_without_credentials(self):
        ledger = runtime.GlobalBudgetLedger(limit_cny=10)
        client = runtime.QwenDirectClient(budget_ledger=ledger, max_retries=0)
        error = urllib.error.HTTPError("https://offline.invalid", 400, "bad request", {}, io.BytesIO(b"{}"))
        with self.assertRaises(runtime.QwenTransportError) as caught:
            self.invoke(client, opener=CaptureOpener(error=error))
        row = ledger.as_dict()["reservations"][0]
        self.assertEqual(row["telemetry"]["effective_request"]["max_completion_tokens"], 40_960)
        self.assertEqual(caught.exception.record["effective_request"], row["telemetry"]["effective_request"])
        self.assertEqual(row["actual_cny"], 0)
        self.assertNotIn("offline-placeholder", json.dumps(caught.exception.record))


if __name__ == "__main__":
    unittest.main()
