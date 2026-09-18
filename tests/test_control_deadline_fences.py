"""Local fixtures for late control outcomes; no provider or agent calls."""
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.relay import RequestContext
from simulator.openhands.state import TaskState


class ControlDeadlineTests(unittest.TestCase):
    def test_cancelled_audit_cannot_transition_send_accept_or_pause(self):
        for operation in ('transition', 'send', 'accept', 'pause'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
                episode.private = Path(directory)
                episode.lock = threading.RLock()
                episode.state = TaskState()
                episode.saved = dict(public=[], code_sources=[], control_results={}, last_code_reply='done')
                episode.collect_user_sources = Mock()
                episode.acceptance_gate = Mock()
                episode.requirement = Mock(return_value={'body': 'visible requirement'})
                episode.persist = Mock()
                episode.public = Mock()
                cancel = Path(directory)/'request.cancel'
                def late_review(*args):
                    cancel.touch()
                    return {'allowed': True}
                episode.guard = Mock()
                episode.guard.review.side_effect = late_review
                before = copy.deepcopy(episode.state.data)
                packet = dict(request_id='late', operation=operation,
                    payload={'task_id': 'task-1'},
                    _request_context=RequestContext(time.time()+30, cancel))
                if operation == 'transition':
                    packet['payload']['candidates'] = [dict(
                        state='BUILD', control='CONTINUE', reason='Delegate current task')]
                with self.assertRaises(TimeoutError):
                    episode.control(packet)
                self.assertEqual(episode.state.data, before)
                self.assertEqual(episode.saved['control_results'], {})
                episode.public.assert_not_called()
                if operation == 'transition':
                    episode.persist.assert_called_once_with()
                    self.assertEqual(len(episode.saved['transition_selections']), 1)
                else:
                    episode.persist.assert_not_called()

    def test_expired_entry_does_not_collect_or_review(self):
        episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.lock = threading.RLock()
        episode._control = Mock()
        with self.assertRaises(TimeoutError):
            episode.control(dict(_request_context=RequestContext(time.time()-1)))
        episode._control.assert_not_called()
