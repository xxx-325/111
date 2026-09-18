import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from simulator.openhands.episode import HostPauseRequested, OpenHandsEpisode
from simulator.openhands.pause_run import submit, wait_for_ack
from simulator.openhands.state import TaskState


class PauseAgent:
    def __init__(self, error=None, close_error=None):
        self.error = error
        self.close_error = close_error
        self.calls = 0

    def pause(self):
        self.calls += 1
        if self.error:
            raise self.error

    def close(self):
        if self.close_error:
            raise self.close_error


class HostPauseTests(unittest.TestCase):
    def episode(self, root):
        episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.root = root
        episode.private = root / "private"
        episode.private.mkdir()
        episode.checkpoint = episode.private / "checkpoint.json"
        episode.pause_request = episode.private / "pause-request.json"
        episode.lock = threading.RLock()
        episode.state = TaskState()
        episode.saved = {
            "state": episode.state.data,
            "in_flight": {"role": "code", "id": "turn-1", "public_start": 3},
        }
        episode.budget = SimpleNamespace(snapshot=lambda: {})
        episode.elapsed_before = 0
        episode.started = time.monotonic()
        return episode

    def test_request_file_is_consumed_by_host_and_in_flight_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            episode = self.episode(root)
            episode.persist()
            checkpoint, request = submit(root, "operator review")
            with self.assertRaises(HostPauseRequested) as raised:
                episode.check_pause_request()
            episode.agents = {"code": PauseAgent(), "user": PauseAgent()}
            episode.record_host_pause(raised.exception)

            saved = json.loads(checkpoint.read_text())
            self.assertEqual(saved["state"]["status"], "paused")
            self.assertEqual(saved["state"]["pause_reason"], "Host pause requested: operator review")
            self.assertEqual(saved["host_pause"]["request_id"], request["id"])
            self.assertEqual(saved["host_pause"]["in_flight"]["id"], "turn-1")
            self.assertEqual(saved["host_pause"]["freeze"]["status"], "paused")
            self.assertFalse(episode.pause_request.exists())
            self.assertEqual(wait_for_ack(checkpoint, request["id"], 0), saved["host_pause"])

    def test_freeze_failure_is_recorded_without_reopening_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            episode = self.episode(root)
            episode.agents = {
                "code": PauseAgent(RuntimeError("freeze failed")),
                "user": PauseAgent(),
            }
            request = HostPauseRequested("pause-1", "inspect state", time.time())
            episode.record_host_pause(request)
            saved = json.loads(episode.checkpoint.read_text())
            self.assertEqual(saved["state"]["status"], "paused")
            self.assertEqual(saved["host_pause"]["freeze"]["status"], "failed")
            self.assertIn("freeze failed", saved["host_pause"]["freeze"]["roles"]["code"]["error"])
            self.assertEqual(saved["in_flight"]["id"], "turn-1")
            with self.assertRaisesRegex(RuntimeError, "role freezes failed"):
                wait_for_ack(episode.checkpoint, "pause-1", 0)

    def test_invalid_request_fails_closed_as_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            episode = self.episode(root)
            episode.pause_request.write_text('{"reason":"missing identity"}')
            with self.assertRaisesRegex(HostPauseRequested, "Invalid host pause request"):
                episode.check_pause_request()

    def test_submit_does_not_edit_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            checkpoint = private / "checkpoint.json"
            original = json.dumps({"state": {"status": "running"}})
            checkpoint.write_text(original)
            _, first = submit(root, "pause safely")
            self.assertEqual(checkpoint.read_text(), original)
            request_path = private / "pause-request.json"
            self.assertEqual(json.loads(request_path.read_text()), first)
            self.assertEqual(list(private.glob(".pause-request.json.*.tmp")), [])
            with self.assertRaises(FileExistsError):
                submit(root, "second request")
            self.assertEqual(json.loads(request_path.read_text()), first)

    def test_pending_freeze_is_not_reported_as_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint.write_text(json.dumps({
                "host_pause": {
                    "request_id": "pause-1",
                    "freeze": {"status": "pending", "roles": {}},
                }
            }))
            with self.assertRaisesRegex(TimeoutError, "did not acknowledge"):
                wait_for_ack(checkpoint, "pause-1", 0)


if __name__ == "__main__":
    unittest.main()
