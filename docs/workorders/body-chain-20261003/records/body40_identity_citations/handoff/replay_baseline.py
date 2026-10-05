"""Run corrected fixtures against committed, pre-fix modules without editing files.

Run from repository root with the same offline PYTHONPATH as the pytest suite.
The subprocess test is excluded because it would load the current working tree.
"""
import importlib
from pathlib import Path
import subprocess
import pytest

ROOT = Path.cwd()
for name, relative in (
    ('optomind_research.runtime.upgrade3.review_unit_writer',
     'optomind_research/runtime/upgrade3/review_unit_writer.py'),
    ('scripts.upgrade3.review_unit_writer', 'scripts/upgrade3/review_unit_writer.py'),
    ('scripts.upgrade3.full_review_draft', 'scripts/upgrade3/full_review_draft.py'),
):
    module = importlib.import_module(name)
    original = subprocess.check_output(['git', 'show', 'HEAD:' + relative], text=True)
    exec(compile(original, str(ROOT / relative), 'exec'), module.__dict__)
raise SystemExit(pytest.main([
    '-q', 'tests/upgrade3/test_body40_citation_diagnostic_handoff.py',
    '-k', 'not offline_fake_cli_script_entry',
]))
