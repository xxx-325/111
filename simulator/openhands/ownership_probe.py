"""One-pass review of saved ownership errors and labeled control fixtures."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from ..episode import load_environment
from .role_diagnostic import run_reviews
from .state import TaskState


def ownership_cases(source):
    source=Path(source)
    # Use the exact previous review request, not a later conversation summary.
    provider=source/'private/user/provider.jsonl'
    transition=None
    for row in map(json.loads,provider.read_text().splitlines()):
        if row.get('kind')!='request' or row.get('input',{}).get('tools'):
            continue
        for message in row['input']['messages']:
            if message.get('role')!='user':
                continue
            data=json.loads(message['content'])
            if data.get('operation')=='transition':
                state=TaskState()
                state.data.update({k:copy.deepcopy(v) for k,v in data['state'].items() if k in state.data})
                if state.data['code_reply'] is not None or state.data['permit'] is not None or data['public_history']:
                    raise ValueError('Expected the first initial-delegation transition')
                transition=dict(id='saved-wrong-transition',source='saved original transition',operation='transition',
                    expected=False,state=state.data,payload=data['proposal'],requirement=data['requirement'],history=[],
                    source_request_id=row['id'],source_sha256=hashlib.sha256(provider.read_bytes()).hexdigest())
                break
        if transition:break
    if transition is None:
        raise ValueError('Saved transition review input not found')
    saved_bytes=(source/'report.json').read_bytes()
    original=json.loads(saved_bytes)
    draft=original['draft']
    if draft['delivered'] or draft['state']['code_reply'] is not None or draft['state'].get('messages'):
        raise ValueError('Expected an intercepted first draft without delivery')
    send_state=TaskState()
    send_state.data.update({k:copy.deepcopy(v) for k,v in draft['state'].items() if k in send_state.data})
    send_state.data['transitions']=[copy.deepcopy(send_state.data['permit'])] if send_state.data['permit'] else []
    send=dict(id='saved-wrong-send',operation='send',source='saved original intercepted draft',expected=False,
        state=send_state.data,payload=copy.deepcopy(draft['payload']),history=[],
        reconstructed_fields={'messages':'empty: first draft was intercepted','transitions':'the recorded first send permit'},
        requirement=original['input']['current_requirement'],source_sha256=hashlib.sha256(saved_bytes).hexdigest())
    cases=[transition,send]
    requirement=transition['requirement']
    fixtures=[
        ('direct-help','BUILD','请求 Code 帮忙检查日期生成问题','生成的日期不太对，帮我看看。',None),
        ('run-check','OPERATE','请求 Code 实际运行检查并报告结果','你运行一下，看看生成的日期是什么。',None),
        ('implementation-question','UNDERSTAND','追问 Code 刚检查的功能入口','刚才检查的是哪个入口？','我检查了日期生成功能，还没有修改代码。'),
        ('clarification-answer','BUILD','回答 Code 是否可开始检查的澄清','可以，你先看看。','我先检查日期生成功能，可以吗？')]
    for name,category,reason,text,reply in fixtures:
        state=TaskState()
        if reply:state.data['code_reply']=dict(id='fixture-code',text=reply,stopped=True)
        proposal=dict(task_id='task-1',state=category,control='CONTINUE',reason=reason,evidence_ids=[])
        history=[dict(kind='assistant',role='assistant',text=reply,phase='final')] if reply else []
        cases.append(dict(id=name+'-transition',source='artificial review fixture, not real dialogue',
            operation='transition',expected=True,state=copy.deepcopy(state.data),payload=proposal,requirement=requirement,history=history))
        permit=state.transition(proposal)
        cases.append(dict(id=name+'-send',source='artificial review fixture, not real dialogue',operation='send',
            expected=True,state=copy.deepcopy(state.data),payload=dict(task_id='task-1',permit_id=permit['id'],text=text,evidence_ids=[]),
            requirement=requirement,history=history))
    return cases


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','env-file','source','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    load_environment(args.env_file)
    config=json.loads(args.config.read_text())
    report=run_reviews(config,args.output,cases=ownership_cases(args.source))
    print(json.dumps({'status':report['status'],'stopped_at':report.get('stopped_at')}))
