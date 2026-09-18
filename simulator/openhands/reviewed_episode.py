"""Explicitly continue an approved first-draft SDK session with reviewed handoffs."""
import argparse
import copy
import hashlib
import json
import shutil
import time
from pathlib import Path

from ..episode import save, load_environment
from .progressive import ProgressiveEpisode, progressive_config
from .issue_stages import validate
from .judge import candidate_hash
from .policy import policy_record
from .review_barrier import await_review, fingerprint
from .dialogue_export import export_dialogue
from .user_projection import task_feedback


def reset_cloned_execution(private):
    """Discard generated sandbox identities while retaining role history and output."""
    for role in ('code', 'judge'):
        execution = private / role / 'execution'
        for name in ('environment.json', 'auth', 'client'):
            path = execution / name
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        remote = private / role / 'sdk' / 'remote-tools'
        if remote.exists():
            shutil.rmtree(remote)


class ReviewedEpisode(ProgressiveEpisode):
    checkpoint_schema='openhands-reviewed-import-v6-minimal-contracts'

    def __init__(self, config, output, resume=False):
        config=reviewed_config(config)
        super().__init__(config,output,resume)

    def import_approved_first(self, source):
        source=Path(source).resolve()
        report=json.loads((source/'report.json').read_text())
        original=json.loads((source/'private/checkpoint.json').read_text())
        review=json.loads((source/'assistant-review.json').read_text())
        draft=report['draft']
        active=json.loads((source/'private/user/outbox/active.json').read_text())
        if (draft['delivered'] or original.get('in_flight') or original['public']
                or active['status']!='stopped' or draft.get('review',{}).get('allowed') is not True
                or review.get('decision')!='acceptable_first_draft_for_this_case'):
            raise ValueError('Source is not an approved, stopped, unsubmitted first draft')
        if original['base']!=self.saved['base'] or original['config']['user']!=self.config['user']:
            raise ValueError('Source baseline or model differs')
        if original['policy']['prompt_sha256']!=self.policy['prompt_sha256']:
            raise ValueError('Role prompts changed since approved draft')
        if candidate_hash(source/'workspace/candidate')!=candidate_hash(self.root/'workspace/candidate'):
            raise ValueError('Source candidate differs from current base')
        if self.saved['public'] or self.saved['revision']:
            raise ValueError('Import only into a fresh run')

        # This is an explicit user-authorized opening overlay, not a model output
        # or a claim that the prior automatic audit approved this combined plan.
        current=self.current()
        original_plan=copy.deepcopy(current['plan'])
        task=self.saved['tasks'][0]
        opening=dict(id='opening',category='symptom',text=report['input']['current_requirement']['body'],
                     source_quote='result is wrong.',requires=[],related=[])
        combined=validate({'items':[opening]+original_plan['items']},{k:task[k] for k in ('title','body')})
        current.update(plan=combined,released=['opening'],release_history=[dict(reason='explicit approved minimal opening',added=['opening'],after=['opening'])])
        save(self.private/'opening-overlay.json',dict(source_plan=original_plan,plan=combined,
             provenance='User-authorized minimal abstraction; original audited plan retained unchanged'))

        shutil.copytree(source/'private/user',self.private/'user')
        cfgpath=self.private/'user/inbox/config.json'
        cfg=json.loads(cfgpath.read_text())
        cfg['container_suffix']=hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        save(cfgpath,cfg)
        events=(self.private/'user/outbox/events.jsonl').read_text().splitlines()
        self.saved['offsets']['user']=len(events)
        self.state.data=copy.deepcopy(original['state'])
        self.state.data.update(status='running',phase='user')
        self.state.data.pop('pause_reason',None)
        message=self.state.send(copy.deepcopy(draft['payload']))
        self.public(dict(kind='user',role='user',text=message['text']),message['id'])
        self.saved['bootstrap']=dict(source=str(source),source_report_sha256=fingerprint(report),
            conversation_id=cfg['conversation_id'],message_id=message['id'],prior_budget=original.get('budget'),
            authority='User explicitly approved this first draft and requested full issue continuation')
        self.persist()

    def _control(self, packet):
        if self.saved.pop('pending_user_rejection', None) is not None:
            # The resumed User has received the rejection in its turn input.
            # Consume it on the first resulting action, without touching the
            # retained draft, gate, or SDK history.
            self.persist()
        if packet.get('operation')=='send' and packet['request_id'] not in self.saved.get('control_results',{}):
            stage=self.state.communication()['stage']
            decision=await_review(self.private/'assistant-gates','send-'+packet['request_id'],
                dict(text=packet['payload'].get('text', ''), requirement=self.requirement(),
                     stage=stage, task_result=(None if stage=='initial_delegation' else task_feedback(self.current()))),
                min(self.budget.deadline,time.monotonic()+120))
            if not decision['approved']:
                self.state.data.update(status='paused',pause_reason='Assistant rejected User draft: '+decision['reason'])
                result=dict(accepted=False,handoff=True,reason=decision['reason'])
                self.saved.setdefault('control_results',{})[packet['request_id']]=result
                self.persist()
                return result
        return super()._control(packet)

    def augment_user_input(self, value):
        rejected=self.saved.get('pending_user_rejection')
        if rejected:
            value['rejected_send']=copy.deepcopy(rejected)
        return value

    def review_decision(self, record):
        if record.get('status') != 'ready':
            return record
        version=record.get('feedback_version',0)
        if record.get('assistant_reviewed_feedback_version') == version:
            return record
        identifier=f"{record['job']['id']}-feedback-v{version}"
        decision=await_review(self.private/'assistant-gates',identifier,
            dict(requirement=record['disclosure']['requirement'], feedback=record['payload'].get('public_feedback',{}),
                 outcome=record['payload']['outcome'],
                 audit={key: record['review'][key] for key in ('conclusion_valid','feedback_safe','reasons')}),
            self.budget.deadline)
        if decision['approved']:
            record['assistant_reviewed_feedback_version']=version
        else:
            record.update(accepted=False,status='feedback_pending')
            record.setdefault('feedback_rejections',[]).append(dict(source='assistant',reason=decision['reason']))
        save(self.private/'judgments'/f"{record['job']['id']}.json",record)
        self.progress['decisions'][record['job']['id']]=record
        self.persist()
        return record

    def before_user_turn(self):
        # ProgressiveEpisode calls review_decision before committing disclosure and feedback.
        super().before_user_turn()


