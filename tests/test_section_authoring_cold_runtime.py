"""Cold-process regressions for deterministic section authoring finalization."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from optomind_research.runtime.section_authoring_tool_registry import (
    SectionAuthoringToolProvider,
    _make_build_evidence_packet,
    _make_load_authoring_context,
    _make_run_citation_audit,
    _make_submit_argument_plan,
    _make_submit_revision,
    _make_submit_section_draft,
)
from optomind_research.runtime.tool_provider import SectionAuthoringContext


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "optomind_research" / "runtime" / "section_authoring_tool_registry.py"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_context(work_dir: Path, section_id: str) -> SectionAuthoringContext:
    work_dir.mkdir(parents=True, exist_ok=True)
    paper_id = f"paper_{section_id}"
    chunk_id = f"chunk_{section_id}"
    section_data = {
        "section_id": section_id,
        "title": f"Deterministic section {section_id}",
        "chapter_argument": "The section records a bounded mechanism-level synthesis.",
        "claims": [
            {
                "claim_id": "C01",
                "statement": "The material exhibits saturable absorption.",
                "load_bearing": True,
            }
        ],
        "required_roles": ["foundation"],
        "section_contract": {"word_budget": 0, "paragraph_functions": []},
    }
    material_path = work_dir / "SECTION_MATERIAL_PACKAGE.json"
    ledger_path = work_dir / "SECTION_SOURCE_LEDGER.json"
    _write_json(
        material_path,
        {
            "schema_version": "2.0",
            "section_id": section_id,
            "section_title": section_data["title"],
            "chapter_argument": section_data["chapter_argument"],
            "coverage_status": "completed",
            "blocking_gaps_remain": False,
            "sources_by_role": {"foundation": 1},
            "chunk_ids_by_role": {"foundation": [chunk_id]},
        },
    )
    _write_json(
        ledger_path,
        {
            "schema_version": "2.0",
            "section_id": section_id,
            "sources": [
                {
                    "paper_id": paper_id,
                    "title": f"Evidence for {section_id}",
                    "year": 2024,
                    "literature_role": "foundation",
                    "scope_fit": "direct",
                    "canonical_chunk_ids": [chunk_id],
                    "acquisition_status": "fulltext",
                    "not_usable_for": [],
                }
            ],
        },
    )
    kb_path = work_dir / "main_kb.sqlite"
    connection = sqlite3.connect(str(kb_path))
    try:
        connection.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, "
            "evidence_level TEXT, source_kind TEXT)"
        )
        connection.execute(
            "INSERT INTO text_chunks VALUES (?, ?, ?, ?, ?)",
            (
                chunk_id,
                paper_id,
                "The material exhibits saturable absorption under optical excitation. "
                "This evidence supports a bounded mechanism-level interpretation.",
                "fulltext",
                "fulltext",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return SectionAuthoringContext(
        section_id=section_id,
        section_data=section_data,
        kb_sqlite=kb_path,
        temp_kb_sqlite=None,
        work_dir=work_dir,
        material_package_path=material_path,
        source_ledger_path=ledger_path,
    )


def _script_finalize(ctx: SectionAuthoringContext) -> str:
    paper_id = f"paper_{ctx.section_id}"
    chunk_id = f"chunk_{ctx.section_id}"
    assert json.loads(_make_load_authoring_context(ctx)())["status"] == "ok"
    assert json.loads(
        _make_submit_argument_plan(ctx)(
            json.dumps(
                {
                    "argument_flow": "mechanism to bounded synthesis",
                    "paragraphs": [
                        {
                            "paragraph_index": 0,
                            "function": "mechanism and evidence synthesis",
                            "topic_sentence": "The material exhibits saturable absorption.",
                            "key_claims": ["C01"],
                            "evidence_chunk_ids": [chunk_id],
                            "paper_ids": [paper_id],
                            "writing_permission": "factual_assertion",
                            "expected_word_count": 80,
                        }
                    ],
                }
            )
        )
    )["status"] == "ok"
    assert json.loads(
        _make_build_evidence_packet(ctx)(
            json.dumps(
                {
                    "items": [
                        {
                            "chunk_id": chunk_id,
                            "paper_id": paper_id,
                            "literature_role": "foundation",
                            "scope_fit": "direct",
                            "exact_spans": [
                                "The material exhibits saturable absorption under optical excitation."
                            ],
                            "claim_ids": ["C01"],
                            "writing_permission": "factual_assertion",
                            "not_usable_for": [],
                        }
                    ]
                }
            )
        )
    )["status"] == "ok"
    draft = (
        "The material exhibits saturable absorption under optical excitation "
        f"[REF:{paper_id}]. This observation is interpreted at the material level "
        "and is not treated as a universal device-performance guarantee. The "
        "mechanism provides a useful anchor for comparing related optical responses. "
        "The available evidence supports a bounded synthesis while leaving broader "
        "system-level generalization open."
    )
    assert json.loads(_make_submit_section_draft(ctx)(draft, "initial"))["status"] == "ok"
    assert json.loads(_make_run_citation_audit(ctx)("[]"))["audit_passed"] is True
    assert json.loads(_make_submit_revision(ctx)(draft + " The scope boundary remains explicit.", "[]", "scope clarification"))["status"] == "ok"
    result = SectionAuthoringToolProvider(ctx).try_auto_finalize()
    assert result is not None
    assert "VALIDATION_PASSED" in result
    assert json.loads((ctx.work_dir / "SECTION_AUTHORING_PACKAGE.json").read_text(encoding="utf-8"))["authoring_status"] == "completed"
    return result


def test_cold_source_import_registers_all_finalization_helpers():
    """Compile the source directly so an old pyc cannot mask missing symbols."""
    script = f"""
import importlib.util
import pathlib
import sys
import types

module_name = 'optomind_research.runtime.section_authoring_tool_registry'
path = pathlib.Path({str(REGISTRY_PATH)!r})
source = path.read_text(encoding='utf-8-sig')
module = types.ModuleType(module_name)
module.__file__ = str(path)
module.__package__ = 'optomind_research.runtime'
module.__spec__ = importlib.util.spec_from_file_location(module_name, path)
sys.modules[module_name] = module
exec(compile(source, str(path), 'exec'), module.__dict__)
for name in (
    '_section_contract_errors',
    '_visual_artifact_errors',
    '_write_needs_more_literature_package',
    '_is_decodable_image',
):
    assert callable(getattr(module, name)), name
print('cold source import ok')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "cold source import ok" in result.stdout


def test_two_sections_finalize_independently_after_scripted_revision(tmp_path: Path):
    """Provider auto-finalization must not leak state between section workers."""
    first = _make_context(tmp_path / "S01", "S01")
    second = _make_context(tmp_path / "S02", "S02")

    first_result = _script_finalize(first)
    second_result = _script_finalize(second)

    assert "VALIDATION_PASSED" in first_result
    assert "VALIDATION_PASSED" in second_result
    assert not (first.work_dir / "_audit_stale").exists()
    assert not (second.work_dir / "_audit_stale").exists()
