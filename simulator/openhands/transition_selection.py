"""Host-owned enumeration and selection of currently feasible transitions."""

from __future__ import annotations

import copy
import hashlib
import json
import random

from ..state_machine import CONTROL_EVENTS, CORE_STATES, STATE_GUIDANCE
from .state import TransitionError


SELECTION_VERSION = "dynamic-transition-v4-post-solved-followups"
TURN_CACHE_VERSION = "transition-turn-cache-v3"

# These are deliberately uncalibrated relative weights, not probabilities.
BASE_WEIGHTS = {state: 1.0 for state in CORE_STATES}
EXPLANATION_DECAY = {"UNDERSTAND": 0.45, "PLAN": 0.65}
POST_SOLVED_FOLLOWUP_DECAY = 0.5
POST_SOLVED_STATES = ("RETRIEVE", "UNDERSTAND", "PLAN", "OPERATE", "EVALUATE")

# Context, rather than the old simulation-prior graph, determines feasibility.
# In particular, returning from BUILD to UNDERSTAND or PLAN remains reachable.
REACHABLE = {None: frozenset(CORE_STATES), **{
    state: frozenset(CORE_STATES) for state in CORE_STATES
}}

INITIAL_STATES = ("RETRIEVE", "UNDERSTAND", "PLAN", "BUILD", "OPERATE")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def candidates_sha256(candidates):
    """Hash candidate content without making list order a new correction."""
    if not isinstance(candidates, list):
        return hashlib.sha256(_canonical(candidates).encode()).hexdigest()
    normalized = sorted((_canonical(candidate) for candidate in candidates))
    return hashlib.sha256(_canonical(normalized).encode()).hexdigest()


def candidate_fingerprint(candidate):
    """Identify the exact normalized intent that received semantic review."""
    return hashlib.sha256(_canonical(candidate).encode()).hexdigest()


def transition_turn(state_data):
    """Identify one public User turn independently of SDK request IDs."""
    reply = state_data.get("code_reply")
    reply_identity = None
    if reply:
        reply_identity = reply.get("id") or hashlib.sha256(
            _canonical(reply).encode()
        ).hexdigest()
    identity = {
        "task_id": state_data["task_id"],
        "code_reply": reply_identity,
        "published_messages": sum(
            item.get("published") is True and bool(item.get("transition_id"))
            for item in state_data.get("messages", [])
        ),
    }
    return hashlib.sha256(_canonical(identity).encode()).hexdigest(), identity


def _context_text(requirement, state_data, evidence):
    parts = []
    if isinstance(requirement, dict):
        parts.extend(str(requirement.get(key, "")) for key in ("title", "body"))
    reply = state_data.get("code_reply") or {}
    parts.append(str(reply.get("text", "")))
    for row in evidence:
        for key in ("observation", "summary"):
            value = row.get(key)
            if value:
                parts.append(str(value))
    return "\n".join(parts)


def _validated_candidate(candidate, state_data, requirement):
    if not isinstance(candidate, dict):
        raise TransitionError("transition candidates must be objects")
    state = candidate.get("state")
    control = candidate.get("control")
    reason = candidate.get("reason")
    if state not in CORE_STATES or control not in CONTROL_EVENTS:
        raise TransitionError("candidate has an unknown state or control")
    if state not in REACHABLE.get(state_data.get("state"), frozenset()):
        raise TransitionError("candidate state is not reachable from the current state")
    if not isinstance(reason, str) or not 4 <= len(reason.strip()) <= 240:
        raise TransitionError("candidate needs a short, specific reason")
    if state_data.get("code_reply") is None and control != "CONTINUE":
        raise TransitionError("the first delegation uses CONTINUE")

    known = {row["id"]: row for row in state_data.get("checks", [])}
    identifiers = candidate.get("evidence_ids", [])
    if not isinstance(identifiers, list) or any(
        not isinstance(identifier, str) or identifier not in known
        for identifier in identifiers
    ):
        raise TransitionError("candidate evidence must reference current observations")
    evidence = [known[identifier] for identifier in identifiers]

    basis = candidate.get("context_basis")
    if basis is not None:
        if not isinstance(basis, str) or len(basis.strip()) < 4:
            raise TransitionError("context basis must quote current task context")
        if basis.strip() not in _context_text(requirement, state_data, evidence):
            raise TransitionError("context basis is not present in current task context")
        basis = basis.strip()
    if state == "DEBUG" and state_data.get("code_reply") is None:
        failed = any(
            row.get("result") == "failed" or row.get("exit_code") not in (None, 0)
            for row in evidence
        )
        if not failed and not basis:
            raise TransitionError(
                "initial DEBUG needs a current failure or explicit task-context basis"
            )

    affected = candidate.get("affected_operation")
    blockers = state_data.get("blockers", [])
    if state == "OPERATE" and blockers:
        if not isinstance(affected, str) or not affected.strip():
            raise TransitionError(
                "OPERATE must identify its affected operation while blockers exist"
            )
        if any(
            affected.strip().casefold()
            == str(blocker.get("affected_operation", "")).strip().casefold()
            for blocker in blockers
        ):
            raise TransitionError("candidate operation is currently blocked")

    result = {
        "task_id": state_data["task_id"],
        "state": state,
        "control": control,
        "reason": reason.strip(),
        "evidence_ids": identifiers,
    }
    if basis:
        result["context_basis"] = basis
    if isinstance(affected, str) and affected.strip():
        result["affected_operation"] = affected.strip()
    return result


