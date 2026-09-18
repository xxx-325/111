from __future__ import annotations

import random
from dataclasses import dataclass

CORE_STATES = (
    "RETRIEVE",
    "UNDERSTAND",
    "PLAN",
    "BUILD",
    "OPERATE",
    "DEBUG",
    "EVALUATE",
)
CONTROL_EVENTS = ("CONTINUE", "REFINE", "CORRECT")

# Approximate priors from the reviewed real sessions. These are simulation
# priors, not a fitted statistical model or ground-truth annotation.
TRANSITIONS: dict[str, dict[str, float]] = {
    "RETRIEVE": {"RETRIEVE": .72, "UNDERSTAND": .10, "PLAN": .07, "EVALUATE": .06, "DEBUG": .05},
    "UNDERSTAND": {"UNDERSTAND": .50, "PLAN": .20, "BUILD": .15, "DEBUG": .08, "RETRIEVE": .07},
    "PLAN": {"PLAN": .55, "BUILD": .20, "EVALUATE": .13, "UNDERSTAND": .07, "RETRIEVE": .05},
    "BUILD": {"BUILD": .50, "OPERATE": .25, "DEBUG": .15, "EVALUATE": .10},
    "OPERATE": {"OPERATE": .50, "DEBUG": .30, "EVALUATE": .20},
    "DEBUG": {"DEBUG": .69, "BUILD": .14, "OPERATE": .10, "EVALUATE": .07},
    "EVALUATE": {"PLAN": .35, "BUILD": .25, "RETRIEVE": .20, "UNDERSTAND": .20},
}

STATE_GUIDANCE = {
    "RETRIEVE": "确认现状、位置、已有信息或当前结果，再决定下一步。",
    "UNDERSTAND": "追问原因、机制或代码之间的关系，避免无依据地要求修改。",
    "PLAN": "围绕当前目标收敛方案、实施步骤、编码策略、实现约束和验收方式，不新增业务功能。",
    "BUILD": "明确要求编写或修改实际实现；只讨论策略或约束不属于 BUILD。",
    "OPERATE": "要求运行、启动、部署或执行，并关注真实结果。",
    "DEBUG": "基于实际错误或失败结果给出可操作的排查反馈。",
    "EVALUATE": "检查当前结果是否满足目标，指出差距或确认完成。",
}

CONTROL_GUIDANCE = {
    "CONTINUE": "延续当前主要请求。",
    "REFINE": "补充或澄清当前主要请求，包括补充 Code 已公开方案的任务内策略或约束。",
    "CORRECT": "依据新事实、失败结果或用户明确纠偏更正当前请求。",
}

REQUEST_SEMANTICS = {"states": STATE_GUIDANCE, "controls": CONTROL_GUIDANCE}


@dataclass
class UserState:
    current: str = "RETRIEVE"
    last_control: str | None = None


def sample_next(current: str, rng: random.Random) -> str:
    choices = TRANSITIONS.get(current, {"RETRIEVE": 1.0})
    value = rng.random()
    for state, probability in choices.items():
        value -= probability
        if value <= 0:
            return state
    return next(iter(choices))


def guidance(state: str) -> str:
    return STATE_GUIDANCE.get(state, "围绕当前目标提出自然的下一步请求。")


def validate_state(state: str, fallback: str) -> str:
    return state if state in CORE_STATES else fallback


def validate_control(control: str | None) -> str | None:
    return control if control in CONTROL_EVENTS else None
