import json
import tempfile
import unittest
from pathlib import Path

from simulator.openhands.memory_episode import export_episode, visible_events, snapshot_hash, scenario_navigation


class MemoryEpisodeTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'run'
        (source / 'private/code').mkdir(parents=True)
        (source / 'workspace/candidate').mkdir(parents=True)
        (source / 'workspace/candidate/app.py').write_text('value = 2\n')
        events = [dict(id='u', kind='user', timestamp=1, text='Change it.'),
                  dict(id='c', kind='tool_call', timestamp=2, call_id='call',
                       tool_name='file_editor', action={'PRIVATE': 'metadata'}),
                  dict(id='r', kind='tool_result', timestamp=3, call_id='call',
                       tool_name='file_editor', observation={
                           'old_content': 'PRIVATE old', 'new_content': 'PRIVATE new',
                           'content': [{'type': 'text', 'text': 'Unsent unabridged output'}]}),
                  dict(id='a', kind='assistant', timestamp=4, phase='final', text='Done <script>.')]
        (source / 'session.jsonl').write_text('\n'.join(json.dumps(row) for row in events))
        request = dict(kind='request', input={'messages': [
            dict(role='assistant', tool_calls=[dict(id='call', function=dict(
                name='file_editor', arguments=json.dumps(dict(command='str_replace', path='app.py',
                                                             old_str='1', new_str='2'))))]),
            dict(role='tool', tool_call_id='call', content='File updated successfully'),
        ]})
        journal = source / 'private/code/provider.jsonl'
        journal.write_text(json.dumps(request) + '\n')
        config = dict(image='sdk', execution_image='sandbox', execution_backend='ssh_sandbox',
                      code=dict(model='m', key_env='KEY', api_key='PRIVATE'),
                      judge=dict(model='m', key_env='KEY'), tasks=['PRIVATE'])
        (source / 'private/checkpoint.json').write_text(json.dumps(dict(
            state={'status': 'completed'}, config=config, in_flight=None)))
        return source, events, journal

    def test_export_uses_sent_text_not_editor_metadata_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, _ = self.fixture(root)
            before = (source / 'session.jsonl').read_bytes()
            output = root / 'export'
            manifest = export_episode(source, output)
            rows = [json.loads(x) for x in (output / 'dialogue.jsonl').read_text().splitlines()]
            self.assertEqual(rows[2]['text'], 'File updated successfully')
            self.assertEqual(rows[1]['action']['old_str'], '1')
            self.assertNotIn('PRIVATE', (output / 'dialogue.jsonl').read_text())
            self.assertNotIn('PRIVATE', (output / 'control-config.json').read_text())
            self.assertEqual([r['sequence'] for r in rows], [1, 2, 3, 4])
            self.assertEqual(manifest['dialogue']['cutoff_event_id'], 'a')
            self.assertEqual(manifest['snapshot']['sha256'], snapshot_hash(output / 'snapshot'))
            self.assertIn('&lt;script&gt;', (output / 'dialogue.html').read_text())
            self.assertEqual((source / 'session.jsonl').read_bytes(), before)

    def test_missing_delivery_or_unpaired_tool_cannot_export(self):
        with tempfile.TemporaryDirectory() as directory:
            _, events, journal = self.fixture(Path(directory))
            with self.assertRaisesRegex(ValueError, 'unfinished'):
                visible_events(events[:2], journal)
            journal.write_text(json.dumps(dict(kind='request', input={'messages': []})))
            with self.assertRaisesRegex(ValueError, 'provider-bound'):
                visible_events(events, journal)

    def test_first_delivered_clipped_result_stays_clipped(self):
        with tempfile.TemporaryDirectory() as directory:
            _, events, journal = self.fixture(Path(directory))
            first = json.loads(journal.read_text())
            first['input']['messages'][-1]['content'] = 'first\n[output clipped]'
            later = dict(kind='request', input={'messages': [dict(
                role='tool', tool_call_id='call', content='later summary')]})
            journal.write_text(json.dumps(first) + '\n' + json.dumps(later))
            self.assertEqual(visible_events(events, journal)[2]['text'], 'first\n[output clipped]')

    def test_running_or_pending_run_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, _ = self.fixture(root)
            path = source / 'private/checkpoint.json'
            value = json.loads(path.read_text())
            value['in_flight'] = {'id': 'pending'}
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'stopped'):
                export_episode(source, root / 'export')
            self.assertFalse((root / 'export').exists())

    def test_export_excludes_git_and_environment_but_binds_the_copied_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, _ = self.fixture(root)
            candidate = source / 'workspace/candidate'
            (candidate / '.git').mkdir()
            (candidate / '.git/config').write_text('private source')
            (candidate / '.venv').mkdir()
            (candidate / '.venv/tool').symlink_to('/outside')
            (candidate / 'app.py').chmod(0o755)
            export_episode(source, root / 'export')
            copied = root / 'export/snapshot'
            self.assertFalse((copied / '.git').exists())
            self.assertFalse((copied / '.venv').exists())
            self.assertEqual(snapshot_hash(candidate, export_only=True), snapshot_hash(copied))
            self.assertTrue((copied / 'app.py').stat().st_mode & 0o111)

    def test_missing_snapshot_is_not_an_empty_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'existing directory'):
                snapshot_hash(Path(directory) / 'missing')

    def test_navigation_maps_public_events_without_exporting_hidden_facts(self):
        checkpoint = dict(tasks=[dict(scenario=dict(source_commit='source', original=True,
            repository_edits=[dict(path='app.py', before='hidden', after='secret')],
            facts=[dict(text='unreleased')]))], state={'messages': [dict(id='u', task_id='task-1')]})
        events = [dict(id='u', kind='user'), dict(id='c', kind='tool_call'), dict(id='r', kind='tool_result')]
        result = scenario_navigation(checkpoint, events)
        self.assertEqual(result['tasks'][0]['public_event_ids'], ['u', 'c', 'r'])
        self.assertEqual(result['tasks'][0]['changed_paths'], ['app.py'])
        for private in ('hidden', 'secret', 'unreleased'):
            self.assertNotIn(private, json.dumps(result))
