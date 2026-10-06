"""Render original and two live updated plans verbatim as Markdown/JSON."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_directory_repair_comparison_20261006")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def main() -> None:
    payload = load(OUT / "INPUT_PAYLOAD.json")
    full_result = load(OUT / "full_material_plus/RESULT.json")
    max_result = load(OUT / "semantic_catalog_max/OWNER_RESULT.json")
    plans = {
        "original": payload.get("chapter_plan"),
        "full_material_plus": full_result.get("updated_plan"),
        "semantic_catalog_max": max_result.get("updated_plan"),
    }
    metadata = {
        "source_payload_sha256": hashlib.sha256((OUT / "INPUT_PAYLOAD.json").read_bytes()).hexdigest(),
        "original_plan_sha256": digest(plans["original"]),
        "full_material_plus_plan_sha256": digest(plans["full_material_plus"]),
        "semantic_catalog_max_plan_sha256": digest(plans["semantic_catalog_max"]),
        "full_material_plus_cost_cny": 0.353414,
        "semantic_catalog_access_plus_cost_cny": 0.084012,
        "semantic_catalog_max_owner_cost_cny": 2.440908,
        "semantic_catalog_max_total_cost_cny": 2.52492,
        "full_material_plus_status": full_result.get("status"),
        "semantic_catalog_max_status": max_result.get("status"),
    }
    bundle = {"metadata": metadata, "plans": plans}
    (OUT / "PLAN_COMPARISON_FULL.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 两单元细纲完整原文对照",
        "",
        "本文件中的三个 JSON 计划块逐字取自本轮本地输入或模型返回的 `updated_plan` 字段；没有人工改写其中的科学文字、来源句、条件、限制、段落顺序或关系。JSON 语法缩进只用于阅读。机器可核对版本见 `PLAN_COMPARISON_FULL.json`。",
        "",
        "输入固定为两个可编辑单元 `U1_跨癌种关联图谱`、`U2_外部干扰与宿主背景`，只读邻近职责仍在请求中。完整材料组把原请求中的 29 个来源和 2 个工具材料放进生产 messages；按需 Max 的 owner messages 同样带完整语义目录，实际 access 另外附上 5 个完整记录，未把目录外材料称为不存在。",
        "",
        "本轮实际成本：Full Plus `0.353414 CNY`；按需 access Plus `0.084012 CNY`；按需 Max owner `2.440908 CNY`；按需组小计 `2.524920 CNY`。两组均返回 `updated`；按需 owner 未触发续读。access 返回的 5 条请求均标注了只读邻居 U3/U4，这是原始模型返回，未手工修正或重试。",
        "",
        "## 原始输入细纲",
        "",
        "来源：`INPUT_PAYLOAD.json` 的 `chapter_plan`。",
        "",
        "```json",
        pretty(plans["original"]),
        "```",
        "",
        "## Full Plus 完整材料返回细纲",
        "",
        "来源：`full_material_plus/RESULT.json` 的 `updated_plan`；返回模型 `qwen3.5-plus`，`finish_reason=stop`。",
        "",
        "```json",
        pretty(plans["full_material_plus"]),
        "```",
        "",
        "## 按需 Max 返回细纲",
        "",
        "来源：`semantic_catalog_max/OWNER_RESULT.json` 的 `updated_plan`；返回模型 `qwen3.8-max`，`finish_reason=stop`。owner 消费的是完整语义目录和 access 解析出的完整记录，非仅 5 条目录索引。",
        "",
        "```json",
        pretty(plans["semantic_catalog_max"]),
        "```",
        "",
        "## 版本指纹",
        "",
        "```json",
        pretty(metadata),
        "```",
    ]
    (OUT / "PLAN_COMPARISON_FULL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
