"""Synthetic, socket-denied packet-to-writer before/after probe; no model calls."""
import argparse
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import types
from pathlib import Path


def deny(*args, **kwargs):
    raise AssertionError("Network disabled for WO04 writer seam probe")


socket.create_connection = deny
socket.socket.connect = deny
parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, default=Path.cwd())
parser.add_argument("--base", default="")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
repo = args.repo.resolve()
args.output.parent.mkdir(parents=True, exist_ok=True)
# Load only the two real production modules. The original base files are read
# from Git, not checked out or modified, and no runtime/service init is needed.
for name in ("optomind_research", "optomind_research.runtime", "optomind_research.runtime.upgrade3"):
    module = types.ModuleType(name)
    module.__path__ = [str(repo.joinpath(*name.split(".")))]
    sys.modules[name] = module
with tempfile.TemporaryDirectory(prefix="body04-writer-source-") as temporary:
    if args.base:
        prompt = Path(temporary) / "prompts/review_unit_writer.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_bytes(subprocess.check_output(["git", "show", f"{args.base}:prompts/review_unit_writer.md"], cwd=repo))
    for name in ("chapter_arrangement", "review_unit_writer"):
        relative = f"optomind_research/runtime/upgrade3/{name}.py"
        path = repo / relative
        if args.base:
            path = Path(temporary) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(["git", "show", f"{args.base}:{relative}"], cwd=repo))
        fullname = "optomind_research.runtime.upgrade3." + name
        spec = importlib.util.spec_from_file_location(fullname, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[fullname] = module
        spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location("seam_fixtures", repo / "tests/upgrade3/test_body04_writer_tool_handoff.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    scenarios = {
        "mixed_tool_only": ([{"source_handle": "P0001", "paper_id": "review"},
                             {"source_handle": "P0002", "paper_id": "trial", "doi": "10.1000/trial"}], {}),
        "single_scoped_tool_only": ([{"source_handle": "P0002", "paper_id": "trial"}], {"unit_key": "CH01:CH01_U01"}),
        "resolved_old_handle": ([{"source_handle": "P0001", "paper_id": "trial"},
                                  {"paper_id": "unknown", "title": "Unresolved study"}],
                                {"identity": {"P0002": {"paper_id": "trial"}}}),
        "unresolved_collision": ([{"source_handle": "P0001", "paper_id": "unknown_trial"}], {}),
        "current_map_conflict": ([], {"identity": {"P0001": {"paper_id": "other_current_study"}}}),
    }
    summary = {"synthetic": True, "network_calls": 0, "model_calls": 0, "base": args.base or "working_tree", "scenarios": {}}
    evidence_root = Path(temporary) / "fixtures"
    evidence_root.mkdir()
    for name, (sources, kwargs) in scenarios.items():
        target = evidence_root / name
        target.mkdir(exist_ok=True)
        try:
            chapter, arrangement, view, payload = fixtures._deliver(target, sources, **kwargs)
            from optomind_research.runtime.upgrade3.review_unit_writer import unit_messages, write_unit_output
            result = write_unit_output(view, "Synthetic citation check [P0002]", target / "writer_output",
                model="none-offline", language="en", mode="fake", used_messages=unit_messages(view), estimate={})
            summary["scenarios"][name] = {"catalog_handles": list(arrangement["source_catalog"]),
                "writer_handles": list(view.sources), "chapter_tool_materials": payload["chapter_tool_materials"],
                "unknown_citations": result["unknown_citations"]}
        except Exception as exc:
            summary["scenarios"][name] = {"error": type(exc).__name__ + ":" + str(exc)}
    files = []
    for path in sorted(evidence_root.rglob("*.json")):
        if path.name not in {"packet.json", "arrangement.json", "WRITER_MESSAGE.json"}:
            continue
        content = path.read_text().replace(str(evidence_root), "FIXTURE_ROOT").replace(str(repo), "REPOSITORY_ROOT")
        files.append({"relative_filename": str(path.relative_to(evidence_root)), "content": json.loads(content)})
    aggregate = {"provenance": "Synthetic offline fixtures, literal citation text only; no generated science or model call.",
                 "path_redactions": ["FIXTURE_ROOT", "REPOSITORY_ROOT"], "summary": summary, "files": files}
    args.output.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
