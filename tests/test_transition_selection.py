import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.state import TaskState
from simulator.state_machine import CORE_STATES, STATE_GUIDANCE
from simulator.openhands.transition_selection import (
    candidates_sha256,
    enumerate_transition_candidates,
    select_transition,
    transition_turn,
)


class ChoiceFixture:
    def __init__(self, index=0):
        self.index = index
        self.calls = []

    def choices(self, population, *, weights, k):
        self.calls.append(list(weights))
        return [population[self.index]]


class TransitionSelectionTests(unittest.TestCase):
    def candidate(self, state, control="CONTINUE", reason=None, **extra):
        return dict(
            state=state,
            control=control,
            reason=reason or f"Use {state} for this turn",
            **extra,
        )

    def test_context_filters_only_the_affected_operation(self):
        state = TaskState().data
        state["blockers"] = [
            dict(id="b1", affected_operation="deploy", reason="offline")
        ]
        rng = ChoiceFixture()
        result = select_transition(
            [
                self.candidate("OPERATE", affected_operation="deploy"),
                self.candidate("UNDERSTAND"),
            ],
            state,
            {"body": "Please diagnose and deploy when possible."},
            rng,
        )
        self.assertEqual(result["selected"]["state"], "UNDERSTAND")
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(rng.calls, [])  # One feasible option is not padded or redrawn.

    def test_candidate_hash_ignores_reordering_not_content(self):
        first = [self.candidate("BUILD"), self.candidate("PLAN")]
        self.assertEqual(candidates_sha256(first), candidates_sha256(first[::-1]))
        changed = copy.deepcopy(first)
        changed[0]["reason"] = "A genuinely corrected intent"
        self.assertNotEqual(candidates_sha256(first), candidates_sha256(changed))

    def test_host_enumeration_expands_a_single_user_annotation(self):
        state = TaskState().data
        candidates = enumerate_transition_candidates(
            [self.candidate(
                "BUILD", control="CORRECT", reason="Implement the requested change"
            )],
            state,
            {"body": "Implement the requested change."},
        )
        self.assertEqual(
            [candidate["state"] for candidate in candidates],
            ["RETRIEVE", "UNDERSTAND", "PLAN", "BUILD", "OPERATE"],
        )
        self.assertTrue(all(candidate["control"] == "CONTINUE" for candidate in candidates))
        build = next(candidate for candidate in candidates if candidate["state"] == "BUILD")
        self.assertEqual(build["reason"], "Implement the requested change")

    def test_judge_outcome_drives_host_states_controls_and_evidence(self):
        state = TaskState().data
        state["state"] = "BUILD"
        state["code_reply"] = {"id": "code-1", "text": "Implemented it"}
        state["checks"] = [{
            "id": "judge-1",
            "tool": "judge_summary",
            "result": "observed",
            "summary": {"outcome": "unsolved"},
        }]
        candidates = enumerate_transition_candidates(
            [self.candidate("DEBUG")], state, {"body": "Fix it."}
        )
        self.assertEqual([candidate["state"] for candidate in candidates], list(CORE_STATES))
        self.assertTrue(all(candidate["control"] == "CORRECT" for candidate in candidates))
        self.assertTrue(all(candidate["evidence_ids"] == ["judge-1"] for candidate in candidates))

        state["checks"][0].update(result="passed", summary={"outcome": "solved"})
        candidates = enumerate_transition_candidates(
            [self.candidate("BUILD")], state, {"body": "Fix it."}
        )
        self.assertEqual(
            [candidate["state"] for candidate in candidates],
            ["RETRIEVE", "UNDERSTAND", "PLAN", "OPERATE", "EVALUATE"],
        )
        self.assertNotIn("BUILD", [candidate["state"] for candidate in candidates])
        self.assertNotIn("DEBUG", [candidate["state"] for candidate in candidates])
        self.assertTrue(all(candidate["evidence_ids"] == ["judge-1"] for candidate in candidates))
        self.assertEqual(
            next(candidate for candidate in candidates if candidate["state"] == "PLAN")["control"],
            "REFINE",
        )

    def test_solved_plan_can_refine_or_correct_strategy_without_becoming_build(self):
        state = TaskState().data
        state.update(state="BUILD", code_reply={"id": "code-1", "text": "I used a cache."})
        state["checks"] = [{
            "id": "judge-1", "tool": "judge_summary", "result": "passed",
            "summary": {"outcome": "solved"},
        }]
        for proposed, expected in (("REFINE", "REFINE"), ("CORRECT", "CORRECT")):
            with self.subTest(control=proposed):
                candidates = enumerate_transition_candidates(
                    [self.candidate(
                        "PLAN", control=proposed,
                        reason="Keep future edits task-scoped and avoid this cache strategy",
                    )],
                    state,
                    {"body": "Fix the current cache behavior."},
                )
                plan = next(item for item in candidates if item["state"] == "PLAN")
                self.assertEqual(plan["control"], expected)
                self.assertNotIn("BUILD", [item["state"] for item in candidates])
                self.assertEqual(plan["evidence_ids"], ["judge-1"])

    def test_solved_debug_requires_new_failure_evidence(self):
        state = TaskState().data
        state.update(state="BUILD", code_reply={"id": "code-1", "text": "Implemented it"})
        state["checks"] = [{
            "id": "judge-1", "tool": "judge_summary", "result": "passed",
            "summary": {"outcome": "solved"},
        }]
        without_failure = enumerate_transition_candidates(
            [self.candidate("DEBUG")], state, {"body": "Fix it."}
        )
        self.assertNotIn("DEBUG", [item["state"] for item in without_failure])

        state["checks"].append({
            "id": "failure-2", "tool": "terminal", "result": "failed", "exit_code": 1,
        })
        with_failure = enumerate_transition_candidates(
            [self.candidate("DEBUG")], state, {"body": "Fix it."}
        )
        debug = next(item for item in with_failure if item["state"] == "DEBUG")
        self.assertEqual(debug["control"], "CORRECT")
        self.assertEqual(debug["evidence_ids"], ["judge-1", "failure-2"])
        self.assertNotIn("BUILD", [item["state"] for item in with_failure])

    def test_build_can_return_to_understand_and_initial_debug_needs_basis(self):
        state = TaskState().data
        state["state"] = "BUILD"
        result = select_transition(
            [
                self.candidate("DEBUG"),
                self.candidate("UNDERSTAND"),
            ],
            state,
            {"body": "The command currently fails with status 2."},
            ChoiceFixture(),
        )
        self.assertEqual(result["selected"]["state"], "UNDERSTAND")
        grounded = select_transition(
            [
                self.candidate(
                    "DEBUG", context_basis="fails with status 2"
                )
            ],
            state,
            {"body": "The command currently fails with status 2."},
        )
        self.assertEqual(grounded["selected"]["state"], "DEBUG")

    def test_plan_strategy_after_code_reply_uses_refine_or_correct(self):
        state = TaskState().data
        state.update(state="BUILD", code_reply={
            "id": "code-1", "text": "The current implementation uses a cache."
        })
        for proposed, expected in (("REFINE", "REFINE"), ("CORRECT", "CORRECT")):
            with self.subTest(control=proposed):
                candidates = enumerate_transition_candidates(
                    [self.candidate(
                        "PLAN", control=proposed,
                        reason="Keep later edits task-scoped and avoid this strategy",
                    )],
                    state,
                    {"body": "Improve the current implementation."},
                )
                plan = next(item for item in candidates if item["state"] == "PLAN")
                self.assertEqual(plan["control"], expected)
                self.assertEqual(plan["evidence_ids"], [])

    def test_successful_explanation_weight_moves_to_feasible_progress(self):
        state = TaskState().data
        state["code_reply"] = {"id": "code-1", "text": "A new issue is timeout"}
        state["published_state_counts"] = {"UNDERSTAND": 2, "PLAN": 1}
        rng = ChoiceFixture(index=1)
        result = select_transition(
            [
                self.candidate("UNDERSTAND", control="REFINE"),
                self.candidate("BUILD", control="CONTINUE"),
                self.candidate(
                    "PLAN", control="REFINE", context_basis="new issue is timeout"
                ),
            ],
            state,
            {"body": "Implement it."},
            rng,
        )
        self.assertEqual(rng.calls, [[0.45 ** 2, 2.1475, 0.65]])
        self.assertEqual(result["selected"]["state"], "BUILD")

    def test_post_solved_followups_decay_per_state_into_evaluate(self):
        state = TaskState().data
        state.update(
            code_reply={"id": "code-1", "text": "Implemented it"},
            published_state_counts={"UNDERSTAND": 1},
        )
        state["checks"] = [{
            "id": "judge-1", "tool": "judge_summary", "result": "passed",
            "summary": {"outcome": "solved"},
        }]
        candidates = enumerate_transition_candidates(
            [self.candidate("UNDERSTAND")], state, {"body": "Fix it."}
        )
        rng = ChoiceFixture(index=4)
        result = select_transition(candidates, state, {"body": "Fix it."}, rng)
        self.assertEqual(rng.calls, [[1.0, 0.5, 1.0, 1.0, 1.5]])
        self.assertEqual(result["selected"]["state"], "EVALUATE")

    def test_turn_identity_ignores_request_ids_but_tracks_public_progress(self):
        state = TaskState()
        first = transition_turn(state.data)
        self.assertEqual(first, transition_turn(copy.deepcopy(state.data)))
        permit = state.transition(
            self.candidate("BUILD") | {"task_id": "task-1"}
        )
        message = state.send(
            dict(task_id="task-1", permit_id=permit["id"], text="Please implement")
        )
        message["published"] = True
        state.record_published_transition(permit["id"])
        self.assertNotEqual(first, transition_turn(state.data))
        self.assertEqual(state.data["published_state_counts"], {"BUILD": 1})
        state.record_published_transition(permit["id"])
        self.assertEqual(state.data["published_state_counts"], {"BUILD": 1})

    def test_only_published_post_solved_state_counts_and_new_task_resets(self):
        state = TaskState()
        state.data["code_reply"] = {"id": "code-1", "text": "done"}
        state.data["checks"] = [{
            "id": "judge-1", "tool": "judge_summary", "result": "passed",
            "summary": {"outcome": "solved"},
        }]
        rejected = state.transition(
            self.candidate("PLAN", control="REFINE") | {"task_id": "task-1"}
        )
        self.assertEqual(state.data["published_state_counts"], {})
        message = state.send({
            "task_id": "task-1", "permit_id": rejected["id"], "text": "Use this strategy"
        })
        self.assertEqual(state.data["published_state_counts"], {})
        message["published"] = True
        state.record_published_transition(rejected["id"])
        state.record_published_transition(rejected["id"])
        self.assertEqual(state.data["published_state_counts"], {"PLAN": 1})

        state.data["phase"] = "user"
        state.accept({"task_id": "task-1", "reason": "done", "evidence_ids": ["judge-1"]})
        state.release_next()
        self.assertEqual(state.data["published_state_counts"], {})

    def episode(self, root):
        episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.private = Path(root)
        episode.state = TaskState()
        episode.saved = {
            "control_results": {},
            "transition_selections": {},
            "public": [],
            "code_sources": [],
            "tasks": [
                {"kind": "issue", "title": "Real title", "body": "Description"}
            ],
        }
        episode.collect_user_sources = MagicMock()
        episode.requirement = MagicMock(
            return_value={"title": "Real title", "body": "Description"}
        )
        episode.persist = MagicMock()
        episode.guard = MagicMock()
        episode.guard.review.return_value = {
            "allowed": True,
            "reasons": [],
            "warnings": [],
        }
        return episode

    def test_same_public_turn_reuses_selection_across_request_ids_and_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            episode.guard.review.return_value["warnings"] = [
                "Request type/control classification may not match the proposed action."
            ]
            with patch(
                "simulator.openhands.transition_selection.random.SystemRandom",
                return_value=ChoiceFixture(index=1),
            ):
                first = episode._control(
                    {
                        "request_id": "request-1",
                        "operation": "transition",
                        "payload": {
                            "task_id": "task-1",
                            "candidates": [self.candidate("UNDERSTAND")],
                        },
                    }
                )
            second = episode._control(
                {
                    "request_id": "request-2",
                    "operation": "transition",
                    "payload": {
                        "task_id": "task-1",
                        "candidates": [self.candidate("BUILD")],
                    },
                }
            )
            self.assertEqual(first, second)
            self.assertEqual(first["state"], "UNDERSTAND")
            self.assertIn("classification_warning", first)
            self.assertEqual(len(episode.state.data["transitions"]), 1)
            self.assertEqual(episode.guard.review.call_count, 1)

    def test_rejected_payload_is_stable_but_changed_intent_gets_one_review(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            episode.guard.review.side_effect = [
                {
                    "allowed": False,
                    "reasons": ["private safety decision"],
                    "warnings": [],
                },
                {"allowed": True, "reasons": [], "warnings": []},
            ]
            packet = {
                "request_id": "request-1",
                "operation": "transition",
                "payload": {
                    "task_id": "task-1",
                    "candidates": [self.candidate("BUILD")],
                },
            }
            with patch(
                "simulator.openhands.transition_selection.random.SystemRandom",
                return_value=ChoiceFixture(index=3),
            ):
                first = episode._control(packet)
            repeated = copy.deepcopy(packet)
            repeated["request_id"] = "request-2"
            second = episode._control(repeated)
            self.assertEqual(first, second)  # Same payload is not reviewed twice.
            self.assertFalse(first["accepted"])

            corrected = copy.deepcopy(packet)
            corrected["request_id"] = "request-3"
            corrected["payload"]["candidates"] = [
                self.candidate("BUILD"),  # Exact rejected candidate is excluded.
                self.candidate("OPERATE"),
            ]
            with patch(
                "simulator.openhands.transition_selection.random.SystemRandom",
                return_value=ChoiceFixture(index=3),
            ):
                third = episode._control(corrected)
            self.assertTrue(third["accepted"])
            self.assertEqual(third["state"], "OPERATE")
            self.assertEqual(len(episode.state.data["transitions"]), 1)
            self.assertEqual(episode.state.data["published_state_counts"], {})
            self.assertEqual(episode.saved["public"], [])
            self.assertEqual(episode.guard.review.call_count, 2)

            after_success = copy.deepcopy(packet)
            after_success["request_id"] = "request-4"
            after_success["payload"]["candidates"] = [self.candidate("PLAN")]
            self.assertEqual(episode._control(after_success), third)
            self.assertEqual(episode.guard.review.call_count, 2)
            turn_cache = next(iter(episode.saved["transition_selections"].values()))
            self.assertEqual(len(turn_cache["attempts"]), 2)
            self.assertEqual(
                [item["result"]["accepted"] for item in turn_cache["attempts"]],
                [False, True],
            )

    def test_single_infeasible_annotation_cannot_suppress_host_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            with patch(
                "simulator.openhands.transition_selection.random.SystemRandom",
                return_value=ChoiceFixture(index=0),
            ):
                result = episode._control(
                    {
                        "request_id": "invalid",
                        "operation": "transition",
                        "payload": {
                            "task_id": "task-1",
                            "candidates": [self.candidate("DEBUG")],
                        },
                    }
                )
            self.assertTrue(result["accepted"])
            self.assertEqual(result["state"], "RETRIEVE")
            selection = next(iter(episode.saved["transition_selections"].values()))[
                "attempts"
            ][0]
            self.assertEqual(len(selection["eligible"]), 5)
            episode.guard.review.assert_called_once()

    def test_only_projected_commit_title_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            self.assertEqual(
                episode.user_requirement(),
                {"title": "Real title", "body": "Description"},
            )
            episode.saved["tasks"][0]["kind"] = "commit"
            self.assertEqual(episode.user_requirement(), {"body": "Description"})


if __name__ == "__main__":
    unittest.main()
