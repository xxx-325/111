import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator import expression
from simulator.episode import Episode
from test_episode import QueueAPI, response, call


class ExpressionTests(unittest.TestCase):
    def test_packet_is_allowlisted_and_tool_free(self):
        public = [{'kind': 'user', 'text': '需求'},
                  {'kind': 'tool_result', 'stdout': 'PRIVATE_TOOL_BODY'},
                  {'kind': 'assistant', 'phase': 'final', 'text': '请确认空输入行为。'}]
        data = expression.packet(public, '空输入返回空列表', ['尚未实现'], 'clarify', 'direct')
        self.assertLessEqual(len(data['examples']), 2)
        self.assertNotIn('PRIVATE_TOOL_BODY', json.dumps(data))
        api = QueueAPI([response('空列表就行。')])
        self.assertEqual(expression.generate(api, data), '空列表就行。')
        self.assertEqual(len(api.requests[0]), 2)
        self.assertEqual(api.requests[0][0]['role'], 'system')

    def test_routing_cannot_turn_failure_into_completion(self):
        for status, expected in [('needs_changes', 'repair'), ('insufficient_evidence', 'check')]:
            self.assertEqual(expression.choose_action(dict(status=status, interaction='check'), True), expected)
        self.assertEqual(expression.choose_action(dict(status='completed'), False), 'finish')
        self.assertEqual(expression.choose_action(dict(status='completed'), True), 'advance')
        self.assertEqual(expression.choose_action(dict(status='completed', interaction='clarify', focus='empty input?'), True), 'clarify')
        with self.assertRaises(ValueError):
            expression.choose_action(dict(status='completed', interaction='explain'), True)

    def test_rejected_message_is_regenerated_without_private_reason(self):
        safe = response('{"safe":true}')
        verdict = response('{"status":"completed","reason":"PRIVATE_REASON","facts":["通过"],"interaction":"check","focus":""}')
        user = QueueAPI([response('{"requirement":"支持空输入。"}'), safe, response('支持空输入。'), safe,
                         call('test'), verdict, safe, response('接着做其他事情吧。'),
                         response('{"safe":false,"reason":"PRIVATE_REJECTION missing next task"}'),
                         response('好了。'), safe])
        with self.episode(user) as episode:
            self.assertEqual(episode.run(), 'completed')
            expressions = [r for r in user.requests if r[0].get('content') == expression.SYSTEM]
            self.assertEqual(len(expressions), 3)
            for messages in expressions:
                text = json.dumps(messages)
                for secret in ('PRIVATE_REASON', 'PRIVATE_REJECTION', 'HIDDEN_PATCH', 'private-id'):
                    self.assertNotIn(secret, text)
            self.assertNotIn('接着做其他事情吧。', [e.get('text') for e in episode.state['public']])

    def test_obvious_role_inversion_and_empty_continuation_are_blocked(self):
        data = expression.packet([], 'Fix daterange month stepping', [], 'request', 'direct')
        self.assertEqual(data['examples'], [])
        self.assertIsNotNone(expression.structural_error(data, '按你的想法改一下，然后跑一下这段代码。'))
        self.assertIsNotNone(expression.structural_error(data, '这样改：\n```python\ndef fixed():\n    return 1\n```'))
        data['action'] = 'finish'
        self.assertIsNotNone(expression.structural_error(data, '好了，继续。'))
        self.assertIsNone(expression.structural_error(data, '好了。'))

    def test_three_rejections_stop_without_publishing(self):
        safe = response('{"safe":true}')
        reject = response('{"safe":false,"reason":"unsupported"}')
        user = QueueAPI([response('{"requirement":"支持空输入。"}'), safe] + [response('已经通过'), reject] * 3)
        with self.episode(user) as episode:
            with self.assertRaisesRegex(ValueError, 'three attempts'):
                episode.run()
            self.assertEqual(episode.state['public'], [])
            self.assertEqual(episode.state['expression']['attempts'], 3)

    def test_expression_boundary_resume_does_not_repeat_inspection_or_send(self):
        safe = response('{"safe":true}')
        verdict = response('{"status":"completed","reason":"checked","facts":["通过"],"interaction":"check","focus":""}')
        user = QueueAPI([response('{"requirement":"实现。"}'), safe, response('实现。'), safe,
                         call('test'), verdict, safe, response('好了。'), safe])
        with self.episode(user) as episode:
            original = episode.checkpoint
            def interrupt():
                original()
                if episode.state['phase'] == 'express' and episode.state['decisions'] and not episode.state['inflight']:
                    raise KeyboardInterrupt()
            with patch.object(episode, 'checkpoint', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                episode.run()
            resumed = Episode(episode.config, episode.output, resume=True, api_factory=lambda c: user)
            self.assertEqual(resumed.run(), 'completed')
            self.assertEqual(sum(e['kind'] == 'tool_result' for e in resumed.state['audits']), 1)
            self.assertEqual([e['text'] for e in resumed.state['public'] if e['kind'] == 'user'], ['实现。', '好了。'])

    def test_legacy_checkpoint_is_not_migrated(self):
        user = QueueAPI([])
        with self.episode(user) as episode:
            episode.lock.close()
            episode.state.pop('schema_version')
            episode.checkpoint()
            before = episode.state_path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'legacy checkpoint'):
                Episode(episode.config, episode.output, resume=True, api_factory=lambda c: user)
            self.assertEqual(episode.state_path.read_bytes(), before)

    def test_publish_boundary_resumes_without_regenerating_accepted_message(self):
        safe = response('{"safe":true}')
        verdict = response('{"status":"completed","reason":"checked","facts":["通过"],"interaction":"check","focus":""}')
        user = QueueAPI([response('{"requirement":"实现。"}'), safe, response('实现。'), safe,
                         call('test'), verdict, safe, response('好了。'), safe])
        with self.episode(user) as episode:
            original = episode.checkpoint
            def interrupt():
                original()
                if episode.state['phase'] == 'publish' and episode.state['decisions']:
                    raise KeyboardInterrupt()
            with patch.object(episode, 'checkpoint', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                episode.run()
            before = len(user.requests)
            resumed = Episode(episode.config, episode.output, resume=True, api_factory=lambda c: user)
            self.assertEqual(resumed.run(), 'completed')
            self.assertEqual(len(user.requests), before)
            self.assertEqual(sum(e.get('text') == '好了。' for e in resumed.state['public']), 1)

    def test_clarification_is_answered_before_completion(self):
        safe = response('{"safe":true}')
        clarification = response('{"status":"insufficient_evidence","reason":"needs answer","facts":["需求明确空输入返回空列表"],"interaction":"clarify","focus":"回答是否返回空列表"}')
        done = response('{"status":"completed","reason":"passed","facts":["通过"],"interaction":"check","focus":""}')
        user = QueueAPI([response('{"requirement":"空输入返回空列表"}'), safe, response('空输入返回空列表。'), safe,
                         clarification, safe, response('对，返回空列表。'), safe,
                         call('test'), done, safe, response('好了。'), safe])
        code = QueueAPI([response('空输入应该返回空列表吗？'), response('已完成。')])
        with self.episode(user) as episode:
            episode.code_api = code
            self.assertEqual(episode.run(), 'completed')
            self.assertIn('对，返回空列表。', json.dumps(code.requests[-1], ensure_ascii=False))
            actions = [e['action'] for e in episode.state['audits'] if e['kind'] == 'user_action']
            self.assertEqual(actions, ['request', 'clarify', 'finish'])

    def episode(self, user):
        from contextlib import contextmanager
        @contextmanager
        def fixture():
            config = dict(repository='unused', tasks=[], image='test', user={}, code={})
            task = dict(title='task', body='empty input', identifier='private-id', reference=None, patch='HIDDEN_PATCH', base='base')
            code = QueueAPI([response('实现了。')])
            with tempfile.TemporaryDirectory() as directory, patch('simulator.episode.prepare', return_value=(None, 'base', [task])), patch('simulator.episode.snapshot', side_effect=lambda r,b,p:p.mkdir()), patch('simulator.sandbox.Sandbox.execute', return_value=dict(exit_code=0, stdout='pass', stderr='')):
                apis = iter([user, code])
                ep = Episode(config, Path(directory) / 'run', api_factory=lambda c: next(apis))
                try:
                    yield ep
                finally:
                    if not ep.lock.closed:
                        ep.lock.close()
        return fixture()


if __name__ == '__main__':
    unittest.main()
