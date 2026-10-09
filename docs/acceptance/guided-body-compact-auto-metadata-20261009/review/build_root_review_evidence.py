import json,hashlib,collections
from pathlib import Path
ROOT=Path(__file__).resolve().parent
OLD=ROOT.parent/'guided_body_frozen_max_guide_plus_20261009'
def load(p): return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
new=load(ROOT/'LIVE/FULL_BODY_RESULT.json')['segments']
old=load(OLD/'LIVE/FULL_BODY_RESULT.json')['segments']
comparisons=[
    ('Ch2','人群对象','P0270','P0270'),
    ('Ch2','肠芯片控制','P0073','P0073'),
    ('Ch3','阳性PFS与阴性ORR/OS','P0588','P0588'),
    ('Ch4','ICI时间锚点','术后','术后'),
    ('Ch5','MET4与SER401/VE800反例','MET4','MET4'),
    ('Ch6','E.siraeum结果方向','siraeum','siraeum'),
    ('Ch6','供体方案表','供体','供体'),
]
rows=[]
for ch,point,n1,n2 in comparisons:
    i=int(ch[2:])-1
    rows.append(dict(chapter_id=ch,point=point,new_paragraphs=[p for p in new[i]['body_markdown'].split('\n\n') if n1.lower() in p.lower()],old_paragraphs=[p for p in old[i]['body_markdown'].split('\n\n') if n2.lower() in p.lower()]))
