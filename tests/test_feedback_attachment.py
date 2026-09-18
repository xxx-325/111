import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock

from simulator.openhands.control_tools import SendAction, SendReplyTool
from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.feedback_attachment import attach_feedback
from simulator.openhands.judge import candidate_hash
from simulator.openhands.progressive import ProgressiveEpisode
from simulator.openhands.state import TransitionError
from openhands.sdk import Event


class FeedbackAttachmentTests(unittest.TestCase):
    def experience(self, kind="wrong_output", result=None):
        key = "error" if kind == "runtime_error" else "output"
        return {
            "executor": "Judge",
            "revision": 5,
            "candidate_version": "candidate-5",
            "summary_id": "judge-task-4-r5",
            "observation": {
                "kind": kind,
                "input": "--flag value",
                key: (
                    result if result is not None else "line 1\r\nline 2\r\n\x1b[?2004h"
                ),
            },
            "operation_details_allowed": False,
        }

    def test_plain_clarification_has_no_attachment_switch(self):
        action = SendAction(task_id="task-4", permit_id="permit", text="参数是 --flag")
        self.assertEqual(action.text, "参数是 --flag")
        self.assertIsNone(action.attach_feedback)

    def test_send_tool_exposes_single_placeholder_contract(self):
        tool = SendReplyTool.create(None)[0]
        self.assertEqual(tool.executor.operation, "send")
        schema = tool.action_type.model_json_schema()
        self.assertNotIn("attach_feedback", schema["properties"])
        self.assertIn("[[运行结果]]", tool.description)
        self.assertIn("raw_result_available is true", tool.description)

    def test_retired_boolean_event_remains_deserializable_but_hidden(self):
        action = SendAction.model_validate({
            "task_id": "task-4", "permit_id": "permit", "text": "old",
            "attach_feedback": False,
        })
        self.assertFalse(action.attach_feedback)
        self.assertNotIn("attach_feedback", SendAction.model_json_schema()["properties"])

    def test_send_wording_allows_only_reviewed_current_task_tests(self):
        description = SendAction.model_json_schema()["properties"]["text"]["description"]
        self.assertIn("reviewed current-task tests may be shared", description)
        self.assertIn("without reference implementations or internal IDs", description)

    def test_sdk_event_store_can_rebuild_a_completed_legacy_send(self):
        arguments = {
            "task_id": "task-4", "permit_id": "permit", "text": "old reply",
            "evidence_ids": [], "attach_feedback": False,
        }
        event = Event.model_validate_json(json.dumps({
            "id": "969b8427-df92-487a-b10d-100fa00a1c28",
            "timestamp": "2026-09-15T09:26:48.175767",
            "source": "agent", "parent_id": None, "thought": [],
            "action": {**arguments, "kind": "SendAction"},
            "tool_name": "send_reply", "tool_call_id": "call_old",
            "tool_call": {
                "id": "call_old", "responses_item_id": None,
                "name": "send_reply", "arguments": json.dumps(arguments),
                "origin": "completion",
            },
            "llm_response_id": "48227568-e6b2-44fd-ae9f-0f82c2a5e45d",
            "kind": "ActionEvent",
        }))
        self.assertIsInstance(event.action, SendAction)
        self.assertFalse(event.action.attach_feedback)

    def test_followup_instruction_explains_explicit_attachment_choice(self):
        episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.state = MagicMock()
        episode.state.communication.return_value = {
            "stage": "followup",
            "last_code_reply": {"text": "done"},
        }
        episode.state.data = {"task_index": 0}
        episode.saved = {"tasks": [{"kind": "issue"}]}
        episode.requirement = MagicMock(return_value={"title": "task", "body": "body"})
        value = episode.user_input()
        self.assertIn("自然回应 Code", value["instruction"])
        self.assertIn("[[运行结果]]", value["instruction"])
        self.assertNotIn("attach_feedback", value["instruction"])

    def test_crlf_and_ansi_result_are_appended_exactly(self):
        experience = self.experience()
        final, metadata = attach_feedback(
            "这个结果仍不符合预期：[[运行结果]]\n请继续处理。",
            experience,
            revision=5,
            candidate_version="candidate-5",
        )
        self.assertTrue(final.startswith("这个结果仍不符合预期：输入："))
        self.assertTrue(final.endswith("\n请继续处理。"))
        self.assertIn("输入：\n--flag value", final)
        self.assertIn(experience["observation"]["output"], final)
        self.assertIn("\r\n", final)
        self.assertEqual(metadata["final_text"], final)

    def test_runtime_error_uses_error_label(self):
        final, _ = attach_feedback(
            "运行仍报错：[[运行结果]]",
            self.experience("runtime_error", "Traceback\r\nValueError\x1b[0m"),
            revision=5,
            candidate_version="candidate-5",
        )
        self.assertIn("报错：\nTraceback\r\nValueError\x1b[0m", final)

    def test_explicit_empty_output_is_attachable_but_empty_error_is_not(self):
        final, metadata = attach_feedback(
            "结果如下：[[运行结果]]", self.experience("wrong_output", ""),
            revision=5, candidate_version="candidate-5",
        )
        self.assertTrue(final.endswith("输出：\n"))
        self.assertEqual(metadata["final_text"], final)
        with self.assertRaisesRegex(TransitionError, "incomplete"):
            attach_feedback(
                "结果如下：[[运行结果]]", self.experience("runtime_error", ""),
                revision=5, candidate_version="candidate-5",
            )

    def test_plain_text_is_unchanged_and_placeholder_is_not_recursive(self):
        plain, metadata = attach_feedback(
            "参数是 --flag", None, revision=5, candidate_version="candidate-5"
        )
        self.assertEqual(plain, "参数是 --flag")
        self.assertIsNone(metadata)
        experience = self.experience(result="literal [[运行结果]] remains raw")
        final, _ = attach_feedback(
            "结果：[[运行结果]]", experience,
            revision=5, candidate_version="candidate-5",
        )
        self.assertTrue(final.endswith("literal [[运行结果]] remains raw"))
        with self.assertRaisesRegex(TransitionError, "exactly one"):
            attach_feedback(
                "[[运行结果]] and [[运行结果]]", experience,
                revision=5, candidate_version="candidate-5",
            )

    def test_missing_stale_logic_or_private_feedback_fails_closed(self):
        with self.assertRaises(TransitionError):
            attach_feedback("[[运行结果]]", None, revision=5, candidate_version="candidate-5")
        with self.assertRaises(TransitionError):
            attach_feedback(
                "[[运行结果]]", self.experience(), revision=4, candidate_version="candidate-5"
            )
        with self.assertRaises(TransitionError):
            attach_feedback(
                "[[运行结果]]",
                self.experience("logic_error"),
                revision=5,
                candidate_version="candidate-5",
            )
        with self.assertRaises(TransitionError):
            attach_feedback(
                "[[运行结果]]",
                self.experience(result="failure in /workspace/checks/private.py"),
                revision=5,
                candidate_version="candidate-5",
            )

    def test_final_text_is_reviewed_before_delivery_and_raw_draft_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
            episode.private = Path(directory)
            episode.tasks = []
            episode.saved = {
                "control_results": {},
                "last_code_reply": {"text": "done"},
                "public": [],
                "code_sources": [],
            }
            episode.state = MagicMock()
            episode.state.data = {"task_id": "task-4", "blockers": []}
            episode.state.pending_checks.return_value = []
            episode.collect_user_sources = MagicMock()
            episode.requirement = MagicMock(return_value={})
            episode.persist = MagicMock()
            episode.public = MagicMock()
            episode.guard = MagicMock()
            episode.guard.review.return_value = {
                "allowed": False,
                "reasons": ["fixture rejection"],
            }
            final = "User draft\n\n输入：\nraw input\n输出：\nraw output"
            attachment = {
                "summary_id": "judge-task-4-r5",
                "revision": 5,
                "candidate_version": "candidate-5",
                "final_text": final,
            }
            episode.prepare_send_payload = MagicMock(
                return_value=(
                    {
                        "task_id": "task-4",
                        "permit_id": "permit",
                        "text": final,
                    },
                    attachment,
                )
            )
            packet = {
                "request_id": "request-1",
                "operation": "send",
                "payload": {
                    "task_id": "task-4",
                    "permit_id": "permit",
                    "text": "User draft [[运行结果]]",
                },
            }
            result = episode._control(packet)
            self.assertFalse(result["accepted"])
            reviewed = episode.guard.review.call_args.args[1]
            self.assertEqual(reviewed["text"], final)
            episode.state.send.assert_not_called()
            record = __import__("json").loads(
                (episode.private / "controls.jsonl").read_text().splitlines()[-1]
            )
            self.assertEqual(record["payload"]["text"], "User draft [[运行结果]]")
            self.assertEqual(record["attachment"], attachment)

    def test_cached_control_result_does_not_assemble_again(self):
        episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.saved = {"control_results": {"same": {"accepted": True}}}
        episode.prepare_send_payload = MagicMock()
        result = episode._control(
            {"request_id": "same", "operation": "send", "payload": {}}
        )
        self.assertEqual(result, {"accepted": True})
        episode.prepare_send_payload.assert_not_called()

    def test_progressive_authority_checks_current_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "workspace/candidate"
            candidate.mkdir(parents=True)
            (candidate / "value.py").write_text("value = 1\n")
            version = candidate_hash(candidate)
            experience = self.experience()
            experience["candidate_version"] = version
            current = {
                "applied_job": experience["summary_id"],
                "simulated_experience": experience,
                "verdict": {
                    "outcome": "unsolved",
                    "revision": 5,
                    "candidate_version": version,
                },
            }
            episode = ProgressiveEpisode.__new__(ProgressiveEpisode)
            episode.root = root
            episode.saved = {"revision": 5}
            episode.current = MagicMock(return_value=current)
            payload = {"text": "失败如下：[[运行结果]]\n请继续处理。"}
            final, metadata = episode.prepare_send_payload(payload)
            self.assertIn(experience["observation"]["output"], final["text"])
            self.assertEqual(metadata["candidate_version"], version)
            (candidate / "value.py").write_text("value = 2\n")
            with self.assertRaisesRegex(TransitionError, "no unique reviewed"):
                episode.prepare_send_payload(payload)

    def test_legacy_attachment_field_is_rejected(self):
        episode = ProgressiveEpisode.__new__(ProgressiveEpisode)
        with self.assertRaisesRegex(TransitionError, "unsupported"):
            episode.prepare_send_payload({"text": "继续", "attach_feedback": True})


if __name__ == "__main__":
    unittest.main()
