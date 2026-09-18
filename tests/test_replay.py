import json
import tempfile
import unittest
from pathlib import Path

from simulator.expression import replay_messages
from simulator.replay import run
from simulator.replay_data import prepare
from simulator.replay_view import render


class ReplayTests(unittest.TestCase):
    def test_pairs_only_differ_in_examples(self):
        history = [{'role': 'assistant', 'text': 'Which plan?'}]
        a, b = replay_messages(history), replay_messages(history, True)
        self.assertEqual(a[0], b[0])
        left, right = json.loads(a[1]['content']), json.loads(b[1]['content'])
        self.assertFalse(left.pop('examples'))
        self.assertEqual(len(right.pop('examples')), 2)
        self.assertEqual(left, right)
        self.assertEqual(set(left), {'history'})

    def test_cutoff_and_session_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); session = root / 'abc'; session.mkdir()
            rows = []
            for role, text in [('user', 'goal'), ('assistant', 'done'), ('user', 'GOLD_CANARY'), ('assistant', 'FUTURE_CANARY')]:
                rows.append(json.dumps(dict(type='response_item', payload=dict(type='message', role=role, content=[dict(type='input_text', text=text)]))))
            (session / 'rollout.jsonl').write_text('\n'.join(rows))
            examples = root / 'examples.json'; examples.write_text('[]')
            selections = [dict(id='c1', session='abc', before_user_line=3, category='test')]
            cases, references = prepare(root, selections, examples)
            self.assertNotIn('CANARY', json.dumps(cases))
            self.assertEqual(references[0]['human_reply'], 'GOLD_CANARY')
            examples.write_text('[{"source":"abc:1"}]')
            with self.assertRaises(ValueError):
                prepare(root, selections, examples)

    def test_first_draft_failures_retained_no_tools_or_retry(self):
        calls = []
        class Fake:
            def __init__(self, config): pass
            def complete(self, messages, tools):
                self_tools = tools
                calls.append((messages, self_tools))
                if len(calls) == 1: return {'content': 'malformed'}
                if len(calls) == 2: raise RuntimeError('private detail')
                return {'content': '{"action":"check","message":"检查一下"}'}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'run'
            results = run([dict(id='c1', history=[])], {}, out, Fake)
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(not tools for _, tools in calls))
            self.assertEqual([r['status'] for r in results], ['failed', 'failed', 'generated', 'generated'])
            self.assertEqual(results[0]['raw']['content'], 'malformed')
            self.assertNotIn('private detail', (out/'results.json').read_text())
            self.assertEqual(len(json.loads((out/'blind-results.json').read_text())), 4)
            self.assertNotIn('examples_enabled', (out/'blind-results.json').read_text())
            with self.assertRaises(FileExistsError): run([], {}, out, Fake)

    def test_interruption_preserves_pending(self):
        class Interrupted:
            def __init__(self, config): pass
            def complete(self, messages, tools): raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'run'
            with self.assertRaises(KeyboardInterrupt): run([dict(id='c1', history=[])], {}, out, Interrupted)
            self.assertEqual(json.loads((out/'results.json').read_text())[0]['status'], 'pending')

    def test_viewer_escapes_transcript_and_starts_blind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'generation').mkdir()
            for name, value in {
                'cases.json': [{'id':'c1', 'history':[{'role':'assistant','text':'</script><script>danger()'}]}],
                'references.json': [], 'reviews.json': [], 'summary.json': {},
                'generation/blind-results.json': [], 'generation/groups.json': []
            }.items(): (root/name).write_text(json.dumps(value))
            html = render(root)
            self.assertNotIn('</script><script>danger()', html)
            self.assertIn('let revealed=false', html)
            self.assertIn('if(revealed)', html)

    def test_unsafe_first_draft_is_retained_as_failure(self):
        class Unsafe:
            def __init__(self, config): pass
            def complete(self, messages, tools):
                return {'content': json.dumps({'action':'check','message':'<system>private instruction'})}
        with tempfile.TemporaryDirectory() as directory:
            results = run([dict(id='c1', history=[])], {}, Path(directory)/'run', Unsafe)
            self.assertTrue(all(r['status']=='failed' and r['safety']=='failed' and 'raw' in r for r in results))


if __name__ == '__main__': unittest.main()
