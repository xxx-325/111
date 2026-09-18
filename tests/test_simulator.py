from __future__ import annotations

import json
import tempfile
import unittest
import random
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))

from simulator.agents import CodeAgent, GoalExtractor, UserAgent
from simulator.models import CommitTarget
from simulator.state_machine import CORE_STATES, TRANSITIONS, sample_next
from simulator.git_source import manifest_summary, snapshot_manifest
from simulator.runner import SessionRunner


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def complete(self, prompt, *, cwd=None):
        self.prompts.append(prompt)
        return next(self.responses)


class CodeFakeProvider(FakeProvider):
    def complete(self, prompt, *, cwd=None):
        self.prompts.append(prompt)
        if cwd is not None:
            (cwd / "implemented.txt").write_text("implemented", encoding="utf-8")
        return "已完成实现。"


class SimulatorTests(unittest.TestCase):
    def test_goal_extractor_creates_hidden_goal(self):
        provider = FakeProvider(['{"user_goal":"add retry","acceptance":["retry exists"]}'])
        target = CommitTarget("c", "p", "Add retry", "diff --git a/a b/a")
        goal = GoalExtractor(provider).extract(target)
        self.assertEqual(goal.user_goal, "add retry")
        self.assertIn("Patch:", provider.prompts[0])

    def test_user_judge_does_not_have_to_expose_goal(self):
        provider = FakeProvider([
            '{"done":false,"feedback":"retry still fails","next_goal":"","visible_message":"请再检查失败重试。"}'
        ])
        user = UserAgent(provider)
        target = CommitTarget("secret-commit", "parent", "subject", "secret diff")
        from simulator.models import HiddenGoal
        user.judge(HiddenGoal(target, "hidden goal", ["hidden check"]), "done", "verification exit=1")
        self.assertIn("hidden goal", provider.prompts[0])
        self.assertNotIn("secret diff", provider.prompts[0])

    def test_state_machine_rows_and_sampling(self):
        self.assertEqual(set(TRANSITIONS), set(CORE_STATES))
        for row in TRANSITIONS.values():
            self.assertAlmostEqual(sum(row.values()), 1.0)
        self.assertIn(sample_next("DEBUG", random.Random(1)), CORE_STATES)

    def test_user_judge_updates_state_without_exposing_it_to_code_agent(self):
        provider = FakeProvider(['{"done":false,"feedback":"仍有问题","next_goal":"","control":"CORRECT","next_state":"DEBUG","visible_message":"这个错误还没有解决，请继续排查。"}'])
        user = UserAgent(provider, random.Random(0))
        from simulator.models import HiddenGoal
        user.begin_goal("BUILD")
        user.judge(HiddenGoal(CommitTarget("c", "p", "s", "secret"), "hidden", ["check"]), "reply", "verification exit=1")
        self.assertEqual(user.state.current, "DEBUG")
        self.assertNotIn("secret", provider.prompts[0])

    def test_plain_snapshot_verification_reports_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("one", encoding="utf-8")
            before = snapshot_manifest(root)
            (root / "a.txt").write_text("two", encoding="utf-8")
            (root / "b.txt").write_text("new", encoding="utf-8")
            self.assertEqual(manifest_summary(before, root), "M a.txt\nA b.txt")

    def test_runner_keeps_hidden_target_out_of_code_agent_and_writes_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            (repo / "README.md").write_text("base\nfeature\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            commit = subprocess.run(["git", "-C", str(repo), "commit", "-qm", "feature", "--quiet"], check=True)
            del commit
            target = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            goal_provider = FakeProvider(['{"user_goal":"add feature","acceptance":["README contains feature"],"initial_state":"BUILD"}'])
            user_provider = FakeProvider([
                '请实现这个功能。',
                '{"done":true,"feedback":"","next_goal":"","control":null,"next_state":"EVALUATE","visible_message":"看起来已经完成。"}',
            ])
            code_provider = CodeFakeProvider([])
            output = Path(directory) / "session.jsonl"
            SessionRunner(repo, GoalExtractor(goal_provider), UserAgent(user_provider), CodeAgent(code_provider), output).run([target])
            visible = output.read_text(encoding="utf-8")
            private = (output.with_name("run.json")).read_text(encoding="utf-8")
            self.assertIn("请实现这个功能", visible)
            self.assertNotIn("README contains feature", code_provider.prompts[0])
            self.assertNotIn(target, code_provider.prompts[0])
            self.assertIn('"states"', private)

    def test_code_agent_retains_only_visible_conversation_history(self):
        provider = FakeProvider(["第一轮完成。", "第二轮完成。"])
        agent = CodeAgent(provider)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent.run("实现功能", root)
            agent.run("请继续检查", root)
        self.assertIn("User: 实现功能", provider.prompts[1])
        self.assertIn("Code Agent: 第一轮完成。", provider.prompts[1])


if __name__ == "__main__":
    unittest.main()