def reviewed_config(config):
    return dict(config,_reviewed_policy={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                for name in ('reviewed_episode.py','review_barrier.py')})


def resumable_user_pause(reason):
    return (reason == 'User Agent stopped without a public send or task decision'
            or reason.startswith('Assistant rejected User draft:'))


def retained_user_rejection(source, checkpoint, reason):
    """Return one recorded, exact assistant rejection for a stopped send."""
    if not reason.startswith('Assistant rejected User draft:'):
        return None
    rejection=reason.split(':',1)[1].strip()
    gates=source/'private/assistant-gates'
    matches=[]
    for decision_path in gates.glob('send-*.decision.json') if gates.exists() else ():
        decision=json.loads(decision_path.read_text())
        if decision.get('approved') is not False or decision.get('reason')!=rejection:
            continue
        request_path=decision_path.with_name(decision_path.name.replace('.decision.json','.request.json'))
        if not request_path.exists():
            continue
        request=json.loads(request_path.read_text())
        if decision.get('sha256')!=request.get('sha256'):
            continue
        request_id=decision_path.name[len('send-'):-len('.decision.json')]
        control=checkpoint.get('control_results',{}).get(request_id,{})
        material=request.get('material',{})
        payload=material.get('payload',{})
        draft=material.get('text') if isinstance(material.get('text'),str) else payload.get('text')
        if control.get('accepted') is False and isinstance(draft,str):
            matches.append(dict(request_id=request_id, draft=draft,
                                delivered=False, reason=rejection))
    if len(matches)!=1:
        raise ValueError('paused rejection has no unique matching retained send record')
    return matches[0]