def _latest_judge_context(state_data):
    for row in reversed(state_data.get("checks", [])):
        if row.get("tool") != "judge_summary":
            continue
        summary = row.get("summary") or {}
        outcome = summary.get("outcome") if isinstance(summary, dict) else None
        if outcome in ("solved", "unsolved"):
            return outcome, row["id"]
    return None, None


def _new_failure_ids_after(state_data, evidence_id):
    """Return unresolved failures observed after the latest solved judgment."""
    after_judge = False
    failures = []
    for row in state_data.get("checks", []):
        if row.get("id") == evidence_id:
            after_judge = True
            continue
        if not after_judge or row.get("resolved_by"):
            continue
        if row.get("result") == "failed" or row.get("exit_code") not in (None, 0):
            if isinstance(row.get("id"), str):
                failures.append(row["id"])
    return failures


def _host_control(
    state, state_data, judge_outcome, *, annotated_control=None, new_failure=False
):
    if state_data.get("code_reply") is None:
        return "CONTINUE"
    if judge_outcome == "solved":
        if state == "DEBUG" and new_failure:
            return "CORRECT"
        if state == "PLAN":
            return (
                annotated_control
                if annotated_control in ("REFINE", "CORRECT")
                else "REFINE"
            )
        return "CONTINUE"
    if judge_outcome == "unsolved" or any(
        row.get("result") == "failed" and not row.get("resolved_by")
        for row in state_data.get("checks", [])
    ):
        return "CORRECT"
    if state == "PLAN":
        return "CORRECT" if annotated_control == "CORRECT" else "REFINE"
    return "REFINE" if state == state_data.get("state") else "CONTINUE"


def enumerate_transition_candidates(proposals, state_data, requirement):
    """Build the candidate universe independently of how many options User sent.

    User proposals annotate a state with a reason or grounding fields. They do
    not decide which states participate in the draw.
    """
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 7:
        raise TransitionError("provide between one and seven transition annotations")

    annotations = {}
    for raw in proposals:
        if not isinstance(raw, dict):
            continue
        state = raw.get("state")
        if state in CORE_STATES and state not in annotations:
            annotations[state] = raw

    judge_outcome, judge_evidence = _latest_judge_context(state_data)
    new_failure_ids = (
        _new_failure_ids_after(state_data, judge_evidence)
        if judge_outcome == "solved"
        else []
    )
    if judge_outcome == "solved":
        states = POST_SOLVED_STATES + (("DEBUG",) if new_failure_ids else ())
    elif state_data.get("code_reply") is None:
        states = INITIAL_STATES
        if "DEBUG" in annotations:
            states += ("DEBUG",)
    else:
        states = CORE_STATES

    known = {row.get("id") for row in state_data.get("checks", [])}
    candidates = []
    for state in states:
        annotation = annotations.get(state, {})
        identifiers = annotation.get("evidence_ids", [])
        if not isinstance(identifiers, list) or any(
            not isinstance(identifier, str) or identifier not in known
            for identifier in identifiers
        ):
            identifiers = []
        if judge_evidence and judge_evidence not in identifiers:
            identifiers = [*identifiers, judge_evidence]
        if state == "DEBUG":
            identifiers = [
                *identifiers,
                *(identifier for identifier in new_failure_ids if identifier not in identifiers),
            ]
        candidate = {
            "state": state,
            "control": _host_control(
                state,
                state_data,
                judge_outcome,
                annotated_control=annotation.get("control"),
                new_failure=bool(new_failure_ids),
            ),
            "reason": annotation.get("reason")
            if isinstance(annotation.get("reason"), str)
            and 4 <= len(annotation["reason"].strip()) <= 240
            else STATE_GUIDANCE[state],
            "evidence_ids": identifiers,
        }
        for field in ("context_basis", "affected_operation"):
            if annotation.get(field) is not None:
                candidate[field] = annotation[field]
        try:
            candidates.append(_validated_candidate(candidate, state_data, requirement))
        except TransitionError:
            # Operation-scoped blockers or an ungrounded initial DEBUG can make
            # one host option infeasible without letting it suppress the rest.
            continue
    if not candidates:
        raise TransitionError("no host transition is feasible in the current turn")
    return candidates


