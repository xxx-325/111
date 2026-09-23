# Continuous commit memory episodes

This opt-in path retains every original task in a consecutive first-parent commit
chain, and may insert multiple related requirements after each commit. The Code
candidate and both conversations remain continuous. Extensions do not have a
reference patch to apply. The existing state machine, send review, Judge, sandbox
and silent final acceptance remain in use.

## Prepare and run

Run these commands from this repository root with its environment activated.
Resolve configuration paths from that working directory. The offline check is
`python -m pytest tests/ -q`; model runs also require the configured Docker images
and provider credentials.

Start with the existing progressive config and an exact continuous commit chain:

```json
{
  "continuous_commits": true,
  "progressive_issues": true,
  "tasks": [{"commit": "FIRST_FULL_SHA"}, {"commit": "NEXT_FULL_SHA"}]
}
```

Keep the existing repository, base, model, image and budget settings. First
prepare a private scenario using the existing decomposer model and relay:

```bash
python -m simulator.openhands.prepare_scenario \
  --config config.json --env-file provider.env --output runs/scenario-preparation
```

This makes one draft and one read-only review per commit, using the exact diff,
changed files at the base, and prior scenario. It retains inputs, drafts and
reviews, stops on rejection, and writes `frozen/scenario.json` only after all
commits pass. No Code Agent is run during preparation. This command calls the
model; its output is a candidate design, not evidence of memory effectiveness.

Add `scenario_file: "runs/scenario-preparation/frozen/scenario.json"` to the
config. A manually authored file uses the same schema; see
[the structural example](../examples/continuous-commit-scenario.json). Then
prepare the entire expanded task queue and run it through the existing entry:

```bash
python -m simulator.openhands.prepare_progressive \
  --config config.json --env-file provider.env --output runs/requirements
```

Set `prepared_issues: "runs/requirements/report.json"` only if that report passes,
then start a new run:

```bash
python -m simulator --config config.json --env-file provider.env --output runs/episode
```

For this scenario CLI path, successful completion automatically exports to
`runs/episode-package` and compacts the original run. The original directory keeps
a cleanup receipt pointing to the package; it is no longer resumable. Paused,
failed or in-flight runs keep their working state. Existing non-scenario runs do
not acquire automatic cleanup.

Scenario bytes are pinned in the run configuration. A changed scenario or runtime
policy cannot silently resume an old run. Do not skip commits or inject their
reference patches. Exact edit preconditions may fail when the accumulated Code
implementation differs from the preparation; this pauses for inspection rather
than overwriting the agent's work.

## Information and release

Facts contain only an identity, M1–M6 type, text, scope, disclosure trigger and
optional earlier identities they supersede. M1 conventions, M2 external facts
and M6 decisions stay on the controller/User side. Only M3 misleading repository
paths, M4 costly trial and M5 runtime variation may have repository edits.
Edits contain exact before/after text, a relative path, type and reason. All
preconditions are checked before writing; interruption during writes is not
automatically replayed. The Code sandbox is frozen during application. First
User turns and immediate task handoffs both prepare edits before Code can work.

Judge receives the current scoped facts, trigger definitions and Code's current
public question, tool calls and provider-bound tool responses. The existing
read-only audit checks whether a trigger is supported. Passing time or another
failed round never releases a controlled fact by itself. Untriggered facts may
remain unused; they do not justify manufactured errors or prolonged discussion.
This is a semantic model check, not proof that every trigger will be classified
correctly. Test real dialogues before admitting benchmark samples.

Once User knows a fact it is retained across tasks. A later correction applies
only within its stated scope. User knowing a fact does not prove that it was
communicated to Code: downstream QA must cite actual public dialogue or tools.
No additional environment Agent is created. Synthetic external conditions are
frozen settings; real execution evidence still comes from tools, never from
invented outputs. M4/M5 designs needing new runtime dependencies or services
require a separately prepared image; this module does not synthesize them.

## Export for memory and QA

After a run stops at a complete tool boundary:

```bash
python -m simulator.openhands.memory_episode \
  --source runs/episode --output runs/episode-package
```

That standalone export is read-only with respect to the run. Add `--compact-source`
only for a completed run to apply the same verified cleanup. A package exported
automatically at completion already exists; do not export it a second time.

The new output contains:

- `dialogue.jsonl`, `dialogue.json`, `dialogue.html`: public User/Code messages,
  tool calls and tool results, in original order with original event identities.
- `snapshot/`: accumulated candidate without Git, environments or generated caches.
- `manifest.json`: dialogue byte hash, cutoff event and snapshot fingerprint.
- `control-config.json`: model/image settings selected without credentials.
- `private/scenario-index.json`, when applicable: commit/task/path/public-event
  navigation only, with no hidden fact text or patch. Never ingest this as memory.

After compaction, `private/review.json` retains requirements, scenario settings,
scoped corrections, disclosure, state choices and Judge review/rejection reasons.
`private/trace.jsonl.gz` retains private User/Judge events, model responses, unique
instruction/tool definitions, original terminal evidence and Judge check sources.
Public Code tools are already in dialogue; repeated complete provider request
histories and duplicate SDK files are discarded. Request hashes and usage remain;
compacted runs cannot reconstruct every exact model context or resume the SDK.
The final code is kept only in `snapshot/`; temporary candidate/reference copies,
caches, mailboxes, keys and verified run-owned Docker resources are released.
Cleanup first validates the exported dialogue/code and compressed evidence;
active workers or validation failures prevent file removal.

Preparation keeps its small per-commit source/draft files, frozen settings and
review report for editing the design; its provider journal is compressed once.
External source repositories, preparation directories and historical runs are
never recursively cleaned by run compaction. The preparation command removes only
its own temporary source clone after retaining its inputs and review. The HTML and JSON dialogue views
are intentional browse/program interfaces to the same public events.

`model-visible-dialogue-v1` uses `id`, `kind`, one-based `sequence`, `timestamp`,
and message `text` or tool `call_id`/`tool_name` plus `action`/`text`. Sequence is
the authoritative order. Timestamps are the public collector timestamps. The
snapshot hash is `relative-path-executable-content-v1`, shared with QA.

Tool results come from the first provider-bound request containing that response,
before later condensation. Private editor `old_content`/`new_content` cannot
replace a short response actually sent to Code. Tool errors and clipping markers
remain as recorded text. A provider-bound request proves the submitted context,
not that the provider successfully processed it. Missing evidence, unpaired tools,
nontext tool content or an in-flight run refuse export. The old two-party
`dialogue.json` in the run directory is unchanged; use the package for this pipeline.

Only the public dialogue goes to memory extraction. QA may start from public event
IDs or related code locations, but an answer needs public support before the
cutoff. The new solving task follows the cutoff and should exercise the relevant
history without repeating the construction task. Snapshot and control config are
separate inputs to task evaluation. The current downstream comparison is no
memory versus QA-answer oracle context, not a measured retrieval system.

## Verification limits

Offline tests cover consecutive source commits, multiple extensions, guarded
release, scoped updates, exact edits, resume fences and export fidelity. Synthetic
package interoperation validates the QA schema and tool visibility. It does not
establish real agent misdirection, expensive retries, repository-only difficulty
or improved historical compliance. Repository edits and preparation review can
remove direct clues; no claim of complete clue removal follows from dropping Git.
Admit samples only after inspecting real public evidence and comparison runs.
No-memory success is not by itself a reason to discard a sample: fewer repeated
clarifications and failed approaches can still be relevant outcomes.