def clone_feedback_continuation(source, output):
    """Explicitly upgrade a stopped pre-commit feedback review into the correction protocol."""
    source,output=Path(source).resolve(),Path(output).resolve()
    checkpoint=json.loads((source/'private/checkpoint.json').read_text())
    reason=checkpoint.get('state',{}).get('pause_reason','')
    if (checkpoint.get('in_flight') or checkpoint.get('state',{}).get('status')!='paused'
            or not reason.startswith('Assistant rejected Judge feedback:')):
        raise ValueError('source is not a clean assistant-rejected Judge feedback checkpoint')
    progress=checkpoint['progressive']
    task_id=checkpoint['state']['task_id']
    current=progress['tasks'][task_id]
    identifier=current.get('applied_job')
    if not identifier or identifier not in progress['decisions']:
        raise ValueError('source has no applied Judge decision to repair')
    record=progress['decisions'][identifier]
    if record.get('payload',{}).get('outcome')=='solved':
        raise ValueError('solved feedback is intentionally empty and cannot use this repair path')
    if record['job']['revision']!=checkpoint['revision']:
        raise ValueError('source Judge decision is stale')
    if candidate_hash(source/'workspace/candidate')!=record['job']['candidate_version']:
        raise ValueError('source candidate changed after the retained verdict')
    shutil.copytree(source,output)
    checkpoint=json.loads((output/'private/checkpoint.json').read_text())
    progress=checkpoint['progressive'];current=progress['tasks'][task_id];record=progress['decisions'][identifier]
    raw=copy.deepcopy(checkpoint['config'])
    raw.pop('_progressive_policy',None);raw.pop('_reviewed_policy',None)
    resolved=progressive_config(reviewed_config(raw))
    resolved['dialogue_language']=resolved.get('dialogue_language','zh-CN')
    checkpoint['schema']=ReviewedEpisode.checkpoint_schema
    checkpoint['config']=resolved
    checkpoint['policy']=policy_record(resolved['dialogue_language'],resolved.get('delegation_variant','neutral'),
                                       resolved.get('code_prompt_mode','local'))
    rejection=reason.split(':',1)[1].strip()
    record.setdefault('original_payload',copy.deepcopy(record['payload']))
    record.update(accepted=False,status='feedback_pending',feedback_version=record.get('feedback_version',0),
                  feedback_revisions=record.get('feedback_revisions',[]),feedback_attempts=0,release_committed=True)
    record.setdefault('feedback_rejections',[]).append(dict(source='assistant',reason=rejection,imported=True))
    record.pop('assistant_reviewed_feedback_version',None)
    current['feedback']=None;current['verdict']=None;current['simulated_experience']=None
    current.pop('applied_job',None);current.pop('assistant_reviewed_job',None)
    checkpoint['state']['checks']=[item for item in checkpoint['state'].get('checks',[]) if item.get('id')!=identifier]
    checkpoint['state'].update(status='running',phase='user')
    checkpoint['state'].pop('pause_reason',None)
    progress['job']=record['job'];progress['feedback_revision']=None
    checkpoint['in_flight']=None
    suffix=hashlib.sha256(str(output).encode()).hexdigest()[:12]
    for role in ('user','code','judge'):
        cfg=output/'private'/role/'inbox/config.json'
        if cfg.exists():
            value=json.loads(cfg.read_text());value['container_suffix']=suffix;save(cfg,value)
    save(output/'private/judgments'/f'{identifier}.json',record)
    save(output/'private/feedback-continuation.json',dict(source=str(source),verdict_id=identifier,
         source_checkpoint_sha256=fingerprint(json.loads((source/'private/checkpoint.json').read_text())),
         authority='Explicit user-approved feedback correction continuation; no Judge inspection replayed'))
    save(output/'private/checkpoint.json',checkpoint)
    return raw


