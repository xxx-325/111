"""Historical diagnostic recovery must not consume future state."""
import json
import tempfile
import unittest
from pathlib import Path

from simulator.openhands.role_diagnostic import prior_case
from simulator.openhands.state import TaskState
from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.guard import MessageGuard
from unittest.mock import patch, MagicMock
import base64


class DiagnosticRecoveryTests(unittest.TestCase):
    def test_first_draft_saved_before_review_error_and_never_delivered(self):
        import copy
        from simulator.openhands.role_diagnostic import FirstDelegation
        episode=FirstDelegation.__new__(FirstDelegation)
        episode.state=TaskState()
        permit=episode.state.transition(dict(task_id='task-1',state='BUILD',control='CONTINUE',reason='Ask for help'))
        episode.saved={'public':[],'code_sources':[],'tasks':[dict(kind='issue',title='当前需求',body='日期生成结果有问题。')]}
        snapshots=[]
        episode.persist=lambda: snapshots.append(copy.deepcopy(episode.saved))
        episode.collect_user_sources=MagicMock()
        episode.guard=MagicMock()
        def review(*args):
            self.assertEqual(snapshots[-1]['diagnostic_draft']['payload']['text'],'unaltered draft')
            raise ValueError('fixture review parse failure')
        episode.guard.review.side_effect=review
        packet=dict(request_id='send',operation='send',payload=dict(task_id='task-1',permit_id=permit['id'],text='unaltered draft'))
        result=episode._control(packet)
        self.assertTrue(result['handoff'])
        self.assertFalse(result['accepted'])
        self.assertEqual(episode.saved['diagnostic_draft']['error_type'],'ValueError')
        self.assertFalse(episode.saved['diagnostic_draft']['delivered'])
        self.assertEqual(episode.state.data['messages'],[])
        self.assertEqual(episode.saved['public'],[])
        episode._control(packet)
        self.assertEqual(episode.guard.review.call_count,1)

    def test_initial_instruction_requests_help_without_demanding_details(self):
        text=OpenHandsEpisode.__new__(OpenHandsEpisode).initial_instruction()
        self.assertNotIn('信息不足就向 Code 提问',text)
        self.assertIn('让 Code 看一下',text)

    def test_generation_and_guard_use_same_communication_stage(self):
        for reply in (None,{'id':'code','text':'可以先检查吗？','stopped':True}):
            state=TaskState()
            state.data['code_reply']=reply
            episode=OpenHandsEpisode.__new__(OpenHandsEpisode)
            episode.state=state
            episode.saved={'tasks':[{'kind':'issue','title':'当前需求','body':'日期生成结果有问题。'}]}
            expected=episode.user_input()['communication']
            relay=MagicMock(config={'model':'fixture'})
            relay.dispatch.return_value=(200,json.dumps({'choices':[{'message':{'content':json.dumps({
                'decision':'allow','kind':'message','claims_observation':False,
                'state_consistent':True,'reasons':[]})}}]}).encode())
            MessageGuard(relay,[]).review('transition',dict(task_id='task-1',reason='请 Code 检查',control='CONTINUE'),
                state,episode.requirement(),[])
            packet=relay.dispatch.call_args.args[0]
            body=json.loads(base64.b64decode(packet['body']))
            actual=json.loads(body['messages'][1]['content'])['communication']
            self.assertEqual(actual['stage'],expected['stage'])
            self.assertEqual(actual['last_code_reply'],expected['last_code_reply'])

    def test_review_failure_stops_before_next_case_without_retry(self):
        from simulator.openhands.role_diagnostic import run_reviews
        cases=[dict(id=str(i),source='fixture',operation='transition',expected=False,state=TaskState().data,
                    payload={},requirement={},history=[]) for i in range(2)]
        with tempfile.TemporaryDirectory() as tmp, patch('simulator.openhands.role_diagnostic.MessageGuard') as guard:
            guard.return_value.review.return_value={'allowed':True}
            result=run_reviews({'user':{'model':'fixture'}},Path(tmp)/'new',cases=cases)
            self.assertEqual(guard.return_value.review.call_count,1)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['cases'][1]['status'],'not_run')

    def test_only_pre_send_transitions_are_recovered(self):
        state = TaskState()
        initial = state.view()
        payload = dict(task_id='task-1', state='BUILD', control='CONTINUE', reason='Delegate')
        permit = state.transition(payload)
        proposal = dict(task_id='task-1', permit_id=permit['id'], text='Please fix it')
        controls = [
            dict(id='read', operation='read_state', result=dict(accepted=True, state=initial)),
            dict(id='before', operation='transition', payload=payload,
                 result=dict(accepted=True, permit_id=permit['id'])),
            dict(id='send', operation='send', payload=proposal, result=dict(accepted=True)),
            dict(id='future', operation='transition', payload={ }, result=dict(accepted=True)),
        ]
        content = dict(operation='send', proposal=proposal, requirement={}, state=state.view(), public_history=[])
        event = dict(kind='request', id='gate-one', input=dict(messages=[dict(content=json.dumps(content))]))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'private/user').mkdir(parents=True)
            (root/'private/controls.jsonl').write_text('\n'.join(map(json.dumps, controls)))
            (root/'private/user/provider.jsonl').write_text(json.dumps(event)+'\n')
            case = prior_case(root)
        self.assertEqual(case['state']['transitions'], [permit])
        self.assertEqual(case['reconstructed_fields']['transitions'], ['before'])
        self.assertEqual(case['state']['messages'], [])
        self.assertNotIn('future', json.dumps(case))