report={'scope':'post-run independent root reading; not fed to provider','new_source_commit':'737ef95aff9f6ffb8061427489ca7e52fade5a5b','old_source_commit':'6a5ed067f7e302db569c6f8edcf7379c3ed252b5','old_archive_commit':'24681415f913f40da818b649aa186d4684a3e0f0','same_guide':True,'new_final_file_sha256':sha(ROOT/'LIVE/FULL_BODY.md'),'old_final_file_sha256':sha(OLD/'LIVE/FULL_BODY.md'),'chapter_characters':[{'chapter_id':x['chapter_id'],'new':len(x['body_markdown']),'old':len(y['body_markdown'])} for x,y in zip(new,old)],'new_characters':sum(len(x['body_markdown']) for x in new),'old_characters':sum(len(x['body_markdown']) for x in old),'comparisons':rows}
(ROOT/'ROOT_BEFORE_AFTER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
# This taxonomy is a root reading judgment about useful knowledge, not a demand
# to cite every supplied paper. Full unabridged material stays in the navigation.
independent={
 'P0015':'CICB菌株签名外推单药从AUC .65降至.51的方案对照消失。',
 'P0057':'TNBC治疗背景与TMAO焦亡机制没有建立，不能用其他癌种的通用机制代替。',
 'P0123':'PFS预测与PD-L1识别是不同任务；配对比较/化疗背景未展开。',
 'P0152':'多糖结构依赖、早期干预及剂量安全边界未展开；是否扩展范围交编辑判断。',
 'P0186':'B.obeum/B.massiliensis有效、A.rectalis无效及功能通路差异未解释。',
 'P0195':'DAT需要IFNAR1且DC需要LPS共刺激的独立因果控制未出现。',
 'P0204':'HCC多界微生物与18菌种模型/代谢物关系未展开。',
 'P0205':'EGFR+人群无通用Akk信号、纵向U型及倒U型变化未比较。',
 'P0236':'52489总体与45896 NSCLC的不同终点显著性提供反例，未展开。',
 'P0249':'覆盆子人体Akk未显著变化和移植小鼠差异，独立干预边界未出现。',
 'P0266':'Hispanic NSCLC OS有差异但PFS/ORR无差异、时间组OS无差异未展开。',
 'P0310':'抗生素与需静脉类固醇结肠炎风险关联，是暴露×毒性的缺失桥梁。',
 'P0329':'模拟菌群产品改善预测值不能当真实临床获益，此独立方法边界未使用。',
 'P0360':'累计≥14天OS不良而DCR无显著差异的终点边界未展开。',
 'P0367':'培养组学94种测序未检出与校正后部分差异不显著未展开；指南特别安排此职责。',
 'P0393':'965人多界共现网络及32属模型与单界比较未建立。',
 'P0415':'宏转录表达与宏基因组通路的联合实测比较未出现。',
 'P0421':'低肌肉量总体不显著、肺癌PFS正向而OS阴性与毒性阴性未比较。',
 'P0437':'基因层级特征、CICB与单药泛化不同、年龄差异未解释。',
 'P0439':'MSS CRC干预试验与其他癌种的条件比较未展开。',
 'P0443':'与P0123有预印本/发表内容关联，PFS .74与PD-L1 .87是不同任务；不主张二者强制双引。',
 'P0485':'黏膜黑色素瘤原发部位和微生物样本异质性未建立。',
 'P0511':'外部验证AUC .624与PFS不显著，是泛化反例未使用。',
 'P0555':'Fn琥珀酸抑制cGAS-STING这一具体机制与一般STING激活不同，未展开。',
 'P0558':'给药时刻关联在ICI/ICI+TKI成立但化疗组合不成立，未进入节律比较。',
 'P0569':'新辅助NSCLC MPR低基线多样性、后期稳定性提供反例，未与晚期人群比较。',
}
scope={
 'P0001':'文献计量产出/合作网络主要适合引言选材，不是BODY必需知识。',
 'P0012':'主要为TKI而非ICI，不能为引用目标强塞入抗生素ICI结果。',
 'P0024':'HIV ART为旁领域类比，省略不构成ICI独立证据损失。',
 'P0030':'HCC试验方案没有已完成疗效结果；本轮正文不必加入。',
 'P0045':'仍为招募可行性记录而非已完成关联研究。',
 'P0154':'特应性皮炎皮肤领域，非本轮BODY主要对象。',
 'P0217':'肌因子/直肠修复属于外围延展，原本范围是否需要待编辑判断。',
 'P0352':'下呼吸道而非肠道微生物，可作边界补充，不能把未用它直接算漏掉肠道机制。',
 'P0525':'数学模拟可作未来方法补充；不是目前实证疗效的必需案例。',
}
mixed={
 'P0005':'PRR/DC与SCFA已覆盖，但HLA/TLR宿主修饰不充分。',
 'P0113':'HCC一般机制和抗生素阴性已覆盖，但HBV/HCV、NASH及基因背景未充分解释。',
 'P0269':'宿主因素有零星介绍，HLA/FCGR等特定条件未建立完整比较。',
 'P0343':'饮食/压力/菌群干预已覆盖，但维生素D、大麻和具体运动结果未展开。',
 'P0433':'SCFA与一般菌群功能覆盖，TMAO及不同癌种的特定机制未解释。',
 'P0474':'宿主和微环境边界有一般说明，STK11/KEAP1与连续巨噬谱等非核心扩展未完整采用。',
}
nav=load(ROOT/'records/UNUSED_SOURCE_NAVIGATION.json')
unused=[]
for e in nav['entries']:
    h=e['source_handle']
    if h in independent: cat,reason='independent_explanation_or_comparison_missing',independent[h]
    elif h in scope: cat,reason='peripheral_or_no_completed_result',scope[h]
    elif h in mixed: cat,reason='core_covered_but_distinct_extension_not_full',mixed[h]
    else: cat,reason='main_content_covered_by_other_cited_sources','主要关联、机制或干预概览已在相应章由其他来源展开；这是主要内容覆盖，不声称该卡每条发现全部写入。'
    # Exact source snippets are retained for re-reading and challenge.
    unused.append(dict(source_handle=h,title=e['title'],doi=e['doi'],classification=cat,reason=reason,source_entry_sha256=e['entry_sha256'],complete_key_findings=e['key_findings'],evidence_path='records/UNUSED_SOURCE_NAVIGATION.json'))
counts=dict(collections.Counter(r['classification'] for r in unused))
(ROOT/'ROOT_UNCITED_ASSESSMENT.json').write_text(json.dumps({'scope':'All 78 actual supplied-but-uncited identities; root reviewed key findings against fully read BODY. Categories concern knowledge coverage, not mandatory citation quotas; mixed and peripheral require editorial priority decisions. No model evaluation calls.','counts':counts,'sources':unused},ensure_ascii=False,indent=2),encoding='utf-8')
notes=load(ROOT/'ROOT_CHAPTER_NOTES.json')
notes['post_read_corrections']=[
 'P0081不是只含抗生素；实际research_scope及key_findings同时包含PPIs HR1.28。补写22568总纳入人群并非明显错引，但不能把总人数当PPI亚组样本。',
 'P0583的60%与65%均存在于不同实际精读快照，不能只凭数字不同认定writer编造，也不能未经原文核实断言是TRAE/irAE区分。当前正文未主动解释这一输入冲突。',
 'P0588阴性亚组为消化道恶性肿瘤，不应缩窄成胃癌。新稿仍漏总体ORR与OS阴性。'
]
(ROOT/'ROOT_CHAPTER_NOTES.json').write_text(json.dumps(notes,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'comparisons':len(rows),'uncited_counts':counts},ensure_ascii=False))