def clone_user_turn_continuation(source, output):
    """Explicitly continue a clean User-turn pause after a projection-policy fix."""
    source,output=Path(source).resolve(),Path(output).resolve()
    checkpoint=json.loads((source/'private/checkpoint.json').read_text())
    reason=checkpoint.get('state',{}).get('pause_reason','')
    if (checkpoint.get('in_flight') or checkpoint.get('state',{}).get('status')!='paused'
            or not resumable_user_pause(reason)):
        raise ValueError('source is not a clean stopped User-turn checkpoint')
    progress=checkpoint['progressive']
    task_id=checkpoint['state']['task_id']
    current=progress['tasks'][task_id]
    verdict=current.get('verdict') or {}
    rejection=retained_user_rejection(source,checkpoint,reason)
    outcome=verdict.get('outcome')
    final_solved=(outcome=='solved' and rejection is not None
                  and checkpoint['state'].get('task_index')==len(checkpoint.get('tasks',[]))-1)
    if (progress.get('job') is not None or outcome not in ('unsolved','solved')
            or verdict.get('revision')!=checkpoint['revision'] or
            (outcome=='solved' and not final_solved)):
        raise ValueError('source has no resumable current Judge verdict')
    if candidate_hash(source/'workspace/candidate')!=verdict.get('candidate_version'):
        raise ValueError('source candidate changed after the applied verdict')
    shutil.copytree(source,output)
    checkpoint=json.loads((output/'private/checkpoint.json').read_text())
    raw=copy.deepcopy(checkpoint['config'])
    raw.pop('_progressive_policy',None);raw.pop('_reviewed_policy',None)
    resolved=progressive_config(reviewed_config(raw))
    resolved['dialogue_language']=resolved.get('dialogue_language','zh-CN')
    checkpoint['schema']=ReviewedEpisode.checkpoint_schema
    checkpoint['config']=resolved
    checkpoint['policy']=policy_record(resolved['dialogue_language'],resolved.get('delegation_variant','neutral'),
                                       resolved.get('code_prompt_mode','local'))
    checkpoint['state'].update(status='running',phase='user')
    checkpoint['state'].pop('pause_reason',None)
    checkpoint['in_flight']=None
    if rejection:
        checkpoint['pending_user_rejection']=rejection
    else:
        checkpoint.pop('pending_user_rejection',None)
    suffix=hashlib.sha256(str(output).encode()).hexdigest()[:12]
    for role in ('user','code','judge'):
        cfg=output/'private'/role/'inbox/config.json'
        if cfg.exists():
            value=json.loads(cfg.read_text());value['container_suffix']=suffix;save(cfg,value)
    reset_cloned_execution(output/'private')
    save(output/'private/user-turn-continuation.json',dict(source=str(source),
         source_checkpoint_sha256=fingerprint(json.loads((source/'private/checkpoint.json').read_text())),
         rejected_send=copy.deepcopy(rejection),
         authority=('Explicit continuation of a clean final solved User turn after a rejected closing'
                    if final_solved else
                    'Explicit continuation of a clean User turn after a versioned projection or instruction change')))
    save(output/'private/checkpoint.json',checkpoint)
    return raw


