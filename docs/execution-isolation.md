# Execution isolation

The OpenHands control runtime and the project tool runtime are different trust
boundaries. A candidate `PYTHONPATH` selects imports; it does not hide other
installed source files. The older shared-container experiments are not evidence
of source isolation.

## Roles

| Role | Model context | Executable workspace |
| --- | --- | --- |
| User | Released requirement, public conversation, reviewed observations | None; host dialogue controls only |
| Code | Public User messages and its own work | Candidate, read/write |
| Judge | Current full requirement and prior checks | Independent candidate and current reference, read-only; private checks, read/write |

Only the host owns future tasks, provider credentials, and authoritative task
progress. Acceptance requires a current Judge verdict and a User decision.
Reference code is never applied to Code's candidate.

## Tool boundary

The `ssh_sandbox` backend keeps SDK conversations, persistence, and model access
in the control container. Terminal and file actions are sent to a separate
project container over SSH/SFTP. The project image must contain the required
language/test dependencies, not the SDK or another installed version of the
target project. Runtime configuration pins image digests.

Each role has a separate SSH identity and internal network. The model's process
has no host Docker socket, provider keys, SDK logs, reference material from other
roles, or external network access. Code and Judge execute as non-root. Browser
and other unadapted tools must fail closed; adapter failures must never execute
the action locally.

Task tracking is role-local structured state, not arbitrary file access.
Readable long-output files belong only to that role's tool-output directory.
Raw SDK logs remain control-side.

## Continuity and failure

OpenHands continues to own the agent loop and context condensation. A persistent
remote shell retains cwd and environment; file edit history supports undo. These
features need real adapter tests, not merely matching method names.

When freezing Code for a snapshot, freeze its execution container too: stopping
the control process alone does not stop background candidate mutations.
Candidate and reference mounts for Judge remain read-only.

Restore must verify the recorded image, mounts, role, execution identity, and
delivery position. Uncertain commands must not be submitted again. Older
shared-container checkpoints are retained, not silently upgraded.

## Evidence required before model runs

- Both actual terminal and file tools fail to read control-side and cross-role
  sentinels, including through symlinks and `/proc`.
- The target imports from candidate source; the clean environment has no
  installed alternative target version.
- External, cross-role, model-relay, and Docker-socket access fail.
- Edit/test/input/timeout/continuation/background/undo and safe restart work.
- A paused Code background process cannot change the version Judge inspects.
- An adapter transport failure is retained without local execution or replay.

Store commands and results separately from public dialogue. A test's failure to
find a known path is not a proof against all sandbox escapes. Container isolation
also cannot prevent a pretrained model from recognizing a public task.

Actual implementation and probe results are recorded in each run's private
artifacts; this architecture description alone does not certify a run.

Use `python -m simulator.openhands.sandbox_probe --output runs/isolation-probe`
to produce evidence for the image and host you intend to use. Browser and other
tools without an equivalent isolated executor remain outside this boundary.
