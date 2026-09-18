"""Prepare private staged requirements before starting agents."""
import argparse
import json
import hashlib
from pathlib import Path
from ..episode import load_environment,save
from ..tasks import prepare
from .budget import Budget
from .relay import Relay
from .issue_stages import prepare_issue,audit_issue,InvalidDecomposition
from .requirement_scope import prepare_document, validate_scope
from .commit_preparation import prepare_commit, validate_projection
from .progressive_report import render_preparation


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--review-source',type=Path,help='Re-audit an unchanged preparation report; never regenerate fragments')
    parser.add_argument('--continue-after-review',action='store_true',help='After a frozen prefix passes, prepare remaining tasks in order')
    args=parser.parse_args()
    if args.continue_after_review and not args.review_source:
        parser.error('--continue-after-review requires --review-source')
    load_environment(args.env_file)
    config=json.loads(args.config.read_text())
    args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    budget=Budget(dict(config, progressive_issues=True),journal=args.output/'budget.json')
    relay=Relay(config.get('decomposer',config['user']),args.output,args.output/'provider.jsonl',
        deadline=budget.deadline,budget=budget,role='decomposer')
    report={'status':'running','cases':[]}
    save(args.output/'report.json',report)
    try:
        if args.review_source:
            repo,_,source_tasks=prepare(config,args.output,include_patch=False)
            raw=args.review_source.read_bytes()
            source=json.loads(raw)
            if not source.get('cases'):
                raise ValueError('No saved decomposition cases to review')
            report['review_source']=dict(path=str(args.review_source),sha256=hashlib.sha256(raw).hexdigest())
            for index,case in enumerate(source['cases']):
                expected={k:source_tasks[index][k] for k in ('title','body')}
                if case['issue'] != expected:
                    raise ValueError('review source differs from configured task order')
                # Preserve the source and original extraction exactly; old review is separate.
                frozen=dict(id=case['id'],source='原始拆解重审，未重新生成',issue=case['issue'],
                    plan=case['plan'],previous_review=case['review'])
                if 'scope' in case:
                    frozen['scope']=case['scope']
                if 'projection' in case:
                    frozen['projection']=case['projection']
                report['cases'].append(frozen)
                save(args.output/'report.json',report)
                document=validate_scope(frozen['scope'],frozen['issue']) if 'scope' in frozen else frozen['issue']
                if 'projection' in frozen:
                    document=validate_projection(frozen['projection'],source_tasks[index],repo)
                frozen['review']=audit_issue(relay,document,frozen['plan'])
                save(args.output/'report.json',report)
                if frozen['review']['allowed'] is not True:
                    report['status']='rejected';break
            else:report['status']='candidate_pass'
            tasks=(source_tasks[len(source['cases']):]
                   if args.continue_after_review and report['status']=='candidate_pass' else [])
            if tasks:
                report['status']='running'
                save(args.output/'report.json',report)
        else:
            repo,_,tasks=prepare(config,args.output,include_patch=False)
        offset=len(report['cases'])
        for i,task in enumerate(tasks, start=offset):
            issue={k:task[k] for k in ('title','body')}
            result=(prepare_commit(relay,task,repo) if task['kind']=='commit'
                    else prepare_document(relay,issue,task['kind']))
            report['cases'].append(dict(id='requirement-'+str(i+1),source=task['kind'],source_url=task['identifier'],issue=issue,**result))
            save(args.output/'report.json',report)
            if result['review'].get('allowed') is not True:
                report['status']='rejected';break
        else:
            if not args.review_source or (args.continue_after_review and tasks):report['status']='candidate_pass'
    except Exception as error:
        report.update(status='error',error_type=type(error).__name__,error=str(error))
        if isinstance(error,InvalidDecomposition):
            report['raw_draft']=error.draft
    report['budget']=budget.snapshot()
    save(args.output/'report.json',report)
    render_preparation(args.output,report)
    print(json.dumps({'status':report['status']}))


if __name__=='__main__':main()
