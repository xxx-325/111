"""Host orchestration for two persistent OpenHands conversations."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..episode import clone_candidate, save
from ..tasks import prepare, snapshot
from .budget import Budget
from .container import SDKContainer
from .events import private_observation, public_event
from .guard import MessageGuard
from .relay import append, current_context
from .state import TaskState, TransitionError
from .source import project_commit
from .verification import verify
from .policy import role_prompts, policy_record
from .provenance import collect_sources
from .permissions import decide
from .transition_selection import (
    TURN_CACHE_VERSION,
    candidate_fingerprint,
    candidates_sha256,
    enumerate_transition_candidates,
    select_transition,
    transition_turn,
)
from .user_final_fallback import UserFinalFallbackMixin

USER_SYSTEM, CODE_SYSTEM = role_prompts("zh-CN").values()

USER_RESUME_BOOTSTRAP_SCHEMA = "user-public-dialogue-bootstrap-v1"


def pending_user_resume_prompt(saved, private, prompt):
    """Prefix one audited public-history bootstrap to the next User input."""
    record = saved.get("user_resume_bootstrap")
    if not record or record.get("status") == "delivered":
        return prompt, None
    if record.get("schema") != USER_RESUME_BOOTSTRAP_SCHEMA:
        raise RuntimeError("unknown User resume bootstrap schema")
    if record.get("status") != "pending":
        raise RuntimeError("invalid User resume bootstrap status")
    relative = Path(record.get("path", ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("User resume bootstrap must stay under private state")
    private = Path(private).resolve()
    path = (private / relative).resolve()
    if private not in path.parents:
        raise RuntimeError("User resume bootstrap escaped private state")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
        raise RuntimeError("User resume bootstrap digest changed")
    value = json.loads(payload)
    if (
        value.get("schema") != USER_RESUME_BOOTSTRAP_SCHEMA
        or value.get("old_conversation_id") != record.get("old_conversation_id")
        or value.get("new_conversation_id") != record.get("new_conversation_id")
        or not isinstance(value.get("message"), str)
        or not value["message"].startswith("<<RESUMED CONVERSATION>>")
    ):
        raise RuntimeError("User resume bootstrap content is invalid")
    return value["message"] + "\n\n" + prompt, record


def cached_pending_transition(events, saved, state_data, prepare_action=None):
    """Return one unmatched transition only when its accepted draw is cached."""
    observed_actions = {
        event.get("action_id")
        for event in events
        if event.get("kind") == "ObservationEvent"
    }
    observed_calls = {
        event.get("tool_call_id")
        for event in events
        if event.get("kind") in (
            "ObservationEvent",
            "UserRejectObservation",
            "AgentErrorEvent",
        )
    }
    pending = [
        event
        for event in events
        if event.get("kind") == "ActionEvent"
        and event.get("id") not in observed_actions
        and event.get("tool_call_id") not in observed_calls
    ]
    if not pending:
        return None
    if len(pending) != 1:
        raise RuntimeError("resume found multiple unmatched SDK actions")
    event = pending[0]
    if event.get("tool_name") != "request_transition":
        raise RuntimeError("resume found an unknown or side-effecting SDK action")
    action = event.get("action")
    if not isinstance(action, dict) or action.get("task_id") != state_data.get("task_id"):
        raise RuntimeError("pending transition is not bound to the current task")
    action = (prepare_action or copy.deepcopy)(action)
    turn_key, turn_identity = transition_turn(state_data)
    cache = saved.get("transition_selections", {}).get(turn_key)
    if (
        not isinstance(cache, dict)
        or cache.get("version") != TURN_CACHE_VERSION
        or cache.get("turn") != turn_identity
    ):
        raise RuntimeError("pending transition has no current-turn cache")
    submitted = candidates_sha256(action.get("candidates"))
    accepted = [
        attempt
        for attempt in cache.get("attempts", [])
        if attempt.get("candidate_sha256") == submitted
        and attempt.get("result", {}).get("accepted") is True
    ]
    if len(accepted) != 1:
        raise RuntimeError("pending transition has no unique accepted cached result")
    return {
        "action_id": event["id"],
        "tool_call_id": event.get("tool_call_id"),
        "tool_name": event["tool_name"],
    }


class HostPauseRequested(Exception):
    def __init__(self, request_id, reason, requested_at):
        super().__init__(reason)
        self.request_id = request_id
        self.reason = reason
        self.requested_at = requested_at


class OpenHandsEpisode(UserFinalFallbackMixin):
    checkpoint_schema = "openhands-v12-cross-task-final-evidence"

    def __init__(self, config, output, resume=False):
        config = {
            **config,
            "dialogue_language": config.get("dialogue_language", "zh-CN"),
            "dynamic_transition_selection": config.get(
                "dynamic_transition_selection", True
            ),
        }
        if config["dynamic_transition_selection"] is not True:
            raise ValueError("new episodes require dynamic transition selection")
        config["execution_backend"] = config.get("execution_backend", "ssh_sandbox")
        if config["execution_backend"] != "ssh_sandbox":
            raise ValueError("new episodes require independent execution sandboxes")
        if config.get("browser"):
            raise ValueError("browser is not available in the isolated backend")
        from .sandbox import pinned_image

        pinned_image(config.get("execution_image"))
        self.policy = policy_record(
            config["dialogue_language"],
            config.get("delegation_variant", "neutral"),
            config.get("code_prompt_mode", "local"),
        )
        self.lock = threading.RLock()
        self.config, self.root = config, Path(output).resolve()
        self.private = self.root / "private"
        self.checkpoint = self.private / "checkpoint.json"
        self.pause_request = self.private / "pause-request.json"
        self.agents = {}
        self.resume = resume
        if resume:
            self.saved = json.loads(self.checkpoint.read_text())
            if (
                self.saved.get("schema") != self.checkpoint_schema
                or self.saved["config"] != config
                or self.saved.get("policy") != self.policy
            ):
                raise ValueError(
                    "checkpoint version/config mismatch; no automatic migration"
                )
            if self.saved.get("in_flight"):
                role, identifier = (
                    self.saved["in_flight"]["role"],
                    self.saved["in_flight"]["id"],
                )
                result = self.private / role / "outbox" / f"{identifier}.json"
                if (
                    not result.exists()
                    or json.loads(result.read_text()).get("status") == "error"
                ):
                    # One narrow, evidence-checked exception: a Code turn cut short
                    # by a provider 503 or discarded truncation. The failed turn stays on disk,
                    # a source-linked successor command id is used, and the worker
                    # continues the persisted conversation without resending the
                    # user message or replaying any completed tool call.
                    if not self.authorized_provider_503_recovery(role, identifier):
                        raise RuntimeError(
                            "uncertain interrupted SDK call; inspect retained container, do not auto-replay"
                        )
        else:
            self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
            self.private.mkdir(mode=0o700)
            repo, base, tasks = self.prepare_tasks(config)
            snapshot(repo, base, self.root / "workspace/candidate")
            self.saved = dict(
                schema=self.checkpoint_schema,
                config=config,
                policy=self.policy,
                tasks=tasks,
                base=base,
                state=TaskState().data,
                revision=0,
                offsets={"user": 0, "code": 0},
                public=[],
                compression=[],
                code_sources=[],
                transition_selections={},
                in_flight=None,
                next_user_input=None,
                elapsed_seconds=0,
            )
        self.state = TaskState(self.saved["state"])
        remaining = config.get("max_seconds", 1200) - self.saved.get(
            "elapsed_seconds", 0
        )
        self.budget = Budget(
            {**config, "max_seconds": max(0, remaining)},
            self.saved.get("budget"),
            self.private / "budget.json",
        )
        self.started = time.monotonic()
        self.elapsed_before = self.saved.get("elapsed_seconds", 0)
        self.image = subprocess.check_output(
            ["docker", "image", "inspect", config["image"], "--format", "{{.Id}}"],
            text=True,
        ).strip()
        if self.saved.get("image_id", self.image) != self.image:
            raise ValueError("container image changed; refusing resume")
        self.saved["image_id"] = self.image
        self.guard = None
        self.persist()

    def authorized_provider_503_recovery(self, role, identifier):
        """Whether an explicit, evidence-checked provider continuation owns this turn.

        Only the dedicated continuation entry writes ``provider_503_continuation``,
        and only after verifying a 503 or unforwarded output truncation, that
        every tool call is already paired, that no fatal marker exists, and that a
        source-linked successor command id was prepared. Everything else keeps the
        default refusal so an uncertain interrupted call is never silently replayed.
        """
        authorization = self.saved.get("provider_503_continuation") or {}
        return (
            authorization.get("authorized") is True
            and authorization.get("role") == role
            and authorization.get("resumed_command_id") == identifier
            and authorization.get("retained_failed_command_id") != identifier
        )

    def prepare_tasks(self, config):
        return prepare(config, self.private)

    def execution_config(self, role):
        return {
            **self.config.get(role, self.config["user"]),
            "execution_backend": self.config["execution_backend"],
            "execution_image": self.config["execution_image"],
        }

    def persist(self):
        with self.lock:
            self.saved["state"] = self.state.data
            self.saved["budget"] = self.budget.snapshot()
            self.saved["elapsed_seconds"] = (
                self.elapsed_before + time.monotonic() - self.started
            )
            save(self.checkpoint, self.saved)

    def check_pause_request(self):
        if not self.pause_request.exists():
            return
        try:
            request = json.loads(self.pause_request.read_text())
            if (
                set(request) != {"schema", "id", "reason", "requested_at"}
                or request["schema"] != "host-pause-request-v1"
                or not isinstance(request["id"], str)
                or not request["id"]
                or not isinstance(request["reason"], str)
                or not request["reason"].strip()
                or len(request["reason"]) > 1000
                or not isinstance(request["requested_at"], (int, float))
            ):
                raise ValueError("invalid host pause request")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HostPauseRequested(
                "invalid", f"Invalid host pause request: {type(exc).__name__}: {exc}", time.time()
            ) from exc
        raise HostPauseRequested(
            request["id"], request["reason"].strip(), request["requested_at"]
        )

    def record_host_pause(self, request):
        with self.lock:
            reason = "Host pause requested: " + request.reason
            self.state.data.update(status="paused", pause_reason=reason, permit=None)
            self.saved["host_pause"] = {
                "schema": "host-pause-v1",
                "request_id": request.request_id,
                "reason": request.reason,
                "requested_at": request.requested_at,
                "accepted_at": time.time(),
                "in_flight": copy.deepcopy(self.saved.get("in_flight")),
                "freeze": {"status": "pending", "roles": {}},
            }
            self.persist()

        roles = {}
        for role, agent in self.agents.items():
            try:
                agent.pause()
                roles[role] = {"status": "paused"}
            except Exception as exc:
                roles[role] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }

        with self.lock:
            failed = any(item["status"] == "failed" for item in roles.values())
            self.saved["host_pause"]["freeze"] = {
                "status": "failed" if failed else "paused",
                "roles": roles,
                "finished_at": time.time(),
            }
            self.persist()
            if self.pause_request.exists():
                self.pause_request.unlink()

    def requirement(self):
        task = self.saved["tasks"][self.state.data["task_index"]]
        if task["kind"] == "commit":
            if "public_requirement" not in task:
                task["public_requirement"] = project_commit(
                    task, self.agents["user"].relay
                )
                self.persist()
            return task["public_requirement"]
        return {"title": task["title"], "body": task["body"]}

    def user_input(self):
        context = self.state.communication()
        pending_final = self.saved.get("user_final_fallback")
        if (
            isinstance(pending_final, dict)
            and pending_final.get("status") == "awaiting_transition"
            and pending_final.get("task_id") == self.state.data["task_id"]
        ):
            return {
                "task_id": self.state.data["task_id"],
                "current_requirement": self.user_requirement(),
                "retained_draft": pending_final["text"],
                "instruction": (
                    "该普通最终文本已由宿主原样保留。只调用 request_transition 为它申请发送许可；"
                    "不要调用 send_reply，不要重写或重新输出正文。"
                ),
            }
        communication = {
            "stage": context["stage"],
            "last_code_reply": context["last_code_reply"],
        }
        new_task = (
            context["stage"] == "initial_delegation"
            and self.state.data.get("task_index", 0) > 0
        )
        return dict(
            communication=communication,
            current_requirement=self.user_requirement(),
            instruction=(
                self.new_task_instruction()
                if new_task
                else self.initial_instruction()
                if context["stage"] == "initial_delegation"
                else (
                    "自然回应 Code 的上一条消息。若要引用 task_result 的运行结果，在正文相应位置写 "
                    "[[运行结果]]，由宿主替换为原文。"
                )
            ),
        )

    def initial_instruction(self):
        return "直接说出当前问题，让 Code 看一下。"

    def new_task_instruction(self):
        return "直接提出当前需求，无需为上一项任务另作收尾。"

    def user_requirement(self):
        """Remove only a host-generated commit title from User task material."""
        requirement = copy.deepcopy(self.requirement())
        task = self.saved["tasks"][self.state.data["task_index"]]
        if task.get("kind") == "commit":
            requirement.pop("title", None)
        return requirement

    def public(self, item, identifier):
        if any(x["id"] == identifier for x in self.saved["public"]):
            return
        value = dict(
            item,
            id=identifier,
            sequence=len(self.saved["public"]) + 1,
            timestamp=time.time(),
        )
        # Public append may survive a crash before the checkpoint. Reconcile by ID.
        path = self.root / "session.jsonl"
        existing = (
            {json.loads(x)["id"] for x in path.read_text().splitlines()}
            if path.exists()
            else set()
        )
        if identifier not in existing:
            append(path, value)
        self.saved["public"].append(value)

    def collect(self, role):
        rows = self.agents[role].events()
        new_rows = rows[self.saved["offsets"][role] :]
        for event in new_rows:
            if "condens" in event.get("kind", "").lower():
                self.saved["compression"].append(
                    dict(
                        role=role,
                        event_id=event["id"],
                        kind=event["kind"],
                        after_public=len(self.saved["public"]),
                    )
                )
            if role == "code":
                item = public_event(event)
                if item:
                    self.public(item, event["id"])
            else:
                observation = private_observation(event, self.saved["revision"])
                if observation:
                    self.state.data["checks"].append(observation)
                self.saved["code_sources"] = collect_sources(
                    self.saved["code_sources"], [event]
                )
        self.saved["offsets"][role] = len(rows)
        self.persist()
        return new_rows

    def control(self, packet):
        with self.lock:
            packet.get("_request_context", current_context()).check()
            if packet.get("operation") == "authorize_tools":
                actions = packet.get("actions", [])
                result = decide(actions, self.user_run_commands())
                if (
                    self.state.data["phase"] != "user"
                    or self.state.data["status"] != "running"
                ):
                    result = {"accepted": False, "reasons": ["No active User turn"]}
                append(
                    self.private / "approvals.jsonl",
                    dict(
                        request_id=packet["request_id"],
                        task_id=self.state.data["task_id"],
                        revision=self.saved["revision"],
                        actions=actions,
                        result=result,
                    ),
                )
                return result
            return self._control(packet)

    def user_run_commands(self):
        configured = self.config.get("tasks")
        tasks = configured if isinstance(configured, list) else self.saved["tasks"]
        return tasks[self.state.data["task_index"]].get("user_run_commands", [])

    def before_user_turn(self):
        """Optional host preparation, never a replacement agent loop."""

    def prepare_pending_transition(self, action):
        """Reapply deterministic host bindings used by transition review."""
        return copy.deepcopy(action)

    def acceptance_gate(self):
        """Optional mode-specific authority checked before semantic acceptance."""

    def validate_acceptance_intent(self):
        """Optional mode-specific binding between selection and acceptance."""

    def collect_user_sources(self):
        self.collect("user")
        self.saved["code_sources"] = collect_sources(
            self.saved["code_sources"], checks_dir=self.root / "user-workspace/checks"
        )
        self.persist()

    def prepare_send_payload(self, payload):
        if "attach_feedback" in payload:
            raise TransitionError("attach_feedback is unsupported; use [[运行结果]] in text")
        if "[[运行结果]]" in payload.get("text", ""):
            raise TransitionError(
                "this episode has no reviewed Judge feedback for [[运行结果]]"
            )
        return payload, None

    def after_message_published(self, message):
        """Optional mode-specific bookkeeping after a durable public append."""

    def _control(self, packet):
        context = packet.get("_request_context", current_context())
        context.check()
        request_id = packet["request_id"]
        if request_id in self.saved.get("control_results", {}):
            return self.saved["control_results"][request_id]
        self.collect_user_sources()
        operation, payload = packet.get("operation"), packet.get("payload", {})
        before = copy.deepcopy(self.state.data)
        record = {"id": str(uuid.uuid4()), "operation": operation, "payload": payload}
        transition_turn_key = None
        transition_attempt = None
        try:
            if operation == "read_state":
                result = {
                    "accepted": True,
                    "state": self.state.view(),
                    "current_requirement": self.user_requirement(),
                    "user_run_commands": self.user_run_commands(),
                }
            elif operation == "verify":
                self.state.identity(payload)
                checks = verify(
                    self.root / "workspace/candidate",
                    (
                        self.config.get("tasks")
                        if isinstance(self.config.get("tasks"), list)
                        else self.saved["tasks"]
                    )[self.state.data["task_index"]],
                    self.private,
                    self.image,
                    max(1, min(120, self.budget.deadline - time.monotonic())),
                    self.saved["revision"],
                )
                context.check()
                self.state.data["checks"].extend(checks)
                result = {"accepted": True, "checks": checks}
            elif operation in ("transition", "send", "accept", "pause"):
                self.state.identity(payload)
                if operation == "accept" and not self.saved.get("last_code_reply"):
                    raise TransitionError(
                        "there is no Code Agent work/report for this task yet"
                    )
                if operation == "accept":
                    if "send_closing_reply" in payload:
                        raise TransitionError(
                            "send_closing_reply is no longer supported; final acceptance ends silently"
                        )
                    self.validate_acceptance_intent()
                    self.acceptance_gate()
                effective_payload, attachment = payload, None
                if operation == "transition":
                    turn_key, turn_identity = transition_turn(self.state.data)
                    selections = self.saved.setdefault("transition_selections", {})
                    turn_cache = selections.get(turn_key)
                    if turn_cache is not None and (
                        turn_cache.get("version") != TURN_CACHE_VERSION
                        or turn_cache.get("turn") != turn_identity
                        or not isinstance(turn_cache.get("attempts"), list)
                    ):
                        raise TransitionError("transition cache version mismatch")
                    submitted_sha256 = candidates_sha256(payload.get("candidates"))
                    attempts = turn_cache["attempts"] if turn_cache else []
                    transition_attempt = next(
                        (item for item in attempts
                         if item.get("result", {}).get("accepted") is True),
                        None,
                    )
                    if transition_attempt is None:
                        transition_attempt = next(
                            (item for item in attempts if "result" not in item), None
                        )
                    if transition_attempt is None:
                        transition_attempt = next(
                            (item for item in attempts
                             if item.get("candidate_sha256") == submitted_sha256),
                            None,
                        )
                    if transition_attempt is None:
                        rejected = {
                            item.get("selected_fingerprint")
                            or candidate_fingerprint(item["selected"])
                            for item in attempts
                            if item.get("result", {}).get("accepted") is False
                        }
                        requirement = self.requirement()
                        host_candidates = enumerate_transition_candidates(
                            payload.get("candidates"), self.state.data, requirement
                        )
                        transition_attempt = select_transition(
                            host_candidates, self.state.data, requirement,
                            excluded_fingerprints=rejected,
                        )
                        transition_attempt["host_candidate_sha256"] = (
                            transition_attempt["candidate_sha256"]
                        )
                        # Resume and correction idempotency bind to what the
                        # User actually submitted, while the recorded host hash
                        # proves which independently enumerated set was drawn.
                        transition_attempt["candidate_sha256"] = submitted_sha256
                        transition_attempt["request_id"] = request_id
                        if turn_cache is None:
                            turn_cache = {
                                "version": TURN_CACHE_VERSION,
                                "turn": turn_identity,
                                "attempts": [],
                            }
                            selections[turn_key] = turn_cache
                        turn_cache["attempts"].append(transition_attempt)
                        # Persist the draw before semantic review so a timeout,
                        # retry, or replacement request cannot redraw this attempt.
                        self.persist()
                    transition_turn_key = turn_key
                    effective_payload = copy.deepcopy(transition_attempt["selected"])
                    record["selection"] = copy.deepcopy(transition_attempt)
                    record["selected_payload"] = copy.deepcopy(effective_payload)
                    if "result" in transition_attempt:
                        result = copy.deepcopy(transition_attempt["result"])
                        record["result"] = result
                        append(self.private / "controls.jsonl", record)
                        self.saved.setdefault("control_results", {})[
                            request_id
                        ] = result
                        self.persist()
                        return result
                elif operation == "send":
                    effective_payload, attachment = self.prepare_send_payload(payload)
                if attachment:
                    record["attachment"] = attachment
                review_args = (
                    operation,
                    effective_payload,
                    self.state,
                    self.requirement(),
                    [
                        p
                        for p in self.saved["public"]
                        if p["kind"] in ("user", "assistant")
                    ],
                    self.saved["code_sources"],
                )
                supporting_checks = self.user_final_supporting_checks(packet)
                review = (
                    self.guard.review(
                        *review_args, supporting_checks=supporting_checks
                    )
                    if supporting_checks
                    else self.guard.review(*review_args)
                )
                record["review"] = review
                # A late audit is not authority to advance or publish a message.
                context.check()
                if not review["allowed"]:
                    result = {"accepted": False, **review}
                elif (
                    operation == "send"
                    and review.get("handoff_kind") == "internal_plan"
                ):
                    result = dict(
                        accepted=False,
                        handoff=False,
                        retained_private=True,
                        reason="This is a private next-step plan, not a message for Code. Continue privately, provide an available observation, delegate a concrete check, or accept/pause as appropriate.",
                    )
                elif operation == "transition":
                    permit = self.state.transition(effective_payload)
                    result = {
                        "accepted": True,
                        "permit_id": permit["id"],
                        "state": permit["state"],
                        "control": permit["control"],
                        "reason": permit["reason"],
                    }
                    if review.get("warnings"):
                        result["classification_warning"] = review["warnings"][0]
                    pending_final = self.saved.get("user_final_fallback")
                    if (
                        isinstance(pending_final, dict)
                        and pending_final.get("status") == "awaiting_transition"
                        and pending_final.get("task_id") == self.state.data["task_id"]
                    ):
                        result["handoff"] = True
                elif operation == "send":
                    message = self.state.send(effective_payload)
                    if packet.get("_public_message_id"):
                        message["id"] = packet["_public_message_id"]
                    self.public(
                        dict(kind="user", role="user", text=message["text"]),
                        message["id"],
                    )
                    message["published"] = True
                    self.state.record_published_transition(message["transition_id"])
                    self.after_message_published(message)
                    if self.saved.get("closing"):
                        self.state.data.update(status="completed", phase="ended")
                        message["delivery"] = "closing_not_forwarded_to_code"
                    result = {
                        "accepted": True,
                        "message_id": message["id"],
                        "handoff": True,
                    }
                    if review.get("warnings"):
                        warning = review["warnings"][0]
                        result["classification_warning"] = warning
                elif operation == "accept":
                    decision = self.state.accept(payload)
                    if self.state.data["task_index"] + 1 < len(self.saved["tasks"]):
                        selected = set(decision.get("evidence_ids", []))
                        self.saved["accepted_final_context"] = {
                            "schema": "accepted-final-context-v1",
                            "command_id": (self.saved.get("in_flight") or {}).get("id"),
                            "from_task_id": decision["task_id"],
                            "to_task_id": f"task-{self.state.data['task_index'] + 2}",
                            "code_reply_id": (
                                self.state.data.get("code_reply") or {}
                            ).get("id"),
                            "evidence": [
                                {**copy.deepcopy(item), "task_id": decision["task_id"]}
                                for item in self.state.data["checks"]
                                if item.get("id") in selected
                            ],
                        }
                        self.state.release_next()
                        self.saved["last_code_reply"] = None
                        result = {
                            "accepted": True,
                            "decision": decision,
                            "next_requirement": self.user_requirement(),
                            "state": self.state.view(),
                            "instruction": self.new_task_instruction(),
                        }
                    else:
                        self.saved["closing"] = True
                        self.saved["closing_mode"] = "silent"
                        self.state.data.update(status="completed", phase="ended")
                        result = dict(
                            accepted=True,
                            decision=decision,
                            handoff=True,
                            ended=True,
                            instruction="Task accepted; session ended without another public message.",
                        )
                else:
                    self.state.pause(payload)
                    result = {"accepted": True, "handoff": True, "paused": True}
            else:
                raise TransitionError("unsupported operation")
        except TransitionError as exc:
            self.state.data = before
            result = {
                "accepted": False,
                "reason": str(exc),
                "unresolved": self.state.pending_checks(),
                "blockers": self.state.data["blockers"],
            }
        if transition_turn_key is not None:
            transition_attempt["result"] = copy.deepcopy(result)
        record["result"] = result
        append(self.private / "controls.jsonl", record)
        self.saved.setdefault("control_results", {})[request_id] = result
        self.persist()
        return result

    def sync_candidate(self):
        source, destination = (
            self.root / "workspace/candidate",
            self.root / "user-workspace/candidate",
        )
        destination.mkdir(parents=True, exist_ok=True)
        (self.root / "user-workspace/checks").mkdir(exist_ok=True)
        for path in source.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError(
                    "candidate contains links/special files; unsafe host synchronization denied"
                )
        # Exact dedicated destination. Keep its inode so the SDK terminal CWD remains valid.
        subprocess.run(
            ["rsync", "-a", "--delete", str(source) + "/", str(destination) + "/"],
            check=True,
            capture_output=True,
        )

    def turn(self, role, prompt, *, resume_pending_control=None):
        pending = self.saved.get("in_flight")
        identifier = pending["id"] if pending else f"turn-{time.time_ns()}"
        self.saved["in_flight"] = pending or {
            "role": role,
            "id": identifier,
            "public_start": len(self.saved["public"]),
        }
        self.persist()
        self.agents[role].turn(
            prompt,
            command_id=identifier,
            resume_pending_control=resume_pending_control,
        )
        return self.collect(role)

    def resume_pending_user_control(self):
        """Finish an interrupted transition before any fallback publication."""
        if not self.resume:
            return False
        pending_control = cached_pending_transition(
            self.agents["user"].events(),
            self.saved,
            self.state.data,
            self.prepare_pending_transition,
        )
        if not pending_control:
            return False
        self.turn("user", None, resume_pending_control=pending_control)
        # The resumed SDK turn has now recorded its Observation and returned to
        # the host boundary. Publication is safe only after this point.
        self.saved["in_flight"] = None
        self.persist()
        fallback = self.saved.get("user_final_fallback", {})
        if fallback.get("status") in ("awaiting_transition", "sending"):
            self.deliver_user_final_fallback()
            if fallback.get("status") == "awaiting_transition":
                self.state.data.update(
                    status="paused",
                    pause_reason="User Agent stopped without obtaining a transition for retained final",
                )
                self.persist()
        return True

    def run(self):
        if (
            self.saved.get("closing")
            and self.state.data["phase"] == "ended"
            and len(self.state.data["accepted"]) == len(self.saved["tasks"])
        ):
            # A closed episode is idempotent, even if a later diagnostic startup failed.
            return "completed"
        try:
            if not self.resume:
                self.sync_candidate()
            prompts = role_prompts(
                self.config["dialogue_language"],
                self.config.get("code_prompt_mode", "local"),
            )
            for role in ("code", "user"):
                system = prompts[role]
                directory = self.root / (
                    "workspace" if role == "code" else "user-workspace"
                )
                agent = SDKContainer(
                    self.private / role,
                    directory,
                    self.execution_config(role),
                    self.image,
                    role,
                    system,
                    self.budget.deadline,
                    control=self.control if role == "user" else None,
                    browser=self.config.get("browser", False),
                    condenser_max_size=self.config.get("condenser_max_size", 120),
                    budget=self.budget,
                    neutral_tools=self.config.get("delegation_variant", "neutral")
                    == "neutral",
                    pause_check=self.check_pause_request,
                )
                self.agents[role] = agent
            self.guard = MessageGuard(
                self.agents["user"].relay,
                self.saved["tasks"],
                self.config["dialogue_language"],
            )
            for agent in self.agents.values():
                agent.start(resume=self.resume)
            if (
                self.resume
                and self.state.data["status"] == "paused"
                and not self.state.data.get("pause_reason", "").startswith("User Agent")
            ):
                self.state.data["status"] = "running"
            while self.state.data["status"] == "running":
                self.check_pause_request()
                if time.monotonic() >= self.budget.deadline:
                    raise TimeoutError("run time limit reached")
                role = (self.saved.get("in_flight") or {}).get(
                    "role"
                ) or self.state.data["phase"]
                if role == "user":
                    if self.resume_pending_user_control():
                        continue
                    if self.deliver_user_final_fallback():
                        self.saved["in_flight"] = None
                        self.persist()
                        continue
                    fallback = self.saved.get("user_final_fallback", {})
                    if (
                        fallback.get("status") in ("rejected", "retained_private")
                        and self.state.data["phase"] == "user"
                    ):
                        self.state.data.update(
                            status="paused",
                            pause_reason="Retained User final was not eligible for public delivery",
                        )
                        self.persist()
                        break
                    self.before_user_turn()
                    if self.state.data["status"] != "running":
                        break
                    prompt = json.dumps(self.user_input(), ensure_ascii=False)
                    prompt, resume_bootstrap = pending_user_resume_prompt(
                        self.saved, self.private, prompt
                    )
                    fallback_waiting = (
                        self.saved.get("user_final_fallback", {}).get("status")
                        == "awaiting_transition"
                    )
                    user_events = self.turn("user", prompt)
                    if resume_bootstrap is not None:
                        resume_bootstrap.update(
                            status="delivered",
                            delivered_command_id=self.saved["in_flight"]["id"],
                        )
                        self.persist()
                    self.saved["next_user_input"] = None
                    self.retain_user_final(user_events)
                    self.deliver_user_final_fallback()
                    if (
                        fallback_waiting
                        and self.saved.get("user_final_fallback", {}).get("status")
                        == "awaiting_transition"
                    ):
                        self.state.data.update(
                            status="paused",
                            pause_reason="User Agent stopped without obtaining a transition for retained final",
                        )
                    if (
                        self.state.data["phase"] == "user"
                        and self.state.data["status"] == "running"
                        and self.saved.get("user_final_fallback", {}).get("status")
                        != "awaiting_transition"
                    ):
                        self.state.data.update(
                            status="paused",
                            pause_reason="User Agent stopped without a public send or task decision",
                        )
                elif role == "code":
                    message = self.state.data["messages"][-1]
                    # Stable command ID across uncertain delivery boundaries.
                    if not self.saved.get("in_flight"):
                        self.saved["in_flight"] = {
                            "role": "code",
                            "id": "turn-" + message["id"],
                            "public_start": len(self.saved["public"]),
                        }
                    before = self.saved["in_flight"]["public_start"]
                    self.turn("code", message["text"])
                    message["delivered"] = True
                    finals = [
                        p["text"]
                        for p in self.saved["public"][before:]
                        if p["kind"] == "assistant"
                        and p.get("phase") == "final"
                        and p.get("text", "").strip()
                    ]
                    if not finals:
                        raise RuntimeError(
                            "SDK stopped without a public final reply; not interpreted as completion"
                        )
                    self.saved["last_code_reply"] = finals[-1]
                    self.saved["revision"] += 1
                    final_item = next(
                        p
                        for p in reversed(self.saved["public"][before:])
                        if p["kind"] == "assistant" and p.get("phase") == "final"
                    )
                    self.state.data["code_reply"] = dict(
                        id=final_item["id"], text=finals[-1], stopped=True
                    )
                    self.state.data["checks"].append(
                        dict(
                            id=final_item["id"],
                            revision=self.saved["revision"],
                            result="reported",
                            tool="code_report",
                            summary=finals[-1],
                        )
                    )
                    self.agents["code"].pause()
                    try:
                        self.sync_candidate()
                    finally:
                        self.agents["code"].unpause()
                    self.state.data["phase"] = "user"
                    self.saved["next_user_input"] = json.dumps(
                        {
                            "code_agent_stopped": True,
                            "final_reply": finals[-1],
                            "candidate_updated": True,
                        },
                        ensure_ascii=False,
                    )
                self.saved["in_flight"] = None
                self.persist()
        except HostPauseRequested as exc:
            self.record_host_pause(exc)
        except (Exception, KeyboardInterrupt) as exc:
            with self.lock:
                self.state.data.update(
                    status="paused", pause_reason=type(exc).__name__ + ": " + str(exc)
                )
                self.persist()
        finally:
            close_errors = {}
            for role, agent in self.agents.items():
                try:
                    agent.close()
                except Exception as exc:
                    close_errors[role] = f"{type(exc).__name__}: {exc}"
            if close_errors and self.saved.get("host_pause"):
                self.saved["host_pause"]["cleanup"] = {
                    "status": "failed",
                    "errors": close_errors,
                    "finished_at": time.time(),
                }
            elif close_errors:
                raise RuntimeError("SDK container cleanup failed: " + repr(close_errors))
            self.persist()
        return self.state.data["status"]
