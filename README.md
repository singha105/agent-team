# AgentTeam

A simulated software engineering team of AI agents. You are the manager; the agents are the team.

Four agents — Backend, DevOps, Frontend, Database — each with a distinct role, model, and system
prompt. They read and write files in a shared sandboxed workspace, run commands, and (from Phase 3)
talk to each other. Every API call, tool call, and message is persisted so any run can be
reconstructed exactly.

## Status

**Phase 1 of 5 — foundations and one working agent.** CLI only, no UI yet. The Backend agent runs
end to end: it reasons, calls tools, writes code into `workspace/`, and every step lands in the DB.

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

## Running an agent

```bash
python -m app.cli run --agent backend --task "Create a FastAPI endpoint that returns a list of books from a hardcoded list, with a Pydantic response model."
```

Output lands in `workspace/`. Inspect the trace with:

```bash
python -m app.cli show --task <task_id>
```

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
