from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .agents import CodeAgent, GoalExtractor, UserAgent
from .git_source import manifest_summary, materialize_snapshot, remove_worktree, resolve_commits, snapshot_manifest
from .models import SessionEvent


class SessionRunner:
    def __init__(self, repo: Path, extractor: GoalExtractor, user: UserAgent, code: CodeAgent, output: Path, max_rounds: int = 8) -> None:
        self.repo, self.extractor, self.user, self.code = repo, extractor, user, code
        self.output, self.max_rounds = output, max_rounds

    def run(self, commits: list[str], verify_command: str | None = None, save_prompts: bool = False) -> None:
        targets = resolve_commits(self.repo, commits)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        private = {"commits": [], "warning": "private; do not publish"}
        with self.output.open("w", encoding="utf-8") as stream:
            for index, target in enumerate(targets, 1):
                goal = self.extractor.extract(target)
                self.user.begin_goal(goal.initial_state)
                private_commit = {"commit": target.commit, "goal": goal.user_goal, "acceptance": goal.acceptance, "rounds": 0, "done": False, "states": [], "controls": []}
                private["commits"].append(private_commit)
                with tempfile.TemporaryDirectory(prefix="agent-session-") as temp:
                    cwd = Path(temp) / "repo"
                    materialize_snapshot(self.repo, target.parent, cwd)
                    baseline = snapshot_manifest(cwd)
                    try:
                        message = self.user.initial_request(goal)
                        self._write(stream, SessionEvent("user", message))
                        if save_prompts:
                            self._save_prompt(index, "initial-user.txt", message)
                        for round_no in range(1, self.max_rounds + 1):
                            private_commit["rounds"] = round_no
                            private_commit["states"].append(self.user.state.current)
                            reply = self.code.run(message, cwd)
                            self._write(stream, SessionEvent("assistant", reply))
                            verification = self._verify(cwd, verify_command, baseline)
                            if verification:
                                self._write(stream, SessionEvent("tool", verification))
                            decision = self.user.judge(goal, reply, verification)
                            if decision.done:
                                private_commit["done"] = True
                                self._write(stream, SessionEvent("user", decision.text))
                                break
                            goal.current_feedback.append(decision.feedback)
                            private_commit["controls"].append(decision.control)
                            message = decision.text
                            self._write(stream, SessionEvent("user", message))
                        else:
                            raise RuntimeError(f"commit {target.commit} did not complete within {self.max_rounds} rounds")
                    finally:
                        remove_worktree(self.repo, cwd)
        self.output.with_name("run.json").write_text(json.dumps(private, ensure_ascii=False, indent=2), encoding="utf-8")

    def _verify(self, cwd: Path, command: str | None, baseline: dict[str, bytes]) -> str:
        if not command:
            return manifest_summary(baseline, cwd)
        proc = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True, timeout=900)
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return f"verification exit={proc.returncode}\n{output[-6000:]}"

    def _write(self, stream, event: SessionEvent) -> None:
        stream.write(json.dumps(event.as_json(), ensure_ascii=False) + "\n")
        stream.flush()

    def _save_prompt(self, index: int, name: str, text: str) -> None:
        directory = self.output.parent / "prompts" / str(index)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")
