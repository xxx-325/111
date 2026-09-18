"""No-provider regression tests for safe SDK control-action continuation."""

import unittest
from types import SimpleNamespace

from simulator.openhands.episode import cached_pending_transition
from simulator.openhands.transition_selection import (
    TURN_CACHE_VERSION,
    candidates_sha256,
    transition_turn,
)
from simulator.openhands.worker import prepare_sdk_turn


def fixture_state():
    return {
        "task_id": "task-5",
        "state": "EVALUATE",
        "code_reply": {"id": "reply-5", "text": "fixture"},
        "messages": [],
        "checks": [],
        "published_state_counts": {},
    }


def fixture_action(tool_name="request_transition"):
    candidates = [
        {
            "state": "OPERATE",
            "control": "CONTINUE",
            "reason": "Run the current verification again",
            "evidence_ids": [],
        }
    ]
    return {
        "kind": "ActionEvent",
        "id": "action-1",
        "tool_name": tool_name,
        "tool_call_id": "call-1",
        "action": {"task_id": "task-5", "candidates": candidates},
    }


def fixture_saved(state, action):
    turn_key, turn = transition_turn(state)
    return {
        "transition_selections": {
            turn_key: {
                "version": TURN_CACHE_VERSION,
                "turn": turn,
                "attempts": [
                    {
                        "candidate_sha256": candidates_sha256(
                            action["action"]["candidates"]
                        ),
                        "result": {"accepted": True},
                    }
                ],
            }
        }
    }


class FakeStateType:
    @staticmethod
    def get_unmatched_actions(branch):
        return list(branch)


class FakeConversation:
    def __init__(self, pending):
        self.pending = pending
        self.state = SimpleNamespace(active_branch=lambda: self.pending)
        self.order = []

    def run(self):
        self.order.append("tool_result")
        self.pending.clear()

    def send_message(self, message):
        self.order.append(("user", message))

    def condense(self):
        self.order.append("condense")


class PendingControlResumeTests(unittest.TestCase):
    def test_cached_action_is_closed_before_a_new_user_message(self):
        state = fixture_state()
        action = fixture_action()
        expected = cached_pending_transition(
            [action], fixture_saved(state, action), state
        )
        pending = [
            SimpleNamespace(
                id="action-1",
                tool_name="request_transition",
                tool_call_id="call-1",
            )
        ]
        conversation = FakeConversation(pending)
        from simulator.openhands import worker

        original = worker.ConversationState
        worker.ConversationState = FakeStateType
        try:
            prepare_sdk_turn(
                conversation, {"resume_pending_control": expected}
            )
            conversation.run()
            prepare_sdk_turn(conversation, {"message": "next input"})
        finally:
            worker.ConversationState = original
        self.assertEqual(
            conversation.order, ["tool_result", ("user", "next input")]
        )

    def test_completed_action_is_not_resumed(self):
        state = fixture_state()
        action = fixture_action()
        observation = {
            "kind": "ObservationEvent",
            "action_id": "action-1",
            "tool_call_id": "call-1",
        }
        self.assertIsNone(
            cached_pending_transition(
                [action, observation], fixture_saved(state, action), state
            )
        )

    def test_unknown_action_is_not_executed(self):
        state = fixture_state()
        action = fixture_action("terminal")
        with self.assertRaisesRegex(RuntimeError, "side-effecting"):
            cached_pending_transition([action], {}, state)

    def test_uncached_transition_is_not_executed(self):
        state = fixture_state()
        action = fixture_action()
        with self.assertRaisesRegex(RuntimeError, "no current-turn cache"):
            cached_pending_transition([action], {}, state)

    def test_deterministic_host_evidence_binding_matches_cached_review(self):
        state = fixture_state()
        action = fixture_action()
        prepared = fixture_action()["action"]
        prepared["candidates"][0]["evidence_ids"] = ["judge-current"]
        saved = fixture_saved(state, {"action": prepared})

        def bind(payload):
            payload = dict(payload)
            payload["candidates"] = [dict(payload["candidates"][0])]
            payload["candidates"][0]["evidence_ids"] = ["judge-current"]
            return payload

        self.assertEqual(
            cached_pending_transition([action], saved, state, bind)["action_id"],
            "action-1",
        )


if __name__ == "__main__":
    unittest.main()
