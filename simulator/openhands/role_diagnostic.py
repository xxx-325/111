"""Bounded role diagnostics: review once; intercept the first public draft."""
import argparse
import copy
import html
import json
from pathlib import Path

from ..episode import load_environment, save
from .budget import Budget
from .container import SDKContainer
from .episode import OpenHandsEpisode
from .guard import MessageGuard
from .policy import policy_record, role_prompts
from .relay import Relay
from .state import TaskState
from .provenance import collect_sources
from .user_projection import UserViewMixin


def recheck_saved(config, output, source):
    """Recheck an unchanged first draft using only events before that draft."""
    original = json.loads((source/'report.json').read_text())
    events = []
    for line in (source/'private/user/outbox/events.jsonl').read_text().splitlines():
        event = json.loads(line)
        if event.get('tool_name') == 'send_reply' and event.get('action'):
            break
        events.append(event)
    else:
        raise ValueError('first send boundary not found')
    sources = collect_sources([], events)
    state_data = copy.deepcopy(original['draft']['state'])
    controls = [json.loads(line) for line in (source/'private/controls.jsonl').read_text().splitlines()]
    state_data.update(messages=[],transitions=[dict(id=c['result']['permit_id']) for c in controls
        if c['operation']=='transition' and c['result'].get('accepted')])
    if original['input']['communication']['code_has_spoken_for_current_task']:
        raise ValueError('only first delegation replay supported')
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    budget = Budget(config,journal=output/'budget.json')
    relay = Relay(config['user'],output,output/'provider.jsonl',deadline=budget.deadline,budget=budget)
    report = dict(mode='unchanged_draft_recheck',status='running',input=original['input'],
        draft=copy.deepcopy(original['draft']),code_sources=sources,original_review=original['draft']['review'],
        source=str(source),assistant_review='pending')
    save(output/'report.json',report)
    try:
        result=MessageGuard(relay,[],config.get('dialogue_language','zh-CN')).review('send',
            original['draft']['payload'],TaskState(state_data),original['input']['current_requirement'],[],sources)
        report['draft']['review']=result
        report['status']='candidate_pass' if result['allowed'] else 'rejected'
    except Exception as error:
        report.update(status='error',error_type=type(error).__name__)
    report['budget']=budget.snapshot()
    save(output/'report.json',report)
    report_page(output,report)
    return report


def export_draft(directory, report):
    """Render only an unsent original draft, without embedded diagnostic records."""
    draft = report.get('draft')
    if not draft:
        return
    text = draft['payload']['text']
    (directory/'user-draft.txt').write_text(text, encoding='utf-8')
    (directory/'user-draft.html').write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>User 首稿</title><style>body{max-width:760px;margin:48px auto;padding:0 24px;'
        'font:17px/1.8 system-ui;color:#263244;background:#f7f8fa}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}</style><pre>'
        + html.escape(text) + '</pre></html>', encoding='utf-8')


