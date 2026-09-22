"""Opt-in progressive issue orchestration over persistent SDK conversations."""

import hashlib
import copy
import json
import shutil
import subprocess
import time
from pathlib import Path

from ..episode import save
from ..tasks import snapshot, prepare
from .container import SDKContainer
from .episode import OpenHandsEpisode
from .events import private_observation
from .issue_stages import prepare_issue, visible_requirement, validate, SCHEMA
from .judge import (
    SYSTEM,
    bind_submission,
    candidate_hash,
    validate_verdict,
    validate_verdict_core,
    review_verdict,
    assessment_basis,
)
from .feedback_projection import (
    PublicFeedbackError,
    feedback_units_failure_key,
    has_public_feedback_source,
    project_latest_feedback,
    has_projectable_execution_blocks,
    validate_public_feedback,
    feedback_projection_mapping,
)
from .feedback_attachment import attach_feedback
from .disclosure import (initial_release, release_after, release_feedback,
                          released_feedback_unit)
from .relay import Relay, append, current_context
from .state import TransitionError
from .user_projection import UserViewMixin
from .simulated_experience import build_experience
from .requirement_scope import prepare_document, validate_scope
from .commit_preparation import validate_projection


PROGRESSIVE_POLICY_FILES = (
    "progressive.py",
    "judge.py",
    "judge_tools.py",
    "feedback_projection.py",
    "feedback_attachment.py",
    "issue_stages.py",
    "disclosure.py",
    "fragment_text.py",
    "user_projection.py",
    "container.py",
    "../tasks.py",
    "../task_source.py",
    "requirement_scope.py",
    "commit_preparation.py",
    "source.py",
    "sandbox.py",
    "snapshot_volume.py",
    "remote_tools.py",
    "remote_safety.py",
    "evo_tests.py",
    "../swe_chain_evo.py",
)

POST_SOLVED_FOLLOWUP_SCHEMA = "post-solved-followup-v1"
SOLVED_VERDICT_CARRY_SCHEMA = "solved-verdict-carry-v1"
CARRYABLE_POST_SOLVED_STATES = frozenset(("RETRIEVE", "UNDERSTAND", "PLAN"))


def progressive_config(config):
    return dict(
        config,
        _progressive_policy={
            name: hashlib.sha256(
                (Path(__file__).parent / name).read_bytes()
            ).hexdigest()
            for name in PROGRESSIVE_POLICY_FILES
        },
    )


def bind_current_judge_evidence(packet, current):
    """Replace private Judge event IDs with the current host summary authority."""
    identifier = current.get("applied_job")
    private_ids = set(
        (current.get("simulated_experience") or {}).get("evidence_ids", [])
    )
    payloads = packet["payload"].get("candidates")
    if not isinstance(payloads, list):
        payloads = [packet["payload"]]
    for payload in payloads:
        identifiers = payload.setdefault("evidence_ids", [])
        identifiers[:] = [item for item in identifiers if item not in private_ids]
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
        for field in ("resolves", "dismisses", "clears_blockers"):
            values = payload.get(field)
            if isinstance(values, list):
                payload[field] = [item for item in values if item not in private_ids]
        blocker = payload.get("blocker")
        if isinstance(blocker, dict) and isinstance(blocker.get("evidence_ids"), list):
            blocker["evidence_ids"] = [
                item for item in blocker["evidence_ids"] if item not in private_ids
            ]
            if identifier and identifier not in blocker["evidence_ids"]:
                blocker["evidence_ids"].append(identifier)


def attach_pending_judge_policy_notice(saved, prompt, command_id):
    """Bind one recorded policy update to the next normal Judge command."""
    notice = saved.get("judge_policy_notice")
    if not notice or notice.get("delivered_command_id"):
        return False
    assigned = notice.get("assigned_command_id")
    if assigned and assigned != command_id:
        raise RuntimeError("Judge policy notice is bound to another command")
    notice["assigned_command_id"] = command_id
    prompt["policy_update"] = notice["text"]
    return True


def plan_turn_disclosure(current, payload):
    """Choose at most one new issue fragment or Judge feedback unit."""
    before = list(current["released"])
    explicit_question = bool(payload.get("requested_fragment_ids"))
    feedback_release = release_feedback(
        payload.get("public_feedback", {}),
        current.get("released_feedback_unit_ids", []),
        feedback_units_failure_key(current.get("feedback_units", [])),
    )
    if explicit_question:
        known = {unit["id"] for unit in feedback_release["units"]}
        feedback_release["released_unit_ids"] = [
            identifier for identifier in current.get("released_feedback_unit_ids", [])
            if identifier in known
        ]
        feedback_release["added"] = []
        target = release_after(current["plan"], before, payload)
    elif feedback_release["added"]:
        target = before
    else:
        target = release_after(current["plan"], before, payload)
    return target, feedback_release


