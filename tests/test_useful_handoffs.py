import tempfile
import unittest
import json
import hashlib
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.state import TaskState
from simulator.episode import save


class HandoffTests(unittest.TestCase):
    def test_only_clean_user_pause_reasons_are_resumable(self):
        from simulator.openhands.reviewed_episode import resumable_user_pause
        self.assertTrue(resumable_user_pause('User Agent stopped without a public send or task decision'))
        self.assertTrue(resumable_user_pause('Assistant rejected User draft: too detailed'))
        self.assertFalse(resumable_user_pause('Judge uncertain'))

    def reviewed_episode(self, root):
        from simulator.openhands.reviewed_episode import ReviewedEpisode
        e=ReviewedEpisode.__new__(ReviewedEpisode)
        e.private=Path(root)
        e.private.mkdir(parents=True,exist_ok=True)
        e.state=TaskState()
        e.saved={'control_results':{},'revision':1,
                 'tasks':[{'kind':'issue','title':'x','body':'x'}]}
        e.budget=SimpleNamespace(deadline=999999999999)
        e.requirement=lambda: {'title':'x','body':'x'}
        e.current=lambda: {}
        e.persist=lambda: None
        return e

    def test_assistant_rejection_reason_returns_to_same_user(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.reviewed_episode(root)
            packet={'request_id':'send-1','operation':'send','payload':{'text':'too much'}}
            decision={'approved':False,'reason':'只反馈观察，不要补实现指令'}
            with patch('simulator.openhands.reviewed_episode.await_review',return_value=decision):
                result=e._control(packet)
            self.assertEqual(decision['reason'],result['reason'])
            self.assertEqual(e.saved['control_results']['send-1'],result)
            self.assertNotIn('send-1',[item.get('id') for item in e.saved.get('public',[])])

    def continuation_source(self, root, reason, with_gate=False, current_gate=False):
        from simulator.openhands.judge import candidate_hash
        source=Path(root)/'source';candidate=source/'workspace/candidate'
        candidate.mkdir(parents=True);(candidate/'file.py').write_text('value=1\n')
        (source/'private').mkdir()
        version=candidate_hash(candidate)
        checkpoint={'schema':'old','config':{'progressive_issues':True,'dialogue_language':'zh-CN'},
            'policy':{},'state':{'status':'paused','pause_reason':reason,'task_id':'task-1'},
            'revision':1,'in_flight':None,'control_results':{},'progressive':{
                'job':None,'decisions':{},'tasks':{'task-1':{'verdict':{
                    'outcome':'unsolved','revision':1,'candidate_version':version}}}}}
        if with_gate:
            rejection=reason.split(':',1)[1].strip();request_id='request-1'
            material=({'text':'retained draft'} if current_gate
                      else {'payload':{'text':'retained draft'}})
            digest=hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            gates=source/'private/assistant-gates';gates.mkdir(parents=True)
            save(gates/f'send-{request_id}.request.json',{'sha256':digest,'material':material})
            save(gates/f'send-{request_id}.decision.json',{'sha256':digest,'approved':False,'reason':rejection})
            checkpoint['control_results'][request_id]={'accepted':False,'reason':'not delivered'}
        save(source/'private/checkpoint.json',checkpoint)
        return source

    def test_clone_carries_only_recorded_rejection_without_delivering_draft(self):
        from simulator.openhands.reviewed_episode import clone_user_turn_continuation
        with tempfile.TemporaryDirectory() as root:
            reason='Assistant rejected User draft: too detailed'
            source=self.continuation_source(root,reason,with_gate=True);output=Path(root)/'output'
            clone_user_turn_continuation(source,output)
            checkpoint=json.loads((output/'private/checkpoint.json').read_text())
            self.assertEqual(checkpoint['pending_user_rejection'],{
                'request_id':'request-1','draft':'retained draft','delivered':False,'reason':'too detailed'})
            self.assertFalse((output/'session.jsonl').exists())
            self.assertEqual(json.loads((source/'private/checkpoint.json').read_text())['state']['status'],'paused')

    def test_resumed_rejection_is_input_once_then_consumed_by_user_action(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.reviewed_episode(root)
            rejection={'request_id':'request-1','draft':'retained draft',
                       'delivered':False,'reason':'too detailed'}
            e.saved['pending_user_rejection']=rejection
            self.assertEqual(e.user_input()['rejected_send'],rejection)
            logged=json.loads((Path(root)/'user-inputs.jsonl').read_text().splitlines()[-1])
            self.assertEqual(logged['rejected_send'],rejection)
            packet={'request_id':'send-2','operation':'send','payload':{'text':'short'}}
            with patch('simulator.openhands.reviewed_episode.await_review',return_value={
                    'approved':False,'reason':'still too detailed'}):
                e._control(packet)
            self.assertNotIn('pending_user_rejection',e.saved)

    def test_clone_reads_current_top_level_gate_draft(self):
        from simulator.openhands.reviewed_episode import clone_user_turn_continuation
        with tempfile.TemporaryDirectory() as root:
            reason='Assistant rejected User draft: too detailed'
            source=self.continuation_source(root,reason,with_gate=True,current_gate=True)
            output=Path(root)/'output';clone_user_turn_continuation(source,output)
            checkpoint=json.loads((output/'private/checkpoint.json').read_text())
            self.assertEqual(checkpoint['pending_user_rejection']['draft'],'retained draft')

    def test_clone_can_return_one_rejected_closing_to_final_solved_user(self):
        from simulator.openhands.reviewed_episode import clone_user_turn_continuation
        with tempfile.TemporaryDirectory() as root:
            reason='Assistant rejected User draft: accept silently'
            source=self.continuation_source(root,reason,with_gate=True,current_gate=True)
            checkpoint=json.loads((source/'private/checkpoint.json').read_text())
            checkpoint['tasks']=[{'id':'task-1'}]
            checkpoint['state']['task_index']=0
            checkpoint['progressive']['tasks']['task-1']['verdict']['outcome']='solved'
            save(source/'private/checkpoint.json',checkpoint)
            output=Path(root)/'output'
            clone_user_turn_continuation(source,output)
            resumed=json.loads((output/'private/checkpoint.json').read_text())
            self.assertEqual(resumed['state']['status'],'running')
            self.assertEqual(resumed['pending_user_rejection']['reason'],'accept silently')

    def test_clone_non_rejection_pause_does_not_invent_rejection(self):
        from simulator.openhands.reviewed_episode import clone_user_turn_continuation
        with tempfile.TemporaryDirectory() as root:
            source=self.continuation_source(root,'User Agent stopped without a public send or task decision')
            for role in ('code', 'judge'):
                execution=source/'private'/role/'execution'
                (execution/'auth').mkdir(parents=True)
                (execution/'client').mkdir()
                (execution/'home').mkdir()
                (execution/'outputs').mkdir()
                save(execution/'environment.json', {'status':'ready','old':'identity'})
                remote=source/'private'/role/'sdk/remote-tools';remote.mkdir(parents=True)
                save(remote/'openhands-identity.json', {'identity':'old'})
            output=Path(root)/'output';clone_user_turn_continuation(source,output)
            checkpoint=json.loads((output/'private/checkpoint.json').read_text())
            self.assertNotIn('pending_user_rejection',checkpoint)
            self.assertIsNone(json.loads((output/'private/user-turn-continuation.json').read_text())['rejected_send'])
            for role in ('code', 'judge'):
                execution=output/'private'/role/'execution'
                self.assertFalse((execution/'environment.json').exists())
                self.assertFalse((execution/'auth').exists())
                self.assertFalse((execution/'client').exists())
                self.assertTrue((execution/'home').is_dir())
                self.assertTrue((execution/'outputs').is_dir())
                self.assertFalse((output/'private'/role/'sdk/remote-tools').exists())

    def test_verified_judge_start_clone_preserves_revision_history_and_sessions(self):
        from simulator.openhands.judge import candidate_hash
        from simulator.openhands.reviewed_episode import clone_judge_start_continuation, reviewed_config
        from simulator.openhands.progressive import progressive_config
        from simulator.openhands.policy import policy_record
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'source';candidate=source/'workspace/candidate'
            candidate.mkdir(parents=True);(candidate/'file.py').write_text('fixed\n')
            private=source/'private';private.mkdir()
            budget={'attempts':3,'calls':3,'prompt_tokens':10,'completion_tokens':2,
                    'cost':None,'usage_missing':False,'pending':{}}
            config=progressive_config(reviewed_config({'progressive_issues':True,'dialogue_language':'zh-CN'}))
            checkpoint={'schema':'old','config':config,'policy':policy_record('zh-CN'),
                'state':{'status':'paused','phase':'user','task_id':'task-1',
                    'pause_reason':'RuntimeError: SDK worker exited; inspect private worker.log',
                    'code_reply':{'id':'code-r2','text':'fixed','stopped':True},
                    'checks':[{'id':'code-r2','revision':2,'tool':'code_report'}]},
                'revision':2,'in_flight':None,'budget':budget,'public':[{'id':'old'}],
                'progressive':{'job':None,'feedback_revision':None,'decisions':{},
                    'tasks':{'task-1':{'verdict':{'outcome':'unsolved','revision':1,
                        'candidate_version':'old'}}}}}
            save(private/'checkpoint.json',checkpoint);save(private/'budget.json',budget)
            for role in ('user','code','judge'):
                inbox=private/role/'inbox';outbox=private/role/'outbox'
                inbox.mkdir(parents=True);outbox.mkdir()
                save(inbox/'config.json',{'conversation_id':role+'-conversation','container_suffix':'0123456789ab'})
                save(outbox/'active.json',{'status':'stopped'})
            (private/'judge/worker.log').write_text(
                "ValueError: Cannot resume conversation: tools were removed mid-conversation (removed: ['task_tracker'])\n")
            output=Path(root)/'output';clone_judge_start_continuation(source,output)
            cloned=json.loads((output/'private/checkpoint.json').read_text())
            self.assertEqual(cloned['revision'],2)
            self.assertEqual(cloned['public'],checkpoint['public'])
            self.assertEqual(cloned['budget'],budget)
            self.assertEqual(cloned['state']['phase'],'user')
            self.assertEqual(candidate_hash(output/'workspace/candidate'),candidate_hash(candidate))
            record=json.loads((output/'private/judge-start-continuation.json').read_text())
            self.assertEqual(record['conversation_ids'],{
                'user':'user-conversation','code':'code-conversation','judge':'judge-conversation'})
            self.assertEqual(record['revision'],2)

    def episode(self, root, kind='internal_plan'):
        e=OpenHandsEpisode.__new__(OpenHandsEpisode)
        e.private=Path(root)
        e.state=TaskState()
        e.saved={'last_code_reply':'done', 'tasks':[{}], 'public':[]}
        e.collect_user_sources=lambda: None
        e.requirement=lambda: {'title':'x','body':'x'}
        e.acceptance_gate=lambda: None
        e.persist=lambda: None
        e.saved['code_sources']=[]
        e.guard=SimpleNamespace(review=lambda *a: {'allowed':True,'handoff_kind':kind})
        e.public=lambda *a: self.fail('Unexpected public delivery')
        return e

    def test_private_plan_keeps_user_turn_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.episode(root)
            packet={'request_id':'s1','operation':'send','payload':{'task_id':'task-1','text':'I will check later'}}
            result=e._control(packet)
            self.assertTrue(result['retained_private'])
            self.assertFalse(result['handoff'])
            self.assertEqual(e.state.data['phase'],'user')
            self.assertEqual(e._control(packet),result)
            self.assertEqual(len((Path(root)/'controls.jsonl').read_text().splitlines()),1)

    def test_silent_accept_ends_without_public_message(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.episode(root)
            result=e._control({'request_id':'a1','operation':'accept','payload':{'task_id':'task-1','reason':'satisfied'}})
            self.assertTrue(result['ended'])
            self.assertEqual(e.state.data['status'],'completed')
            self.assertEqual(e.saved['closing_mode'],'silent')

    def test_requested_closing_is_rejected_and_cannot_reopen_user_phase(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.episode(root)
            result=e._control({'request_id':'a1','operation':'accept','payload':{
                'task_id':'task-1','reason':'satisfied','send_closing_reply':True}})
            self.assertFalse(result['accepted'])
            self.assertEqual(e.state.data['phase'],'user')
            self.assertNotIn('closing_mode',e.saved)
