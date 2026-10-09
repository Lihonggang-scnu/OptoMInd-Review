"""Root-corrected, read-only comparison of the user-selected two baselines."""
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parent
BASE = Path(r"F:\OptoMind-Review-2\outputs")
OLD = BASE / "body_full_staged_acceptance_20261004_40cny" / "body_assembly_final"
RECENT = BASE / "guided_body_compact_auto_metadata_20261009_12cny" / "LIVE"
INPUT = next((ROOT / "input").glob("*/FULL_BODY_INPUT.json"))
CH = re.compile(r"(?m)^#{1,2}\s*第([1-7一二三四五六七])章[^\n]*")
REF = re.compile(r"\[\s*(P\d{4}(?:\s*[,;，；]\s*P\d{4})*)\s*\]")
ORDER = {str(i): f"Ch{i}" for i in range(1, 8)} | {c: f"Ch{i}" for i, c in enumerate("一二三四五六七", 1)}

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def identity(row, fallback):
    doi = str(row.get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    return "DOI:" + doi if doi else "ID:" + str(row.get("source_id") or row.get("paper_id") or fallback)

old_refs = json.loads((OLD / "REFERENCES.json").read_text(encoding="utf-8"))
old_map = {}
for row in old_refs["references"]:
    for handle in row.get("handles", []) + row.get("used_handles_in_text", []):
        old_map[handle] = identity(row, row.get("canonical_handle", handle))
book = json.loads(INPUT.read_text(encoding="utf-8"))
recent_map = {}
for handle, row in book["source_identities"].items():
    recent_map[handle] = identity(row, handle)
for alias, target in book.get("source_aliases", {}).items():
    seen = set()
    while target in book.get("source_aliases", {}) and target not in seen:
        seen.add(target)
        target = book["source_aliases"][target]
    if target in recent_map:
        recent_map[alias] = recent_map[target]

result = {"scope": "Each manuscript's own identities; Ch1-Ch5 matched scope and whole BODY cross-check", "correction": "Previous worker statistic used a different historical draft and was replaced before upload.", "manuscripts": {}}
for name, path, mapping, mapping_path, expected in [
    ("historical_173_flash", OLD / "REVIEW_DRAFT_HANDLES.md", old_map, OLD / "REFERENCES.json", 173),
    ("recent_117_guided_plus", RECENT / "FULL_BODY.md", recent_map, INPUT, 117),
]:
    text = path.read_text(encoding="utf-8")
    ref_heading = re.search(r"(?m)^#{1,2}\s*参考文献[^\n]*", text)
    if ref_heading:
        text = text[:ref_heading.start()]
    headings = list(CH.finditer(text))
    sections = {}
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        tokens = [t for bracket in REF.findall(text[m.end():end]) for t in re.findall(r"P\d{4}", bracket)]
        sections[ORDER[m[1]]] = {"occurrences": len(tokens), "cited_identities": sorted({mapping[t] for t in tokens if t in mapping}), "unknown_P_tokens": sorted({t for t in tokens if t not in mapping})}
    whole = {v for sec in sections.values() for v in sec["cited_identities"]}
    first5 = {v for ch, sec in sections.items() if ch in {f"Ch{i}" for i in range(1, 6)} for v in sec["cited_identities"]}
    result["manuscripts"][name] = {"body_path": str(path), "body_sha256": sha(path), "mapping_path": str(mapping_path), "mapping_sha256": sha(mapping_path), "chapters": sections, "ch1_ch5_unique_count": len(first5), "ch1_ch5_occurrences": sum(sec["occurrences"] for ch, sec in sections.items() if ch in {f"Ch{i}" for i in range(1, 6)}), "whole_body_unique_count": len(whole), "expected_historical_whole_count": expected, "whole_count_matches": len(whole) == expected}
    if len(whole) != expected:
        raise RuntimeError(f"historical whole citation recount mismatch: {name}: {len(whole)} vs {expected}")
(ROOT / "HISTORICAL_REFERENCE_STATS.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
lines = ["# 用户指定基线的同范围引用统计（已纠正）", "", "173篇稿使用自身REFERENCES身份；117篇稿使用其原始输入身份与alias。先核对整篇计数，再比较Ch1–Ch5，不跨run套用P号身份。", "", "| 稿件 | 前五章去重身份 | 七章全文去重身份 | 前五章出现次数 |", "|---|---:|---:|---:|---:|"]
for name, row in result["manuscripts"].items():
    lines.append(f"| {name} | {row['ch1_ch5_unique_count']} | {row['whole_body_unique_count']} | {row['ch1_ch5_occurrences']} |")
lines += ["| 本轮旧单元Plus（部分稿） | 132 | 未完成 | 见CITATION_AUDIT |", "", "补充统计初版误选了另一份旧实验稿，根智能体在上传前纠正；原始正文和模型费用未变。"]
(ROOT / "HISTORICAL_REFERENCE_STATS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(json.dumps({k: {f: v[f] for f in ('ch1_ch5_unique_count', 'whole_body_unique_count', 'whole_count_matches')} for k, v in result['manuscripts'].items()}, ensure_ascii=False))