class ProgressiveEpisode(UserViewMixin, OpenHandsEpisode):
    checkpoint_schema = "openhands-progressive-v20-cross-task-final-evidence"

    def prepare_tasks(self, config):
        return prepare(config, self.private, include_patch=False)

    def __init__(self, config, output, resume=False):
        config = progressive_config(config)
        assessment_basis(config.get("verification_mode", "default"))
        super().__init__(config, output, resume)
        if not config.get("progressive_issues"):
            raise ValueError("progressive mode must be enabled")
        self.saved.setdefault(
            "progressive",
            dict(
                tasks={},
                job=None,
                decisions={},
                control_results={},
                feedback_revision=None,
            ),
        )
        self.progress = self.saved["progressive"]
        self.progress.setdefault("feedback_revision", None)
        self.persist()

    def current(self):
        key = self.state.data["task_id"]
        if key not in self.progress["tasks"]:
            task = self.saved["tasks"][self.state.data["task_index"]]
            issue = {k: task[k] for k in ("title", "body")}
            directory = self.private / "disclosure" / key
            directory.mkdir(parents=True, exist_ok=True)
            original = directory / "issue.json"
            if not original.exists():
                save(original, issue)
            cached = directory / "preparation.json"
            if cached.exists():
                result = json.loads(cached.read_text())
            elif self.config.get("prepared_issues"):
                prepared = json.loads(Path(self.config["prepared_issues"]).read_text())
                case = prepared["cases"][self.state.data["task_index"]]
                if case["issue"] != issue:
                    raise ValueError(
                        "cached decomposition source differs from current original issue"
                    )
                result = {k: case[k] for k in ("plan", "review")}
                if "scope" in case:
                    result["scope"] = case["scope"]
                if "projection" in case:
                    result["projection"] = case["projection"]
                save(cached, result)
            else:
                if task["kind"] == "commit":
                    raise ValueError(
                        "commit tasks require a reviewed preparation report"
                    )
                if (directory / "started.json").exists():
                    raise RuntimeError(
                        "uncertain decomposition call; inspect retained input/output, no automatic retry"
                    )
                save(
                    directory / "started.json",
                    dict(
                        schema=SCHEMA,
                        issue_sha256=hashlib.sha256(
                            json.dumps(issue).encode()
                        ).hexdigest(),
                    ),
                )
                relay = Relay(
                    self.config.get("decomposer", self.config["user"]),
                    directory,
                    directory / "provider.jsonl",
                    deadline=self.budget.deadline,
                    budget=self.budget,
                    role="decomposer",
                )
                result = prepare_document(relay, issue, task["kind"])
                save(cached, result)
            if (
                result.get("plan", {}).get("schema") != SCHEMA
                or result.get("review", {}).get("allowed") is not True
            ):
                raise RuntimeError(
                    "decomposition rejected; inspect private disclosure, do not regenerate"
                )
            scope = result.get("scope")
            if task["kind"] == "pull_request" and not scope:
                raise ValueError(
                    "PR task requires an approved current requirement scope"
                )
            requirement_document = validate_scope(scope, issue) if scope else issue
            if task["kind"] == "commit":
                repo = Path(self.config["repository"]).expanduser()
                if not repo.is_dir():
                    repo = self.private / "source"
                requirement_document = validate_projection(
                    result.get("projection", {}), task, repo
                )
            rebuilt = validate({"items": result["plan"]["items"]}, requirement_document)
            if rebuilt != result["plan"]:
                raise ValueError("cached decomposition order or content is invalid")
            released = initial_release(result["plan"])
            self.progress["tasks"][key] = dict(
                plan=result["plan"],
                released=released,
                feedback=None,
                verdict=None,
                feedback_units=[],
                released_feedback_unit_ids=[],
                requirement_document=requirement_document,
                release_history=[
                    dict(reason="initial", added=released, after=released)
                ],
            )
            self.persist()
        current = self.progress["tasks"][key]
        # Older checkpoints predate Judge feedback units.  Keep them resumable
        # while ensuring no full public_feedback object leaks through User.
        current.setdefault("feedback_units", [])
        current.setdefault("released_feedback_unit_ids", [])
        current.setdefault("post_solved_followup", None)
        current.setdefault("verdict_carries", [])
        return self.progress["tasks"][key]

    def judgment_task(self):
        task = self.saved["tasks"][self.state.data["task_index"]]
        return dict(
            task,
            verification_mode=self.config.get("verification_mode", "default"),
            **self.current().get(
                "requirement_document", {k: task[k] for k in ("title", "body")}
            ),
        )

    def requirement(self):
        current = self.current()
        return visible_requirement(current["plan"], current["released"])

    def user_run_commands(self):
        # Configured commands can themselves disclose withheld reproduction details.
        # Only expose them once all original issue information has been released.
        current = self.current()
        if set(current["released"]) != {
            item["id"] for item in current["plan"]["items"]
        }:
            return []
        return super().user_run_commands()

    def prepare_send_payload(self, payload):
        if "attach_feedback" in payload:
            raise TransitionError("attach_feedback is unsupported; use [[运行结果]] in text")
        self._prepared_post_solved_followup = None
        state = getattr(self, "state", None)
        permit = (state.data.get("permit") or {}) if state is not None else {}
        if permit.get("post_solved") and permit.get("state") != "EVALUATE":
            self.acceptance_gate()
            verdict = copy.deepcopy(self.current()["verdict"])
            self._prepared_post_solved_followup = dict(
                schema=POST_SOLVED_FOLLOWUP_SCHEMA,
                task_id=self.state.data["task_id"],
                transition_id=permit["id"],
                state=permit["state"],
                origin_revision=self.saved["revision"],
                origin_code_reply_id=(self.state.data.get("code_reply") or {}).get("id"),
                candidate_version=verdict["candidate_version"],
                source_verdict=verdict,
                source_evidence_id=self.current().get("applied_job"),
            )
        if "[[运行结果]]" not in payload.get("text", ""):
            return payload, None
        current = self.current()
        experience = current.get("simulated_experience")
        verdict = current.get("verdict") or {}
        candidate_version = candidate_hash(self.root / "workspace/candidate")
        if (
            not experience
            or current.get("applied_job") != experience.get("summary_id")
            or verdict.get("outcome") != "unsolved"
            or verdict.get("revision") != self.saved["revision"]
            or verdict.get("candidate_version") != candidate_version
        ):
            raise TransitionError(
                "no unique reviewed current Judge failure is available"
            )
        final_text, attachment = attach_feedback(
            payload.get("text"),
            experience,
            revision=self.saved["revision"],
            candidate_version=candidate_version,
        )
        return {**payload, "text": final_text}, attachment

    def after_message_published(self, message):
        snapshot = getattr(self, "_prepared_post_solved_followup", None)
        self._prepared_post_solved_followup = None
        if not snapshot:
            return
        if snapshot["transition_id"] != message["transition_id"]:
            raise RuntimeError("published follow-up does not match its solved transition")
        self.current()["post_solved_followup"] = {
            **snapshot,
            "message_id": message["id"],
            "status": "pending",
        }

    def carry_post_solved_verdict(self):
        """Carry a solved verdict across one read-only conversational reply."""
        current = self.current()
        pending = current.get("post_solved_followup")
        if not isinstance(pending, dict):
            return False
        if (
            self.state.data.get("phase") != "user"
            or self.state.data.get("status") != "running"
            or self.progress.get("job") is not None
            or self.progress.get("feedback_revision") is not None
        ):
            return False
        if pending.get("status") == "carried":
            verdict = current.get("verdict") or {}
            return (
                pending.get("current_revision") == self.saved["revision"]
                and verdict.get("outcome") == "solved"
                and verdict.get("revision") == self.saved["revision"]
                and verdict.get("candidate_version") == pending.get("candidate_version")
            )
        if pending.get("status") != "pending":
            return False

        candidate_version = candidate_hash(self.root / "workspace/candidate")
        source = pending.get("source_verdict") or {}
        message = next(
            (
                item
                for item in self.state.data.get("messages", [])
                if item.get("id") == pending.get("message_id")
            ),
            None,
        )
        transition = next(
            (
                item
                for item in self.state.data.get("transitions", [])
                if item.get("id") == pending.get("transition_id")
            ),
            None,
        )
        source_evidence = next(
            (
                item
                for item in self.state.data.get("checks", [])
                if item.get("id") == pending.get("source_evidence_id")
            ),
            None,
        )
        current_code_reply_id = (self.state.data.get("code_reply") or {}).get("id")
        if (
            self.saved["revision"] == pending.get("origin_revision")
            and current_code_reply_id == pending.get("origin_code_reply_id")
        ):
            return False
        # A prior carry authorizes the conversational revision without changing
        # the original Judge evidence or inventing a new check.
        carried_source = any(
            record.get("schema") == SOLVED_VERDICT_CARRY_SCHEMA
            and record.get("task_id") == pending.get("task_id")
            and record.get("source_evidence_id") == pending.get("source_evidence_id")
            and record.get("candidate_version") == candidate_version
            and record.get("current_revision") == pending.get("origin_revision")
            and record.get("state") in CARRYABLE_POST_SOLVED_STATES
            for record in current.get("verdict_carries", [])
        )
        source_is_current = (
            pending.get("schema") == POST_SOLVED_FOLLOWUP_SCHEMA
            and pending.get("task_id") == self.state.data["task_id"]
            and isinstance(message, dict)
            and message.get("published") is True
            and message.get("task_id") == pending.get("task_id")
            and message.get("transition_id") == pending.get("transition_id")
            and isinstance(transition, dict)
            and transition.get("task_id") == pending.get("task_id")
            and transition.get("state") == pending.get("state")
            and transition.get("post_solved") is True
            and current_code_reply_id
            and current_code_reply_id != pending.get("origin_code_reply_id")
            and source.get("outcome") == "solved"
            and source.get("revision") == pending.get("origin_revision")
            and source.get("candidate_version") == pending.get("candidate_version")
            and current.get("applied_job") == pending.get("source_evidence_id")
            and isinstance(source_evidence, dict)
            and source_evidence.get("tool") == "judge_summary"
            and (source_evidence.get("revision") == pending.get("origin_revision")
                 or carried_source)
            and source_evidence.get("result") in ("passed", "static_solved")
            and (source_evidence.get("summary") or {}).get("outcome") == "solved"
            and current.get("verdict") == source
            and self.saved["revision"] == pending.get("origin_revision", -1) + 1
        )
        carryable = (
            source_is_current
            and pending.get("state") in CARRYABLE_POST_SOLVED_STATES
            and candidate_version == pending.get("candidate_version")
        )
        if not carryable:
            pending.update(
                status="judge_required",
                current_revision=self.saved["revision"],
                observed_candidate_version=candidate_version,
            )
            self.persist()
            return False

        carried = {**source, "revision": self.saved["revision"]}
        current["verdict"] = carried
        record = dict(
            schema=SOLVED_VERDICT_CARRY_SCHEMA,
            task_id=pending["task_id"],
            message_id=pending["message_id"],
            source_evidence_id=pending.get("source_evidence_id"),
            origin_revision=pending["origin_revision"],
            current_revision=self.saved["revision"],
            state=pending["state"],
            candidate_version=candidate_version,
        )
        carries = current.setdefault("verdict_carries", [])
        if not any(
            item.get("message_id") == record["message_id"]
            and item.get("current_revision") == record["current_revision"]
            for item in carries
        ):
            carries.append(record)
        pending.update(
            status="carried",
            current_revision=self.saved["revision"],
            observed_candidate_version=candidate_version,
        )
        self.persist()
        return True

    def projection_context(self):
        return dict(self.current(), active_revision=self.saved["revision"])

    def acceptance_gate(self):
        verdict = self.current()["verdict"]
        if (
            not verdict
            or verdict["outcome"] != "solved"
            or verdict["revision"] != self.saved["revision"]
        ):
            raise TransitionError(
                "current issue has no solved Judge verdict for this revision"
            )
        if verdict["candidate_version"] != candidate_hash(
            self.root / "workspace/candidate"
        ):
            raise TransitionError(
                "candidate changed after Judge verdict; acceptance denied"
            )
        tasks = self.saved.get("tasks", [])
        task = tasks[self.state.data["task_index"]] if tasks else {}
        if task.get("kind") == "swe_chain_evo" and verdict.get(
            "required_tests"
        ) != "passed":
            raise TransitionError(
                "current SWE-Chain-Evo candidate has no passing required-test record"
            )

    def validate_acceptance_intent(self):
        # A solved task may end from any still-open post-solved selection.  The
        # selection is an invitation to continue the conversation, not a
        # requirement to invent or send another message before accepting.
        # acceptance_gate() remains the authority for the solved revision,
        # candidate hash, required tests, and unresolved failures.
        return None

    def prepare_pending_transition(self, action):
        """Reproduce current-Judge evidence binding without re-reviewing."""
        payload = copy.deepcopy(action)
        verdict = self.current().get("verdict")
        if verdict and verdict["revision"] == self.saved["revision"]:
            bind_current_judge_evidence({"payload": payload}, self.current())
        return payload

    def _control(self, packet):
        packet.get("_request_context", current_context()).check()
        if packet["request_id"] in self.saved.get("control_results", {}):
            return super()._control(packet)
        if packet.get("operation") in ("accept", "send", "transition"):
            verdict = self.current().get("verdict")
            if verdict and verdict["revision"] == self.saved["revision"]:
                if packet["operation"] == "accept":
                    try:
                        self.acceptance_gate()
                    except TransitionError:
                        return super()._control(packet)
                packet = copy.deepcopy(packet)
                bind_current_judge_evidence(packet, self.current())
        if packet.get("operation") == "verify":
            try:
                self.state.identity(packet.get("payload", {}))
                result = dict(
                    accepted=True,
                    external_check=self.current()["feedback"],
                    instruction="Judge checks automatically after Code replies. This is not a User tool observation.",
                )
            except TransitionError as error:
                result = dict(accepted=False, reason=str(error))
            append(
                self.private / "controls.jsonl", dict(operation="verify", result=result)
            )
            self.saved.setdefault("control_results", {})[packet["request_id"]] = result
            self.persist()
            return result
        return super()._control(packet)

    def judge_events(self):
        job = self.progress["job"]
        events = self.agents["judge"].events()[job["event_start"] :]
        pipeline_exit_policy = (
            "pipefail"
            if self.config.get("execution_backend") == "ssh_sandbox"
            else None
        )
        observations = [
            private_observation(
                e, job["revision"], pipeline_exit_policy=pipeline_exit_policy
            )
            for e in events
            if e.get("tool_name") != "submit_verdict"
        ]
        return events, [o for o in observations if o]

    def judge_control(self, packet):
        with self.lock:
            context = packet.get("_request_context", current_context())
            context.check()
            key = packet["request_id"]
            if key in self.progress["control_results"]:
                return self.progress["control_results"][key]
            correction = self.progress.get("feedback_revision")
            if correction:
                if correction.get("kind") == "verdict":
                    return self._judge_verdict_control(packet, correction)
                return self._judge_feedback_control(packet, correction)
            job = self.progress["job"]
            events, observations = self.judge_events()
            if packet.get("operation") != "judge_verdict":
                return dict(
                    accepted=False, reason="Judge has no User control authority"
                )
            submitted_payload = packet.get("payload", {})
            record = dict(
                request_id=key,
                job=job.copy(),
                submitted_payload=submitted_payload,
                events=events,
                observations=observations,
            )
            # First submission is retained even when rejected; no silent regeneration.
            try:
                payload = bind_submission(
                    submitted_payload, job, observations, project_feedback=False
                )
                record["payload"] = payload
                current = self.current()
                validate_verdict_core(payload, job, observations, current["plan"])
                required = job.get("required_tests")
                if required:
                    from .evo_tests import validate_required_verdict

                    if required.get("candidate_version") != job["candidate_version"]:
                        raise ValueError("required tests are bound to another candidate")
                    validate_required_verdict(required, payload["outcome"])
                feedback_error = None
                try:
                    if payload["outcome"] != "solved":
                        payload["public_feedback"] = project_latest_feedback(
                            observations,
                            submitted_payload.get("feedback_detail", ""),
                            submitted_payload.get("feedback", ""),
                        )
                    projected_feedback = validate_public_feedback(
                        payload.get("public_feedback", {}),
                        observations,
                        payload.get("evidence_ids", []),
                    )
                    if payload["outcome"] == "unsolved" and not projected_feedback:
                        raise PublicFeedbackError(
                            "unsolved requires observable public feedback"
                        )
                    if payload["outcome"] == "unsolved" and not projected_feedback.get(
                        "symptom"
                    ):
                        raise PublicFeedbackError(
                            "unsolved requires a short observable feedback symptom"
                        )
                    if payload["outcome"] == "solved" and projected_feedback:
                        raise PublicFeedbackError(
                            "solved verdict must not expose public feedback"
                        )
                    payload["public_feedback"] = projected_feedback
                    record["payload"] = payload
                    record["feedback_projection_mapping"] = feedback_projection_mapping(
                        projected_feedback, observations
                    )
                except PublicFeedbackError as error:
                    feedback_error = str(error)
                if (
                    candidate_hash(self.root / "judge-workspace/candidate")
                    != job["candidate_version"]
                ):
                    raise ValueError("original Judge candidate changed")
                if (
                    candidate_hash(self.root / "workspace/candidate")
                    != job["candidate_version"]
                ):
                    raise ValueError("Code candidate changed during Judge inspection")
                if feedback_error:
                    target = list(current["released"])
                    feedback_disclosure = dict(
                        failure_key=None, units=[], released_unit_ids=[], added=[])
                else:
                    target, feedback_disclosure = plan_turn_disclosure(current, payload)
                visible = visible_requirement(current["plan"], target)
                record["disclosure"] = dict(
                    before=list(current["released"]),
                    after=target,
                    added=[i for i in target if i not in current["released"]],
                    requirement=visible,
                )
                record["feedback_disclosure"] = copy.deepcopy(feedback_disclosure)
                reviewed_payload = dict(
                    payload,
                    public_feedback=(
                        {}
                        if payload["outcome"] == "solved" or feedback_error
                        else payload["public_feedback"]
                    ),
                )
                record["reviewed_payload"] = reviewed_payload
                record["review"] = review_verdict(
                    self.agents["judge"].relay,
                    self.judgment_task(),
                    job,
                    reviewed_payload,
                    events,
                    observations,
                    visible,
                    current["plan"],
                    public_history=[
                        p
                        for p in self.saved["public"]
                        if p["kind"] in ("user", "assistant")
                    ],
                )
                context.check()
                record["feedback_version"] = 0
                record["feedback_revisions"] = []
                if not record["review"]["conclusion_valid"]:
                    if (
                        record["review"].get("grounded") is True
                        and record["review"].get("verdict_valid") is False
                    ):
                        record.update(
                            accepted=False,
                            status="verdict_pending",
                            verdict_rejections=[dict(
                                source="automatic_audit",
                                reason="; ".join(record["review"]["reasons"])
                                or "grounded Judge outcome requires correction",
                            )],
                            verdict_revisions=[],
                        )
                    else:
                        raise ValueError("Judge conclusion audit failed")
                elif feedback_error:
                    record.update(
                        accepted=False,
                        status="feedback_pending",
                        feedback_rejections=[
                            dict(source="schema_validation", reason=feedback_error)
                        ],
                    )
                elif record["review"]["feedback_safe"]:
                    record.update(accepted=True, status="ready")
                else:
                    record.update(
                        accepted=False,
                        status="feedback_pending",
                        feedback_rejections=[
                            dict(
                                source="automatic_audit",
                                reason="; ".join(record["review"]["reasons"])
                                or "public feedback audit failed",
                            )
                        ],
                    )
            except Exception as error:
                error_text = type(error).__name__ + ": " + str(error)
                if (
                    submitted_payload.get("outcome") == "solved"
                    and (
                        "solved requires a successful validation exit" in error_text
                        or "solved validation pipeline has no trusted exit policy"
                        in error_text
                    )
                ):
                    current = self.current()
                    visible = visible_requirement(
                        current["plan"], current.get("released", [])
                    )
                    record.setdefault(
                        "disclosure",
                        dict(
                            before=list(current.get("released", [])),
                            after=list(current.get("released", [])),
                            added=[],
                            requirement=visible,
                        ),
                    )
                    record.setdefault(
                        "feedback_disclosure",
                        dict(
                            failure_key=None,
                            units=[],
                            released_unit_ids=[],
                            added=[],
                        ),
                    )
                    record.setdefault("feedback_version", 0)
                    record.setdefault("feedback_revisions", [])
                    record["reviewed_payload"] = copy.deepcopy(record.get("payload", {}))
                    # Keep the original evidence and let the same Judge correct
                    # the outcome once; do not silently turn an invalid success
                    # into a passed verdict or rerun inspection.
                    record.update(
                        accepted=False,
                        status="verdict_pending",
                        verdict_rejections=[dict(
                            source="validation_evidence",
                            reason=error_text,
                        )],
                        verdict_revisions=[],
                    )
                else:
                    record.update(
                        accepted=False,
                        status="invalid",
                        error=error_text,
                    )
            record_id = job["id"]
            save(self.private / "judgments" / f"{record_id}.json", record)
            self.progress["decisions"][record_id] = record
            status = record.get("status")
            retained = status in ("feedback_pending", "verdict_pending")
            result = dict(
                accepted=status in ("ready", "feedback_pending", "verdict_pending"),
                handoff=True,
                feedback_revision_required=status == "feedback_pending",
                verdict_revision_required=status == "verdict_pending",
                reason=(
                    "Grounded verdict retained; outcome requires correction."
                    if status == "verdict_pending"
                    else "Verdict retained; public feedback requires correction."
                    if status == "feedback_pending"
                    else (
                        "Verdict saved; host will apply it."
                        if record.get("accepted")
                        else record["error"]
                    )
                ),
            )
            self.progress["control_results"][key] = result
            self.persist()
            return result

    def _judge_verdict_control(self, packet, correction):
        context = packet.get("_request_context", current_context())
        context.check()
        key = packet["request_id"]
        record = self.progress["decisions"][correction["verdict_id"]]
        if packet.get("operation") != "judge_verdict_revision":
            result = dict(
                accepted=False,
                handoff=True,
                reason="Only grounded verdict correction is allowed in this turn.",
            )
        else:
            submitted = packet.get("payload")
            try:
                new_events = self.agents["judge"].events()[correction["event_start"] :]
                extra = [
                    event.get("tool_name")
                    for event in new_events
                    if event.get("tool_name")
                    and event.get("tool_name") != "revise_verdict"
                ]
                if extra:
                    raise ValueError(
                        "verdict correction may not run additional inspection tools: "
                        + ", ".join(extra)
                    )
                allowed = {"outcome", "reason", "feedback", "feedback_detail"}
                if not isinstance(submitted, dict) or set(submitted) != allowed:
                    raise ValueError(
                        "verdict correction changes only outcome, reason, and feedback"
                    )
                revised = bind_submission(
                    submitted,
                    record["job"],
                    record["observations"],
                    project_feedback=False,
                )
                original = record["payload"]
                revised["requested_fragment_ids"] = list(
                    original.get("requested_fragment_ids", [])
                )
                if revised["evidence_ids"] != original.get("evidence_ids", []):
                    raise ValueError("verdict correction evidence changed")
                if revised["outcome"] == "unsolved":
                    revised["public_feedback"] = project_latest_feedback(
                        record["observations"],
                        submitted["feedback_detail"],
                        submitted["feedback"],
                    )
                else:
                    revised["public_feedback"] = {}
                validate_verdict_core(
                    revised,
                    record["job"],
                    record["observations"],
                    self.current()["plan"],
                )
                feedback = validate_public_feedback(
                    revised.get("public_feedback", {}),
                    record["observations"],
                    revised["evidence_ids"],
                )
                if revised["outcome"] == "unsolved" and (
                    not feedback or not feedback.get("symptom")
                ):
                    raise PublicFeedbackError(
                        "corrected unsolved verdict requires an observable symptom"
                    )
                revised["public_feedback"] = feedback
                required = record["job"].get("required_tests")
                if required:
                    from .evo_tests import validate_required_verdict

                    validate_required_verdict(required, revised["outcome"])
                if (
                    candidate_hash(self.root / "judge-workspace/candidate")
                    != record["job"]["candidate_version"]
                    or candidate_hash(self.root / "workspace/candidate")
                    != record["job"]["candidate_version"]
                ):
                    raise ValueError("candidate changed during verdict correction")

                current = self.current()
                target, feedback_disclosure = plan_turn_disclosure(current, revised)
                if target != record["disclosure"]["after"]:
                    raise ValueError("verdict correction changed issue release")
                visible = visible_requirement(current["plan"], target)
                review = review_verdict(
                    self.agents["judge"].relay,
                    self.judgment_task(),
                    record["job"],
                    revised,
                    record["events"],
                    record["observations"],
                    visible,
                    current["plan"],
                    public_history=[
                        item for item in self.saved["public"]
                        if item["kind"] in ("user", "assistant")
                    ],
                )
                context.check()
                revision = dict(
                    version=correction["version"],
                    payload=copy.deepcopy(submitted),
                    review=review,
                    rejected_reason=correction["reason"],
                    feedback_projection_mapping=feedback_projection_mapping(
                        feedback, record["observations"]
                    ),
                )
                record.setdefault("verdict_revisions", []).append(revision)
                record["verdict_version"] = correction["version"]
                record["reviewed_payload"] = revised
                record["review"] = review
                record["feedback_disclosure"] = copy.deepcopy(feedback_disclosure)
                if not review["conclusion_valid"] or not review["feedback_safe"]:
                    raise ValueError("corrected Judge verdict did not pass audit")
                record["payload"] = revised
                record.update(accepted=True, status="ready")
                save(self.private / "judgments" / f"{record['job']['id']}.json", record)
                result = dict(
                    accepted=True,
                    handoff=True,
                    verdict_valid=True,
                    reason="Corrected verdict retained for host application.",
                )
            except Exception as error:
                record.update(
                    accepted=False,
                    status="invalid",
                    error=type(error).__name__ + ": " + str(error),
                )
                save(self.private / "judgments" / f"{record['job']['id']}.json", record)
                result = dict(accepted=False, handoff=True, reason=record["error"])
        self.progress["feedback_revision"] = None
        self.progress["control_results"][key] = result
        self.persist()
        return result

    def _judge_feedback_control(self, packet, correction):
        context = packet.get("_request_context", current_context())
        context.check()
        key = packet["request_id"]
        record = self.progress["decisions"][correction["verdict_id"]]
        if packet.get("operation") != "judge_feedback_revision":
            result = dict(
                accepted=False,
                handoff=True,
                reason="Only public feedback correction is allowed in this turn.",
            )
        else:
            payload = packet.get("payload")
            try:
                new_events = self.agents["judge"].events()[correction["event_start"] :]
                extra = [
                    event.get("tool_name")
                    for event in new_events
                    if event.get("tool_name")
                    and event.get("tool_name") != "revise_feedback"
                ]
                if extra:
                    raise ValueError(
                        "feedback correction may not run additional inspection tools: "
                        + ", ".join(extra)
                    )
                if not isinstance(payload, dict):
                    raise PublicFeedbackError("feedback correction payload must be an object")
                symptom = payload.get("feedback")
                detail = payload.get("feedback_detail", "")
                if not isinstance(symptom, str) or not isinstance(detail, str):
                    raise PublicFeedbackError("feedback correction requires a string feedback field")
                feedback = project_latest_feedback(
                    record["observations"], detail, symptom
                )
                if record["payload"]["outcome"] == "unsolved" and not feedback:
                    raise PublicFeedbackError(
                        "unsolved requires observable public feedback"
                    )
                if record["payload"]["outcome"] == "unsolved" and not feedback.get(
                    "symptom"
                ):
                    raise PublicFeedbackError(
                        "unsolved requires a short observable feedback symptom"
                    )
                reviewed_payload = dict(record["payload"], public_feedback=feedback)
                current = self.current()
                target, feedback_disclosure = plan_turn_disclosure(
                    current, reviewed_payload
                )
                visible = visible_requirement(current["plan"], target)
                review = review_verdict(
                    self.agents["judge"].relay,
                    self.judgment_task(),
                    record["job"],
                    reviewed_payload,
                    record["events"],
                    record["observations"],
                    visible,
                    self.current()["plan"],
                    public_history=[
                        p
                        for p in self.saved["public"]
                        if p["kind"] in ("user", "assistant")
                    ],
                )
                context.check()
                revision = dict(
                    version=correction["version"],
                    payload=payload,
                    review=review,
                    rejected_reason=correction["reason"],
                    feedback_projection_mapping=feedback_projection_mapping(
                        feedback, record["observations"]
                    ),
                )
                record.setdefault("feedback_revisions", []).append(revision)
                record["feedback_version"] = correction["version"]
                record["reviewed_payload"] = reviewed_payload
                record["disclosure"] = dict(
                    before=list(current["released"]), after=target,
                    added=[i for i in target if i not in current["released"]],
                    requirement=visible,
                )
                record["feedback_disclosure"] = copy.deepcopy(feedback_disclosure)
                record["review"] = review
                if not review["conclusion_valid"]:
                    raise ValueError(
                        "Judge conclusion audit failed during feedback correction"
                    )
                if review["feedback_safe"]:
                    record["payload"] = reviewed_payload
                    record.update(accepted=True, status="ready")
                else:
                    record.update(accepted=False, status="feedback_pending")
                    record.setdefault("feedback_rejections", []).append(
                        dict(
                            source="automatic_audit",
                            reason="; ".join(review["reasons"])
                            or "corrected public feedback audit failed",
                        )
                    )
                save(self.private / "judgments" / f"{record['job']['id']}.json", record)
                result = dict(
                    accepted=True,
                    handoff=True,
                    feedback_safe=review["feedback_safe"],
                    reason="Corrected feedback retained for host review.",
                )
            except PublicFeedbackError as error:
                record.update(accepted=False, status="feedback_pending")
                record.setdefault("feedback_revisions", []).append(
                    dict(
                        version=correction["version"],
                        payload=copy.deepcopy(payload),
                        rejected_reason=correction["reason"],
                        schema_error=str(error),
                    )
                )
                record.setdefault("feedback_rejections", []).append(
                    dict(source="schema_validation", reason=str(error))
                )
                save(self.private / "judgments" / f"{record['job']['id']}.json", record)
                result = dict(
                    accepted=True,
                    handoff=True,
                    feedback_safe=False,
                    reason="Corrected feedback still requires correction.",
                )
            except Exception as error:
                record.update(
                    accepted=False,
                    status="invalid",
                    error=type(error).__name__ + ": " + str(error),
                )
                save(self.private / "judgments" / f"{record['job']['id']}.json", record)
                result = dict(accepted=False, handoff=True, reason=record["error"])
        self.progress["feedback_revision"] = None
        self.progress["control_results"][key] = result
        self.persist()
        return result

    def review_decision(self, record):
        """Optional delivery review hook. It may request feedback correction, never rewrite it."""
        return record

    def request_feedback_revision(self, record):
        # A resumed retained verdict reaches this path before a fresh inspection would
        # normally initialize the persistent Judge conversation.
        attempts = record.get(
            "feedback_attempts", len(record.get("feedback_revisions", []))
        )
        if attempts >= 2:
            record.update(
                accepted=False,
                status="invalid",
                error="Public feedback remained unsafe after two correction attempts",
            )
            save(self.private / "judgments" / f"{record['job']['id']}.json", record)
            self.persist()
            return record
        if attempts >= 1 and not has_public_feedback_source(
            record.get("observations", [])
        ):
            record.update(
                accepted=False,
                status="verdict_pending",
                verdict_rejections=[dict(
                    source="feedback_source",
                    reason=(
                        "current Judge observations contain no candidate-only "
                        "evidence that can support public feedback"
                    ),
                )],
                verdict_revisions=[],
            )
            save(self.private / "judgments" / f"{record['job']['id']}.json", record)
            self.persist()
            return record
        self.start_judge()
        rejection = record.get("feedback_rejections", [])[-1]["reason"]
        version = attempts + 1
        record["feedback_attempts"] = version
        save(self.private / "judgments" / f"{record['job']['id']}.json", record)
        correction = dict(
            verdict_id=record["job"]["id"],
            version=version,
            reason=rejection,
            event_start=len(self.agents["judge"].events()),
        )
        self.progress["feedback_revision"] = correction
        self.persist()
        prompt = dict(
            rejection=rejection,
            closed_execution_blocks_available=has_projectable_execution_blocks(
                record.get("observations", [])
            ),
            instruction=(
                "只修正一句可见现象。有闭合运行块时，feedback 写明实际输出与预期行为的"
                "具体可观察差异，宿主会同时保留原始输入输出；没有闭合运行块时也必须"
                "提交一句已观察现象。不要重新检查。"
            ),
        )
        command_id = f"turn-{record['job']['id']}-feedback-v{version}"
        self.agents["judge"].turn(
            json.dumps(prompt, ensure_ascii=False), command_id=command_id
        )
        if self.progress.get("feedback_revision"):
            record.update(
                accepted=False,
                status="invalid",
                error="Judge stopped without correcting public feedback",
            )
            self.progress["feedback_revision"] = None
            save(self.private / "judgments" / f"{record['job']['id']}.json", record)
            self.persist()
        return record

    def request_verdict_revision(self, record):
        self.start_judge()
        attempts = record.get("verdict_attempts", 0)
        if attempts >= 1:
            record.update(
                accepted=False,
                status="invalid",
                error="Judge verdict correction failed after one attempt",
            )
            save(self.private / "judgments" / f"{record['job']['id']}.json", record)
            self.persist()
            return record
        rejection = record.get("verdict_rejections", [])[-1]["reason"]
        record["verdict_attempts"] = 1
        correction = dict(
            kind="verdict",
            verdict_id=record["job"]["id"],
            version=1,
            reason=rejection,
            event_start=len(self.agents["judge"].events()),
        )
        self.progress["feedback_revision"] = correction
        save(self.private / "judgments" / f"{record['job']['id']}.json", record)
        self.persist()
        prompt = dict(
            rejection=rejection,
            verdict=record["payload"],
            instruction=(
                "只使用已有 observations 纠正 outcome、reason 和相应 feedback，"
                "不要运行工具或重新检查。已观察到必需行为违反时必须判 unsolved；"
                "uncertain 仅用于关键证据缺失、环境阻塞或证据冲突。"
            ),
        )
        self.agents["judge"].turn(
            json.dumps(prompt, ensure_ascii=False),
            command_id=f"turn-{record['job']['id']}-verdict-v1",
        )
        if self.progress.get("feedback_revision"):
            record.update(
                accepted=False,
                status="invalid",
                error="Judge stopped without correcting the grounded verdict",
            )
            self.progress["feedback_revision"] = None
            save(self.private / "judgments" / f"{record['job']['id']}.json", record)
            self.persist()
        elif record.get("status") == "verdict_pending":
            record.update(
                accepted=False,
                status="invalid",
                error="Judge verdict correction did not produce a valid result",
            )
            save(self.private / "judgments" / f"{record['job']['id']}.json", record)
            self.persist()
        return record

    def resolve_decision(self, record):
        while record.get("status") in ("feedback_pending", "verdict_pending"):
            if record.get("status") == "verdict_pending":
                record = self.request_verdict_revision(record)
            else:
                record = self.request_feedback_revision(record)
            if record.get("status") == "ready":
                record = self.review_decision(record)
        if record.get("status") == "ready":
            record = self.review_decision(record)
        return record

    def sync_judge(self):
        directory = self.root / "judge-workspace"
        for name in ("candidate", "checks", "experiments"):
            (directory / name).mkdir(parents=True, exist_ok=True)
        candidate_hash(self.root / "workspace/candidate")
        subprocess.run(
            [
                "rsync",
                "-a",
                "--delete",
                str(self.root / "workspace/candidate") + "/",
                str(directory / "candidate") + "/",
            ],
            check=True,
            capture_output=True,
        )
        reference = self.private / "judge-reference"
        reference.mkdir(exist_ok=True)
        task = self.saved["tasks"][self.state.data["task_index"]]
        marker = reference / "task.json"
        if (
            not marker.exists()
            or json.loads(marker.read_text())["task_id"] != self.state.data["task_id"]
        ):
            for name in ("base", "fixed"):
                destination = reference / name
                if destination.exists():
                    shutil.rmtree(
                        destination
                    )  # Dedicated generated reference snapshot, never the source repository.
            repo = Path(self.config["repository"]).expanduser()
            if not repo.is_dir():
                repo = self.private / "source"
            if task.get("reference"):
                snapshot(repo, task["base"], reference / "base")
                snapshot(repo, task["reference"], reference / "fixed")
                if task.get("kind") != "swe_chain_evo":
                    task["patch"] = subprocess.check_output(
                        [
                            "git",
                            "-C",
                            str(repo),
                            "diff",
                            task["base"],
                            task["reference"],
                        ],
                        text=True,
                    )
            save(reference / "diff.json", dict(diff=task.get("patch", "")))
            save(
                marker,
                dict(
                    task_id=self.state.data["task_id"],
                    has_reference=bool(task.get("reference")),
                ),
            )

    def start_judge(self):
        if "judge" in self.agents:
            return
        directory = self.private / "judge"
        resuming = (directory / "inbox/config.json").exists()
        agent = SDKContainer(
            directory,
            self.root / "judge-workspace",
            self.execution_config("judge"),
            self.image,
            "judge",
            SYSTEM,
            self.budget.deadline,
            control=self.judge_control,
            budget=self.budget,
            condenser_max_size=self.config.get("condenser_max_size", 120),
            reference=self.private / "judge-reference",
            readonly_candidate=True,
        )
        self.agents["judge"] = agent
        agent.start(resume=resuming)

    def apply_decision(self, record):
        job = record["job"]
        if (
            job["task_id"] != self.state.data["task_id"]
            or job["revision"] != self.saved["revision"]
        ):
            raise ValueError("cannot apply a judgment from another task or revision")
        if self.current().get("applied_job") == job["id"]:
            return
        if (
            record.get("status", "ready" if record.get("accepted") else "invalid")
            != "ready"
        ):
            self.state.data.update(
                status="paused",
                pause_reason=record.get("error", "Judge public feedback is not ready"),
            )
            self.persist()
            return
        payload = record["payload"]
        current = self.current()
        before = list(current["released"])
        explicit_question = bool(payload.get("requested_fragment_ids"))
        target = record.get("disclosure", {}).get("after") or before
        current["verdict"] = dict(
            outcome=payload["outcome"],
            revision=job["revision"],
            candidate_version=job["candidate_version"],
            verification_mode=self.config.get("verification_mode", "default"),
            required_tests=(job.get("required_tests") or {}).get("outcome"),
        )
        pending_followup = current.get("post_solved_followup")
        if (
            isinstance(pending_followup, dict)
            and pending_followup.get("status") == "judge_required"
            and pending_followup.get("current_revision") == job["revision"]
        ):
            pending_followup.update(status="judged", judge_job_id=job["id"])
        if payload["outcome"] == "unsolved":
            feedback_release = copy.deepcopy(record.get("feedback_disclosure") or {
                "failure_key": None, "units": [],
                "released_unit_ids": [], "added": [],
            })
            current["feedback_units"] = feedback_release["units"]
            current["released_feedback_unit_ids"] = feedback_release[
                "released_unit_ids"
            ]
            record["feedback_disclosure"] = {
                "feedback_units": copy.deepcopy(feedback_release["units"]),
                "released_unit_ids": list(
                    feedback_release["released_unit_ids"]
                ),
                "added": list(feedback_release["added"]),
            }
            unit = released_feedback_unit(feedback_release)
            observation = copy.deepcopy((unit or {}).get("observation", {}))
        else:
            current["feedback_units"] = []
            current["released_feedback_unit_ids"] = []
            record["feedback_disclosure"] = {
                "feedback_units": [],
                "released_unit_ids": [],
                "added": [],
            }
            observation = {}
        current["released"] = target
        if not record.get("release_committed"):
            current.setdefault("release_history", []).append(
                dict(
                    job_id=job["id"],
                    reason="question" if explicit_question else payload["outcome"],
                    before=before,
                    after=list(target),
                    added=[i for i in target if i not in before],
                )
            )
        record.setdefault("disclosure", {})["after"] = list(target)
        record["disclosure"]["added"] = [i for i in target if i not in before]
        current["feedback"] = dict(
            source="Judge",
            outcome=payload["outcome"],
            observation=observation,
            evidence_id=job["id"],
        )
        current["simulated_experience"] = build_experience(
            record, observation=observation
        )
        if current["simulated_experience"]:
            save(
                self.private / "judgments" / f"{job['id']}-simulated-experience.json",
                current["simulated_experience"],
            )
        # Only the audited summary enters User context, never the raw Judge observation list.
        self.state.data["checks"].append(
            dict(
                id=job["id"],
                revision=job["revision"],
                tool="judge_summary",
                source="Judge",
                result=(
                    "static_solved"
                    if payload["outcome"] == "solved"
                    and self.config.get("verification_mode") == "static_reference"
                    else "passed"
                    if payload["outcome"] == "solved"
                    else "observed"
                ),
                summary=current["feedback"],
                simulated_experience=current["simulated_experience"],
            )
        )
        current["applied_job"] = job["id"]
        if payload["outcome"] == "uncertain" and not payload.get(
            "requested_fragment_ids"
        ):
            self.state.data.update(
                status="paused",
                pause_reason="Judge uncertain; inspect private evidence before continuing",
            )
        self.progress["job"] = None
        self.persist()

    def before_user_turn(self):
        current = self.current()
        if not self.state.data.get("code_reply") or self.saved.get("closing"):
            return
        self.carry_post_solved_verdict()
        if (
            current["verdict"]
            and current["verdict"]["revision"] == self.saved["revision"]
            and self.progress.get("job") is None
            and self.progress.get("feedback_revision") is None
        ):
            return
        self.agents["code"].pause()
        try:
            job = self.progress.get("job")
            if job and job["id"] in self.progress["decisions"]:
                record = self.resolve_decision(self.progress["decisions"][job["id"]])
                self.apply_decision(record)
                return
            if job:
                result = self.private / "judge/outbox" / f"{job['command_id']}.json"
                if (
                    not result.exists()
                    or json.loads(result.read_text()).get("status") != "ok"
                ):
                    raise RuntimeError(
                        "uncertain interrupted Judge call; no automatic replay"
                    )
                raise RuntimeError(
                    "Judge stopped without a saved verdict; inspect retained events"
                )
            self.sync_judge()
            # Bind the job to the copy Judge actually reads, not just Code's tree.
            candidate_version = candidate_hash(self.root / "workspace/candidate")
            judge_version = candidate_hash(self.root / "judge-workspace/candidate")
            if judge_version != candidate_version:
                raise RuntimeError(
                    "judge 镜像与候选不同步 (Judge snapshot differs from candidate): "
                    f"candidate={candidate_version}, judge={judge_version}"
                )
            self.start_judge()
            self.agents['judge'].sandbox.sync_snapshots()
            visible_version = self.agents['judge'].sandbox.candidate_hash()
            if visible_version != judge_version:
                raise RuntimeError(
                    'Judge 容器内候选与宿主快照不同步: '
                    f'host={judge_version}, container={visible_version}'
                )
            identifier = f"judge-{self.state.data['task_id']}-r{self.saved['revision']}"
            (self.private / "judgments").mkdir(exist_ok=True)
            job = dict(
                id=identifier,
                command_id="turn-" + identifier,
                task_id=self.state.data["task_id"],
                revision=self.saved["revision"],
                candidate_version=judge_version,
                code_reply=self.state.data["code_reply"],
                event_start=len(self.agents["judge"].events()),
            )
            task = self.judgment_task()
            if task.get("kind") == "swe_chain_evo":
                from .evo_tests import compact_result, run_required_tests

                label = (
                    f"required-{job['task_id']}-r{job['revision']}-"
                    f"{job['candidate_version'][:12]}"
                )
                record = run_required_tests(
                    self.agents["judge"].sandbox,
                    self.root / "judge-workspace/candidate",
                    self.root / "judge-workspace/experiments",
                    task,
                    label,
                    "candidate",
                    apply_test_patch=True,
                    test_base=self.private / "judge-reference/base",
                    timeout=max(
                        1, min(900, self.budget.deadline - time.monotonic())
                    ),
                    candidate_pythonpath=self.config.get("judge", {}).get(
                        "candidate_pythonpath"
                    ),
                )
                if record["candidate_version"] != job["candidate_version"]:
                    raise RuntimeError("required tests used a different candidate")
                job["required_tests"] = compact_result(record)
            self.progress["job"] = job
            self.persist()
            prompt = dict(
                issue={k: task[k] for k in ("title", "body")},
                code_reply=job["code_reply"],
                assessment_basis=assessment_basis(
                    task.get("verification_mode", "default")
                ),
                fragments=[
                    {
                        "id": item["id"],
                        "category": item["category"],
                        "text": item["text"],
                    }
                    for item in current["plan"]["items"]
                ],
                released_fragment_ids=current["released"],
            )
            if job.get("required_tests"):
                prompt["required_tests"] = job["required_tests"]
                prompt["required_test_instruction"] = (
                    "Inspect the recorded result_file before submitting a verdict. "
                    "A solved verdict requires outcome=passed."
                )
            notice_attached = attach_pending_judge_policy_notice(
                self.saved, prompt, job["command_id"]
            )
            if notice_attached:
                self.persist()
            save(self.private / "judgments" / f"{identifier}-input.json", prompt)
            self.agents["judge"].turn(
                json.dumps(prompt, ensure_ascii=False), command_id=job["command_id"]
            )
            if notice_attached:
                self.saved["judge_policy_notice"]["delivered_command_id"] = job[
                    "command_id"
                ]
                self.persist()
            if identifier not in self.progress["decisions"]:
                raise RuntimeError("Judge stopped without submitting a verdict")
            events, _ = self.judge_events()
            for event in events:
                if "condens" in event.get("kind", "").lower():
                    self.saved["compression"].append(
                        dict(
                            role="judge",
                            event_id=event["id"],
                            kind=event["kind"],
                            after_public=len(self.saved["public"]),
                        )
                    )
            record = self.resolve_decision(self.progress["decisions"][identifier])
            self.apply_decision(record)
        finally:
            self.agents["code"].unpause()

    def run(self):
        from .dialogue_export import export_dialogue

        try:
            result = super().run()
        finally:
            export_dialogue(self.root)
        from .progressive_report import render

        render(self.root, self.saved)
        return result
