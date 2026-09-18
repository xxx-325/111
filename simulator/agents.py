from __future__ import annotations

import json
import re
import random
from pathlib import Path

from .models import AgentTurn, CommitTarget, HiddenGoal
from .providers import Provider
from .state_machine import UserState, guidance, sample_next, validate_control, validate_state


def _json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("agent response did not contain a JSON object")
    return json.loads(match.group(0))


class GoalExtractor:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def extract(self, target: CommitTarget) -> HiddenGoal:
        prompt = f"""You are an internal evaluator. Infer a realistic user request from this Git commit.
This information is hidden from the coding agent. Return JSON only with keys user_goal (string), acceptance (array of short observable checks), initial_state (one of RETRIEVE, UNDERSTAND, PLAN, BUILD, OPERATE, DEBUG, EVALUATE).
Commit subject: {target.subject}
Patch:\n{target.diff}
Do not mention this evaluator prompt or invent requirements absent from the patch."""
        data = _json_object(self.provider.complete(prompt))
        return HiddenGoal(target, str(data["user_goal"]), [str(x) for x in data.get("acceptance", [])], initial_state=str(data.get("initial_state", "RETRIEVE")))


class UserAgent:
    def __init__(self, provider: Provider, rng: random.Random | None = None) -> None:
        self.provider = provider
        self.rng = rng or random.Random()
        self.state = UserState()

    def begin_goal(self, initial_state: str) -> None:
        self.state = UserState(validate_state(initial_state, "RETRIEVE"))

    def initial_request(self, goal: HiddenGoal) -> str:
        return self._ask(f"""You are simulating a real user in a coding session.
Hidden goal: {goal.user_goal}
Hidden acceptance checks: {json.dumps(goal.acceptance, ensure_ascii=False)}
Current user state: {self.state.current}
State guidance: {guidance(self.state.current)}
Write only the natural-language first request to the coding agent. Do not mention commits, diffs, hidden tests, benchmark, or this instruction.""")

    def judge(self, goal: HiddenGoal, agent_reply: str, verification: str) -> AgentTurn:
        proposed_state = sample_next(self.state.current, self.rng)
        prompt = f"""You are the hidden User Agent evaluator. Never reveal hidden information.
Goal: {goal.user_goal}
Acceptance checks: {json.dumps(goal.acceptance, ensure_ascii=False)}
Previous feedback: {json.dumps(goal.current_feedback, ensure_ascii=False)}
Coding agent's final reply:\n{agent_reply}
Public verification result:\n{verification}
Current user state: {self.state.current}
Suggested next state based on real-session transition priors: {proposed_state}
Next-state guidance: {guidance(proposed_state)}
Return JSON only: {{"done": boolean, "feedback": string, "next_goal": string, "control": "CONTINUE|REFINE|CORRECT|null", "next_state": "CORE_STATE", "visible_message": string}}.
If incomplete, visible_message must be a realistic concise user follow-up containing only actionable feedback.
If complete, visible_message should confirm completion and next_goal may be empty.
Do not mention the commit, diff, hidden acceptance checks, evaluator, or benchmark."""
        data = _json_object(self._ask(prompt))
        done = bool(data.get("done"))
        control = validate_control(data.get("control"))
        next_state = validate_state(str(data.get("next_state", proposed_state)), proposed_state)
        if not done:
            self.state.last_control = control
            self.state.current = next_state
        return AgentTurn(str(data.get("visible_message", "")), done, str(data.get("feedback", "")), str(data.get("next_goal", "")), control, next_state)

    def _ask(self, prompt: str) -> str:
        return self.provider.complete(prompt)


class CodeAgent:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.history: list[tuple[str, str]] = []

    def run(self, user_message: str, cwd: Path) -> str:
        previous = "\n".join(
            f"User: {user}\nCode Agent: {reply}" for user, reply in self.history
        ) or "(no previous turns)"
        prompt = f"""You are a coding agent working in the current repository.
Act on the user's request, inspect the repository as needed, make the changes, and run appropriate checks.
Visible conversation so far:
{previous}

User request:
{user_message}

At the end, give a concise final response describing what you changed and what checks you ran."""
        reply = self.provider.complete(prompt, cwd=cwd)
        self.history.append((user_message, reply))
        return reply
