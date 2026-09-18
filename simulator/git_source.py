from __future__ import annotations

import subprocess
from pathlib import Path
import shutil

from .models import CommitTarget


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode:
        raise GitError(proc.stderr.strip() or "git command failed")
    return proc.stdout


def resolve_commits(repo: Path, commits: list[str]) -> list[CommitTarget]:
    if not commits:
        raise ValueError("at least one commit is required")
    resolved: list[CommitTarget] = []
    previous = None
    for raw in commits:
        commit = _git(repo, "rev-parse", raw).strip()
        parent_line = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()
        if len(parent_line) < 2:
            raise GitError(f"commit {commit} has no parent; root commits are unsupported")
        parent = parent_line[1]
        if previous and parent != previous:
            raise GitError(f"commits are not a linear sequence: {raw} does not follow the previous commit")
        subject = _git(repo, "show", "-s", "--format=%s", commit).strip()
        diff = _git(repo, "diff", "--binary", parent, commit)
        resolved.append(CommitTarget(commit, parent, subject, diff))
        previous = commit
    return resolved


def materialize_snapshot(repo: Path, base_commit: str, destination: Path) -> None:
    """Materialize only a commit tree, without .git or reachable history."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", base_commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    import tarfile
    import io
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        tar.extractall(destination)


def remove_worktree(repo: Path, destination: Path) -> None:
    del repo
    shutil.rmtree(destination, ignore_errors=True)


def diff_summary(repo: Path) -> str:
    return _git(repo, "status", "--short") + _git(repo, "diff", "--stat")


def snapshot_manifest(root: Path) -> dict[str, bytes]:
    """Return file bytes for a plain snapshot directory (which has no .git)."""
    result: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def manifest_summary(before: dict[str, bytes], root: Path) -> str:
    after = snapshot_manifest(root)
    changed = sorted(set(before) | set(after))
    lines: list[str] = []
    for name in changed:
        if name not in before:
            lines.append(f"A {name}")
        elif name not in after:
            lines.append(f"D {name}")
        elif before[name] != after[name]:
            lines.append(f"M {name}")
    return "\n".join(lines) or "No working-tree changes were reported."
