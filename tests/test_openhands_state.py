import unittest
import json
from pathlib import Path

from simulator.openhands.state import TaskState, TransitionError
from simulator.openhands.events import public_event, private_observation
from simulator.openhands.budget import Budget


class StateTests(unittest.TestCase):
    def test_continue_retains_state(self):
        state = TaskState()
        state.data['state'] = 'DEBUG'
        state.transition(dict(task_id='task-1', control='CONTINUE', reason='Continue the diagnosis'))
        self.assertEqual(state.data['state'], 'DEBUG')
        state.transition(dict(task_id='task-1', state=None, control='REFINE', reason='Clarify'))
        self.assertEqual(state.data['state'], 'DEBUG')

    def test_saved_full02_annotations_follow_shared_state_boundary(self):
        path = Path(__file__).parent / 'fixtures/full02_state_annotations.json'
        rows = json.loads(path.read_text())
        self.assertEqual(len(rows), 10)
        for row in rows:
            if row['turn'] == 'clarification':
                self.assertEqual((row['state'], row['control']), ('BUILD', 'REFINE'))
            elif row['turn'] == 'initial':
                self.assertEqual(row['state'], 'BUILD')
            else:
                self.assertEqual((row['state'], row['control']), ('DEBUG', 'CORRECT'))

    def test_no_text_bypass_or_duplicate_send(self):
        state = TaskState()
        payload = dict(task_id='task-1', text='Please fix the reported behavior.')
        with self.assertRaises(TransitionError):
            state.send(payload)
        permit = state.transition(dict(task_id='task-1', state='BUILD', control='REFINE', reason='Request the current implementation'))
        payload['permit_id'] = permit['id']
        state.send(payload)
        self.assertFalse(state.data['messages'][-1]['published'])
        self.assertEqual(state.view()['published_messages'], 0)
        state.data['messages'][-1]['published'] = True
        self.assertEqual(state.view()['published_messages'], 1)
        with self.assertRaises(TransitionError):
            state.send(payload)

    def test_accept_without_check_is_not_test_pass(self):
        state = TaskState()
        result = state.accept(dict(task_id='task-1', reason='Trust the report'))
        self.assertFalse(result['checks_passed'])
        self.assertEqual(result['basis'], 'user_acceptance_without_independent_check')

    def test_failure_cannot_be_accepted(self):
        state = TaskState()
        state.data['checks'].append(dict(id='e1', result='failed', revision=1))
        with self.assertRaises(TransitionError):
            state.accept(dict(task_id='task-1', reason='Looks good'))
        with self.assertRaises(TransitionError):
            state.transition(dict(task_id='task-1', control='CONTINUE', reason='Ignore failure', resolves=['e1']))

    def test_future_task_and_fake_evidence_rejected(self):
        state = TaskState()
        with self.assertRaises(TransitionError):
            state.release_next()
        with self.assertRaises(TransitionError):
            state.transition(dict(task_id='task-2', control='CONTINUE', reason='future'))
        with self.assertRaises(TransitionError):
            state.accept(dict(task_id='task-1', reason='checked', evidence_ids=['invented']))

    def test_pause_is_not_completion(self):
        state = TaskState()
        state.pause(dict(task_id='task-1', reason='environment unavailable'))
        self.assertEqual(state.data['accepted'], [])
        with self.assertRaises(TransitionError):
            state.release_next()

    def test_public_projection_excludes_reasoning_and_user(self):
        self.assertIsNone(public_event(dict(kind='MessageEvent', source='user', llm_message={'content': [{'type': 'text', 'text': 'private requirement'}]})))
        item = public_event(dict(kind='ActionEvent', thought='private analysis', reasoning_content='private reasoning',
                                 tool_name='terminal', tool_call_id='x', action={'command': 'pytest'}))
        self.assertNotIn('reasoning', str(item))
        self.assertNotIn('thought', item)
        self.assertEqual(item['action'], {'command': 'pytest'})
        self.assertIsNone(public_event(dict(kind='ActionEvent', tool_name='think', action={'thought':'private'})))
        error = public_event(dict(kind='AgentErrorEvent', tool_name='terminal', tool_call_id='x', error='invalid argument'))
        self.assertEqual(error['observation']['error'], 'invalid argument')

    def test_cost_limit_requires_verifiable_model_pricing(self):
        with self.assertRaises(ValueError):
            Budget({'max_cost': 1})

    def test_cost_missing_usage_fails_closed(self):
        config = dict(max_cost=1, user={'model': 'm'}, code={'model': 'm'}, pricing={
            'source': 'configured test fixture', 'currency': 'USD',
            'user': dict(model='m', input_per_million=1, output_per_million=1),
            'code': dict(model='m', input_per_million=1, output_per_million=1)})
        budget = Budget(config)
        budget.after('user', {})
        with self.assertRaises(RuntimeError):
            budget.before('user', {})

    def test_piped_pytest_failure_is_not_success(self):
        result = private_observation(dict(kind='ObservationEvent', id='e1', tool_name='terminal',
            observation={'exit_code':0, 'content':[{'text':'===== 2 failed, 1 passed in 0.2s ====='}]}), 1)
        self.assertEqual(result['result'], 'failed')
        passed = private_observation(dict(kind='ObservationEvent', id='e2', tool_name='terminal',
            observation={'exit_code':0, 'content':[{'text':'===== 3 passed in 0.2s ====='}]}), 2)
        self.assertEqual(passed['result'], 'passed')

    def test_explicit_null_exit_code_falls_back_to_terminal_metadata(self):
        result = private_observation(dict(
            kind='ObservationEvent', id='e3', tool_name='terminal',
            observation={
                'exit_code': None,
                'metadata': {'exit_code': 7},
                'content': [{'text': 'build failed'}],
            },
        ), 3)
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['result'], 'failed')

    def test_failed_observation_can_be_resolved_by_real_success(self):
        state = TaskState()
        state.data['checks'] = [dict(id='failure', result='failed', revision=1),
                                dict(id='success', result='observed', exit_code=0, revision=2)]
        state.transition(dict(task_id='task-1', state='DEBUG', control='CORRECT', reason='The same behavior now succeeds',
                              evidence_ids=['success'], resolves=['failure']))
        self.assertTrue(state.data['checks'][0]['resolved_by'])

    def test_scoped_blocker_allows_discussion_not_acceptance(self):
        state = TaskState()
        state.data['checks'] = [dict(id='e1', result='reported', revision=1)]
        state.transition(dict(task_id='task-1', state='PLAN', control='REFINE', reason='Discuss an offline alternative',
            blocker={'reason':'Package unavailable', 'affected_operation':'dependency installation', 'evidence_ids':['e1']}))
        self.assertEqual(state.data['state'], 'PLAN')
        with self.assertRaises(TransitionError):
            state.accept(dict(task_id='task-1', reason='Done'))


if __name__ == '__main__':
    unittest.main()
