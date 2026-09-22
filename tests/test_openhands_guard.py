import base64
import json
import tempfile
import unittest
from pathlib import Path

from simulator.openhands.guard import MessageGuard
from simulator.openhands.relay import Relay
from simulator.openhands.state import TaskState
from simulator.state_machine import REQUEST_SEMANTICS


class ReviewFixture:
    config = {"model": "fixture"}
    response = {
        "decision": "allow",
        "kind": "closing",
        "claims_observation": False,
        "state_consistent": True,
        "reasons": [],
    }

    def dispatch(self, packet):
        return (
            200,
            json.dumps(
                {"choices": [{"message": {"content": json.dumps(self.response)}}]}
            ).encode(),
        )


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.state = TaskState()
        self.guard = MessageGuard(ReviewFixture(), [])

    def test_completion_declaration_requires_accept_tool(self):
        result = self.guard.review(
            "send", {"text": "Done, accepted"}, self.state, {}, []
        )
        self.assertFalse(result["allowed"])

    def test_duplicate_report_requires_accept_even_without_completion_wording(self):
        relay = ReviewFixture()
        relay.response = {**relay.response}
        guard = MessageGuard(relay, [])
        self.assertFalse(
            guard.review("send", {"text": "检查结果和你一致"}, self.state, {}, [])[
                "allowed"
            ]
        )
        self.state.accept({"task_id": "task-1", "reason": "private decision"})
        self.assertTrue(
            guard.review("send", {"text": "可以"}, self.state, {}, [])["allowed"]
        )

    def test_useful_clarifications_and_permissions_are_not_length_gated(self):
        relay = ReviewFixture()
        relay.response = {**relay.response, "kind": "message"}
        guard = MessageGuard(relay, [])
        for text in [
            "按这个做",
            "用第二种方案",
            "这个改动会影响旧接口吗？",
            "具体报错如下：" + ("details " * 500),
        ]:
            self.assertTrue(
                guard.review("send", {"text": text}, self.state, {}, [])["allowed"]
            )

    def test_request_semantics_keep_strategy_constraints_in_plan(self):
        self.assertIn("编码策略", REQUEST_SEMANTICS["states"]["PLAN"])
        self.assertIn("实现约束", REQUEST_SEMANTICS["states"]["PLAN"])
        self.assertIn("实际实现", REQUEST_SEMANTICS["states"]["BUILD"])
        self.assertIn("Code 已公开方案", REQUEST_SEMANTICS["controls"]["REFINE"])

    def test_language_mismatch_rejected_without_rewriting(self):
        relay = ReviewFixture()
        relay.response = {
            **relay.response,
            "decision": "reject",
            "reasons": ["language mismatch"],
        }
        result = MessageGuard(relay, []).review(
            "send", {"text": "Confirmed"}, self.state, {}, []
        )
        self.assertFalse(result["allowed"])
        self.assertNotIn("text", result)

    def test_state_label_difference_is_warning_only(self):
        relay = ReviewFixture()
        relay.response = {
            **relay.response,
            "kind": "message",
            "state_consistent": False,
            "reasons": ["DEBUG may describe the feedback more precisely than BUILD"],
        }
        result = MessageGuard(relay, []).review(
            "transition",
            {
                "task_id": "task-1",
                "state": "BUILD",
                "control": "CORRECT",
                "reason": "Correct the implementation direction after a failed check",
            },
            self.state,
            {},
            [],
        )
        self.assertTrue(result["allowed"])
        self.assertTrue(result["warnings"])
        self.assertIn("DEBUG may describe", result["reasons"][0])

    def test_send_state_mismatch_is_a_warning_when_content_is_safe(self):
        relay = ReviewFixture()
        relay.response = {
            **relay.response,
            "kind": "message",
            "state_consistent": False,
        }
        permit = self.state.transition(
            {
                "task_id": "task-1",
                "state": "BUILD",
                "control": "CONTINUE",
                "reason": "Ask Code to implement the requested change",
            }
        )
        before = json.loads(json.dumps(self.state.data))
        result = MessageGuard(relay, []).review(
            "send",
            {
                "task_id": "task-1",
                "permit_id": permit["id"],
                "text": "请继续处理",
            },
            self.state,
            {},
            [],
        )
        self.assertTrue(result["allowed"])
        self.assertIn(
            "Request type/control classification may not match the proposed action.",
            result["warnings"],
        )
        self.assertEqual(self.state.data, before)

    def test_send_matching_state_remains_allowed(self):
        relay = ReviewFixture()
        relay.response = {**relay.response, "kind": "message"}
        permit = self.state.transition(
            {
                "task_id": "task-1",
                "state": "BUILD",
                "control": "CONTINUE",
                "reason": "Ask Code to implement the requested change",
            }
        )
        result = MessageGuard(relay, []).review(
            "send",
            {
                "task_id": "task-1",
                "permit_id": permit["id"],
                "text": "请继续处理",
            },
            self.state,
            {},
            [],
        )
        self.assertTrue(result["allowed"])

    def test_reject_decision_remains_authoritative_with_state_warning(self):
        relay = ReviewFixture()
        relay.response = {
            **relay.response,
            "decision": "reject",
            "kind": "message",
            "state_consistent": False,
            "reasons": ["private information"],
        }
        result = MessageGuard(relay, []).review(
            "transition",
            {
                "task_id": "task-1",
                "state": "BUILD",
                "control": "CORRECT",
                "reason": "Correct the implementation direction",
            },
            self.state,
            {},
            [],
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reasons"], ["private information"])
        self.assertTrue(result["warnings"])

    def test_internal_id_prefix_never_sent(self):
        self.state.data["checks"].append({"id": "12345678-abcd", "result": "failed"})
        result = self.guard.review(
            "send", {"text": "Check 12345678 failed"}, self.state, {}, []
        )
        self.assertFalse(result["allowed"])

    def test_explicit_check_evidence_is_not_replaced_by_private_id_scan(self):
        relay = ReviewFixture()
        relay.response = {**relay.response, "kind": "message"}
        self.state.data["checks"].append(
            {"id": "check-current", "revision": 1, "result": "passed"}
        )
        self.state.data["transitions"].append({"id": "transition-private"})
        result = MessageGuard(relay, []).review(
            "send",
            {"text": "The current check passed.", "evidence_ids": ["check-current"]},
            self.state,
            {},
            [],
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(
            result["evidence_linking"]["effective_ids"], ["check-current"]
        )

    def test_old_task_support_is_send_only_and_requires_real_owned_check(self):
        relay = ReviewFixture()
        relay.response = {
            **relay.response, "kind": "message", "claims_observation": True,
        }
        state = TaskState()
        state.data.update(task_index=1, task_id="task-2")
        old_check = {
            "id": "judge-task-1-r1", "task_id": "task-1",
            "revision": 1, "result": "passed", "tool": "judge_summary",
            "summary": {"outcome": "solved"},
        }
        result = MessageGuard(relay, []).review(
            "send",
            {"text": "上一任务验证通过，请调查新需求。", "evidence_ids": []},
            state,
            {},
            [],
            supporting_checks=[old_check],
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(
            result["evidence_linking"]["effective_ids"], ["judge-task-1-r1"]
        )
        with self.assertRaisesRegex(ValueError, "recorded observations"):
            state.accept({
                "task_id": "task-2", "reason": "not valid for new task",
                "evidence_ids": ["judge-task-1-r1"],
            })
        with self.assertRaisesRegex(ValueError, "recorded observations"):
            state.transition({
                "task_id": "task-2", "state": "BUILD", "control": "CONTINUE",
                "reason": "not valid for new task", "evidence_ids": ["judge-task-1-r1"],
            })

        missing = MessageGuard(relay, []).review(
            "send",
            {"text": "上一任务验证通过，请调查新需求。", "evidence_ids": []},
            state,
            {},
            [],
            supporting_checks=[{**old_check, "task_id": "task-2"}],
        )
        self.assertFalse(missing["allowed"])
        self.assertTrue(any("evidence_ids" in item for item in missing["reasons"]))

    def test_private_test_source_reports_origin_without_rewriting(self):
        from simulator.openhands.provenance import record

        result = self.guard.review(
            "send",
            {"text": "Traceback\nassert actual == expected_value\nAssertionError"},
            self.state,
            {},
            [],
            [record("private", "assert actual == expected_value", event_id="e1")],
        )
        self.assertFalse(result["allowed"])
        self.assertNotIn("unsent_proposal", result)
        self.assertEqual(result["provenance"]["matches"][0]["event_id"], "e1")

    def test_inferred_reviewed_logic_feedback_bypasses_only_its_source_overlap(self):
        from simulator.openhands.provenance import record

        relay=ReviewFixture()
        relay.response={**relay.response,"kind":"message","claims_observation":True}
        summary='调用 faulthandler.enable() 后实际返回 UnsupportedOperation fileno'
        self.state.data['checks'].append({
            'id':'judge1','revision':1,'tool':'judge_summary','result':'failed',
            'simulated_experience':{
                'summary_id':'judge1',
                'observation':{'kind':'logic_error','evidence_id':'obs1','summary':summary},
            },
        })
        source=record('private','faulthandler.enable()',event_id='judge-test')
        result=MessageGuard(relay,[]).review(
            'send',{'text':'复现结果：'+summary},self.state,{},[],[source])
        self.assertTrue(result['allowed'])
        self.assertEqual(result['evidence_linking']['effective_ids'],['judge1'])
        self.assertFalse(result['provenance']['matches'])

        unbound=MessageGuard(relay,[]).review(
            'send',{'text':'复现结果：'+summary},TaskState(),{},[],[source])
        self.assertFalse(unbound['allowed'])

    def test_bound_test_patch_line_defers_but_implementation_line_does_not(self):
        relay=ReviewFixture()
        relay.response={**relay.response,"kind":"message","claims_observation":True}
        test_line='assert runner.invoke(command).exception is None and result.exit_code == 0'
        implementation='return wrap_stream(stream, errors="replace", preserve_fileno=True)'
        patch_text=('diff --git a/tests/test_cli.py b/tests/test_cli.py\n'
                    '+++ b/tests/test_cli.py\n+'+test_line+'\n'
                    'diff --git a/src/cli.py b/src/cli.py\n'
                    '+++ b/src/cli.py\n+'+implementation)
        tasks=[{'identifier':'private-task','reference':'b'*40,'title':'problem',
                'body':'symptom','patch':patch_text}]
        for text,allowed in ((test_line,True),(implementation,False),(patch_text,False)):
            with self.subTest(text=text[:20]):
                state=TaskState()
                state.data['checks'].append({
                    'id':'judge1','revision':1,'tool':'judge_summary','result':'failed',
                    'simulated_experience':{
                        'summary_id':'judge1',
                        'observation':{'kind':'logic_error','evidence_id':'obs1','summary':text},
                    },
                })
                result=MessageGuard(relay,tasks).review(
                    'send',{'text':text,'evidence_ids':['judge1']},state,{},[])
                self.assertEqual(result['allowed'],allowed)

    def test_raw_failure_feedback_must_be_pasted_verbatim(self):
        relay = ReviewFixture()
        relay.response = {
            **relay.response,
            "claims_observation": True,
            "kind": "message",
        }
        guard = MessageGuard(relay, [])
        experience = dict(
            summary_id="judge1",
            observation={
                "kind": "wrong_output",
                "evidence_id": "obs1",
                "input": "INPUT: 1月到3月",
                "output": "OUTPUT: 只有1月",
            },
            allowed_claims=["verbatim_input", "verbatim_output"],
        )
        self.state.data["checks"].append(
            dict(
                id="judge1",
                revision=1,
                tool="judge_summary",
                result="failed",
                simulated_experience=experience,
            )
        )
        from simulator.openhands.provenance import record

        sources = [record("private", "OUTPUT: 只有1月", event_id="private-test")]
        exact = guard.review(
            "send",
            {
                "text": "输入是\nINPUT: 1月到3月\n输出是\nOUTPUT: 只有1月",
                "evidence_ids": ["judge1"],
            },
            self.state,
            {},
            [],
            sources,
        )
        self.assertTrue(exact["allowed"])
        changed = guard.review(
            "send",
            {"text": "输入是 1月到3月，输出只有1月", "evidence_ids": ["judge1"]},
            self.state,
            {},
            [],
        )
        self.assertFalse(changed["allowed"])
        self.assertTrue(
            any("preserve the exact" in reason for reason in changed["reasons"])
        )

    def test_bound_failure_does_not_turn_clarification_into_observation(self):
        relay = ReviewFixture()
        relay.response = {**relay.response, "kind": "message"}
        guard = MessageGuard(relay, [])
        self._add_failure_experience()
        result = guard.review(
            "send",
            {
                "text": "请保留公开导入路径，并只把该接口从生成的 API 文档中隐藏。",
                "evidence_ids": ["judge1"],
            },
            self.state,
            {},
            [],
        )
        self.assertTrue(result["allowed"])

    def test_long_bound_input_can_be_clarified_without_reporting_output(self):
        relay = ReviewFixture()
        relay.response = {**relay.response, "kind": "message"}
        guard = MessageGuard(relay, [])
        experience = self._add_failure_experience()
        result = guard.review(
            "send",
            {
                "text": "你问的输入是：\n" + experience["observation"]["input"],
                "evidence_ids": ["judge1"],
            },
            self.state,
            {},
            [],
        )
        self.assertTrue(result["allowed"])

    def test_exact_raw_quote_enforces_complete_feedback_when_review_misses_claim(self):
        relay = ReviewFixture()
        relay.response = {**relay.response, "kind": "message"}
        guard = MessageGuard(relay, [])
        experience = self._add_failure_experience()
        observation = experience["observation"]
        result = guard.review(
            "send",
            {
                "text": "请继续排查；实际结果是\n" + observation["output"],
                "evidence_ids": ["judge1"],
            },
            self.state,
            {},
            [],
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(any("exact input" in reason for reason in result["reasons"]))

    def test_runtime_error_feedback_requires_exact_input_and_error(self):
        relay = ReviewFixture()
        relay.response = {
            **relay.response,
            "claims_observation": True,
            "kind": "message",
        }
        guard = MessageGuard(relay, [])
        experience = self._add_failure_experience(kind="runtime_error")
        observation = experience["observation"]
        exact = guard.review(
            "send",
            {
                "text": observation["input"] + "\n" + observation["error"],
                "evidence_ids": ["judge1"],
            },
            self.state,
            {},
            [],
        )
        self.assertTrue(exact["allowed"])
        paraphrased = guard.review(
            "send",
            {
                "text": "这个较长输入触发了一个运行时异常。",
                "evidence_ids": ["judge1"],
            },
            self.state,
            {},
            [],
        )
        self.assertFalse(paraphrased["allowed"])

    def test_missing_semantic_claim_field_fails_closed(self):
        relay = ReviewFixture()
        relay.response = {
            key: value
            for key, value in relay.response.items()
            if key != "claims_observation"
        }
        with self.assertRaisesRegex(ValueError, "invalid gate response"):
            MessageGuard(relay, []).review(
                "send", {"text": "请继续处理"}, self.state, {}, []
            )

    def _add_failure_experience(self, kind="wrong_output"):
        result_key = "error" if kind == "runtime_error" else "output"
        experience = {
            "summary_id": "judge1",
            "observation": {
                "kind": kind,
                "evidence_id": "obs1",
                "input": "INPUT: a deliberately distinctive input value for this execution",
                result_key: "RESULT: a deliberately distinctive observed failure value",
            },
            "allowed_claims": ["verbatim_input", "verbatim_output"],
        }
        self.state.data["checks"].append(
            {
                "id": "judge1",
                "revision": 1,
                "tool": "judge_summary",
                "result": "failed",
                "simulated_experience": experience,
            }
        )
        return experience

    def test_provider_relay_blocks_arbitrary_endpoints_and_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = Relay({}, Path(directory), Path(directory) / "audit.jsonl")
            for path, body in [
                ("/v1/responses", {}),
                ("/control", {"operation": "accept"}),
                ("/v1/chat/completions", {"stream": True}),
                ("/v1/chat/completions", {"tools": [{"type": "web_search"}]}),
            ]:
                with self.assertRaises(ValueError):
                    relay.dispatch(
                        {
                            "path": path,
                            "body": base64.b64encode(
                                json.dumps(body).encode()
                            ).decode(),
                        }
                    )


if __name__ == "__main__":
    unittest.main()