def select_transition(
    candidates, state_data, requirement, rng=None, *, excluded_fingerprints=()
):
    """Filter candidates and choose one with recorded, uncalibrated weights."""
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 7:
        raise TransitionError("provide between one and seven transition candidates")
    eligible, rejected, identities = [], [], set()
    judge_outcome, _ = _latest_judge_context(state_data)
    for index, raw in enumerate(candidates):
        try:
            candidate = _validated_candidate(raw, state_data, requirement)
            if candidate_fingerprint(candidate) in excluded_fingerprints:
                raise TransitionError("candidate was already reviewed and rejected")
            # Multiple controls for one state would otherwise multiply that
            # state's sampling mass without representing another request type.
            identity = candidate["state"]
            if identity in identities:
                raise TransitionError("duplicate state/control candidate")
            identities.add(identity)
            count = state_data.get("published_state_counts", {}).get(
                candidate["state"], 0
            )
            weight = BASE_WEIGHTS[candidate["state"]]
            if judge_outcome == "solved" and candidate["state"] != "EVALUATE":
                weight *= POST_SOLVED_FOLLOWUP_DECAY ** count
            elif candidate["state"] in EXPLANATION_DECAY:
                weight *= EXPLANATION_DECAY[candidate["state"]] ** count
            eligible.append({"payload": candidate, "weight": weight})
        except TransitionError as exc:
            rejected.append({"index": index, "reason": str(exc)})
    if not eligible:
        raise TransitionError("no proposed transition is feasible in the current turn")
    if judge_outcome == "solved":
        # Repeating one published follow-up type makes acceptance more likely
        # without suppressing another type's first question.
        evaluate = next(
            (item for item in eligible if item["payload"]["state"] == "EVALUATE"),
            None,
        )
        if evaluate:
            evaluate["weight"] += sum(
                BASE_WEIGHTS[item["payload"]["state"]] - item["weight"]
                for item in eligible
                if item["payload"]["state"] != "EVALUATE"
            )
        else:
            for item in eligible:
                item["weight"] = BASE_WEIGHTS[item["payload"]["state"]]
    else:
        # Move only repeated-explanation mass to feasible implementation or
        # debugging options. Never invent a candidate to receive it.
        removed_weight = sum(
            BASE_WEIGHTS[item["payload"]["state"]] - item["weight"]
            for item in eligible
            if item["payload"]["state"] in EXPLANATION_DECAY
        )
        advancing = [
            item for item in eligible
            if item["payload"]["state"] in ("BUILD", "DEBUG", "OPERATE")
        ]
        if advancing:
            increment = removed_weight / len(advancing)
            for item in advancing:
                item["weight"] += increment
    chooser = rng or random.SystemRandom()
    chosen = (
        eligible[0]
        if len(eligible) == 1
        else chooser.choices(
            eligible, weights=[item["weight"] for item in eligible], k=1
        )[0]
    )
    return {
        "version": SELECTION_VERSION,
        "selected": copy.deepcopy(chosen["payload"]),
        "selected_weight": chosen["weight"],
        "eligible": copy.deepcopy(eligible),
        "rejected": rejected,
        "candidate_sha256": candidates_sha256(candidates),
        "selected_fingerprint": candidate_fingerprint(chosen["payload"]),
    }
