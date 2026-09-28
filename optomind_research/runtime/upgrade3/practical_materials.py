"""Simple, writing-oriented reading helpers for Upgrade 3.

The helpers keep the acquired document readable and let useful prose survive
without claim-level quote matching or a second scientific gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


_BLOCK_PREFIX = re.compile(r"\[block-[^\]]+\]\s*")
_TABLE_CELL = re.compile(
    r"^\[table cell block-[^\]]+ row=(\d+) col=(\d+) rowspan=(\d+) colspan=(\d+) header=(true|false)\]\s*(.*)$",
    re.IGNORECASE,
)
_REFERENCE_HEADING = re.compile(r"^#{1,6}\s+(?:\[block-[^\]]+\]\s*)?References\s*$", re.IGNORECASE)


def load_practical_material(snapshot_dir: str | Path) -> dict[str, Any]:
    """Read the ordinary snapshot view and bibliography without hash checks."""

    root = Path(snapshot_dir)
    reading_view = (root / "READING_VIEW.md").read_text(encoding="utf-8")
    references_path = root / "REFERENCES.json"
    references_payload = json.loads(references_path.read_text(encoding="utf-8")) if references_path.exists() else {}
    references = references_payload.get("references") if isinstance(references_payload, Mapping) else []
    normalized_references = []
    for index, row in enumerate(references or (), start=1):
        if not isinstance(row, Mapping):
            continue
        marker = str(row.get("marker") or "").strip()
        citation = str(row.get("text") or "").strip()
        normalized_references.append({
            "reference_id": f"R{index}",
            "source_marker": marker,
            "citation": citation,
        })
    return {
        "body": _clean_reading_view(reading_view),
        "references": normalized_references,
    }


def _clean_reading_view(reading_view: str) -> str:
    lines = reading_view.splitlines()
    # Prefer the publisher's article body over generated metadata abstracts and
    # retrieved snippets which may repeat the same summary before the body.
    start = next((i for i, line in enumerate(lines) if re.match(r"^##\s+Abstract\s*$", line)), None)
    if start is not None:
        lines = lines[start:]
    output: list[str] = []
    table_cells: list[tuple[int, int, int, int, bool, str]] = []

    def flush_table() -> None:
        if not table_cells:
            return
        rows: dict[int, dict[int, str]] = {}
        headers: set[int] = set()
        min_row = min(cell[0] for cell in table_cells)
        min_col = min(cell[1] for cell in table_cells)
        max_col = 0
        for row, col, _rowspan, colspan, is_header, text in table_cells:
            rowspan = max(1, _rowspan)
            colspan = max(1, colspan)
            text = text.replace("|", "\\|").replace("\n", " ").strip()
            if is_header:
                headers.add(row - min_row)
            # Snapshot coordinates are normally zero-based. Shift from the
            # first observed cell so tables imported with one-based coordinates
            # do not gain an empty leading row or column.
            for row_offset in range(rowspan):
                display_row = row - min_row + row_offset
                for col_offset in range(colspan):
                    display_col = col - min_col + col_offset
                    rows.setdefault(display_row, {})[display_col] = text
                    max_col = max(max_col, display_col)
        columns = max_col + 1
        header_row = min(headers) if headers else None
        if header_row is None:
            heading = [f"Column {i + 1}" for i in range(columns)]
        else:
            heading = [rows.get(header_row, {}).get(i, "") or f"Column {i + 1}" for i in range(columns)]
        output.append("| " + " | ".join(heading) + " |")
        output.append("| " + " | ".join("---" for _ in range(columns)) + " |")
        for row_id in sorted(rows):
            if row_id == header_row:
                continue
            values = [rows[row_id].get(i, "") for i in range(columns)]
            output.append("| " + " | ".join(values) + " |")
        output.append("")
        table_cells.clear()

    for line in lines:
        if _REFERENCE_HEADING.match(line):
            flush_table()
            break
        match = _TABLE_CELL.match(line)
        if match:
            row, col, rowspan, colspan = (int(match.group(i)) for i in range(1, 5))
            table_cells.append((row, col, rowspan, colspan, match.group(5).casefold() == "true", match.group(6)))
            continue
        flush_table()
        if line.startswith("Locator: ") or "observed_metadata" in line:
            continue
        if line.startswith("<!-- snapshot_id:") or line.startswith("> Material depth:") or line.startswith("> This view is a structured"):
            continue
        line = re.sub(r"^#{1,6}\s+\[block-[^\]]+\]\s*", lambda m: "#" * m.group(0).count("#") + " ", line)
        line = _BLOCK_PREFIX.sub("", line)
        # Remove harmless block-id chips left by headings, but keep section
        # structure and all publisher text.
        output.append(line)
    flush_table()
    return "\n".join(output).strip()


def build_practical_reader_messages(
    *,
    title: str,
    questions: Sequence[Mapping[str, Any]],
    required_outputs: Sequence[Mapping[str, Any]],
    material: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build one prompt for useful, attributed writing material."""

    system = """你是为文献综述作者准备定向精读笔记的研究员。目标是交付信息充实、可供编排和写作的研究材料。
请通读所给正文和表格，围绕任务真正询问的关系取材。引言中简短介绍的相关研究也要读到。

请完成两件事：
1. examples：把有用的具体研究整理成独立笔记。一项笔记属于一篇论文；不同作者/论文分别成项，
   不要把两项研究合写，再让它们共享研究对象、模型或实验条件。同一篇论文的多个实验可以分别成项。
   finding 充分说明研究做了什么、具体发现、方法、机制或论证；conditions 保留与结果相连的重要条件、
   比较对象、研究设置及其差异。定量结果要带着它本来衡量的指标和研究对象，避免省掉条件后改变意思。
   use_in_review 说明它对当前任务的具体作用。按研究实际做的事判断：关联分析、因果或机制分析、预测/模型验证、
   操作或干预研究、理论或方法论论证解决的不是同一个问题。回答所问关系的研究用作直接材料，回答别的问题的相关研究用作背景或比较，
   在用途里明确写清，不把后者称为前者的答案。相关背景有价值，可以保留。
2. explanation：整理这篇文章明确阐述的概念、原理、机制和作者自己的综合解释，服务于所问问题。
   具体研究的结果已经放在 examples，这里不用再复述成一篇小综述，不自行拼出新的跨研究总括结论。
   不把不同研究的百分比合成一个范围、不改变对象/材料/数据/方法身份、不把研究设置变成普遍适用规则。
   解释需要具体、清楚，讲明过程和关系，避免只有空洞的主题清单。

原文没有具体交代的方法细节可以略去，重要的未回答问题简记在 remaining_points。
综述自己的检索方法、数据提取模板、可选指标列表不是每项原始研究实际采用的方法；
   “多中心”“队列”“meta分析”或其他设计标签本身不能推出训练测试划分、随机分配、因果识别、采样时点或独立验证。
只整理材料实际提供的信息，不靠常识补齐细节、不补写猜测性局限。

当前论文的原始研究直接用“本研究”标识，身份由本地程序补入；综述介绍的研究使用文中已有的作者/年份。
综述对原始研究的介绍可正常用于写作，不需要再读原论文，也不因转述而降低使用权重。
不要求逐条引文、原文摘抄或书目复写；optional_bibliography 中的 R 编号方便时可以填写，否则留空。
内容用中文（保留必要术语），只返回下面结构的 JSON，各个例子放进对应问题的 examples 列表：
{"question_material": [{"question_id": "问题ID", "examples": [{"attribution": "一篇研究或本研究",
"use_in_review": "针对当前任务的用途", "finding": "充实的研究内容", "conditions": "与发现相连的条件",
"reference_ids": []}], "explanation": "概念原理和源作者解释", "remaining_points": []}]}
"""
    user = {
        "paper_title": title,
        "questions": [dict(row) for row in questions],
        "required_outputs": [dict(row) for row in required_outputs],
        "source_material": str(material.get("body") or ""),
        "optional_bibliography": [dict(row) for row in material.get("references") or ()],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False, indent=2)},
    ]


