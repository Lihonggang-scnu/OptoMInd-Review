"""Expand a public archive plan for offline inspection, never a production cache.

Example: python read_public_plan.py --run body_restore_20261001 --output /tmp/public-plan.json
The result contains explicit publisher-text redactions and must not be represented as
the complete original live-model input. No network, credentials or model calls.
"""
from pathlib import Path
import argparse,json
ARCHIVE=Path(__file__).resolve().parents[2]
def expand(value,memo):
    if isinstance(value,dict):
        if set(value)=={'_archive_fragment'}:
            rel=value['_archive_fragment'];p=(ARCHIVE/rel).resolve()
            if not p.is_relative_to(ARCHIVE.resolve()):raise ValueError('fragment outside archive')
            if rel not in memo:memo[rel]=expand(json.loads(p.read_text(encoding='utf8')),memo)
            return memo[rel]
        return {k:expand(v,memo) for k,v in value.items()}
    if isinstance(value,list):return [expand(v,memo) for v in value]
    return value
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',choices=['new_plan','body_restore_20261001','old_baseline'],required=True);parser.add_argument('--output',type=Path)
    args=parser.parse_args();root=ARCHIVE/'curated/complete_plans'/f'{args.run}.json';plan=expand(json.loads(root.read_text(encoding='utf8')),{})
    summary={'run':args.run,'fields':len(plan),'chapters':len(plan.get('chapters',[])),'units':sum(len(c.get('chapter_plan',c).get('units',[])) for c in plan.get('chapters',[])),'source_identity_entries':len(plan.get('source_identity_map',{})),'writer_packets':len(plan.get('writer_packets',[])),'public_redacted_copy':True}
    print(json.dumps(summary,ensure_ascii=False))
    if args.output:
        p=args.output.resolve()
        if p.is_relative_to(ARCHIVE.resolve()):raise ValueError('choose output outside archive to preserve immutable records')
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
if __name__=='__main__':main()
