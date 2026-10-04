"""Run reviewer regression tests against baseline production modules, no checkout mutation.

From repository root, with normal dependencies on PYTHONPATH:
python docs/workorders/body-chain-20261003/records/preflight_materials/review/replay_baseline.py
Expected: the three reviewer regressions fail on d82541f.
"""
import importlib
import subprocess
import sys
import types
from pathlib import Path
import pytest

ROOT = Path.cwd()
REVISION = "d82541f04341558698657086667f43c5fb3865cd"
package = importlib.import_module("optomind_research.runtime.upgrade3")
for leaf in ("directed_reading", "progressive_review_plan"):
    name = "optomind_research.runtime.upgrade3." + leaf
    path = "optomind_research/runtime/upgrade3/" + leaf + ".py"
    source = subprocess.check_output(["git", "show", REVISION + ":" + path], text=True)
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / path)
    module.__package__ = "optomind_research.runtime.upgrade3"
    sys.modules[name] = module
    setattr(package, leaf, module)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
raise SystemExit(pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests/upgrade3/test_preflight_material_review_regressions.py"]))
