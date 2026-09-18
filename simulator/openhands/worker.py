"""Container-side persistent SDK Conversation; OpenHands owns the agent loop."""

import json
import subprocess
import time
import traceback
import uuid
from pathlib import Path

from pydantic import Field, SecretStr
from openhands.sdk import Action, Agent, Conversation, LLM, Observation, TextContent
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.sdk.tool import Tool, ToolDefinition, ToolExecutor, register_tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.browser_use import BrowserToolSet
from .control_tools import CONTROL_TOOLS, request_control
from .judge_tools import JUDGE_TOOLS
from .tool_wording import tool_specs
from openhands.sdk.security.confirmation_policy import AlwaysConfirm
from openhands.sdk.conversation.state import ConversationState


class ControlAction(Action):
    operation: str = Field(
        description="read_state, transition, send, accept, pause, or verify"
    )
    payload: dict = Field(
        default_factory=dict, description="Arguments required by the host operation"
    )


class ControlObservation(Observation):
    result: dict

    @property
    def to_llm_content(self):
        return [TextContent(text=json.dumps(self.result, ensure_ascii=False))]


class ControlExecutor(ToolExecutor):
    def __call__(self, action, conversation=None):
        result = request_control(action.operation, action.payload)
        if result.get("handoff") and conversation is not None:
            conversation.pause()
        return ControlObservation(result=result)


class SessionControlTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return [
            cls(
                description="Read authoritative host state; request a task transition; send a public user message; accept or pause the current task. Only an accepted send is public. Read state for required fields.",
                action_type=ControlAction,
                observation_type=ControlObservation,
                executor=ControlExecutor(),
            )
        ]


register_tool(SessionControlTool.name, SessionControlTool)


def validate_pending_control_resume(conversation, expected):
    """Fail closed before resuming one host-approved control action."""
    pending = ConversationState.get_unmatched_actions(
        conversation.state.active_branch()
    )
    if len(pending) != 1:
        raise RuntimeError("pending control resume requires exactly one SDK action")
    action = pending[0]
    identity = {
        "action_id": str(action.id),
        "tool_call_id": action.tool_call_id,
        "tool_name": action.tool_name,
    }
    if identity != expected:
        raise RuntimeError("pending SDK action differs from host-approved control")
    if action.tool_name != "request_transition":
        raise RuntimeError("only cached request_transition may resume without a new message")


