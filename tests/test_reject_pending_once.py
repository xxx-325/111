import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).parent / "fixtures/reject_pending_once.py"
SPEC = importlib.util.spec_from_file_location("reject_pending_once", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
_reject_stale = MODULE._reject_stale
recover = MODULE.recover


class FakeStateType:
    @staticmethod
    def get_unmatched_actions(branch):
        return list(branch.pending)


class FakeConversation:
    def __init__(self, action_id="expected-action", task_id="task-3"):
        action = SimpleNamespace(task_id=task_id)
        event = SimpleNamespace(id=action_id, tool_name="accept_task", action=action)
        self.id = "expected-conversation"
        self.state = SimpleNamespace(
            execution_status="WAITING_FOR_CONFIRMATION",
            active_branch=lambda: SimpleNamespace(pending=[event]),
        )
        self.rejections = []
        self.closed = False

    def reject_pending_actions(self, reason):
        self.rejections.append(reason)
        self.state.active_branch = lambda: SimpleNamespace(pending=[])

    def close(self):
        self.closed = True


class RejectPendingOnceTests(unittest.TestCase):
    expected = {
        "conversation_id": "expected-conversation",
        "action_id": "expected-action",
        "reason": "stale action already committed",
    }

    def test_exact_stale_action_is_rejected_without_model_run(self):
        conversation = FakeConversation()
        result = _reject_stale(conversation, FakeStateType, self.expected)
        self.assertEqual(conversation.rejections, [self.expected["reason"]])
        self.assertEqual(result["pending_after"], 0)
        self.assertFalse(result["model_run"])

    def test_identity_mismatch_fails_without_rejection(self):
        conversation = FakeConversation(action_id="different-action")
        with self.assertRaisesRegex(RuntimeError, "identity differs"):
            _reject_stale(conversation, FakeStateType, self.expected)
        self.assertEqual(conversation.rejections, [])

    def test_worker_is_intercepted_before_run_or_message(self):
        from simulator.openhands import worker

        calls = []
        conversation = FakeConversation()

        def factory(*args, **kwargs):
            calls.append("construct")
            return conversation

        def worker_main():
            worker.Conversation()
            calls.append("continued")

        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            with patch.object(worker, "Conversation", factory), patch.object(
                worker, "main", worker_main
            ), patch.object(worker, "ConversationState", FakeStateType):
                recover(self.expected, receipt)
            self.assertEqual(calls, ["construct"])
            self.assertTrue(conversation.closed)
            self.assertTrue(receipt.exists())


if __name__ == "__main__":
    unittest.main()