def report_page(directory, report):
    def block(title, value):
        text = value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)
        return '<section><h2>'+html.escape(title)+'</h2><pre>'+html.escape(text)+'</pre></section>'
    body = block('阶段与停止状态', {k:v for k,v in report.items() if k not in ('cases','draft','tools','input')})
    for case in report.get('cases',[]):
        body += block(case['id']+' · '+case['source'], case)
    for key,title in [('input','首次输入'),('tools','状态申请与实际私有工具'),('draft','首条草稿与审核（未发送给 Code）')]:
        if key in report:body+=block(title,report[key])
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>首次委托角色诊断</title><style>body{max-width:1000px;margin:32px auto;padding:0 18px;font:16px/1.6 system-ui;background:#f5f6f8;color:#182638}section{background:white;border:1px solid #dbe2e9;padding:18px;margin:18px 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 monospace}h1{font-size:26px}h2{font-size:20px}</style><h1>首次委托角色诊断</h1><p>测试夹具不是用户数据。草稿不投递给 Code；失败即停止，不自动重试。审核结果与助手人工复核分开。</p>'+body+'</html>'
    (directory/'index.html').write_text(page)


def prior_case(directory):
    """Recover the exact accepted first-send input, not its later revised history."""
    controls = [json.loads(x) for x in (directory/'private/controls.jsonl').read_text().splitlines()]
    send = next(c for c in controls if c['operation']=='send' and c['result'].get('accepted'))
    for line in (directory/'private/user/provider.jsonl').read_text().splitlines():
        event=json.loads(line)
        if event.get('kind')!='request' or not event['id'].startswith('gate-'):
            continue
        content=json.loads(event['input']['messages'][-1]['content'])
        if content.get('operation')=='send' and content['proposal']==send['payload']:
            state = copy.deepcopy(content['state'])
            transitions, sources = [], []
            current = None
            have_current = False
            for record in controls:
                if record is send:
                    break
                if record['operation'] == 'read_state' and record['result'].get('accepted'):
                    current = record['result']['state']['state']
                    have_current = True
                if record['operation'] == 'transition' and record['result'].get('accepted'):
                    if not have_current:
                        raise ValueError('missing historical state before transition')
                    payload = record.get('selected_payload', record['payload'])
                    target = payload.get('state', current)
                    transitions.append(dict(id=record['result']['permit_id'],
                        task_id=payload['task_id'], previous=current, state=target,
                        control=payload['control'], reason=payload['reason'],
                        evidence_ids=payload.get('evidence_ids', [])))
                    sources.append(record['id'])
                    current = target
            if not transitions or transitions[-1] != state['permit']:
                raise ValueError('historical transitions do not match reviewed permit')
            if content['public_history']:
                raise ValueError('first-send recovery requires empty public history')
            state.update(transitions=transitions, messages=[])
            return dict(id=directory.name, source='真实失败消息及当时证据', expected=False,
                        requirement=content['requirement'], state=state,
                        reconstructed_fields={'transitions': sources, 'messages': 'No public history before first accepted send'},
                        payload=content['proposal'], history=content['public_history'])
    raise ValueError('exact original review input not found')


def review_cases(source_runs):
    cases=[prior_case(path) for path in source_runs]
    requirement=cases[0]['requirement']
    examples=[
        ('normal-request','BUILD','REFINE','委托修复当前日期递增问题','请修复 daterange 从十二月开始、step=(1, 0, 0) 时隔年输出的问题，预期是每年递增。',None,True),
        ('clarification','UNDERSTAND','REFINE','回答 Code 关于兼容性的澄清','保持现有 API，修复月份计算就行。','这次需要保持现有 API 吗？',True),
        ('authorization','BUILD','CONTINUE','授权 Code 执行其提出的修复','按你说的做。','我建议先转成零基月份计算，再转回来，可以这样修改吗？',True),
        ('technical-question','UNDERSTAND','REFINE','询问修复方案的影响','如果按零基月份来算，负的月份步长和跨年会不会受影响？',None,True),
        ('intent-mismatch','UNDERSTAND','REFINE','只询问实现原理，当前不要请求改代码','请直接修改 daterange 的月份运算，并添加回归测试。',None,False),
    ]
    for name,category,control,reason,text,reply,expected in examples:
        state=TaskState()
        if reply:state.data['code_reply']={'id':'fixture-code','text':reply,'stopped':True}
        permit=state.transition(dict(task_id='task-1',state=category,control=control,reason=reason))
        cases.append(dict(id=name,source='人工构造的审核测试消息（不是真实用户）',expected=expected,
                          requirement=requirement,state=state.data,payload=dict(task_id='task-1',permit_id=permit['id'],text=text),
                          history=[dict(kind='assistant',role='assistant',phase='final',text=reply)] if reply else []))
    return cases


def run_reviews(config, output, source_runs=(), *, cases=None):
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    budget=Budget(config,journal=output/'budget.json')
    relay=Relay(config['user'],output,output/'provider.jsonl',deadline=budget.deadline,budget=budget)
    gate=MessageGuard(relay,[],config.get('dialogue_language','zh-CN'))
    cases=copy.deepcopy(cases) if cases is not None else review_cases(source_runs)
    for case in cases:
        case['status']='not_run'
    report=dict(mode='review',status='running',policy=policy_record(config.get('dialogue_language','zh-CN')),
                model=config['user']['model'],cases=cases,assistant_review='逐条复核后填写，程序匹配不等于人工确认')
    save(output/'report.json',report)
    try:
        for case in cases:
            report['active_case']=case['id']
            case['status']='started'
            save(output/'report.json',report)
            state=TaskState(copy.deepcopy(case['state']))
            case['communication']=state.communication()
            case['result']=gate.review(case.get('operation','send'),case['payload'],state,case['requirement'],case['history'])
            case['status']='reviewed'
            case['matches_expected']=case['result']['allowed']==case['expected']
            save(output/'report.json',report)
            report_page(output,report)
            if not case['matches_expected']:
                report.update(status='failed',stopped_at=case['id'])
                break
        else:report['status']='candidate_pass'
    except BaseException as error:
        report.update(status='error',error_type=type(error).__name__)
        if report.get('active_case'):
            next(c for c in cases if c['id']==report['active_case'])['status']='error'
    finally:
        report['budget']=budget.snapshot()
        save(output/'report.json',report)
        report_page(output,report)
    return report


class FirstDelegation(UserViewMixin, OpenHandsEpisode):
    checkpoint_schema = 'openhands-first-draft-v4-rejected-transition-corrections'

    def run(self):
        raise RuntimeError('First-draft diagnostics cannot start Code or resume delivery; use run_first with a new directory')

    def _control(self, packet):
        if packet.get('operation')!='send':
            result=super()._control(packet)
            if not result.get('accepted'):
                self.state.data.update(status='paused',pause_reason='Diagnostic control rejection; no retry')
                self.persist()
                return {**result,'handoff':True}
            return result
        if self.saved.get('diagnostic_draft'):
            return {'accepted':False,'handoff':True,'reason':'First draft already intercepted'}
        self.collect_user_sources()
        payload=packet.get('payload',{})
        draft=dict(payload=payload,communication=self.state.communication(),state=self.state.view(),delivered=False)
        self.saved['diagnostic_draft']=draft
        self.persist()
        try:
            self.state.identity(payload)
            if not self.state.data.get('permit') or payload.get('permit_id') != self.state.data['permit']['id']:
                raise ValueError('missing or stale send permit')
            draft['review']=self.guard.review('send',payload,self.state,self.requirement(),[],self.saved['code_sources'])
        except Exception as error:
            draft['error_type']=type(error).__name__
        self.state.data.update(status='paused',pause_reason='First draft intercepted; never delivered to Code')
        self.persist()
        return {'accepted':False,'handoff':True,'reason':'Diagnostic stop after first draft; no regeneration or delivery'}


def run_first(config, output, review_run=None, comparison=False):
    if not comparison:
        reviewed=json.loads((review_run/'report.json').read_text())
        if reviewed.get('status')!='candidate_pass' or reviewed.get('assistant_review')!='passed' or reviewed['policy']!=policy_record(config.get('dialogue_language','zh-CN'), config.get('delegation_variant','neutral')):
            raise ValueError('matching reviewed gate diagnostics must pass before generation')
    if len(config['tasks'])!=1:
        raise ValueError('first-delegation probe requires one task')
    episode=FirstDelegation(config,output)
    report=dict(mode='first_delegation',status='running',input=episode.user_input(),model=config['user']['model'],
                policy=episode.policy, variant=config.get('delegation_variant','neutral'), assistant_review='pending')
    try:
        episode.sync_candidate()
        agent=SDKContainer(episode.private/'user',episode.root/'user-workspace',config['user'],episode.image,'user',
            role_prompts(config.get('dialogue_language','zh-CN'))['user'],episode.budget.deadline,control=episode.control,
            browser=config.get('browser',False),condenser_max_size=config.get('condenser_max_size',120),budget=episode.budget,
            neutral_tools=config.get('delegation_variant','neutral') == 'neutral')
        episode.agents['user']=agent
        episode.guard=MessageGuard(agent.relay,episode.saved['tasks'],config.get('dialogue_language','zh-CN'))
        save(output/'report.json',report)
        agent.start()
        episode.turn('user',json.dumps(report['input'],ensure_ascii=False))
        episode.saved['in_flight']=None
        report.update(status='intercepted' if episode.saved.get('diagnostic_draft') else 'stopped_without_draft')
    except BaseException as error:
        report.update(status='error',error_type=type(error).__name__)
    finally:
        for agent in episode.agents.values():agent.close()
        episode.state.data['status']='paused'
        episode.persist()
        report['draft']=episode.saved.get('diagnostic_draft')
        report['tools']=[dict(tool=e.get('tool_name'),action=e.get('action'),observation=e.get('observation'))
            for agent in episode.agents.values() for e in agent.events() if e.get('kind') in ('ActionEvent','ObservationEvent')]
        report['budget']=episode.budget.snapshot()
        save(output/'report.json',report)
        export_draft(output,report)
        report_page(output,report)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['review','first','compare','recheck'])
    parser.add_argument('--source',type=Path)
    parser.add_argument('--variant',choices=['baseline','delegate','neutral'],default='neutral')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--sources',type=Path,nargs=2)
    parser.add_argument('--review-run',type=Path)
    args=parser.parse_args()
    load_environment(args.env_file)
    config=json.loads(args.config.read_text())
    if args.mode=='recheck':
        if not args.source:parser.error('--source required')
        result=recheck_saved(config,args.output,args.source)
    elif args.mode=='review':
        if not args.sources:parser.error('--sources requires the two recorded failures')
        result=run_reviews(config,args.output,args.sources)
    elif args.mode == 'compare':
        config['delegation_variant']=args.variant
        result=run_first(config,args.output,comparison=True)
    else:
        if not args.review_run:parser.error('--review-run required')
        result=run_first(config,args.output,args.review_run)
    print(json.dumps({'status':result['status'],'output':str(args.output)},ensure_ascii=False))


if __name__=='__main__':main()
