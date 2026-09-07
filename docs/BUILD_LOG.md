# AgentTeam — Build Log

One section per phase: what was built, what was decided, what was deferred.

---

## Phase 1 — Foundations and one working agent

**Goal:** one real agent, doing real work, end to end from a terminal. No UI.

### Built

| Area | Detail |
|---|---|
| Scaffold | Repo layout per spec §2, `pyproject.toml` with ruff (line-length 100, `E/F/I/B/UP/ASYNC/S`) and black, `.gitignore` covering `.env`, `*.db`, `data/`, `workspace/`, `.env.example` with every setting documented |
| API | FastAPI app, `GET /health` returning version, DB status, sandbox mode and whether a key is configured. 503 when the DB probe fails |
| Data | Five tables — `agents`, `tasks`, `messages`, `tool_calls`, `usage` — plus the initial Alembic migration |
| Config | `config/agents/backend.yaml`, Pydantic-validated loader with `extra="forbid"` |
| Runtime | Generic `AgentRuntime` running the Messages API tool-use loop with full persistence and budget enforcement |
| Tools | `read_file`, `write_file`, `list_files`, `run_command` |
| Sandbox | Path resolution plus Docker container isolation |
| CLI | `run`, `show`, `agents` |
| Tests | 150, all passing |

### Decisions

**Docker-isolated command execution (§3 amendment, approved before implementation).**
The spec's allow-list — `python`, `pip`, `node`, `npm`, `pytest`, `ls`, `cat`, `mkdir`, `git` —
is not a security boundary: `python -c "import os; os.system(...)"` escapes in one command, `pip`
and `npm` execute code at install time, and `git` aliases of the form `!sh -c '...'` run arbitrary
shell. Every command now runs in a disposable container (`--rm --network=none --read-only
--cap-drop ALL --security-opt no-new-privileges`, memory and PID caps, workspace as the only
writable mount, host uid:gid so files land owned by the host user). The allow-list is retained as
defense in depth. `AGENTTEAM_SANDBOX_MODE=subprocess` keeps the original behaviour for hosts
without Docker, documented as not a boundary.

**Manual tool-use loop rather than the SDK's beta tool runner.** Every step has to be persisted as
it happens and budgets checked between iterations; the runner exposes neither.

**`thinking` is never sent.** Current models run adaptive by default. Sending an explicit config
would break the moment a different model is set in YAML, which contradicts "changing a model is a
one-line config edit". `output_config.effort` is per-agent with an `off` value that omits
`output_config` entirely for models that reject it.

**Two columns beyond the spec.** `usage.cache_creation_tokens` — cache writes bill at 1.25× input,
so the cost estimate is wrong without it. `tasks.halt_reason` — `budget_exceeded` has to say which
limit tripped.

**Pricing in `config/pricing.yaml`.** Opus 5 at $5/$25 per MTok, Sonnet 5 at $2/$10, cache read at
0.1× and cache write at 1.25× input. Updatable without a code change.

**Integer primary keys, not UUIDs.** The trace is meant to be readable from a bare `sqlite3` shell
and referenced from the CLI as `--task 3`.

### Bugs found and fixed during the phase

1. **`Task.subtasks` / `Task.parent` were both many-to-one.** `remote_side` belongs only on the
   many-to-one side. The adjacency list never configured its mappers; this only surfaced once the
   ORM was exercised rather than just its metadata.
2. **Concurrent `session.add()` inside `asyncio.gather`.** An `AsyncSession` is not
   concurrency-safe, so parallel tool calls could interleave flushes and corrupt the trace. Split
   into write-rows-before, execute-concurrently, write-results-after — which also preserves the §3
   requirement that commands are logged before execution.
3. **Alembic never committed the version stamp on SQLite.** SQLite takes the non-transactional DDL
   path, so `CREATE TABLE` auto-commits but the `alembic_version` INSERT did not. `upgrade head`
   looked like it worked and left the DB unstamped; a second run failed with `table agents already
   exists`. The documented setup path was broken.
4. **`_serialize` guarded on `model_dump` but called `model_dump_json`.**
5. **A missing host executable raised `FileNotFoundError` out of the sandbox** instead of returning
   exit 127 with a readable message.

### Verified

- Container isolation, against a running daemon: host filesystem unreachable, network egress fails
  with `ENETUNREACH`, writes to `/` rejected by the read-only rootfs, a 60s sleep killed at the 5s
  timeout with no container left behind, and files written inside the container landing in the host
  workspace owned by the host user.
- Full pipeline — real sandbox, real DB, real CLI, real tool stack — driving the Backend agent
  through the books-endpoint task: `books.py` and `test_books.py` written into `workspace/`, and
  `python -m pytest -q` run inside the container returning exit 0. The whole run is reconstructable
  with `python -m app.cli show --task 1`.
- `pytest` 150 passed; `ruff check` and `black --check` clean.

### Deferred

- **The live API call is unverified.** No `ANTHROPIC_API_KEY`, no `ant` CLI and no credential
  profile exist on this machine, so the one step never exercised against the real service is
  `client.messages.create`. Everything on either side of it is verified. This is the first thing to
  run in Phase 2.
- **`docker-compose.yml`** is in the §2 layout but not written. The app shells out to `docker run`
  for the sandbox, so containerising the API needs a deliberate decision about Docker-socket
  access; shipping a compose file that silently breaks the sandbox would be worse than not shipping
  one.
- **GitHub Actions CI** — in §2's tooling list but not in the Phase 1 build list.
- **The other three agent YAMLs** (frontend, database, devops) — Phase 1 specifies the Backend
  agent only.
- **Inter-agent messaging.** `Message.message_type` includes `agent_to_agent`,
  `Task.parent_task_id` exists, and `BudgetTracker` counts hops, but nothing writes them yet.
