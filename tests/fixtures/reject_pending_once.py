"""Reject one verified stale SDK approval without running the conversation."""

import argparse
import json
from pathlib import Path


class RecoveryComplete(SystemExit):
    """Stop worker.main immediately after the verified SDK mutation."""


def _write_once(path, value):
    if path.exists():
        raise FileExistsError(f"recovery receipt already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary recovery receipt already exists: {temporary}")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def _reject_stale(conversation, conversation_state, expected):
    if str(conversation.id) != expected["conversation_id"]:
        raise RuntimeError("unexpected conversation; no action rejected")
    execution_status = str(conversation.state.execution_status)
    if "WAITING_FOR_CONFIRMATION" not in execution_status:
        raise RuntimeError("conversation is not waiting for confirmation")
    pending = conversation_state.get_unmatched_actions(
        conversation.state.active_branch()
    )
    if len(pending) != 1:
        raise RuntimeError("expected exactly one pending action")
    event = pending[0]
    action = event.action
    if (
        str(event.id) != expected["action_id"]
        or event.tool_name != "accept_task"
        or getattr(action, "task_id", None) != "task-3"
    ):
        raise RuntimeError("pending action identity differs; no action rejected")
    conversation.reject_pending_actions(expected["reason"])
    remaining = conversation_state.get_unmatched_actions(
        conversation.state.active_branch()
    )
    if remaining:
        raise RuntimeError("pending action rejection did not settle SDK state")
    return {
        "conversation_id": str(conversation.id),
        "rejected_action_id": str(event.id),
        "tool_name": event.tool_name,
        "task_id": action.task_id,
        "pending_after": 0,
        "model_run": False,
    }


def recover(expected, receipt):
    if receipt.exists():
        raise FileExistsError(f"recovery receipt already exists: {receipt}")
    from simulator.openhands import worker

    original_conversation = worker.Conversation

    def intercept_conversation(*args, **kwargs):
        conversation = original_conversation(*args, **kwargs)
        result = _reject_stale(conversation, worker.ConversationState, expected)
        _write_once(receipt, result)
        conversation.close()
        raise RecoveryComplete(0)

    worker.Conversation = intercept_conversation
    try:
        worker.main()
    except RecoveryComplete as complete:
        if complete.code != 0:
            raise
    finally:
        worker.Conversation = original_conversation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    expected = {
        "conversation_id": args.conversation_id,
        "action_id": args.action_id,
        "reason": (
            "Task 3 acceptance already committed by host; this stale duplicate is "
            "not executed. Read current task 4."
        ),
    }
    recover(expected, args.receipt)


if __name__ == "__main__":
    main()
