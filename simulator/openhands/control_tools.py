"""Typed SDK tools for host-controlled transitions and public delivery."""

import json
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field
from openhands.sdk import Action, Observation, TextContent
from openhands.sdk.tool import ToolDefinition, ToolExecutor, register_tool
from ..state_machine import CONTROL_GUIDANCE, STATE_GUIDANCE


CONTROL_DEADLINE_SECONDS = 175
CONTROL_RESPONSE_GRACE_SECONDS = 2


class ControlRequestError(RuntimeError):
    """A safe, structured error returned by the host control bridge."""

    def __init__(self, message, *, status=None, error_code="CONTROL_UNAVAILABLE", request_id=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.request_id = request_id
        self.retryable = retryable


def request_control(operation, payload, *, top_level=False):
    """Send one control request within the shared bridge deadline."""
    body = {"operation": operation}
    if top_level:
        body.update(payload)
    else:
        body["payload"] = payload
    request = Request(
        "http://127.0.0.1:8789/control",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Request-Timeout": str(CONTROL_DEADLINE_SECONDS),
        },
    )
    try:
        with urlopen(
            request, timeout=CONTROL_DEADLINE_SECONDS + CONTROL_RESPONSE_GRACE_SECONDS
        ) as response:
            raw = response.read(256 * 1024)
            return json.loads(raw)
    except HTTPError as error:
        try:
            raw = error.read(256 * 1024)
            value = json.loads(raw)
            detail = value.get("error", {}) if isinstance(value, dict) else {}
            if not isinstance(detail, dict):
                detail = {}
        except Exception:
            detail = {}
        safe_code = detail.get("error_code", "CONTROL_HTTP_ERROR")
        if not isinstance(safe_code, str) or len(safe_code) > 64:
            safe_code = "CONTROL_HTTP_ERROR"
        request_id = detail.get("request_id")
        if not isinstance(request_id, str) or len(request_id) > 128:
            request_id = None
        raise ControlRequestError(
            "Control bridge request failed",
            status=error.code,
            error_code=safe_code,
            request_id=request_id,
            retryable=detail.get("retryable") is True,
        ) from None
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ControlRequestError(
            "Control bridge is unavailable",
            error_code="CONTROL_UNAVAILABLE",
        ) from error


class StateAction(Action):
    pass


class SessionTaskAction(Action):
    task_id: str = Field(description="Current task ID returned by session_state")


class TransitionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal[
        "RETRIEVE", "UNDERSTAND", "PLAN", "BUILD", "OPERATE", "DEBUG", "EVALUATE"
    ] = Field(description=(
        "One currently reasonable request type. "
        + "; ".join(f"{key}: {value}" for key, value in STATE_GUIDANCE.items())
    ))
    control: Literal["CONTINUE", "REFINE", "CORRECT"] | None = Field(
        default=None,
        description=(
            "Optional semantic suggestion; the host derives the actual control. "
            + "; ".join(f"{key}: {value}" for key, value in CONTROL_GUIDANCE.items())
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=240,
        description="Short, specific reason this option fits the current turn",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Current observation IDs needed by this option",
    )
    context_basis: str | None = Field(
        default=None,
        description=(
            "Exact short excerpt from the current requirement, Code reply, or cited observation; "
            "use only to ground an initial DEBUG"
        ),
    )
    affected_operation: str | None = Field(
        default=None,
        description="Specific operation delegated by OPERATE when current blockers exist",
    )


class TransitionAction(SessionTaskAction):
    candidates: list[TransitionCandidate] = Field(
        min_length=1,
        max_length=7,
        description=(
            "Reason or grounding annotations for genuinely reasonable states. "
            "The host independently enumerates the feasible state/control set, so omitting a state "
            "does not remove it from the draw. Do not pad the list or repeat a state."
        ),
    )


class SendAction(SessionTaskAction):
    permit_id: str = Field(description="Permit returned by request_transition")
    text: str = Field(
        description=(
            "Your own public reply, without reference implementations or internal IDs; "
            "reviewed current-task tests may be shared"
        )
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Full observation IDs supporting inspection/test/browser claims in text; leave empty only when no such result is claimed",
    )
    # Persisted SDK events from the retired boolean API must remain readable.
    # This field is intentionally absent from the model-visible tool schema.
    attach_feedback: bool | None = None

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler(core_schema)
        schema.get("properties", {}).pop("attach_feedback", None)
        return schema


class AcceptAction(SessionTaskAction):
    reason: str
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Checks supporting acceptance; may be empty when trusting the Code Agent report",
    )


class PauseAction(SessionTaskAction):
    reason: str


class HostObservation(Observation):
    result: dict

    @property
    def to_llm_content(self):
        return [TextContent(text=json.dumps(self.result, ensure_ascii=False))]


class HostExecutor(ToolExecutor):
    def __init__(self, operation):
        self.operation = operation

    def __call__(self, action, conversation=None):
        payload = action.model_dump(exclude_none=True)
        payload.pop("kind", None)
        result = request_control(self.operation, payload)
        if result.get("handoff") and conversation:
            conversation.pause()
        return HostObservation(result=result)


def definition(cls, action, operation, description):
    return [
        cls(
            description=description,
            action_type=action,
            observation_type=HostObservation,
            executor=HostExecutor(operation),
        )
    ]


class SessionStateTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(cls, StateAction, "read_state", "Read the current task.")


class RequestTransitionTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(
            cls,
            TransitionAction,
            "transition",
            "Annotate reasonable request types. The host chooses and returns the state, control and permit for this turn. Compose your message for that selected intent; requesting again does not change it.",
        )


class SendReplyTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(
            cls,
            SendAction,
            "send",
            "Send your own message matching the permit's selected state/control. If rejected, correct the message under the same permit. Use [[运行结果]] only when task_result.observation.raw_result_available is true; otherwise relay its summary.",
        )


class AcceptTaskTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(
            cls,
            AcceptAction,
            "accept",
            "Accept the task; the final task ends silently.",
        )


class PauseTaskTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(
            cls, PauseAction, "pause", "Pause the current task with a reason."
        )


CONTROL_TOOLS = [
    SessionStateTool,
    RequestTransitionTool,
    SendReplyTool,
    AcceptTaskTool,
    PauseTaskTool,
]
for tool in CONTROL_TOOLS:
    register_tool(tool.name, tool)
