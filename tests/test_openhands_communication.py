"""Role authority and gate fixtures; no model calls."""
import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from simulator.openhands.state import TaskState
from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.guard import MessageGuard
from simulator.openhands.user_projection import control_result


class GateFixture:
    config = {'model':'fixture'}

    def __init__(self, **changes):
        self.result = dict(decision='allow', kind='message', claims_observation=False,
                           state_consistent=True, reasons=[])
        self.result.update(changes)

    def dispatch(self, packet):
        self.input = json.loads(base64.b64decode(packet['body']))
        return 200, json.dumps({'choices':[{'message':{'content':json.dumps(self.result)}}]}).encode()


class CommunicationTests(unittest.TestCase):
    def test_send_mismatch_keeps_permit_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
            episode.private = Path(root)
            episode.state = TaskState()
            permit = episode.state.transition({
                'task_id': 'task-1', 'state': 'BUILD', 'control': 'CONTINUE',
                'reason': 'Ask Code to implement the requested change',
            })
            episode.saved = {
                'control_results': {}, 'public': [], 'code_sources': [],
                'tasks': [{'kind': 'issue', 'title': 'Bug', 'body': 'Description'}],
            }
            episode.collect_user_sources = MagicMock()
            episode.requirement = MagicMock(return_value={'title': 'Bug', 'body': 'Description'})
            episode.persist = MagicMock()
            episode.public = MagicMock()
            episode.guard = MagicMock()
            episode.guard.review.return_value = {
                'allowed': False,
                'handoff_kind': 'request_or_feedback',
                'warnings': [],
                'reasons': ['Request type/control does not match the proposed action.',
                            'private semantic detail'],
            }
            packet = {
                'request_id': 'send-mismatch-1', 'operation': 'send',
                'payload': {'task_id': 'task-1', 'permit_id': permit['id'],
                            'text': '请继续处理', 'evidence_ids': []},
            }
            before = copy.deepcopy(episode.state.data)
            first = episode._control(packet)
            episode.state = TaskState(json.loads(json.dumps(episode.state.data)))
            episode.saved = json.loads(json.dumps(episode.saved))
            second = episode._control(packet)

            self.assertFalse(first['accepted'])
            self.assertEqual(first, second)
            self.assertEqual(episode.guard.review.call_count, 1)
            self.assertEqual(episode.state.data, before)
            self.assertEqual(episode.state.data['permit'], permit)
            self.assertEqual(episode.state.data['messages'], [])
            self.assertEqual(episode.state.view()['published_messages'], 0)
            episode.public.assert_not_called()

            projected = control_result('send', first, episode.state, {})
            self.assertNotIn('reasons', projected)
            self.assertNotIn('private semantic detail', json.dumps(projected))
            self.assertIn('修改正文', projected['reason'])
            self.assertIn('不要重新申请状态', projected['reason'])
            self.assertEqual(projected['active_request']['permit_id'], permit['id'])

    def test_successful_control_returns_only_safe_classification_warning(self):
        with tempfile.TemporaryDirectory() as root:
            episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
            episode.private = Path(root)
            episode.state = TaskState()
            episode.saved = {
                'control_results': {}, 'public': [], 'code_sources': [],
                'tasks': [{'kind': 'issue', 'title': 'Bug', 'body': 'Description'}],
            }
            episode.collect_user_sources = MagicMock()
            episode.requirement = MagicMock(return_value={'title': 'Bug', 'body': 'Description'})
            episode.persist = MagicMock()
            episode.public = MagicMock()
            episode.guard = MagicMock()
            warning = 'Request type/control classification may not match the proposed action.'
            episode.guard.review.return_value = {
                'allowed': True, 'handoff_kind': 'request_or_feedback',
                'warnings': [warning], 'reasons': ['private reviewed detail'],
            }
            transition = episode._control({
                'request_id': 'transition-1', 'operation': 'transition',
                'payload': {'task_id': 'task-1', 'candidates': [{
                    'state': 'BUILD', 'control': 'CONTINUE', 'reason': 'clarify'}]},
            })
            self.assertEqual(transition['classification_warning'], warning)
            self.assertNotIn('reasons', transition)

            send = episode._control({
                'request_id': 'send-1', 'operation': 'send',
                'payload': {'task_id': 'task-1', 'permit_id': transition['permit_id'],
                            'text': '公开正文', 'evidence_ids': []},
            })
            self.assertEqual(send['classification_warning'], warning)
            public_item = episode.public.call_args.args[0]
            self.assertEqual(public_item['text'], '公开正文')
            self.assertNotIn('classification_warning', public_item)

            episode.state.data['phase'] = 'user'
            before = copy.deepcopy(episode.state.data)
            episode.guard.review.return_value = {
                'allowed': False, 'warnings': [warning],
                'reasons': ['safety rejection'],
            }
            rejected = episode._control({
                'request_id': 'transition-2', 'operation': 'transition',
                'payload': {'task_id': 'task-1', 'candidates': [{
                    'state': 'DEBUG', 'control': 'CONTINUE', 'reason': 'unsafe',
                    'context_basis': 'Description'}]},
            })
            self.assertFalse(rejected['accepted'])
            self.assertNotIn('classification_warning', rejected)
            self.assertEqual(episode.state.data, before)

    def test_initial_and_followup_context_share_host_authority(self):
        state = TaskState()
        self.assertIsNone(state.data['state'])
        episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.config = {}
        episode.state = state
        episode.saved = {'tasks':[{'kind':'issue', 'title':'Bug', 'body':'Description'}]}
        for reply in (None, {'id':'code-1','text':'需要兼容旧接口吗？','stopped':True}):
            state.data['code_reply'] = reply
            relay = GateFixture()
            MessageGuard(relay, []).review('send', {'text':'保持旧接口'}, state, {}, [])
            audit = json.loads(relay.input['messages'][-1]['content'])
            self.assertEqual(episode.user_input()['communication'], {
                'stage': state.communication()['stage'],
                'last_code_reply': reply,
            })
            self.assertEqual(audit['communication'], state.communication())
            self.assertEqual(audit['communication']['last_code_reply'], reply)
            self.assertEqual(audit['communication']['code_has_spoken_for_current_task'], reply is not None)
            self.assertEqual(set(audit), {
                'operation', 'proposal', 'requirement', 'communication', 'current_action',
                'evidence', 'simulated_observations', 'dialogue_language', 'current_state',
                'request_semantics'})
            self.assertEqual(audit['current_state'], state.data['state'])
            self.assertEqual(set(audit['request_semantics']), {'states', 'controls'})
        state.transition({'task_id':'task-1','state':'RETRIEVE','control':'REFINE','reason':'Ask Code where the entrypoint is'})
        self.assertEqual(state.data['state'],'RETRIEVE')

    def test_evidence_and_role_failures_are_both_preserved(self):
        relay = GateFixture(decision='reject', claims_observation=True, reasons=['wrong speaker'])
        state = TaskState()
        guard = MessageGuard(relay, [])
        result = guard.review('send', {'text':'需要我帮你修吗？'}, state, {}, [])
        self.assertIn('wrong speaker', result['reasons'])
        self.assertTrue(any('evidence_ids' in r for r in result['reasons']))
        state.data['checks'] = [{'id':'e1','result':'observed'}]
        self.assertFalse(guard.review('send', {'text':'需要我帮你修吗？','evidence_ids':['e1']}, state, {}, [])['allowed'])

    def test_intent_mismatch_and_missing_flags_fail_closed(self):
        state = TaskState()
        state.transition({'task_id':'task-1','state':'UNDERSTAND','control':'REFINE','reason':'Ask about compatibility'})
        relay = GateFixture(decision='reject', reasons=['intent mismatch'])
        self.assertFalse(MessageGuard(relay, []).review('send', {'text':'直接重写'}, state, {}, [])['allowed'])
        data = json.loads(relay.input['messages'][-1]['content'])
        self.assertEqual(data['current_action'],state.data['permit'])
        del relay.result['kind']
        with self.assertRaises(ValueError):
            MessageGuard(relay, []).review('send', {'text':'直接重写'}, state, {}, [])

    def test_state_inconsistency_is_private_warning_without_host_rewrite(self):
        state = TaskState()
        relay = GateFixture(state_consistent=False)
        result = MessageGuard(relay, []).review(
            'transition',
            {'task_id':'task-1', 'state':'UNDERSTAND', 'control':'CONTINUE',
             'reason':'先看看现状再实现'},
            state, {}, [],
        )
        self.assertTrue(result['allowed'])
        self.assertEqual(result['reasons'], [])
        self.assertTrue(any('Request type/control' in warning for warning in result['warnings']))
        self.assertIsNone(state.data['state'])

    def test_safety_rejection_is_not_overridden_by_state_warning(self):
        state = TaskState()
        relay = GateFixture(
            decision='reject', state_consistent=False, reasons=['wrong speaker']
        )
        result = MessageGuard(relay, []).review(
            'transition',
            {'task_id':'task-1', 'state':'UNDERSTAND', 'control':'CONTINUE',
             'reason':'Code should let me edit it instead'},
            state, {}, [],
        )
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reasons'], ['wrong speaker'])
        self.assertTrue(result['warnings'])

    def test_state_roundtrip_and_new_task_do_not_invent_code_reply(self):
        state = TaskState()
        state.data['code_reply']={'id':'c1','text':'done','stopped':True}
        permit=state.transition({'task_id':'task-1','state':'UNDERSTAND','control':'CONTINUE','reason':'followup'})
        restored=TaskState(json.loads(json.dumps(state.data)))
        self.assertEqual(restored.communication(),state.communication())
        self.assertEqual(restored.data['permit'],permit)
        state.accept({'task_id':'task-1','reason':'accepted'})
        state.release_next()
        self.assertEqual(state.communication()['stage'],'initial_delegation')
        self.assertIsNone(state.communication()['last_code_reply'])

    def test_resume_rejects_old_schema_without_mutation(self):
        import tempfile
        from pathlib import Path
        from simulator.openhands.policy import policy_record
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'private'
            path.mkdir()
            config={'dialogue_language':'zh-CN'}
            raw=json.dumps({'schema':'openhands-v2','config':config,'policy':policy_record('zh-CN')})
            checkpoint=path/'checkpoint.json'
            checkpoint.write_text(raw)
            with self.assertRaises(ValueError):
                OpenHandsEpisode(config,folder,resume=True)
            self.assertEqual(checkpoint.read_text(),raw)
