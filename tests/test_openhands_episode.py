"""Program fixtures, not evidence of agent naturalness or real generation."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from simulator.openhands.episode import OpenHandsEpisode


class FixtureGate:
    def __init__(self, *args):
        pass

    def review(self, *args):
        return {'allowed': True, 'reasons': []}


class FixtureAgent:
    all = []

    def __init__(self, directory, workspace, config, image, role, system, deadline, control=None, **kwargs):
        self.workspace, self.role, self.control = workspace, role, control
        self.name, self.relay, self.rows, self.calls = role, None, [], 0
        FixtureAgent.all.append(self)

    def start(self, **kwargs):
        pass

    def close(self):
        pass

    def pause(self):
        pass

    def unpause(self):
        pass

    def events(self):
        return self.rows

    def request(self, operation, **payload):
        return self.control(dict(operation=operation, payload=payload, request_id=str(uuid.uuid4())))

    def turn(self, message, **kwargs):
        self.calls += 1
        if self.role == 'code':
            file = self.workspace/'candidate/result.txt'
            if self.calls == 2:
                assert file.read_text() == 'first implementation'
            file.write_text('first implementation' if self.calls == 1 else 'first implementation plus second')
            self.rows.append(dict(kind='ActionEvent', id=str(uuid.uuid4()), tool_name='finish', action={'message': 'done'}))
            return
        view = self.request('read_state')
        task_id = view['state']['task_id']
        if self.calls > 1:
            accepted = self.request('accept', task_id=task_id, reason='I trust the report')
            if accepted.get('ended'):
                return
            task_id = accepted.get('state', {}).get('task_id', task_id)
        permit = self.request('transition', task_id=task_id, candidates=[dict(
            state='BUILD', control='CONTINUE', reason='Proceed with the released task')])
        self.request('send', task_id=task_id, permit_id=permit['permit_id'], text='request '+task_id)


class EpisodeTests(unittest.TestCase):
    def test_two_tasks_keep_candidate_and_context_without_testing(self):
        FixtureAgent.all = []
        with tempfile.TemporaryDirectory() as temporary:
            tasks = [dict(kind='issue', title='A', body='first', identifier='hidden-1', patch=''),
                     dict(kind='issue', title='B', body='second', identifier='hidden-2', patch='')]
            def fake_snapshot(repo, base, destination):
                destination.mkdir(parents=True)
                (destination/'base.txt').write_text('base')
            config = dict(repository='fixture', tasks=[{}, {}], image='fixture',
                          execution_image='sha256:' + '0' * 64,
                          user={'model':'m'}, code={'model':'m'})
            with patch('simulator.openhands.episode.prepare', return_value=(None, 'base', tasks)), \
                 patch('simulator.openhands.episode.snapshot', side_effect=fake_snapshot), \
                 patch('simulator.openhands.episode.SDKContainer', FixtureAgent), \
                 patch('simulator.openhands.episode.MessageGuard', FixtureGate), \
                 patch('simulator.openhands.sandbox.pinned_image', return_value=config['execution_image']), \
                 patch('simulator.openhands.episode.subprocess.check_output', return_value='image'), \
                 patch('simulator.openhands.episode.subprocess.run'):
                episode = OpenHandsEpisode(config, Path(temporary)/'run')
                self.assertEqual(episode.run(), 'completed')
                self.assertEqual(len(FixtureAgent.all), 2)
                self.assertEqual([a.calls for a in FixtureAgent.all], [2, 3])
                self.assertEqual(len(episode.state.data['accepted']), 2)
                self.assertTrue(all(not a['checks_passed'] for a in episode.state.data['accepted']))
                self.assertEqual((episode.root/'workspace/candidate/result.txt').read_text(), 'first implementation plus second')
                public = [json.loads(x) for x in (episode.root/'session.jsonl').read_text().splitlines()]
                self.assertEqual([p['kind'] for p in public], ['user', 'assistant', 'user', 'assistant'])
                message = episode.state.data['messages'][0]
                episode.public({'kind': 'user', 'text': message['text']}, message['id'])
                self.assertEqual(len((episode.root/'session.jsonl').read_text().splitlines()), 4)


if __name__ == '__main__':
    unittest.main()