def clone_judge_start_continuation(source, output):
    """Continue one verified Judge tool-compatibility startup failure."""
    source,output=Path(source).resolve(),Path(output).resolve()
    checkpoint=json.loads((source/'private/checkpoint.json').read_text())
    state=checkpoint.get('state',{})
    progress=checkpoint.get('progressive',{})
    task=progress.get('tasks',{}).get(state.get('task_id'),{})
    code_reply=state.get('code_reply') or {}
    matching_code_checks=[item for item in state.get('checks',[])
                          if item.get('id')==code_reply.get('id')
                          and item.get('revision')==checkpoint.get('revision')
                          and item.get('tool')=='code_report']
    worker_log=source/'private/judge/worker.log'
    budget_log=source/'private/budget.json'
    verdict=task.get('verdict') or {}
    role_configs={role:source/'private'/role/'inbox/config.json' for role in ('user','code','judge')}
    role_active={role:source/'private'/role/'outbox/active.json' for role in ('user','code','judge')}
    if (checkpoint.get('in_flight') or state.get('status')!='paused' or state.get('phase')!='user'
            or state.get('pause_reason')!='RuntimeError: SDK worker exited; inspect private worker.log'
            or progress.get('job') is not None or progress.get('feedback_revision') is not None
            or not code_reply.get('stopped') or len(matching_code_checks)!=1
            or verdict.get('revision')==checkpoint.get('revision')
            or any(not path.exists() for path in role_configs.values())
            or any(not path.exists() or json.loads(path.read_text()).get('status')!='stopped'
                   for path in role_active.values())
            or not worker_log.exists()
            or "Cannot resume conversation: tools were removed mid-conversation (removed: ['task_tracker'])" not in worker_log.read_text()
            or not budget_log.exists() or json.loads(budget_log.read_text()).get('pending')
            or json.loads(budget_log.read_text())!=checkpoint.get('budget')
            or checkpoint.get('budget',{}).get('pending')):
        raise ValueError('source is not the verified clean Judge task-tracker startup failure')
    identifier=f"judge-{state['task_id']}-r{checkpoint['revision']}"
    if ((source/'private/judgments'/f'{identifier}-input.json').exists()
            or (source/'private/judgments'/f'{identifier}.json').exists()):
        raise ValueError('Judge work for the current revision already started')
    candidate_version=candidate_hash(source/'workspace/candidate')
    raw=copy.deepcopy(checkpoint['config'])
    raw.pop('_progressive_policy',None);raw.pop('_reviewed_policy',None)
    resolved=progressive_config(reviewed_config(raw))
    resolved['dialogue_language']=resolved.get('dialogue_language','zh-CN')
    current_policy=policy_record(resolved['dialogue_language'],resolved.get('delegation_variant','neutral'),
                                 resolved.get('code_prompt_mode','local'))
    if checkpoint['config'].get('_progressive_policy')!=resolved['_progressive_policy']:
        raise ValueError('source progressive policy differs beyond the Judge tool repair')
    old_reviewed=checkpoint['config'].get('_reviewed_policy',{}).copy()
    new_reviewed=resolved['_reviewed_policy'].copy()
    old_reviewed.pop('reviewed_episode.py',None);new_reviewed.pop('reviewed_episode.py',None)
    old_policy=copy.deepcopy(checkpoint.get('policy',{}));new_policy=copy.deepcopy(current_policy)
    old_policy.get('implementation_sha256',{}).pop('tool_wording.py',None)
    new_policy.get('implementation_sha256',{}).pop('tool_wording.py',None)
    if old_reviewed!=new_reviewed or old_policy!=new_policy:
        raise ValueError('source policy differs beyond the explicit continuation implementation and Judge tool repair')
    shutil.copytree(source,output)
    checkpoint=json.loads((output/'private/checkpoint.json').read_text())
    checkpoint['schema']=ReviewedEpisode.checkpoint_schema
    checkpoint['config']=resolved
    checkpoint['policy']=current_policy
    checkpoint['state'].update(status='running',phase='user')
    checkpoint['state'].pop('pause_reason',None)
    checkpoint['in_flight']=None
    suffix=hashlib.sha256(str(output).encode()).hexdigest()[:12]
    conversations={}
    for role in ('user','code','judge'):
        cfg=output/'private'/role/'inbox/config.json'
        value=json.loads(cfg.read_text());conversations[role]=value['conversation_id']
        value['container_suffix']=suffix;save(cfg,value)
    save(output/'private/judge-start-continuation.json',dict(source=str(source),
         source_checkpoint_sha256=fingerprint(json.loads((source/'private/checkpoint.json').read_text())),
         revision=checkpoint['revision'],candidate_version=candidate_version,
         conversation_ids=conversations,
         authority='Explicit continuation of a verified clean Judge task-tracker compatibility startup failure'))
    save(output/'private/checkpoint.json',checkpoint)
    return raw


