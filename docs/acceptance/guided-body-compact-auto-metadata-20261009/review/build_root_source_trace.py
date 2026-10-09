import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
checks = [
    ('Ch1','P0081','新增补写将22568名患者抗生素荟萃分析写进PPI段，核对对象与HR'),
    ('Ch1','P0583','TRAE与irAE口径并非同一指标，核对60%/65%'),
    ('Ch2','P0270','mCRPC对象修复，但肌苷促转移推论需分开核对'),
    ('Ch2','P0090','初治mRCC实际人群与数据'),
    ('Ch2','P0073','肠芯片具体方法是否实际有材料'),
    ('Ch3','P0588','旧稿ORR与OS阴性，新稿胃癌分层阴性，不同否定结果保留情况'),
    ('Ch4','P0517','总体与PD-L1分层比较'),
    ('Ch4','P0575','ICI启动时间与输入术前/术后污染'),
    ('Ch5','P0090','是否能推出该RCT不是TACITO'),
    ('Ch5','P0387','SER401阴性与制剂试验条件'),
    ('Ch6','P0239','E.siraeum无irAE响应者方向是否修复'),
    ('Ch6','P0017','人与鼠结肠炎的条件/统计'),
    ('Ch6','P0583','FMT供体×治疗方案比较信息与指南要求'),
    ('Ch7','P0589','导航有身份但本轮本章是否真的供语义材料'),
    ('Ch7','P0545','波兰队列正负方向是否仍有解释'),
]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
rows=[]
for chap,handle,obs in checks:
    path=next((ROOT/'LIVE'/'stages'/f'author_{int(chap[2:]):03d}').rglob('MESSAGES.json'))
    messages=json.loads(path.read_text(encoding='utf-8-sig'))
    payload=json.loads(messages[-1]['content'])
    materials=payload['materials']
    atoms=[a for a in materials['evidence_atoms'] if a.get('source_handle')==handle]
    semantic=[a for a in atoms if any(str(p).lower() in {'key_findings','scope_and_limits','approach_and_setting','planning_summary','method','methods'} for p in a.get('field_path',[]))]
    bodypath=path.parent/'BODY.md'
    body=bodypath.read_text(encoding='utf-8-sig')
    paragraphs=[p for p in body.split('\n\n') if f'[{handle}]' in p]
    if chap=='Ch1':
        final=json.loads((ROOT/'LIVE'/'FULL_BODY_RESULT.json').read_text(encoding='utf-8-sig'))['segments'][0]['body_markdown']
        paragraphs=[p for p in final.split('\n\n') if f'[{handle}]' in p]
    rows.append(dict(chapter=chap,handle=handle,observation=obs,message_path=str(path.relative_to(ROOT)),message_sha256=sha(path),body_path=str(bodypath.relative_to(ROOT)),body_sha256=sha(bodypath),actual_input_atom_count=len(atoms),actual_input_semantic_atoms=semantic,actual_output_paragraphs=paragraphs,source_identity=materials['source_identities'].get(handle)))
(ROOT/'ROOT_SOURCE_TRACE.json').write_text(json.dumps({'scope':'post-run root evaluation only; never sent to models','checks':rows},ensure_ascii=False,indent=2),encoding='utf-8')
notes=json.loads((ROOT/'ROOT_CHAPTER_NOTES.json').read_text(encoding='utf-8-sig'))
for chap,obs in [
    ('Ch6',[
        '完整亲读。正确保留E.siraeum在无irAE响应者富集，修复上轮方向反转；B.luti分开关联、非结肠炎小样本限制更清楚。',
        '9只鼠3死对照0、P=.052没有称显著；MIMic小样本未观察风险没有写成总体风险为零。',
        '仍只有毒性表型表，指南安排的供体×ICI方案对照表未形成；complete=true没有触发内容缺口补写。',
        'FMT-LUMINate65%、供体ClusterB及5/11、心肌炎3人15%再次详述，和Ch1/5基本结果重复；P=.051被概括为决定性作用仍过强。',
        '肠屏障易位/3kDa替代材料、章节开场回顾和末尾下一章预告继续。P0017/P0600有会议与发表关联，不能凭换号断言错引。'
    ]),
    ('Ch7',[
        '完整亲读。统一5队列303人计算管道、低生物量对照、欧洲MR边界、年龄/儿科/地域、多组学与器官芯片仍有具体职责。',
        '波兰P.copri正向差异被简短恢复到地域段，但Ch1不再展开该队列背景；正文有泛化和小样本条件。',
        'TME浓度缺口仍与Ch3反复出现，章首前六章目录式回顾、末尾通用转化宣言继续，紧凑提示词未稳定解决任务书语气。',
        'P0589在合法身份导航中但不在本轮194份语义供材，并出现在TME技术段；身份可解析不等于新增方法主张有实际供材。'
    ])]:
    path=next((ROOT/'LIVE'/'stages'/f'author_{int(chap[2:]):03d}').rglob('BODY.md'))
    row=dict(chapter_id=chap,read_full=True,author_body_path=str(path),author_body_byte_sha256=sha(path),observations=obs)
    notes['chapters']=[r for r in notes['chapters'] if r['chapter_id']!=chap]+[row]
(ROOT/'ROOT_CHAPTER_NOTES.json').write_text(json.dumps(notes,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'source_checks':len(rows),'notes_chapters':len(notes['chapters'])}))
