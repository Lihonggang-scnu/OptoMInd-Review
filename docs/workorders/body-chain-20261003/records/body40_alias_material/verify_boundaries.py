"""Compare frozen planning/prompt boundaries against accepted first-stop source."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess

BASE = 'e75c66c1ffd018a93636960caa7c51e74911c2af'
ROOT = Path.cwd()
OUT = Path(__file__).resolve().parent
WRITER = 'optomind_research/runtime/upgrade3/review_unit_writer.py'
old = subprocess.check_output(['git', 'show', BASE + ':' + WRITER], text=True)
new = (ROOT / WRITER).read_text()
a, b = ast.parse(old), ast.parse(new)

def defs(tree, name):
    return [ast.dump(node, include_attributes=False) for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name]

def prompts(tree):
    return {node.targets[0].id: ast.dump(node.value, include_attributes=False)
            for node in tree.body if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and any(term in node.targets[0].id for term in ('PROMPT', 'INSTRUCTIONS'))}

checks = {'prompt_constants_unchanged': prompts(a) == prompts(b)}
for name in ('load_writer_prompt', 'unit_messages', '_effective_numeric_citation_map',
             '_repair_numeric_citations', '_output_diagnostics', '_read_card_material'):
    checks[name + '_unchanged'] = bool(defs(a, name)) and defs(a, name) == defs(b, name)
paths = ['optomind_research/runtime/upgrade3/progressive_review_plan.py',
         'optomind_research/runtime/upgrade3/chapter_arrangement.py',
         'optomind_research/runtime/upgrade3/planning_material_triage.py',
         'scripts/upgrade3', 'prompts', 'docs/acceptance/body40-20261005']
checks['planner_arrangement_cli_prompts_archive_unchanged'] = not subprocess.check_output(
    ['git', 'diff', '--name-only', BASE, '--', *paths])
assert all(checks.values()), checks
result = {'base_source_sha': BASE, 'checks': checks,
          'production_files_changed': [WRITER],
          'writer_sha256': hashlib.sha256((ROOT / WRITER).read_bytes()).hexdigest(),
          'note': 'Completion message data is intentionally alias-aware; no claim of full message byte equality. Existing prompt text and scientific stages unchanged.'}
(OUT / 'SOURCE_AND_BOUNDARY_CHECKS.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
