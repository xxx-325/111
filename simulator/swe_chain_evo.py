"""Pinned SWE-Chain-Evo JSON source adapter."""

from __future__ import annotations

import json
import re
from pathlib import Path


REVISION = re.compile(r"[0-9a-f]{40}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SWE-Chain-Evo {name} must be non-empty text")
    return value


def _strings(value, name):
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"SWE-Chain-Evo {name} must be a non-empty string list")
    return list(value)


def _download(source, filename):
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as error:
        raise RuntimeError(
            "huggingface-hub is required to read the pinned SWE-Chain-Evo cache"
        ) from error
    return Path(
        hf_hub_download(
            repo_id=source["repo_id"],
            filename=filename,
            repo_type="dataset",
            revision=source["revision"],
            local_dir=source["local_dir"],
            local_files_only=source.get("local_files_only", False),
        )
    )


def _load(source, filename, download):
    path = Path(download(source, filename))
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"SWE-Chain-Evo file is not a JSON object: {filename}")
    return value


def load_chain(config, repo, download=_download):
    """Return an explicit base and existing task records for one pinned chain."""
    source = config.get("swe_chain_evo")
    if not isinstance(source, dict):
        raise ValueError("swe_chain_evo must be an object")
    required = {"repo_id", "revision", "chain_id", "local_dir", "limit"}
    missing = required - set(source)
    if missing:
        raise ValueError("missing SWE-Chain-Evo fields: " + ", ".join(sorted(missing)))
    if not REVISION.fullmatch(str(source["revision"])):
        raise ValueError("SWE-Chain-Evo revision must be a full 40-character commit")
    limit = source["limit"]
    if not isinstance(limit, int) or not 1 <= limit <= 2:
        raise ValueError("SWE-Chain-Evo limit must be 1 or 2 for this experiment")
    if config.get("continuous_commits"):
        raise ValueError("SWE-Chain-Evo must not use continuous_commits")

    prefix = f"chains/{source['chain_id']}"
    manifest = _load(source, prefix + "/chain.json", download)
    if manifest.get("chain_id") != source["chain_id"]:
        raise ValueError("SWE-Chain-Evo manifest identity mismatch")
    instances = manifest.get("instances")
    if (
        not isinstance(instances, list)
        or manifest.get("instance_count") != len(instances)
        or len(instances) < limit
    ):
        raise ValueError("SWE-Chain-Evo manifest instance count is invalid")

    tasks = []
    for index, summary in enumerate(instances[:limit]):
        if (
            not isinstance(summary, dict)
            or summary.get("version_chain_index") != index
            or not isinstance(summary.get("instance_id"), str)
        ):
            raise ValueError("SWE-Chain-Evo manifest order is invalid")
        filename = f"{prefix}/instances/{index:03d}-{summary['instance_id']}.json"
        instance = _load(source, filename, download)
        raw = instance.get("raw")
        if not isinstance(raw, dict):
            raise ValueError("SWE-Chain-Evo instance raw field is missing")
        if (
            instance.get("instance_id") != summary["instance_id"]
            or instance.get("version_chain_id") != manifest["chain_id"]
            or instance.get("version_chain_index") != index
            or instance.get("repo") != manifest.get("repo")
            or raw.get("repo") != manifest.get("repo")
        ):
            raise ValueError("SWE-Chain-Evo instance identity mismatch")
        base = _text(raw.get("base_commit"), "raw.base_commit")
        target = _text(raw.get("target_commit"), "raw.target_commit")
        base = _resolve(repo, base)
        target = _resolve(repo, target)
        fail_to_pass = _strings(instance.get("fail_to_pass"), "fail_to_pass")
        pass_to_pass = _strings(instance.get("pass_to_pass"), "pass_to_pass")
        if fail_to_pass != raw.get("FAIL_TO_PASS") or pass_to_pass != raw.get(
            "PASS_TO_PASS"
        ):
            raise ValueError("SWE-Chain-Evo public and raw test targets differ")
        task = dict(
            kind="swe_chain_evo",
            title="Project evolution milestone",
            body=_text(instance.get("problem_statement"), "problem_statement"),
            identifier=instance["instance_id"],
            reference=target,
            base=base,
            patch=_text(raw.get("patch"), "raw.patch"),
            test_patch=_text(raw.get("test_patch"), "raw.test_patch"),
            test_cmds=_strings(raw.get("test_cmds"), "raw.test_cmds"),
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            target_test_names=_strings(
                raw.get("target_test_names"), "raw.target_test_names"
            ),
            p2p_guard_tests=_strings(
                raw.get("p2p_guard_tests"), "raw.p2p_guard_tests"
            ),
            source=dict(
                repo_id=source["repo_id"],
                revision=source["revision"],
                chain_id=manifest["chain_id"],
                index=index,
                image=instance.get("image"),
            ),
        )
        tasks.append(task)

    for previous, current in zip(tasks, tasks[1:]):
        if current["base"] != previous["reference"]:
            raise ValueError(
                "SWE-Chain-Evo base gap: the next base_commit is not the previous "
                "target_commit; this experiment will not inject hidden commits"
            )
    return tasks[0]["base"], tasks


def _resolve(repo, revision):
    from .tasks import git

    return git(repo, "rev-parse", "--verify", revision + "^{commit}")