def prepare_sdk_turn(conversation, command):
    """Prepare exactly one SDK turn without crossing a pending tool boundary."""
    pending_control = command.get("resume_pending_control")
    if pending_control:
        if command.get("message") or command.get("condense"):
            raise RuntimeError(
                "pending control resume cannot include a new message"
            )
        validate_pending_control_resume(conversation, pending_control)
        return
    if command.get("condense"):
        conversation.condense()
    if command.get("message"):
        conversation.send_message(command["message"])


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def main():
    config = json.loads(Path("/inbox/config.json").read_text())
    bridge = subprocess.Popen(["python", "-m", "simulator.native_http"])
    events = Path("/outbox/events.jsonl")

    def callback(event):
        with events.open("a") as stream:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()
        if config.get("browser") and event.__class__.__name__ == "SystemPromptEvent":
            if not any(
                getattr(t, "name", "") == "browser_navigate" for t in event.tools
            ):
                raise RuntimeError(
                    "Required SDK browser tools are unavailable; refusing silent fallback"
                )

    llm = LLM(
        model="openai/" + config["model"],
        api_key=SecretStr("local-relay-only"),
        base_url="http://127.0.0.1:8789/v1",
        stream=False,
        num_retries=0,
        max_input_tokens=65536,
        max_output_tokens=4096,
        disable_vision=True,
        temperature=config.get("temperature", 0.3),
        usage_id=config["role"],
    )
    if config.get("execution_backend") == "ssh_sandbox":
        if config.get("browser"):
            raise RuntimeError("browser has no isolated executor")
        from .remote_tools import remote_tool_specs

        tools = (
            []
            if config["role"] == "user"
            else remote_tool_specs(
                config["role"],
                candidate_pythonpath=config.get("candidate_pythonpath"),
                **config["remote_tools"]
            )
        )
    elif config.get("execution_backend") == "shared_diagnostic":
        tools = tool_specs(
            config["role"],
            config.get("neutral_tools", True),
            config.get("candidate_pythonpath"),
        )
    else:
        raise RuntimeError("unknown execution policy; no local fallback")
    if config.get("browser"):
        tools.append(Tool(name=BrowserToolSet.name))
    if config.get("control"):
        if config["role"] in ("user", "judge"):
            tools.extend(
                Tool(name=t.name)
                for t in (CONTROL_TOOLS if config["role"] == "user" else JUDGE_TOOLS)
            )
        else:
            tools.append(Tool(name=SessionControlTool.name))
    agent = Agent(
        llm=llm,
        tools=tools,
        system_prompt=config.get("system"),
        include_default_tools=(
            [] if config.get("control") else ["FinishTool", "ThinkTool"]
        ),
        tool_concurrency_limit=1,
        condenser=LLMSummarizingCondenser(
            llm=llm.model_copy(update={"usage_id": "condenser"}),
            max_size=config.get("condenser_max_size", 120),
            keep_first=2,
        ),
    )
    conversation = Conversation(
        agent=agent,
        workspace="/workspace/candidate",
        persistence_dir="/sdk",
        conversation_id=uuid.UUID(config["conversation_id"]),
        callbacks=[callback],
        visualizer=None,
        delete_on_close=False,
        max_iteration_per_run=1000000,
    )
    if config["role"] == "user":
        conversation.set_confirmation_policy(AlwaysConfirm())
    write(Path("/outbox/ready.json"), {"conversation_id": str(conversation.id)})
    try:
        while True:
            for path in sorted(Path("/inbox").glob("turn-*.json")):
                result_path = Path("/outbox") / path.name
                if result_path.exists():
                    continue
                command = json.loads(path.read_text())
                write(
                    Path("/outbox/active.json"),
                    {"command_id": path.stem, "status": "in_flight"},
                )
                try:
                    prepare_sdk_turn(conversation, command)
                    if (
                        config.get("browser")
                        and "browser_navigate" not in conversation.agent.tools_map
                    ):
                        raise RuntimeError(
                            "Required browser tool missing after SDK initialization"
                        )
                    conversation.run()
                    if Path("/sdk/remote-tools/fatal.json").exists():
                        raise RuntimeError(
                            "uncertain tool execution; saved without retry"
                        )
                    while config[
                        "role"
                    ] == "user" and "WAITING_FOR_CONFIRMATION" in str(
                        conversation.state.execution_status
                    ):
                        pending = ConversationState.get_unmatched_actions(
                            conversation.state.active_branch()
                        )
                        approval = request_control(
                            "authorize_tools",
                            {
                                "actions": [
                                    dict(
                                        id=e.id,
                                        tool_name=e.tool_name,
                                        action=e.action.model_dump(),
                                    )
                                    for e in pending
                                ]
                            },
                            top_level=True,
                        )
                        write(
                            Path("/outbox/approval-latest.json"),
                            dict(actions=[e.id for e in pending], decision=approval),
                        )
                        if not approval.get("accepted"):
                            conversation.reject_pending_actions(
                                "; ".join(approval.get("reasons", ["Denied"]))
                            )
                        conversation.run()
                    write(
                        result_path,
                        {
                            "status": str(conversation.state.execution_status),
                            "command_id": path.stem,
                        },
                    )
                except Exception as exc:
                    write(
                        result_path,
                        {
                            "status": "error",
                            "error_type": type(exc).__name__,
                            "traceback": traceback.format_exc(),
                        },
                    )
                write(
                    Path("/outbox/active.json"),
                    {"command_id": path.stem, "status": "stopped"},
                )
            time.sleep(0.1)
    finally:
        conversation.close()
        bridge.terminate()


if __name__ == "__main__":
    main()
