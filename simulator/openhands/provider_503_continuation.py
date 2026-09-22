"""Explicitly continue a Code turn interrupted by 503 or discarded truncation.

This is deliberately narrower than the other continuation entries. It does not
replay the retained turn and it does not resend the User message: the persisted
Code conversation is continued with ``message=None``, so the SDK re-issues only
the model call that the provider dropped and every already-completed tool call
stays done.

What it guarantees, in order:

1. The retained failure is a provider 503 or unforwarded output truncation, the tool
   call/observation log is fully paired, and no fatal marker was written.
2. The retained failed turn stays on disk untouched; recovery uses a new,
   source-linked command id, so the old evidence is never overwritten.
3. ``in_flight.public_start``, the event offsets and the delivery identity of the
   current User message are preserved, so the resumed Code final still belongs to
   this same round.
4. The execution sandbox is re-derived from the copied run directory. A new
   container suffix is written so the clone gets its own control container,
   sandbox container and network instead of colliding with the source run's
   retained ones; the candidate tree, the persisted conversation and the
   accumulated budget all travel with the copy.
5. Policy drift is explicit: only the files this continuation is allowed to
   change may differ, everything else must match the source checkpoint.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from ..episode import save
from .judge import candidate_hash
from .policy import policy_record
from .progressive import ProgressiveEpisode, progressive_config
from .review_barrier import fingerprint
from .reviewed_episode import reset_cloned_execution

RESUMED_COMMAND_SUFFIX = "-r503"
PROVIDER_FAILURE_STATUS = 503
TRUNCATED_OUTPUT = "PROVIDER_OUTPUT_TRUNCATED"
# Implementation files this continuation is allowed to differ on: episode.py
# carries the authorization check, sandbox.py carries the offline candidate
# metadata provisioning. Everything else must match the source checkpoint.
EXPLICITLY_UPGRADED_IMPLEMENTATIONS = ("episode.py", "sandbox.py")
EXPLICITLY_UPGRADED_PROGRESSIVE_FILES = ("sandbox.py",)


def retained_provider_failures(source):
    """Return the provider_failure records retained in the Code relay journal."""
    journal = Path(source) / "private/code/provider.jsonl"
    if not journal.exists():
        return []
    failures = []
    for line in journal.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("kind") == "provider_failure":
            failures.append(record)
    return failures


def paired_tool_events(source):
    """Return (calls, results) tool-call ids recorded by the Code SDK events."""
    calls, results = set(), set()
    for path in sorted((Path(source) / "private/code/sdk").glob("*/events/event-*.json")):
        try:
            event = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError("source Code SDK events are not readable JSON") from error
        identifier = event.get("tool_call_id")
        if not identifier:
            continue
        if event.get("action"):
            calls.add(identifier)
        if event.get("observation"):
            results.add(identifier)
    return calls, results


def _verify_source(source):
    """Validate every precondition and return the retained identifiers."""
    checkpoint = json.loads((source / "private/checkpoint.json").read_text())
    state = checkpoint.get("state", {})
    in_flight = checkpoint.get("in_flight") or {}
    if checkpoint.get("schema") != ProgressiveEpisode.checkpoint_schema:
        raise ValueError("source checkpoint schema is not the progressive schema")
    if state.get("status") != "paused" or state.get("phase") != "code":
        raise ValueError("source is not a paused Code turn")
    if in_flight.get("role") != "code" or not in_flight.get("id"):
        raise ValueError("source in_flight is not a Code turn")
    if not str(state.get("pause_reason", "")).startswith("RuntimeError: SDK turn failed"):
        raise ValueError("source pause is not an SDK turn failure")

    failed_command_id = in_flight["id"]
    active = json.loads((source / "private/code/outbox/active.json").read_text())
    if active.get("status") != "stopped" or active.get("command_id") != failed_command_id:
        raise ValueError("source Code worker has not stopped at the failed command")
    result_path = source / "private/code/outbox" / f"{failed_command_id}.json"
    if not result_path.exists():
        raise ValueError("source has no retained failed Code turn result")
    result = json.loads(result_path.read_text())
    if result.get("status") != "error" or result.get("error_type") != "ConversationRunError":
        raise ValueError("source Code turn result is not a ConversationRunError")

    failures = retained_provider_failures(source)
    if not failures:
        raise ValueError("source has no retained provider failure")
    failure = failures[-1]
    truncated = failure.get("error_code") == TRUNCATED_OUTPUT
    if truncated:
        if (failure.get("stage") != "response_validation"
                or failure.get("error_type") != "RelayOutputLimitError"):
            raise ValueError("truncation was not rejected before forwarding")
    elif failure.get("status") != PROVIDER_FAILURE_STATUS:
        raise ValueError("source provider failure is not replayable")
    failure_id = failure.get("request_id") or failure.get("id")
    if not failure_id:
        raise ValueError("source provider failure has no request id")
    if failure_id not in result.get("traceback", ""):
        raise ValueError("provider failure does not belong to the failed Code turn")
    journal = [json.loads(line) for line in
               (source / "private/code/provider.jsonl").read_text().splitlines() if line.strip()]
    requests = [item for item in journal if item.get("kind") == "request"]
    if not requests or requests[-1].get("id") != failure_id:
        raise ValueError("provider request occurred after retained failure")
    events = sorted((source / "private/code/sdk").glob("*/events/event-*.json"))
    if not events or json.loads(events[-1].read_text()).get("kind") != "ConversationErrorEvent":
        raise ValueError("Code events do not end at the retained failure")
    if truncated:
        responses = [item for item in journal if item.get("kind") == "response"
                     and item.get("id") == failure_id]
        if len(responses) != 1 or not any(choice.get("finish_reason") == "length"
                for choice in responses[0].get("output", {}).get("choices", [])):
            raise ValueError("missing retained truncated provider response")

    if (source / "private/code/sdk/remote-tools/fatal.json").exists():
        raise ValueError("source retains a fatal tool-execution marker")

    calls, results = paired_tool_events(source)
    if calls - results:
        raise ValueError("source has unpaired tool calls; refusing to continue")
    if not calls:
        raise ValueError("source retains no tool call to anchor the continuation")

    pending = (checkpoint.get("budget") or {}).get("pending") or {}
    if truncated:
        if failure_id in pending:
            raise ValueError("truncated response has not been accounted for")
    elif failure_id not in pending:
        raise ValueError("source pending budget does not retain the 503 call")

    return checkpoint, in_flight, failed_command_id, failure


def _resolve_policy(checkpoint):
    """Rebuild config/policy, allowing only the documented implementation drift."""
    raw = copy.deepcopy(checkpoint["config"])
    raw.pop("_progressive_policy", None)
    resolved = progressive_config(raw)
    old_progressive = copy.deepcopy(checkpoint["config"].get("_progressive_policy", {}))
    new_progressive = copy.deepcopy(resolved["_progressive_policy"])
    for value in (old_progressive, new_progressive):
        for name in EXPLICITLY_UPGRADED_PROGRESSIVE_FILES:
            value.pop(name, None)
    if old_progressive != new_progressive:
        raise ValueError("source progressive policy differs beyond the 503 continuation")
    language = resolved.get("dialogue_language", "zh-CN")
    current = policy_record(
        language,
        resolved.get("delegation_variant", "neutral"),
        resolved.get("code_prompt_mode", "local"),
    )
    old_policy = copy.deepcopy(checkpoint.get("policy", {}))
    new_policy = copy.deepcopy(current)
    for value in (old_policy, new_policy):
        implementation = value.get("implementation_sha256", {})
        for name in EXPLICITLY_UPGRADED_IMPLEMENTATIONS:
            implementation.pop(name, None)
    if old_policy != new_policy:
        raise ValueError("source policy differs beyond the explicit 503 continuation")
    return raw, resolved, current


def _clear_stale_runtime(suffix):
    """Remove containers and networks left behind by an interrupted attempt.

    The suffix is derived from the output path, so only resources belonging to
    this exact continuation target can match. The source run keeps its own
    unsuffixed containers and networks and is never touched.
    """
    if not suffix:
        return []
    removed = []
    listing = subprocess.run(
        ['docker', 'ps', '-aq', '--filter', f'name={suffix}'],
        capture_output=True, text=True,
    )
    for identifier in listing.stdout.split():
        inspect = subprocess.run(
            ['docker', 'inspect', '--format', '{{.Name}}', identifier],
            capture_output=True, text=True,
        )
        name = inspect.stdout.strip().lstrip('/')
        subprocess.run(['docker', 'rm', '-f', identifier], capture_output=True)
        removed.append(name)
    networks = subprocess.run(
        ['docker', 'network', 'ls', '-q', '--filter', f'name={suffix}'],
        capture_output=True, text=True,
    )
    for identifier in networks.stdout.split():
        inspect = subprocess.run(
            ['docker', 'network', 'inspect', '--format', '{{.Name}}', identifier],
            capture_output=True, text=True,
        )
        name = inspect.stdout.strip()
        subprocess.run(['docker', 'network', 'rm', identifier], capture_output=True)
        removed.append(name)
    return removed


def clone_provider_503_continuation(source, output):
    """Prepare a new run directory that resumes the interrupted Code turn.

    The copy is staged and only published once every rewrite succeeded, so an
    interrupted continuation can never leave a half-prepared run directory
    behind that looks resumable but is not.
    """
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("output already exists; refusing to overwrite a run directory")

    checkpoint, in_flight, failed_command_id, failure = _verify_source(source)
    failure_id = failure.get("request_id") or failure["id"]
    raw, resolved, current_policy = _resolve_policy(checkpoint)
    candidate_version = candidate_hash(source / "workspace/candidate")

    resumed_command_id = failed_command_id + (
        "-rtruncated" if failure.get("error_code") == TRUNCATED_OUTPUT else RESUMED_COMMAND_SUFFIX)
    if (source / "private/code/inbox" / f"{resumed_command_id}.json").exists():
        raise ValueError("source already retains the resumed command id")

    suffix = hashlib.sha256(str(output).encode()).hexdigest()[:12]
    stale = _clear_stale_runtime(suffix)

    staging = output.with_name(output.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(source, staging)
    checkpoint = json.loads((staging / "private/checkpoint.json").read_text())

    checkpoint["schema"] = ProgressiveEpisode.checkpoint_schema
    checkpoint["config"] = resolved
    checkpoint["policy"] = current_policy
    # Keep the round identity: same role, same public_start, only a new command id.
    checkpoint["in_flight"] = dict(in_flight, id=resumed_command_id)
    checkpoint["state"].update(status="running")
    checkpoint["state"].pop("pause_reason", None)

    # The resume command carries no message: the worker must continue the
    # persisted conversation instead of re-delivering the User turn.
    save(
        staging / "private/code/inbox" / f"{resumed_command_id}.json",
        dict(message=None, condense=False, resume_pending_control=None),
    )

    suffix = hashlib.sha256(str(output).encode()).hexdigest()[:12]
    for role in ("user", "code", "judge"):
        cfg_path = staging / "private" / role / "inbox/config.json"
        if cfg_path.exists():
            value = json.loads(cfg_path.read_text())
            value["container_suffix"] = suffix
            save(cfg_path, value)
    # Drop generated sandbox identities so the clone builds its own container,
    # network and ssh keypair against the copied candidate tree.
    reset_cloned_execution(staging / "private")

    authorization = dict(
        schema="provider-503-continuation-v1",
        authorized=True,
        source=str(source),
        source_checkpoint_sha256=fingerprint(
            json.loads((source / "private/checkpoint.json").read_text())
        ),
        role="code",
        retained_failed_command_id=failed_command_id,
        resumed_command_id=resumed_command_id,
        retained_public_start=in_flight.get("public_start"),
        provider_failure_request_id=failure_id,
        provider_failure_status=failure.get("status"),
        provider_failure_error_code=failure.get("error_code"),
        paired_tool_calls=len(paired_tool_events(source)[0]),
        candidate_version=candidate_version,
        authority=(
            "Explicit continuation of a Code turn interrupted by a replayable provider failure. "
            "The retained failed turn is preserved, the persisted conversation is "
            "continued with no new message, and no completed tool is replayed."
        ),
    )
    checkpoint["provider_503_continuation"] = authorization
    save(staging / "private/provider-503-continuation.json", authorization)
    save(staging / "private/checkpoint.json", checkpoint)
    os.replace(staging, output)
    return raw
