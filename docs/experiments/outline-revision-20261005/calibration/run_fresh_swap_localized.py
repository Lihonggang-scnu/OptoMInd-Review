# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
from pathlib import Path
from types import SimpleNamespace
import sys, json, difflib

SOURCE = Path(r'<LOCAL_SOURCE_BACKUP_PATH>')
OLD = Path(r'<LOCAL_REVIEW2_PATH>')
OUT = Path(__file__).parent
sys.path.insert(0, str(SOURCE))
from scripts.upgrade3.post_body_revision import make_live_client

def main():
    args = SimpleNamespace(run=True, allow_paid=True, recordings=None, budget_cny=10.0,
                           budget_ledger=str(OLD/'live<LOCAL_RUNTIME_PATH>'), key_file=r'<LOCAL_REVIEW2_PATH>')
    model='qwen3.5-plus'
    config={'variant':'CALIBRATION_SWAP', 'model_settings':{model:{'max_output_tokens':8192, 'thinking':True,
            'thinking_budget':4096, 'json_mode':False, 'timeout_seconds':300}}}
    client=make_live_client(args,config)
    guide=(OLD/'evaluation/JUDGE_GUIDE.md').read_text(encoding='utf-8-sig')
    schema=json.loads((OLD/'evaluation/assessment_schema.json').read_text(encoding='utf-8-sig'))
    selected=['P0289','P0585','P0388','P0085']
    for order in (0,1):
        path=OUT/f'qwen_localized_pair002_order{order}.json'
        if path.exists():
            print(json.dumps({'order':order,'status':'saved_no_new_call'}),flush=True)
            continue
        packet=json.loads((OLD/f'evaluation/packets/pair-002-{order}.json').read_text(encoding='utf-8-sig'))
        material={}
        for key in selected:
            row=packet['materials'][key]
            material[key]={k:v for k,v in row.items() if k!='text' or not row.get('content_fields')}
        payload={k:packet[k] for k in ['case_id','pair_id','pair_input_sha256','left','right','research_question','scope','outline']}
        matcher=difflib.SequenceMatcher(None, packet['left'].splitlines(keepends=True), packet['right'].splitlines(keepends=True), autojunk=False)
        a,b=packet['left'].splitlines(keepends=True),packet['right'].splitlines(keepends=True)
        payload['detected_text_differences']=[{'left':''.join(a[max(0,i-1):min(len(a),j+1)]), 'right':''.join(b[max(0,k-1):min(len(b),l+1)])} for tag,i,j,k,l in matcher.get_opcodes() if tag!='equal']
        assert payload['detected_text_differences'], 'must_have_real_differences'
        payload['materials']=material
        payload['source_identity_map']={k:packet['source_identity_map'][k] for k in selected}
        messages=[{'role':'system','content':guide+'\n两侧文本不同，程序已给出纯字符差异定位(detected_text_differences)，这不是科学答案或审稿意见。必须实际比较这些变化及其相关材料，不得把差异漏看为相同。不要被相同的其他正文淹没局部差异。本次两侧全文作为语境，但仅评价有变化的段落以及相关来源，不能声称核验全部论文。材料是已提供的真实研究认识，不将摘要未提到的实验判断为不存在。请分别判断变化的文字是否改进以及是否仍有残余错误；总体与亚组关系、解释职责、机制与阴性结果应随内容判断。纯换行不算内容收益，合理不改不是失败。你的左/右归属须对应本请求，不猜候选身份。只返回符合以下schema的JSON：\n'+json.dumps(schema,ensure_ascii=False)},
                  {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
        (OUT/f'qwen_localized_pair002_order{order}_messages.json').write_text(json.dumps(messages,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'order':order,'status':'calling','model':model,'input_chars':sum(len(m['content']) for m in messages)},ensure_ascii=False),flush=True)
        raw=client(f'pair002-localized-order{order}',messages,model=model)
        text=raw.get('content','')
        if isinstance(text,str):
            stripped=text.strip()
            if stripped.startswith('```'): stripped=stripped.split('\n',1)[1].rsplit('```',1)[0].strip()
            judgment=json.loads(stripped)
        elif isinstance(text,dict): judgment=text
        else: raise ValueError('unexpected_content_type')
        (OUT/f'qwen_localized_pair002_order{order}_raw.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
        for key in ['case_id','pair_id','pair_input_sha256']:
            assert judgment[key]==packet[key],key
        import jsonschema
        jsonschema.validate(judgment,schema)
        path.write_text(json.dumps(judgment,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'order':order,'status':'complete','winner':judgment['winner'],'cost_cny':raw.get('cost_cny'),'usage':raw.get('usage')},ensure_ascii=False),flush=True)

if __name__=='__main__':
    main()
