import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator import __main__ as simulator_main
from simulator.openhands.progressive import ProgressiveEpisode


class UnattendedRuntimeTests(unittest.TestCase):
    def test_standard_cli_selects_progressive_episode_without_review_barrier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            output = root / 'run'
            config.write_text(json.dumps({
                'runtime': 'openhands',
                'progressive_issues': True,
            }))
            argv = ['simulator', '--config', str(config), '--output', str(output)]
            with patch.object(sys, 'argv', argv), \
                    patch.object(ProgressiveEpisode, '__init__', return_value=None) as initialize, \
                    patch.object(ProgressiveEpisode, 'run', return_value='completed') as run, \
                    patch('simulator.openhands.reviewed_episode.await_review',
                          side_effect=AssertionError('standard CLI must not await assistant review')):
                simulator_main.main()
            initialize.assert_called_once_with(
                {'runtime': 'openhands', 'progressive_issues': True}, output, resume=False)
            run.assert_called_once_with()

    def test_ready_unsolved_decision_needs_no_assistant_file_gate(self):
        episode = ProgressiveEpisode.__new__(ProgressiveEpisode)
        record = {'status': 'ready', 'accepted': True,
                  'payload': {'outcome': 'unsolved'}}
        with patch('simulator.openhands.reviewed_episode.await_review',
                   side_effect=AssertionError('progressive decisions must not await assistant review')):
            self.assertIs(episode.resolve_decision(record), record)

    def test_feedback_correction_continues_automatically_and_remains_bounded(self):
        episode = ProgressiveEpisode.__new__(ProgressiveEpisode)
        record = {'status': 'feedback_pending', 'accepted': False,
                  'payload': {'outcome': 'unsolved'}}
        calls = []

        def correct(current):
            calls.append(current['status'])
            current.update(status='ready', accepted=True)
            return current

        episode.request_feedback_revision = correct
        with patch('simulator.openhands.reviewed_episode.await_review',
                   side_effect=AssertionError('automatic correction must not await assistant review')):
            self.assertIs(episode.resolve_decision(record), record)
        self.assertEqual(calls, ['feedback_pending'])
        self.assertEqual(record['status'], 'ready')
        self.assertEqual(record['payload']['outcome'], 'unsolved')


if __name__ == '__main__':
    unittest.main()
