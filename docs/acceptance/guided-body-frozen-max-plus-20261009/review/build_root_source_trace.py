import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHECKS = [
    ('Ch2', 'P0270', 'mCRPC 材料在 NSCLC 表格行使用'),
    ('Ch2', 'P0583', 'FMT-LUMINate 人群在 RCC 表格行使用'),
    ('Ch3', 'P0588', 'PFS 阳性与 ORR/OS 阴性同时保留'),
    ('Ch4', 'P0517', 'PD-L1 高低分层与总体区别'),
    ('Ch4', 'P0575', 'ICI 时间锚点与部分卡片术前术后表述冲突'),
    ('Ch5', 'P0090', '实际输入含 mRCC 数值；试验名另待原发表核验'),
    ('Ch6', 'P0239', 'E. siraeum 的无 irAE 响应者被正文反转'),
    ('Ch6', 'P0314', '菌的分开关联与响应/irAE 相互关联不同'),
    ('Ch6', 'P0017', '结肠炎会议/发表关系需与 P0600 一起核验'),
    ('Ch6', 'P0600', '输入也有结肠炎 FMT 实验，不凭句柄切换断言错引'),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


result = {'scope': 'post-run root evaluation only; never sent to models', 'checks': []}
for chapter, handle, observation in CHECKS:
    stage = ROOT / 'LIVE' / 'stages' / f'author_{int(chapter[2:]):03d}'
    message_path = next(stage.rglob('MESSAGES.json'))
    messages = json.loads(message_path.read_text(encoding='utf-8-sig'))
    payload = json.loads(messages[-1]['content'])
    materials = payload['materials']
    atoms = [a for a in materials['evidence_atoms'] if a.get('source_handle') == handle]
    semantic = [a for a in atoms if any(str(p).lower() in {'key_findings', 'scope_and_limits', 'approach_and_setting', 'planning_summary', 'method', 'methods'} for p in a.get('field_path', []))]
    body_path = message_path.parent / 'BODY.md'
    body = body_path.read_text(encoding='utf-8-sig')
    excerpts = [p for p in body.split('\n\n') if f'[{handle}]' in p]
    result['checks'].append({
        'chapter': chapter,
        'handle': handle,
        'observation': observation,
        'message_path': str(message_path.relative_to(ROOT)).replace('\\', '/'),
        'message_byte_sha256': sha(message_path.read_bytes()),
        'body_path': str(body_path.relative_to(ROOT)).replace('\\', '/'),
        'body_byte_sha256': sha(body_path.read_bytes()),
        'actual_input_atom_count': len(atoms),
        'actual_input_semantic_atoms': semantic,
        'actual_output_paragraphs': excerpts,
        'source_identity': materials['source_identities'].get(handle),
    })
(ROOT / 'ROOT_SOURCE_TRACE.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'checks': len(result['checks']), 'all_material_found': all(c['actual_input_atom_count'] for c in result['checks']), 'path': str(ROOT / 'ROOT_SOURCE_TRACE.json')}, ensure_ascii=False))
