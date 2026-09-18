# Agent Session Simulator

Generate auditable, multi-turn software-development dialogues from issues, commits, pull requests, or SWE-Chain-Evo tasks. A persistent User agent talks to a persistent Code agent, while a private Judge checks the current candidate and decides whether work should continue.

[中文说明](README.zh.md)

## What it does

- Runs User, Code, and Judge as separate persistent OpenHands conversations.
- Reveals requirements progressively instead of exposing the full answer at turn one.
- Uses a host-controlled state machine to select valid user intents.
- Executes Code and Judge tools in separate SSH-connected Docker sandboxes.
- Keeps reference fixes, future tasks, Judge evidence, and provider credentials out of the Code agent's workspace.
- Continues an unsolved task until it is accepted, blocked, or reaches the configured resource limit.
- Exports `dialogue.json` and `dialogue.html` containing only public User and Code messages.

The simulator is an experimental data-generation tool. A completed run is not proof that its dialogue matches the full distribution of real users.

## Repository layout

```text
simulator/   orchestration, agents, state machine, Judge, exporters
tests/       unit and contract tests
docker/      control and execution image definitions
containers/  OpenHands control image definition
examples/    issue and configuration examples
docs/        architecture and isolation notes
flow.html    standalone pipeline overview
```

Generated runs, model logs, reference material, local datasets, virtual environments, and `.env` files are intentionally excluded from Git.

## Requirements

- Python 3.12
- Docker
- Git
- An OpenAI-compatible model endpoint for each configured role
- macOS or Linux host support for the current SSH sandbox implementation

OpenHands dependencies are pinned in `openhands-requirements.txt` and `openhands-linux.lock`.

Build the local control image from the repository root when needed:

```bash
docker build -f containers/openhands/Dockerfile -t local/session-openhands:1.47.0 .
docker build -f docker/control/Dockerfile -t local/agent-session-control:1.47.0 .
```

## Install

```bash
python3.12 -m venv .venv-openhands
source .venv-openhands/bin/activate
python -m pip install -r openhands-requirements.txt
export PYTHONPATH=.
```

Create a local `.env` that is never committed:

```dotenv
DEEPSEEK_API_KEY=
```

## Prepare a target repository

Example configurations expect the target checkout to be local. For the boltons example:

```bash
mkdir -p runs
git clone https://github.com/mahmoud/boltons.git runs/source-boltons
```

The base revision and task references in a configuration must be reachable from that checkout. Reference commits are available only to the Judge side; they are never applied to the Code candidate.

## Run

```bash
OPENHANDS_SUPPRESS_BANNER=1 python -m simulator \
  --config examples/openhands-progressive-single.json \
  --env-file .env \
  --output runs/example
```

Resume only a compatible, safely stopped run:

```bash
OPENHANDS_SUPPRESS_BANNER=1 python -m simulator \
  --config examples/openhands-progressive-single.json \
  --env-file .env \
  --output runs/example \
  --resume
```

Export a public-only dialogue:

```bash
python -m simulator.openhands.dialogue_export runs/example
```

The exporter writes `dialogue.json` and `dialogue.html`. Private checkpoints, tool events, Judge evidence, and model requests remain under the ignored run directory.

## Progressive flow

1. Prepare the current task and split its known information into small symptom/cause fragments.
2. Release at most one new fragment for a turn.
3. Let User select a valid state-machine action and send a natural request.
4. Let Code inspect and modify only the candidate sandbox.
5. Let Judge inspect an independent candidate copy and the current reference.
6. If unsolved, project only an observable failure back to User and continue.
7. If solved, User may accept or naturally ask about the implementation before accepting.
8. Release the next task only after acceptance.

See [docs/execution-isolation.md](docs/execution-isolation.md) for the trust boundary.

## Configuration notes

- `runtime: "openhands"` enables persistent OpenHands conversations.
- `progressive_issues: true` enables fragment release and Judge-controlled progression.
- `execution_backend: "ssh_sandbox"` is required by current isolated runs.
- `dialogue_language` controls public language; code and raw errors remain unchanged.
- `max_seconds` bounds a run. A timeout preserves the incomplete run; it never marks the task solved.
- User, Code, Judge, and decomposer may use separate model endpoints and key environment variables.
- `code_prompt_mode: "sdk_default"` keeps the Code agent on the SDK default role prompt.

Example files contain public task metadata only. Replace local checkout paths, image digests, and model settings for your environment.

## Testing

Run the dependency-independent and OpenHands contract suite:

```bash
OPENHANDS_SUPPRESS_BANNER=1 python -m unittest discover -s tests
```

Docker integration tests are opt-in and require a compatible local image:

```bash
SIMULATOR_DOCKER_TEST_IMAGE=your-image \
  OPENHANDS_SUPPRESS_BANNER=1 \
  python -m unittest discover -s tests
```

## Security and data handling

- Never place API keys, private keys, raw provider logs, or real user transcripts in the repository.
- User has no repository tools in the isolated mode.
- Code sees only its writable candidate and public User messages.
- Judge sees a read-only candidate/reference copy and a separate writable checks directory.
- Sandbox isolation limits the tool surface; it is not a complete defense against malicious code.
- A model may recognize a public task from pretraining even when the reference patch is isolated.

## License

MIT. See [LICENSE](LICENSE).
