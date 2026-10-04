"""Bounded prior material controls plus writer/citation/path regressions.

No network or provider access. This is not the entire upgrade3 suite and does
not perform real scientific generation. Run from the repository root.
"""
import ast
from pathlib import Path
import socket

import pytest


def denied(*args, **kwargs):
    raise AssertionError('Network disabled for final-seam offline controls')


socket.create_connection = denied
socket.socket.connect = denied
previous = Path(__file__).resolve().parent.parent / 'preflight_materials/run_offline_controls.py'
module = ast.parse(previous.read_text(encoding='utf-8'))
files = next(ast.literal_eval(node.value) for node in module.body
             if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'files' for t in node.targets))
files += [
    'test_preflight_final_citations.py',
    'test_preflight_final_case_path_cache.py',
    'test_review_unit_writer_portable_paths.py',
    'test_body06_formatted_citations.py',
    'test_body06_writer_output_consumption.py',
    'test_review_unit_writer_completion.py',
    'test_body04_writer_tool_handoff.py',
]
raise SystemExit(pytest.main(['-q', *['tests/upgrade3/' + name for name in dict.fromkeys(files)]]))
