"""Deterministically insert already-reviewed Judge feedback into User text."""

from .feedback_projection import authorized_raw_values
from .state import TransitionError


FEEDBACK_PLACEHOLDER = "[[运行结果]]"


def attach_feedback(text, experience, *, revision, candidate_version):
    """Return final public text and minimal authority metadata."""
    if not isinstance(text, str) or not text.strip():
        raise TransitionError("feedback insertion requires User-authored text")
    occurrences = text.count(FEEDBACK_PLACEHOLDER)
    if occurrences == 0:
        return text, None
    if occurrences != 1:
        raise TransitionError("public reply must contain exactly one [[运行结果]] placeholder")
    if not isinstance(experience, dict):
        raise TransitionError("no reviewed current execution feedback is available")
    if (
        experience.get("revision") != revision
        or experience.get("candidate_version") != candidate_version
        or experience.get("executor") != "Judge"
        or experience.get("operation_details_allowed") is not False
    ):
        raise TransitionError("execution feedback authority is stale or invalid")
    observation = experience.get("observation", {})
    kind = observation.get("kind")
    if kind not in ("runtime_error", "wrong_output"):
        raise TransitionError("only raw runtime error or wrong output can be attached")
    values = authorized_raw_values(observation)
    if (len(values) != 2 or not all(isinstance(value, str) for value in values)
            or not values[0] or (kind == "runtime_error" and not values[1])):
        raise TransitionError("reviewed execution feedback is incomplete")
    if any(
        marker in value
        for value in values
        for marker in ("/workspace/checks", "/reference", "/workspace/experiments")
    ):
        raise TransitionError("private Judge evidence cannot be attached")
    result_label = "报错" if kind == "runtime_error" else "输出"
    replacement = f"输入：\n{values[0]}\n{result_label}：\n{values[1]}"
    final_text = text.replace(FEEDBACK_PLACEHOLDER, replacement, 1)
    return final_text, {
        "summary_id": experience.get("summary_id"),
        "revision": revision,
        "candidate_version": candidate_version,
        "final_text": final_text,
    }
