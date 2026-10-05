"""Bounded BODY40 controls; deny network and run real production consumers.

Run from repository root with pytest/ftfy/json-repair/pydantic installed.
Optional documented bootstrap bypasses only unrelated eager AgentScope facade.
"""
import ast
import os
from pathlib import Path
import socket
import subprocess
import sys

# Re-exec once so subprocess writer previews share deterministic JSON key order.
if os.environ.get('PYTHONHASHSEED') != '0':
    raise SystemExit(subprocess.call([sys.executable, __file__], env={**os.environ, 'PYTHONHASHSEED': '0'}))

def deny(*args, **kwargs):
    raise AssertionError('Network disabled for bounded BODY40 controls')
socket.create_connection = deny
socket.socket.connect = deny
import pytest
records = Path(__file__).resolve().parent.parent
files = []
for relative in ('preflight_materials/run_offline_controls.py', 'preflight_final_seams/run_offline_controls.py'):
    tree = ast.parse((records / relative).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)) and any(isinstance(t, ast.Name) and t.id == 'files' for t in node.targets):
            files.extend(ast.literal_eval(node.value))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == 'files':
            files.extend(ast.literal_eval(node.value))
files.extend([
    'test_body40_historical_identity.py',
    'test_body40_numeric_citation_identity.py',
    'test_body40_citation_diagnostic_handoff.py',
    'test_body03_local_lookup_r2.py',
    'test_body03_local_lookup_r2_boundaries.py',
    'test_body03_local_lookup_reading.py',
    'test_body07_delivery_pending_gate.py',
])
raise SystemExit(pytest.main(['-q', *['tests/upgrade3/' + name for name in dict.fromkeys(files)]]))