def decode_practical_json(raw: Any) -> tuple[dict[str, Any], str]:
    """Read a JSON content object, preserving plain text when JSON is absent."""

    value = raw.get("content", raw) if isinstance(raw, Mapping) else raw
    if isinstance(value, Mapping):
        return _arrange_practical_content(value), ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, text
        if isinstance(decoded, Mapping):
            return _arrange_practical_content(decoded), ""
        return {"response": decoded}, ""
    if value is None:
        return {}, ""
    return {"response": value}, ""


def _arrange_practical_content(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a misplaced single-question example in the usable section."""
    result = dict(value)
    sections = result.get("question_material")
    if isinstance(sections, Mapping):
        sections = [dict(sections)]
    if not isinstance(sections, list):
        return result
    sections = [dict(row) if isinstance(row, Mapping) else row for row in sections]
    question_rows = [row for row in sections if isinstance(row, dict)]
    # The question is unambiguous only for a single returned question. Otherwise
    # leave the extra content where it is rather than assign it to a wrong task.
    if len(question_rows) == 1 and result.get("finding"):
        example = {key: result.pop(key) for key in (
            "attribution", "use_in_review", "finding", "conditions", "reference_ids"
        ) if key in result}
        examples = question_rows[0].get("examples") or []
        question_rows[0]["examples"] = [*examples, example] if isinstance(examples, list) else [examples, example]
    result["question_material"] = sections
    return result


def has_practical_content(value: Any) -> bool:
    """A small content-presence check; it does not score or verify claims."""

    content_keys = {
        "explanation", "examples", "finding", "conditions", "content", "prose", "text",
        "answer", "question_material", "useful_material", "auxiliary_material", "contribution", "details",
    }
    ignored_keys = {
        "question_id", "reference_ids", "references", "remaining_points", "remaining_gap",
        "status", "primary_gap_status", "material_usefulness", "outline_action", "attribution", "use_in_review",
    }

    def walk(item: Any, parent_key: str = "") -> bool:
        if isinstance(item, Mapping):
            return any(
                walk(child, str(key))
                for key, child in item.items()
                if str(key) not in ignored_keys
            )
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return any(walk(child, parent_key) for child in item)
        if isinstance(item, str):
            return bool(item.strip()) and parent_key in content_keys
        return False

    return walk(value)


def render_practical_markdown(content: Mapping[str, Any], *, plain_text: str = "") -> str:
    """Render the expected question sections without repeating their content."""

    if plain_text:
        return plain_text.strip()
    sections = content.get("question_material")
    if not isinstance(sections, list):
        return json.dumps(dict(content), ensure_ascii=False, indent=2)
    lines: list[str] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        question_id = str(section.get("question_id") or "Question")
        lines.extend([f"## {question_id}", ""])
        explanation = str(section.get("explanation") or "").strip()
        if explanation:
            lines.extend([explanation, ""])
        examples = section.get("examples")
        if isinstance(examples, list) and examples:
            lines.extend(["### Concrete examples", ""])
            for example in examples:
                if not isinstance(example, Mapping):
                    continue
                attribution = str(example.get("attribution") or "Study")
                finding = str(example.get("finding") or "").strip()
                conditions = str(example.get("conditions") or "").strip()
                refs = ", ".join(str(ref) for ref in example.get("reference_ids") or ())
                use = str(example.get("use_in_review") or "").strip()
                details = " ".join(part for part in (f"Use: {use}." if use else "", finding, f"Conditions: {conditions}" if conditions else "") if part)
                lines.append(f"- **{attribution}:** {details}" + (f" [{refs}]" if refs else ""))
            lines.append("")
        remaining_value = section.get("remaining_points") or ""
        remaining = "; ".join(str(point) for point in remaining_value) if isinstance(remaining_value, list) else str(remaining_value).strip()
        if remaining:
            lines.extend([f"Open points: {remaining}", ""])
    return "\n".join(lines).strip()


__all__ = [
    "build_practical_reader_messages",
    "decode_practical_json",
    "has_practical_content",
    "load_practical_material",
    "render_practical_markdown",
]
