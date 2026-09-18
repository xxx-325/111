from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommitTarget:
    commit: str
    parent: str
    subject: str
    diff: str


@dataclass
class HiddenGoal:
    target: CommitTarget
    user_goal: str
    acceptance: list[str]
    current_feedback: list[str] = field(default_factory=list)
    initial_state: str = "RETRIEVE"


@dataclass
class AgentTurn:
    text: str
    done: bool = False
    feedback: str = ""
    next_goal: str = ""
    control: str | None = None
    next_state: str = ""


@dataclass
class SessionEvent:
    kind: str
    text: str
    commit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        result = {"kind": self.kind, "text": self.text}
        if self.commit:
            result["commit"] = self.commit
        if self.metadata:
            result["metadata"] = self.metadata
        return result
