# AgentTeam

A simulated software engineering team of AI agents. You are the manager; the agents are the team.

Four agents — Backend, DevOps, Frontend, Database — each with a distinct role, model, and system
prompt. They read and write files in a shared sandboxed workspace, run commands, and (from Phase 3)
talk to each other. Every API call, tool call, and message is persisted so any run can be
reconstructed exactly.

## Status

**Phase 2 of 5 — full roster, task lifecycle, live streaming.** No UI yet. All four agents run
independently over HTTP, in the background, with every step streaming over a WebSocket.

| Agent | Name | Model | Owns |
|---|---|---|---|
| Backend | Ada | `claude-opus-5` | Endpoints, business rules, auth, service layer |
| DevOps | Kai | `claude-opus-5` | Containers, CI/CD, env config, deployment, monitoring |
| Frontend | Mira | `claude-sonnet-5` | Components, client state, styling, accessibility |
| Database | Ines | `claude-sonnet-5` | Schema, migrations, indexes, query performance |

## Requirements

- Python 3.11+
- Docker (for sandboxed command execution — see [Sandboxing](#sandboxing))
- An Anthropic API key

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # then set ANTHROPIC_API_KEY
docker build -t agentteam-runtime:latest docker/
alembic -c backend/alembic.ini upgrade head
```

## API

```
POST   /api/tasks              create and dispatch; returns immediately with a task id
GET    /api/tasks              list, filterable by agent_key and status
GET    /api/tasks/{id}         one task with full message and tool-call history
GET    /api/tasks/{id}/trace   the whole run as one list, ordered in real time
POST   /api/tasks/{id}/review  approve, or reject with feedback to re-queue
GET    /api/agents             roster with live status
WS     /ws                     typed event stream
```

Tasks execute in a background worker pool — one queue per agent, so an agent's own
work is serialised while different agents run concurrently. The HTTP request never
waits on a run.

### Task lifecycle

```
queued → in_progress → needs_review → done
              ↓                ↓
     failed / budget_exceeded  └→ queued   (rejection, with feedback)
```

Transitions are validated centrally; an illegal move raises rather than corrupting
the trace. Rejection re-queues the task with your feedback appended as a new user
message, and the agent resumes with its prior conversation still in context.

### Event stream

Seven typed events — `agent.status_changed`, `task.created`, `task.status_changed`,
`message.created`, `tool.started`, `tool.finished`, `usage.updated` — each carrying a
monotonic `seq` so a client can detect a gap. Reconnect with `?since_seq=N` to replay
what was missed.

TypeScript types are generated from the Pydantic schemas:

```bash
python scripts/export_types.py
```

CI fails if the committed `frontend/src/lib/events.ts` drifts from the backend.

## Running an agent

```bash
python -m app.cli run --agent backend --task "Create a FastAPI endpoint that returns a list of books from a hardcoded list, with a Pydantic response model."
```

Output lands in `workspace/`. Inspect the trace with:

```bash
python -m app.cli show --task <task_id>
```

## Running the API

On the host:

```bash
uvicorn app.main:app --reload
```

Or with Compose, which also builds the sandbox image first:

```bash
docker compose up --build
```

Compose mounts the host Docker socket so the API can create sibling sandbox
containers. That gives the API container control of the host daemon, so read the
comment at the top of `docker-compose.yml` before using it. The trust boundary is
unchanged: AgentTeam's own code is trusted, agent-generated code is not, and agent
code still runs only in the locked-down sibling container.

## Configuration

Agent identity is data, not code. Each agent is one YAML file in `config/agents/`:

```yaml
key: backend
display_name: Ada
model: claude-opus-5
role: API and business logic
system_prompt: |
  ...
tools: [read_file, write_file, list_files, run_command]
```

Changing an agent's model is a one-line edit. Adding an agent is a new file. Swapping the whole
roster for a different team — research, data — is a config change, not a rewrite.

## Sandboxing

Agents get a `run_command` tool. It is constrained by the runtime, not by the system prompt.

**Default (`AGENTTEAM_SANDBOX_MODE=docker`):** every command runs in a disposable container —
`--rm`, `--network=none`, non-root user, read-only root filesystem, memory and PID limits, with
`workspace/` bind-mounted as the only writable path. The container is killed on timeout.

**Why a container and not just an allow-list:** the allow-list includes `python`, `pip`, `npm`, and
`git`. Each of those independently grants arbitrary code execution — `python -c "import os; ..."` is
a one-command escape, `pip` and `npm` execute code at install time, and `git` aliases of the form
`!sh -c '...'` run arbitrary shell. An allow-list containing those four is not a security boundary.
The container is the boundary; path resolution and the allow-list are defense in depth on top of it.

**`AGENTTEAM_SANDBOX_MODE=subprocess`** runs commands directly on the host with only the path checks,
allow-list, timeout, and process-group kill. It exists for environments without Docker. It is
**not** a security boundary against adversarial output, for the reasons above. Do not use it to run
agents you do not trust.

### Threat model

| Protects against | Does not protect against |
|---|---|
| Reads and writes outside `workspace/` (path resolution, symlink and `..` escape) | A container escape (0-day in the container runtime) |
| Network exfiltration from agent code (`--network=none` by default) | Anything you enable by setting `AGENTTEAM_SANDBOX_NETWORK=true` |
| Runaway processes (wall-clock timeout, memory and PID caps) | Filling `workspace/` with junk — it is writable by design |
| Privilege escalation on the host (non-root, read-only rootfs, no added capabilities) | Host compromise in `subprocess` mode |

## Budget control

Every task is capped on three axes, all configurable: tool-use iterations (default 25), total
input+output tokens (default 500k), and inter-agent message hops (default 10). Cost is accumulated
from the `usage` field of every API response using the per-MTok rates in `backend/app/core/pricing`.
When a limit trips the task halts with status `budget_exceeded` and records which limit and why.

## Layout

```
backend/app/
  api/       FastAPI routers
  agents/    agent runtime, tools, sandbox, config loader
  models/    SQLAlchemy models
  schemas/   Pydantic schemas
  core/      config, logging, db session
config/agents/   one YAML per agent
workspace/       agent sandbox (gitignored)
docker/          sandbox runtime image
```

## Development

```bash
pytest                       # unit tests (no Docker needed)
pytest -m integration        # container tests (needs a Docker daemon)
ruff check . && black --check .
```

CI runs the same three gates on every push and pull request — lint, the full
test suite on Python 3.11/3.12/3.13 with the sandbox image built so the
container tests actually execute, and a `docker compose config` validation. No
test requires an API key; a test that needed one would spend money in CI.
