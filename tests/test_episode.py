import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.episode import Episode, clone_candidate, obvious_leak
from simulator.tasks import github_name


def response(text):
    return {'role': 'assistant', 'content': text}


def call(command, identifier='call1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identifier, 'type': 'function', 'function': {'name': 'shell', 'arguments': json.dumps({'command': command})}}]}


class QueueAPI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools=False):
        self.requests.append(json.loads(json.dumps(messages)))
        return next(self.responses)


class EpisodeTests(unittest.TestCase):
    def test_real_copy_rejects_link_to_hidden_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'leak').symlink_to(root / 'secret')
            with self.assertRaises(ValueError):
                clone_candidate(source, root / 'copy')

    def test_github_and_reference_guards(self):
        self.assertEqual(github_name('https://github.com/mahmoud/boltons.git'), 'mahmoud/boltons')
        with self.assertRaises(ValueError):
            github_name('https://example.com/owner/repo')
        task = {'identifier': 'https://github.com/a/b/issues/1', 'reference': 'a' * 40, 'patch': '+' + 'reference implementation ' * 5}
        self.assertIsNotNone(obvious_leak('look at ' + 'a' * 12, [task]))
        self.assertIsNotNone(obvious_leak('state DEBUG', [task]))
        self.assertIsNone(obvious_leak('请修复空列表输入。', [task]))

    def test_only_bound_test_patch_lines_can_defer_to_semantic_review(self):
        test_line = 'assert runner.invoke(command).exception is None and result.exit_code == 0'
        implementation_line = 'return wrap_stream(stream, errors="replace", preserve_fileno=True)'
        patch_text = ('diff --git a/tests/test_cli.py b/tests/test_cli.py\n'
                      '--- a/tests/test_cli.py\n+++ b/tests/test_cli.py\n+'
                      + test_line + '\n'
                      'diff --git a/src/cli.py b/src/cli.py\n'
                      '--- a/src/cli.py\n+++ b/src/cli.py\n+'
                      + implementation_line + '\n')
        task = {'identifier':'private-task','reference':'b' * 40,
                'title':'problem','body':'symptom','patch':patch_text}
        self.assertIsNotNone(obvious_leak(test_line,[task]))
        self.assertIsNone(obvious_leak(test_line,[task],
            deferred_reference_test_values=[test_line]))
        self.assertIsNotNone(obvious_leak(implementation_line,[task],
            deferred_reference_test_values=[implementation_line]))
        self.assertIsNotNone(obvious_leak(patch_text,[task],
            deferred_reference_test_values=[test_line]))

    def test_two_tasks_keep_code_and_hide_private_tests_and_resume_without_replay(self):
        verdict = response(json.dumps(dict(status='completed', reason='candidate inspected', facts=['检查通过'], interaction='check', focus='')))
        safe = response('{"safe":true,"reason":"no leak"}')
        user = QueueAPI([response('{"requirement":"增加第一个功能。"}'), safe, response('增加第一个功能。'), safe,
                         call('private-test'), verdict, safe,
                         response('{"requirement":"增加第二个功能。"}'), safe, response('好了，再增加第二个功能。'), safe,
                         call('private-test'), verdict, safe, response('好了。'), safe])
        code = QueueAPI([call('first'), response('已完成。'), call('second'), response('已完成第二个。')])
        tasks = [dict(title=str(i), body='requirement', identifier='private-id-' + str(i), reference=None, patch='', base='base') for i in range(2)]
        config = dict(repository='unused', tasks=[], image='test', user={'role':'user'}, code={'role':'code'})

        def initial(repo, base, destination):
            destination.mkdir()
            (destination / 'base').write_text('base')

        def execute(sandbox, command):
            if command == 'second':
                self.assertTrue((sandbox.workspace / 'first').exists())
                self.assertFalse((sandbox.workspace / 'private-test').exists())
            (sandbox.workspace / command).write_text(command)
            return dict(exit_code=0, stdout='actual command result', stderr='')

        with tempfile.TemporaryDirectory() as directory, patch('simulator.episode.prepare', return_value=(Path(directory), 'base', tasks)), patch('simulator.episode.snapshot', side_effect=initial), patch('simulator.sandbox.Sandbox.execute', execute):
            root = Path(directory) / 'run'
            factory = lambda c: user if c['role'] == 'user' else code
            episode = Episode(config, root, api_factory=factory)
            self.assertEqual(episode.run(), 'completed')
            self.assertFalse((root / 'workspace/private-test').exists())
            public = (root / 'session.jsonl').read_text()
            self.assertNotIn('private-test', public)
            self.assertNotIn('private-id', public)
            self.assertIn('tool_result', public)
            self.assertIn('增加第一个功能', json.dumps(code.requests[-1], ensure_ascii=False))
            public_users = [e['text'] for e in episode.state['public'] if e['kind'] == 'user']
            self.assertEqual(len(public_users), 3)
            drafts = [r for r in user.requests if r[0].get('content', '').startswith('You are the USER')]
            self.assertNotIn('第二个功能', json.dumps(drafts[0], ensure_ascii=False))
            before = public
            resumed = Episode(config, root, resume=True, api_factory=factory)
            self.assertEqual(resumed.run(), 'completed')
            self.assertEqual((root / 'session.jsonl').read_text(), before)

    def test_completion_without_inspection_fails_closed(self):
        user = QueueAPI([response('{"requirement":"修改。"}'), response('{"safe":true}'), response('修改。'), response('{"safe":true}'), response('{"status":"completed","reason":"looks done","facts":[],"interaction":"check","focus":""}')])
        code = QueueAPI([response('done')])
        config = dict(repository='unused', tasks=[], image='test', user={'role':'user'}, code={'role':'code'})
        task = dict(title='task', body='requirement', identifier='private-id', reference=None, patch='', base='base')
        with tempfile.TemporaryDirectory() as directory, patch('simulator.episode.prepare', return_value=(None, 'base', [task])), patch('simulator.episode.snapshot', side_effect=lambda r,b,p:p.mkdir()):
            factory = lambda c: user if c['role']=='user' else code
            root = Path(directory) / 'run'
            episode = Episode(config, root, api_factory=factory)
            with self.assertRaisesRegex(ValueError, 'without candidate inspection'):
                episode.run()
            self.assertTrue((root / 'workspace').exists())
            self.assertEqual(json.loads((root / 'private/checkpoint.json').read_text())['status'], 'failed')
            with self.assertRaisesRegex(RuntimeError, 'uncertain side effects'):
                Episode(config, root, resume=True, api_factory=factory)

    def test_interruption_at_phase_boundary_resumes_without_duplicate_user_message(self):
        safe=response('{"safe":true}')
        verdict=response('{"status":"completed","reason":"inspected","facts":["已检查"],"interaction":"check","focus":""}')
        user=QueueAPI([response('{"requirement":"实现功能。"}'),safe,response('实现功能。'),safe,call('inspect'),verdict,safe,response('好。'),safe])
        code=QueueAPI([response('已完成。')])
        task=dict(title='t',body='r',identifier='private',reference=None,patch='',base='b')
        config=dict(repository='unused',tasks=[],image='test',user={'role':'user'},code={'role':'code'})
        with tempfile.TemporaryDirectory() as directory, patch('simulator.episode.prepare',return_value=(None,'b',[task])), patch('simulator.episode.snapshot',side_effect=lambda r,b,p:p.mkdir()), patch('simulator.sandbox.Sandbox.execute',return_value={'exit_code':0,'stdout':'inspected','stderr':''}):
            factory=lambda c:user if c['role']=='user' else code
            root=Path(directory)/'run'
            episode=Episode(config,root,api_factory=factory)
            original=episode.checkpoint
            triggered=[]
            def interrupt():
                original()
                if episode.state['phase']=='code' and not episode.state['inflight'] and not triggered:
                    triggered.append(True)
                    raise KeyboardInterrupt()
            with patch.object(episode,'checkpoint',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):
                episode.run()
            resumed=Episode(config,root,resume=True,api_factory=factory)
            self.assertEqual(resumed.run(),'completed')
            events=[json.loads(line) for line in (root/'session.jsonl').read_text().splitlines()]
            self.assertEqual(sum(e.get('text')=='实现功能。' for e in events),1)
            self.assertEqual(len(code.requests),1)

    def test_failed_check_produces_feedback_and_second_code_turn(self):
        safe=response('{"safe":true}')
        bad=response('{"status":"needs_changes","reason":"test failed","facts":["空输入还会报错"],"interaction":"check","focus":""}')
        good=response('{"status":"completed","reason":"test passed","facts":["检查通过"],"interaction":"check","focus":""}')
        user=QueueAPI([response('{"requirement":"支持空输入。"}'),safe,response('支持空输入。'),safe,
                       call('failing-test'),bad,safe,response('空输入还会报错，请修复。'),safe,
                       call('passing-test'),good,safe,response('好了'),safe])
        code=QueueAPI([response('第一版。'),response('已修复。')])
        task=dict(title='t',body='r',identifier='private',reference=None,patch='',base='b')
        config=dict(repository='unused',tasks=[],image='test',user={'role':'user'},code={'role':'code'})
        def result(sandbox,command):
            return {'exit_code':1 if command=='failing-test' else 0,'stdout':command,'stderr':''}
        with tempfile.TemporaryDirectory() as directory,patch('simulator.episode.prepare',return_value=(None,'b',[task])),patch('simulator.episode.snapshot',side_effect=lambda r,b,p:p.mkdir()),patch('simulator.sandbox.Sandbox.execute',result):
            episode=Episode(config,Path(directory)/'run',api_factory=lambda c:user if c['role']=='user' else code)
            self.assertEqual(episode.run(),'completed')
            self.assertEqual([v['status'] for v in episode.state['decisions']],['needs_changes','completed'])
            self.assertIn('空输入还会报错',json.dumps(code.requests[1],ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
