import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from simulator.openhands.episode import OpenHandsEpisode
from simulator.openhands.state import TaskState


def final(identifier, text):
    return {
        "id": identifier,
        "kind": "MessageEvent",
        "source": "agent",
        "llm_message": {"content": [{"type": "text", "text": text}]},
    }


class UserFinalFallbackTests(unittest.TestCase):
    def episode(self, root, review=None):
        episode = OpenHandsEpisode.__new__(OpenHandsEpisode)
        episode.root = Path(root)
        episode.private = episode.root / "private"
        episode.private.mkdir()
        episode.state = TaskState()
        episode.saved = {
            "public": [],
            "control_results": {},
            "transition_selections": {},
            "code_sources": [],
            "tasks": [{
                "kind": "issue", "title": "Bug", "body": "Description",
                "identifier": "private", "reference": None, "patch": "",
            }],
            "revision": 0,
        }
        episode.persist = MagicMock()
        episode.collect_user_sources = MagicMock()
        episode.requirement = MagicMock(return_value={"title": "Bug", "body": "Description"})
        episode.user_requirement = MagicMock(return_value={"title": "Bug", "body": "Description"})
        episode.guard = MagicMock()
        episode.guard.review.return_value = review or {
            "allowed": True,
            "handoff_kind": "request_or_feedback",
            "warnings": [],
            "reasons": [],
        }
        return episode

    def permit(self, episode, task_id="task-1"):
        if task_id != episode.state.data["task_id"]:
            episode.state.data["permit"] = {
                "id": "old-permit", "task_id": task_id,
                "state": "BUILD", "control": "CONTINUE", "reason": "old",
            }
            return episode.state.data["permit"]
        return episode.state.transition({
            "task_id": task_id,
            "state": "BUILD",
            "control": "CONTINUE",
            "reason": "delegate the request",
            "evidence_ids": [],
        })

    def test_existing_current_permit_sends_exact_final_through_normal_control(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            self.permit(episode)
            text = "原文第一行\n\n  保留空格与 `code`。"
            record = episode.retain_user_final([final("message-1", text)])
            self.assertEqual(record["status"], "delivered")
            self.assertEqual(record["text"], text)
            self.assertEqual(episode.saved["public"], [unittest.mock.ANY])
            self.assertEqual(episode.saved["public"][0]["id"], "message-1")
            self.assertEqual(episode.saved["public"][0]["text"], text)
            self.assertEqual(episode.state.data["messages"][-1]["text"], text)
            self.assertEqual(episode.state.data["phase"], "code")

    def test_without_permit_same_user_only_requests_transition_then_host_sends(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            text = "请先检查现状，不要修改。"
            record = episode.retain_user_final([final("message-2", text)])
            self.assertEqual(record["status"], "awaiting_transition")
            self.assertEqual(episode.user_input(), {
                "task_id": "task-1",
                "current_requirement": {"title": "Bug", "body": "Description"},
                "retained_draft": text,
                "instruction": (
                    "该普通最终文本已由宿主原样保留。只调用 request_transition 为它申请发送许可；"
                    "不要调用 send_reply，不要重写或重新输出正文。"
                ),
            })
            result = episode._control({
                "request_id": "transition-1",
                "operation": "transition",
                "payload": {"task_id": "task-1", "candidates": [{
                    "state": "UNDERSTAND", "control": "CONTINUE",
                    "reason": "inspect current behavior", "evidence_ids": [],
                }]},
            })
            self.assertTrue(result["accepted"])
            self.assertTrue(result["handoff"])
            self.assertTrue(episode.deliver_user_final_fallback())
            self.assertEqual(record["status"], "delivered")
            self.assertEqual(episode.saved["public"][0]["text"], text)

    def test_old_task_permit_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            episode.state.data.update(task_id="task-2", task_index=1)
            self.permit(episode, "task-1")
            record = episode.retain_user_final([final("message-3", "新任务请求")])
            self.assertEqual(record["status"], "awaiting_transition")
            self.assertEqual(episode.saved["public"], [])

    def test_internal_plan_and_completed_closing_are_not_forwarded(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root, {
                "allowed": True, "handoff_kind": "internal_plan",
                "warnings": [], "reasons": [],
            })
            record = episode.retain_user_final([final("plan", "我下一步会再想想")])
            self.assertEqual(record["status"], "retained_private")
            self.assertEqual(episode.saved["public"], [])

        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            episode.state.data.update(status="completed", phase="ended")
            self.assertIsNone(episode.retain_user_final([final("closing", "已完成")]))
            self.assertNotIn("user_final_fallback", episode.saved)

    def test_rejected_final_is_retained_but_not_sent(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root, {
                "allowed": False, "handoff_kind": "request_or_feedback",
                "warnings": [], "reasons": ["unsafe"],
            })
            record = episode.retain_user_final([final("rejected", "越界内容")])
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["text"], "越界内容")
            self.assertEqual(episode.saved["public"], [])

    def test_pending_or_delivered_event_is_not_replaced_or_repeated(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            first = episode.retain_user_final([final("first", "原始正文")])
            episode.retain_user_final([final("second", "不得覆盖")])
            self.assertEqual(first["event_id"], "first")
            self.assertEqual(first["text"], "原始正文")
            self.permit(episode)
            self.assertTrue(episode.deliver_user_final_fallback())
            self.assertFalse(episode.deliver_user_final_fallback())
            episode.retain_user_final([final("first", "原始正文")])
            self.assertEqual(len(episode.saved["public"]), 1)
            self.assertEqual(episode.saved["public"][0]["text"], "原始正文")

    def test_public_send_then_ordinary_tail_is_not_captured(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            episode.saved["in_flight"] = {
                "role": "user", "id": "turn-1", "public_start": 0,
            }
            episode.saved["public"].append({
                "id": "sent", "kind": "user", "text": "已发送",
            })
            self.assertIsNone(
                episode.retain_user_final([final("tail", "不得再次排队")])
            )
            self.assertNotIn("user_final_fallback", episode.saved)
            episode.saved["public"].clear()
            episode.state.data["phase"] = "code"
            self.assertIsNone(
                episode.retain_user_final([final("code-phase", "也不得排队")])
            )

    def test_cached_transition_finishes_before_fallback_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            episode.resume = True
            episode.agents = {"user": MagicMock()}
            episode.prepare_pending_transition = MagicMock()
            episode.saved["in_flight"] = {
                "role": "user", "id": "turn-1", "public_start": 0,
            }
            episode.saved["user_final_fallback"] = {
                "schema": "user-final-fallback-v1",
                "event_id": "draft", "task_id": "task-1",
                "text": "原稿", "status": "awaiting_transition",
            }
            order = []
            episode.turn = MagicMock(side_effect=lambda *args, **kwargs: order.append("turn"))

            def deliver():
                self.assertIsNone(episode.saved["in_flight"])
                order.append("deliver")
                return False

            episode.deliver_user_final_fallback = MagicMock(side_effect=deliver)
            with patch(
                "simulator.openhands.episode.cached_pending_transition",
                return_value={"action_id": "action", "tool_name": "request_transition"},
            ):
                self.assertTrue(episode.resume_pending_user_control())
            self.assertEqual(order, ["turn", "deliver"])
            self.assertEqual(episode.state.data["status"], "paused")
            self.assertIn("without obtaining a transition", episode.state.data["pause_reason"])

    def test_cross_task_final_reuses_only_bound_acceptance_check(self):
        with tempfile.TemporaryDirectory() as root:
            episode = self.episode(root)
            episode.saved["tasks"].append({
                "kind": "issue", "title": "Next", "body": "Next description",
                "identifier": "private-next", "reference": None, "patch": "",
            })
            episode.saved.update(
                last_code_reply="verified",
                in_flight={"role": "user", "id": "accept-turn", "public_start": 0},
            )
            episode.state.data["code_reply"] = {
                "id": "code-final", "text": "verified", "stopped": True,
            }
            episode.state.data["checks"] = [{
                "id": "judge-task-1-r1", "revision": 1,
                "tool": "judge_summary", "result": "passed",
                "summary": {"outcome": "solved"},
            }]
            accepted = episode._control({
                "request_id": "accept-1", "operation": "accept",
                "payload": {
                    "task_id": "task-1", "reason": "verified",
                    "evidence_ids": ["judge-task-1-r1"],
                },
            })
            self.assertTrue(accepted["accepted"])
            self.assertEqual(episode.state.data["task_id"], "task-2")
            self.assertEqual(episode.state.data["checks"], [])

            record = episode.retain_user_final([
                final("cross-task-final", "上一任务验证通过。请先调查新需求。")
            ])
            self.assertEqual(record["status"], "awaiting_transition")
            support = record["accepted_context"]["evidence"]
            self.assertEqual([item["id"] for item in support], ["judge-task-1-r1"])
            self.assertEqual(support[0]["task_id"], "task-1")

            episode.state.transition({
                "task_id": "task-2", "state": "RETRIEVE", "control": "CONTINUE",
                "reason": "inspect the new request", "evidence_ids": [],
            })
            self.assertTrue(episode.deliver_user_final_fallback())
            request_id = episode._fallback_send_request_id(record)
            self.assertEqual(
                request_id,
                "user-final-fallback-cross-task-final-judge-task-1-r1",
            )
            self.assertIn(request_id, episode.saved["control_results"])
            call = episode.guard.review.call_args_list[-1]
            self.assertEqual(call.kwargs["supporting_checks"], support)
            self.assertEqual(episode.saved["public"][0]["text"], record["text"])
            self.assertFalse(episode.deliver_user_final_fallback())
            self.assertEqual(len(episode.saved["public"]), 1)


if __name__ == "__main__":
    unittest.main()
