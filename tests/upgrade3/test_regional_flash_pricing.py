"""Explicit opt-in Flash Beijing pricing and transport capabilities."""
import pytest

from optomind_research.runtime.upgrade3.module4.runtime import (
    QwenTransportError, build_qwen_wire_body, estimated_cost_cny, model_pricing,
)
from optomind_research.runtime.upgrade3.regional_recognition import RegionalRecognitionConfig


@pytest.mark.parametrize("tokens,input_rate,output_rate", [
    (128000, 0.2, 2.0), (128001, 0.8, 8.0),
    (256000, 0.8, 8.0), (256001, 1.2, 12.0), (991808, 1.2, 12.0),
])
def test_flash_exact_beijing_tier_boundaries(tokens, input_rate, output_rate):
    assert estimated_cost_cny({"prompt_tokens": tokens, "completion_tokens": 100}, model="qwen3.5-flash") == pytest.approx((tokens * input_rate + 100 * output_rate) / 1_000_000)


def test_flash_conservative_bound_context_and_explicit_wire_model(tmp_path):
    assert estimated_cost_cny({"prompt_tokens": 100, "completion_tokens": 200}, model="qwen3.5-flash", conservative=True) == pytest.approx((100 * 1.2 + 200 * 12) / 1_000_000)
    price = model_pricing("qwen3.5-flash")
    assert price["max_input_tokens"] == 991808 and price["max_output_tokens"] == 65536 and price["context_window"] == 1000000
    assert price["thinking_json"] is False
    _, wire = build_qwen_wire_body([{"role": "user", "content": "Return JSON"}], model="qwen3.5-flash", max_output_tokens=2048, thinking=False, json_mode=True, stream=True)
    assert wire["model"] == "qwen3.5-flash" and wire["max_completion_tokens"] == 2048
    with pytest.raises(QwenTransportError, match="model_input_context_exceeded"):
        estimated_cost_cny({"prompt_tokens": 991809, "completion_tokens": 1}, model="qwen3.5-flash")
    assert RegionalRecognitionConfig(cache_root=tmp_path).model == "qwen3.5-plus"


@pytest.mark.parametrize("tokens,input_rate,output_rate", [
    (32000, 0.15, 1.5), (32001, 0.3, 3.0), (128000, 0.3, 3.0),
    (128001, 0.6, 6.0), (256000, 0.6, 6.0), (258048, 0.6, 6.0),
])
def test_vl_flash_conservative_beijing_tier_boundaries(tokens, input_rate, output_rate):
    assert estimated_cost_cny({"prompt_tokens": tokens, "completion_tokens": 100}, model="qwen3-vl-flash") == pytest.approx((tokens * input_rate + 100 * output_rate) / 1_000_000)


@pytest.mark.parametrize("model,input_cap,output_cap,context,input_rate,output_rate", [
    ("qwen3-vl-flash", 258048, 32768, 262144, 0.15, 1.5),
    ("qwen-vl-ocr-2025-11-20", 30000, 8192, 38192, 0.3, 0.5),
    ("qwen3.5-ocr", 49152, 16384, 65536, 0.5, 2.0),
])
def test_explicit_vision_ocr_rates_caps_and_max_tokens_wire(model, input_cap, output_cap, context, input_rate, output_rate):
    usage = {"prompt_tokens": 1000, "completion_tokens": 200}
    assert estimated_cost_cny(usage, model=model) == pytest.approx((1000 * input_rate + 200 * output_rate) / 1_000_000)
    assert estimated_cost_cny(usage, model=model) < estimated_cost_cny(usage, model="qwen3.5-plus")
    pricing = model_pricing(model)
    assert (pricing["max_input_tokens"], pricing["max_output_tokens"], pricing["context_window"]) == (input_cap, output_cap, context)
    json_mode = "ocr" not in model
    _, wire = build_qwen_wire_body([{"role": "user", "content": "Return JSON"}], model=model, max_output_tokens=4096, thinking=False, json_mode=json_mode, stream=True)
    assert wire["model"] == model and wire["max_tokens"] == 4096 and "max_completion_tokens" not in wire
    assert wire["stream_options"] == {"include_usage": True}
    if not json_mode:
        assert "response_format" not in wire and "enable_thinking" not in wire and "thinking_budget" not in wire
    with pytest.raises(QwenTransportError, match="model_input_context_exceeded"):
        estimated_cost_cny({"prompt_tokens": input_cap + 1, "completion_tokens": 1}, model=model)
    with pytest.raises(QwenTransportError, match="model_total_output_exceeded"):
        build_qwen_wire_body([{"role": "user", "content": "Return JSON"}], model=model, max_output_tokens=output_cap + 1, thinking=False, json_mode=json_mode)


@pytest.mark.parametrize("model", ["qwen-vl-ocr-2025-11-20", "qwen3.5-ocr"])
def test_ocr_rejects_unsupported_modes_and_message_roles_before_dispatch(tmp_path, model):
    from optomind_research.runtime.upgrade3.module4.runtime import QwenDirectClient
    message = [{"role": "user", "content": "Return JSON"}]
    for modes, reason in (({"thinking": True, "json_mode": False}, "thinking_unsupported_for_model"),
                          ({"thinking": False, "json_mode": True}, "json_mode_unsupported_for_model")):
        with pytest.raises(QwenTransportError, match=reason): build_qwen_wire_body(message, model=model, max_output_tokens=4096, **modes)
        with pytest.raises(QwenTransportError, match=reason): QwenDirectClient(model=model, max_output_tokens=4096, **modes)
    with pytest.raises(QwenTransportError, match="model_requires_single_user_message"):
        build_qwen_wire_body([{"role": "system", "content": "JSON"}, *message], model=model, max_output_tokens=4096, thinking=False, json_mode=False)
    with pytest.raises(ValueError, match="explicit_json_mode_false"):
        RegionalRecognitionConfig(cache_root=tmp_path, model=model)
