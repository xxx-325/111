"""Host-owned SWE-Chain-Evo tests in a Judge-only experiment copy."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..episode import clone_candidate, save
from .judge import candidate_hash
from .sandbox import writable_tree


SUMMARY = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(.+)$")
COLLECTED = re.compile(r"collected\s+(\d+)\s+items?", re.IGNORECASE)
ENVIRONMENT_ERRORS = (
    "ERROR collecting",
    "ImportError while loading",
    "ModuleNotFoundError",
    "No module named pytest",
    "command not found",
)


def _reported(text):
    rows = []
    for raw in text.splitlines():
        match = SUMMARY.match(raw.strip())
        if not match:
            continue
        node = match.group(2).split(" - ", 1)[0].strip()
        rows.append((match.group(1).lower(), node))
    return rows


def _target_status(target, rows):
    states = [
        state
        for state, node in rows
        if node == target or node.startswith(target + "[")
    ]
    if not states:
        return "missing"
    if any(state in ("failed", "error", "xpass") for state in states):
        return "failed"
    if all(state == "passed" for state in states):
        return "passed"
    return "unknown"


def _apply_test_patch(target, patch):
    before = candidate_hash(target)
    environment = {
        **os.environ,
        "GIT_CEILING_DIRECTORIES": str(Path(target).resolve().parent),
    }
    checked = subprocess.run(
        ["git", "apply", "--no-index", "--check", "--whitespace=nowarn", "-"],
        cwd=target,
        input=patch,
        text=True,
        capture_output=True,
        env=environment,
    )
    if checked.returncode:
        raise ValueError("SWE-Chain-Evo test patch does not apply to the test copy")
    subprocess.run(
        ["git", "apply", "--no-index", "--whitespace=nowarn", "-"],
        cwd=target,
        input=patch,
        text=True,
        check=True,
        capture_output=True,
        env=environment,
    )
    if candidate_hash(target) == before:
        raise RuntimeError("SWE-Chain-Evo test patch made no changes")


def _file_hashes(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path(root).rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _install_test_patch(target, base, patch):
    """Overlay authoritative patched-base files without trusting candidate tests."""
    authoritative = Path(target).parent / (Path(target).name + "-authoritative-tests")
    clone_candidate(Path(base), authoritative)
    before = _file_hashes(authoritative)
    _apply_test_patch(authoritative, patch)
    after = _file_hashes(authoritative)
    changed = sorted(name for name in before.keys() | after.keys()
                     if before.get(name) != after.get(name))
    if not changed:
        raise RuntimeError("SWE-Chain-Evo test patch changed no files")
    for name in changed:
        destination = Path(target) / name
        source = authoritative / name
        if name not in after:
            if destination.exists():
                destination.unlink()
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return changed


def run_required_tests(
    sandbox,
    source,
    experiments,
    task,
    label,
    expectation,
    *,
    apply_test_patch,
    test_base=None,
    timeout=900,
    candidate_pythonpath=None,
):
    """Run dataset commands without writing to the Code or Judge candidate tree."""
    if expectation not in ("base", "candidate", "reference"):
        raise ValueError("unknown SWE-Chain-Evo test expectation")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", label):
        raise ValueError("unsafe SWE-Chain-Evo experiment label")
    experiments = Path(experiments)
    target = experiments / label
    if target.exists():
        raise FileExistsError("SWE-Chain-Evo experiment already exists")
    source_hash = candidate_hash(source)
    clone_candidate(Path(source), target)
    patch_files = []
    if apply_test_patch:
        patch_files = _install_test_patch(
            target, test_base or source, task["test_patch"]
        )
    writable_tree(target)

    commands = []
    rows = []
    collected = 0
    environment_error = False
    for index, command in enumerate(task["test_cmds"], 1):
        started = time.monotonic()
        args = [
            "docker",
            "exec",
            "-u",
            "1000",
            "-w",
            f"/workspace/experiments/{label}",
        ]
        if candidate_pythonpath:
            path = candidate_pythonpath.replace(
                "/workspace/candidate", f"/workspace/experiments/{label}", 1
            )
            args += ["-e", "PYTHONPATH=" + path]
        args += [sandbox.name, "sh", "-lc", command]
        try:
            result = subprocess.run(
                args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            output, exit_code, timed_out = result.stdout, result.returncode, False
        except subprocess.TimeoutExpired as error:
            sandbox.pause()
            output = error.stdout or b""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            stderr = error.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            output += stderr
            log = target / f"test-{index}.log"
            log.write_text(output, encoding="utf-8")
            result_file = f"/workspace/experiments/{label}/result.json"
            save(
                target / "result.json",
                dict(
                    schema="swe-chain-evo-tests-v1",
                    instance_id=task["identifier"],
                    expectation=expectation,
                    outcome="unknown",
                    collected=collected,
                    commands=[
                        *commands,
                        dict(
                            command=command,
                            exit_code=None,
                            timed_out=True,
                            collected=0,
                            duration_seconds=round(time.monotonic() - started, 3),
                            output_file=log.name,
                            output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                        ),
                    ],
                    fail_to_pass={name: "missing" for name in task["fail_to_pass"]},
                    pass_to_pass={name: "missing" for name in task["pass_to_pass"]},
                    test_patch_sha256=hashlib.sha256(
                        task["test_patch"].encode()
                    ).hexdigest(),
                    test_patch_files=patch_files,
                    result_file=result_file,
                    candidate_version=source_hash,
                    stop_reason="timeout; execution sandbox paused as uncertain",
                ),
            )
            raise RuntimeError(
                "SWE-Chain-Evo test timed out; execution sandbox paused as uncertain"
            ) from error
        log = target / f"test-{index}.log"
        log.write_text(output, encoding="utf-8")
        command_rows = _reported(output)
        rows.extend(command_rows)
        counts = [int(value) for value in COLLECTED.findall(output)]
        command_collected = max(counts, default=0)
        collected += command_collected
        environment_error = environment_error or timed_out or any(
            marker in output for marker in ENVIRONMENT_ERRORS
        )
        commands.append(
            dict(
                command=command,
                exit_code=exit_code,
                timed_out=timed_out,
                collected=command_collected,
                duration_seconds=round(time.monotonic() - started, 3),
                output_file=log.name,
                output_sha256=hashlib.sha256(output.encode()).hexdigest(),
            )
        )

    f2p = {target: _target_status(target, rows) for target in task["fail_to_pass"]}
    p2p = {target: _target_status(target, rows) for target in task["pass_to_pass"]}
    missing = any(value == "missing" for value in [*f2p.values(), *p2p.values()])
    if environment_error or collected == 0 or missing:
        outcome = "unknown"
    elif expectation == "base":
        outcome = (
            "expected_failure"
            if all(value == "failed" for value in f2p.values())
            and all(value == "passed" for value in p2p.values())
            else "failed"
        )
    else:
        outcome = (
            "passed"
            if all(value == "passed" for value in [*f2p.values(), *p2p.values()])
            and all(item["exit_code"] == 0 for item in commands)
            else "failed"
        )
    record = dict(
        schema="swe-chain-evo-tests-v1",
        instance_id=task["identifier"],
        expectation=expectation,
        outcome=outcome,
        collected=collected,
        commands=commands,
        fail_to_pass=f2p,
        pass_to_pass=p2p,
        test_patch_sha256=hashlib.sha256(task["test_patch"].encode()).hexdigest(),
        test_patch_files=patch_files,
        result_file=f"/workspace/experiments/{label}/result.json",
        candidate_version=source_hash,
    )
    save(target / "result.json", record)
    return record


def compact_result(record):
    """Keep the Judge prompt small while retaining actual command identities."""
    return dict(
        schema=record["schema"],
        expectation=record["expectation"],
        outcome=record["outcome"],
        collected=record["collected"],
        commands=[
            dict(
                command=item["command"],
                exit_code=item["exit_code"],
                timed_out=item["timed_out"],
                collected=item["collected"],
                output_file=item["output_file"],
            )
            for item in record["commands"]
        ],
        fail_to_pass=dict(record["fail_to_pass"]),
        pass_to_pass=dict(record["pass_to_pass"]),
        result_file=record["result_file"],
        candidate_version=record["candidate_version"],
    )


def validate_required_verdict(required, outcome):
    if required["outcome"] == "unknown" and outcome != "uncertain":
        raise ValueError(
            "unknown required-test execution only supports an uncertain verdict"
        )
    if outcome == "solved" and required["outcome"] != "passed":
        raise ValueError("solved requires passing SWE-Chain-Evo tests")
