import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_memory_episode as memory_fixture
from simulator.openhands.memory_episode import export_episode
from simulator.openhands.retention import compact_completed_run, compact_preparation, owned_resources


class EpisodeRetentionTests(unittest.TestCase):
    def fixture(self, root):
        source, _, journal = memory_fixture.MemoryEpisodeTests().fixture(root)
        for name in ('judge-workspace/checks', 'user-workspace/candidate', 'private/judge/outbox'):
            (source / name).mkdir(parents=True)
        (source / 'judge-workspace/checks/check.py').write_text('assert False\n')
        (source / 'private/judge/outbox/events.jsonl').write_text(json.dumps(
            dict(id='evidence', kind='ObservationEvent', observation={'text': 'raw failure'})) + '\n')
        checkpoint = source / 'private/checkpoint.json'
        saved = json.loads(checkpoint.read_text())
        saved['progressive'] = dict(tasks={}, decisions={'judge1': dict(events=[{'id': 'evidence'}],
            observations=[{'id': 'evidence', 'file': '/workspace/experiments/result/receipt.xml'}],
            payload={'outcome': 'unsolved'}, review={'allowed': True})})
        checkpoint.write_text(json.dumps(saved))
        result = source / 'judge-workspace/experiments/result'
        result.mkdir(parents=True)
        (result / 'receipt.xml').write_text('<failure/>')
        (result / 'temporary-copy.py').write_text('disposable')
        with journal.open('a') as stream:
            stream.write(json.dumps(dict(kind='response', id='model1', usage={'prompt_tokens': 3},
                                         output={'error': 'retained response'})) + '\n')
        export_episode(source, root / 'package')
        return source, root / 'package'

    def test_completed_cleanup_preserves_code_dialogue_and_review_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            source, package = self.fixture(Path(directory))
            dialogue = (package / 'dialogue.jsonl').read_bytes()
            result = compact_completed_run(source, package)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual([p.name for p in source.iterdir()], ['retention.json'])
            self.assertEqual((package / 'dialogue.jsonl').read_bytes(), dialogue)
            self.assertEqual((package / 'snapshot/app.py').read_text(), 'value = 2\n')
            summary = json.loads((package / 'private/review.json').read_text())
            decision = summary['decisions']['judge1']
            self.assertEqual(decision['observations_ids'], ['evidence'])
            self.assertNotIn('observations', decision)
            with gzip.open(package / 'private/trace.jsonl.gz', 'rt') as stream:
                rows = [json.loads(line) for line in stream]
            self.assertTrue(any(r.get('value', {}).get('id') == 'evidence' for r in rows))
            self.assertTrue(any(r['kind'] == 'check_file' for r in rows))
            self.assertEqual((package / 'private/experiments/result/receipt.xml').read_text(), '<failure/>')
            self.assertFalse((package / 'private/experiments/result/temporary-copy.py').exists())
            request = next(r['value'] for r in rows if r['kind'] == 'provider' and r['value']['kind'] == 'request')
            self.assertNotIn('input', request)
            self.assertIn('request_sha256', request)

    def test_paused_pending_or_unverified_export_keeps_source(self):
        for change in ('paused', 'in_flight', 'changed_code', 'changed_dialogue', 'active_worker'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                source, package = self.fixture(Path(directory))
                path = source / 'private/checkpoint.json'
                saved = json.loads(path.read_text())
                if change == 'paused':
                    saved['state']['status'] = 'paused'
                elif change == 'in_flight':
                    saved['in_flight'] = {'role': 'code'}
                elif change == 'changed_code':
                    (package / 'snapshot/app.py').write_text('modified')
                elif change == 'changed_dialogue':
                    (package / 'dialogue.jsonl').write_text('{}\n')
                else:
                    (source / 'private/judge/outbox/active.json').write_text('{"status":"in_flight"}')
                path.write_text(json.dumps(saved))
                with self.assertRaises(ValueError):
                    compact_completed_run(source, package)
                self.assertTrue(path.exists())
                self.assertTrue((source / 'workspace/candidate/app.py').exists())

    def test_archive_or_resource_failure_prevents_host_deletion(self):
        for operation in ('write_trace', 'owned_resources'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                source, package = self.fixture(Path(directory))
                with patch('simulator.openhands.retention.' + operation, side_effect=RuntimeError('failed')):
                    with self.assertRaisesRegex(RuntimeError, 'failed'):
                        compact_completed_run(source, package)
                self.assertTrue((source / 'private/checkpoint.json').exists())

    def test_preparation_keeps_exact_requests_and_failed_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'frozen').mkdir()
            (root / 'frozen/commit-1-draft.json').write_text('{"invalid":true}')
            raw = b'{"kind":"request","input":{"messages":[]}}\n'
            (root / 'provider.jsonl').write_bytes(raw)
            compact_preparation(root)
            self.assertFalse((root / 'provider.jsonl').exists())
            with gzip.open(root / 'provider.jsonl.gz', 'rb') as stream:
                self.assertEqual(stream.read(), raw)
            self.assertTrue((root / 'frozen/commit-1-draft.json').exists())

    def test_preparation_removes_only_its_own_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clone = root / 'source'
            clone.mkdir()
            (clone / 'HEAD').write_text('generated clone')
            compact_preparation(root, cloned_source=clone)
            self.assertFalse(clone.exists())
            outside = root / 'actual-repository'
            outside.mkdir()
            with self.assertRaisesRegex(ValueError, 'preparation-owned'):
                compact_preparation(root, cloned_source=outside)
            self.assertTrue(outside.exists())

    def test_docker_active_or_foreign_container_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            config = source / 'private/code/inbox/config.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'control_container_id': 'a' * 64}))
            row = dict(Id='a' * 64, Name='/session-oh-code', State={'Running': True, 'Paused': False},
                       Mounts=[dict(Type='bind', Source=str(source / 'private/code'))])
            for active in (True, False):
                if not active:
                    row['State']['Running'] = False
                    row['Mounts'][0]['Source'] = '/unrelated'
                with patch('simulator.openhands.retention.subprocess.run') as run:
                    run.return_value.stdout = json.dumps([row])
                    with self.assertRaisesRegex(ValueError, 'active or not owned'):
                        owned_resources(source)

    def test_scenario_cli_compacts_completed_only(self):
        from simulator.__main__ import main
        for status in ('completed', 'paused'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'config.json'
                config.write_text(json.dumps(dict(runtime='openhands', progressive_issues=True,
                                                  scenario_file='scenario.json')))
                with patch('sys.argv', ['simulator', '--config', str(config), '--output', str(root / 'run')]), \
                        patch('simulator.openhands.progressive.ProgressiveEpisode') as episode, \
                        patch('simulator.openhands.memory_episode.export_episode') as export, \
                        patch('simulator.openhands.retention.compact_completed_run') as compact, \
                        patch('builtins.print'):
                    episode.return_value.run.return_value = status
                    if status == 'completed':
                        main()
                        export.assert_called_once_with(root / 'run', root / 'run-package')
                        compact.assert_called_once_with(root / 'run', root / 'run-package')
                    else:
                        with self.assertRaises(SystemExit):
                            main()
                        export.assert_not_called()
                        compact.assert_not_called()


if __name__ == '__main__':
    unittest.main()