def clone_evo_test_continuation(source, output):
    """Continue one clean required-test setup failure after the runner is fixed."""
    source, output = Path(source).resolve(), Path(output).resolve()
    checkpoint = json.loads((source / 'private/checkpoint.json').read_text())
    state = checkpoint.get('state', {})
    progress = checkpoint.get('progressive', {})
    task = progress.get('tasks', {}).get(state.get('task_id'), {})
    code_reply = state.get('code_reply') or {}
    checks = [item for item in state.get('checks', [])
              if item.get('id') == code_reply.get('id')
              and item.get('revision') == checkpoint.get('revision')
              and item.get('tool') == 'code_report']
    expected_reason = 'ValueError: SWE-Chain-Evo test patch does not apply to the test copy'
    if (checkpoint.get('in_flight') or state.get('status') != 'paused'
            or state.get('phase') != 'user' or state.get('pause_reason') != expected_reason
            or progress.get('job') is not None or progress.get('feedback_revision') is not None
            or not code_reply.get('stopped') or len(checks) != 1
            or (task.get('verdict') or {}).get('revision') == checkpoint.get('revision')
            or checkpoint.get('budget', {}).get('pending')):
        raise ValueError('source is not the verified clean SWE-Chain-Evo test setup failure')
    candidate_version = candidate_hash(source / 'workspace/candidate')
    label = f"required-{state['task_id']}-r{checkpoint['revision']}-{candidate_version[:12]}"
    failed_experiment = source / 'judge-workspace/experiments' / label
    if not failed_experiment.is_dir() or (failed_experiment / 'result.json').exists():
        raise ValueError('source does not retain the expected incomplete test experiment')

    raw = copy.deepcopy(checkpoint['config'])
    raw.pop('_progressive_policy', None)
    raw.pop('_reviewed_policy', None)
    resolved = progressive_config(reviewed_config(raw))
    resolved['dialogue_language'] = resolved.get('dialogue_language', 'zh-CN')
    current_policy = policy_record(
        resolved['dialogue_language'], resolved.get('delegation_variant', 'neutral'),
        resolved.get('code_prompt_mode', 'local'))
    old_progressive = checkpoint['config'].get('_progressive_policy', {}).copy()
    new_progressive = resolved['_progressive_policy'].copy()
    for name in ('evo_tests.py', 'progressive.py', 'sandbox.py'):
        old_progressive.pop(name, None)
        new_progressive.pop(name, None)
    old_reviewed = checkpoint['config'].get('_reviewed_policy', {}).copy()
    new_reviewed = resolved['_reviewed_policy'].copy()
    old_reviewed.pop('reviewed_episode.py', None)
    new_reviewed.pop('reviewed_episode.py', None)
    old_policy = copy.deepcopy(checkpoint.get('policy', {}))
    new_policy = copy.deepcopy(current_policy)
    for value in (old_policy, new_policy):
        implementation = value.get('implementation_sha256', {})
        for name in ('evo_tests.py', 'progressive.py', 'sandbox.py'):
            implementation.pop(name, None)
    if old_progressive != new_progressive or old_reviewed != new_reviewed or old_policy != new_policy:
        raise ValueError('source policy differs beyond the explicit test-runner repair')

    shutil.copytree(source, output)
    checkpoint = json.loads((output / 'private/checkpoint.json').read_text())
    checkpoint['schema'] = ReviewedEpisode.checkpoint_schema
    checkpoint['config'] = resolved
    checkpoint['policy'] = current_policy
    checkpoint['state'].update(status='running', phase='user')
    checkpoint['state'].pop('pause_reason', None)
    checkpoint['in_flight'] = None
    shutil.rmtree(output / 'judge-workspace/experiments' / label)
    authoritative = output / 'judge-workspace/experiments' / (label + '-authoritative-tests')
    if authoritative.exists():
        shutil.rmtree(authoritative)
    suffix = hashlib.sha256(str(output).encode()).hexdigest()[:12]
    for role in ('user', 'code', 'judge'):
        cfg = output / 'private' / role / 'inbox/config.json'
        if cfg.exists():
            value = json.loads(cfg.read_text())
            value['container_suffix'] = suffix
            save(cfg, value)
    reset_cloned_execution(output / 'private')
    save(output / 'private/evo-test-continuation.json', dict(
        source=str(source), source_checkpoint_sha256=fingerprint(json.loads(
            (source / 'private/checkpoint.json').read_text())),
        revision=checkpoint['revision'], candidate_version=candidate_version,
        removed_incomplete_experiment=label,
        authority='Explicit continuation after the authoritative test-overlay repair'))
    save(output / 'private/checkpoint.json', checkpoint)
    return raw


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','env-file','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--approved-first',type=Path)
    parser.add_argument('--fresh',action='store_true',help='Start with the prepared first fragment and supervised User drafts')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    load_environment(args.env_file)
    config=json.loads(args.config.read_text())
    if args.resume and (args.fresh or args.approved_first):
        raise ValueError('Resume cannot import or start another fresh conversation')
    episode=ReviewedEpisode(config,args.output,resume=args.resume)
    if not args.resume:
        if args.approved_first:
            episode.import_approved_first(args.approved_first)
        elif not args.fresh:
            raise ValueError('Use --fresh or provide an explicit approved source')
    result=episode.run()
    export_dialogue(args.output)
    print(json.dumps({'status':result}))
