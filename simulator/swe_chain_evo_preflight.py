"""Reference preflight for one pinned SWE-Chain-Evo milestone; no model calls."""

import argparse
import json
import uuid
from pathlib import Path

from .episode import save
from .tasks import prepare, snapshot
from .openhands.evo_tests import run_required_tests
from .openhands.sandbox import ExecutionSandbox


def preflight(config, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    repo, _, tasks = prepare(config, private, include_patch=False)
    if len(tasks) != 1 or tasks[0].get("kind") != "swe_chain_evo":
        raise ValueError("preflight requires exactly one SWE-Chain-Evo task")
    task = tasks[0]
    reference = private / "reference"
    snapshot(repo, task["base"], reference / "base")
    snapshot(repo, task["reference"], reference / "fixed")
    workspace = output / "judge-workspace"
    snapshot(repo, task["base"], workspace / "candidate")
    (workspace / "checks").mkdir(parents=True)
    (workspace / "experiments").mkdir(parents=True)
    sandbox = ExecutionSandbox(
        private / "sandbox",
        workspace,
        config["execution_image"],
        "judge",
        "evo-preflight-" + uuid.uuid4().hex,
        reference,
    )
    sandbox.prepare()
    pythonpath = config.get("judge", {}).get("candidate_pythonpath")
    try:
        base = run_required_tests(
            sandbox,
            reference / "base",
            workspace / "experiments",
            task,
            "base",
            "base",
            apply_test_patch=True,
            candidate_pythonpath=pythonpath,
        )
        fixed = run_required_tests(
            sandbox,
            reference / "fixed",
            workspace / "experiments",
            task,
            "reference",
            "reference",
            apply_test_patch=False,
            candidate_pythonpath=pythonpath,
        )
    finally:
        sandbox.pause()
    report = dict(
        schema="swe-chain-evo-preflight-v1",
        source=task["source"],
        instance_id=task["identifier"],
        base_commit=task["base"],
        target_commit=task["reference"],
        base=base,
        reference=fixed,
        status=(
            "passed"
            if base["outcome"] == "expected_failure"
            and fixed["outcome"] == "passed"
            else "failed"
        ),
    )
    save(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Run the first SWE-Chain-Evo reference preflight without agents."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = preflight(json.loads(args.config.read_text()), args.output)
    print(json.dumps({"status": result["status"], "output": str(args.output)}))
    if result["status"] != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
