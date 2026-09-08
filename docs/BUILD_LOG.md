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
- **The other three agent YAMLs** (frontend, database, devops) — Phase 1 specifies the Backend
  agent only.
- **Inter-agent messaging.** `Message.message_type` includes `agent_to_agent`,
  `Task.parent_task_id` exists, and `BudgetTracker` counts hops, but nothing writes them yet.

### Closed after the phase review

- **`docker-compose.yml` and `docker/backend.Dockerfile`** — written. The api service mounts the
  host Docker socket to create sibling sandbox containers; the cost of that (the api container
  gains control of the host daemon) is stated in the compose file and the README rather than left
  implicit. Running `uvicorn` on the host remains the socket-free alternative.
- **GitHub Actions CI** — lint, the full suite on Python 3.11/3.12/3.13 with the sandbox image
  built so container tests execute, migrations applied as their own step, and compose validation.
  `ANTHROPIC_API_KEY` is empty in CI by design: a test that needs a live key spends money on every
  push.
- **Four more bugs, found in a post-phase audit and a fresh-clone test.**
  - `list_files` raised an unhandled `FileNotFoundError` on a dangling symlink — only `resolve()`
    was guarded, not `stat()`. Agents create broken symlinks routinely and `list_files` is the
    first tool the system prompt tells them to call, so the very first tool call could fail.
  - A model absent from `config/pricing.yaml` raised `UnknownModelError` out of `run()`, leaving
    the task in `RUNNING` forever with no status and no reason. Since changing a model is meant to
    be a one-line config edit, that is a realistic mistake. Now a pre-flight check that fails
    before the first API call. A catch-all was added so no unforeseen error can strand a task
    again; the traceback is logged rather than swallowed.
  - `.env.example` shipped `AGENTTEAM_HOST_WORKSPACE_ROOT=` empty, which pydantic parsed as
    `Path('.')` rather than `None`. The sandbox then bind-mounted the literal path `.`, so anyone
    following the README's `cp .env.example .env` got a broken sandbox and three failing tests on a
    clean checkout. Only a fresh clone surfaced this — the working copy had no `.env`.
  - `test_container_cannot_read_host_home` read a hardcoded developer home path, so it passed
    vacuously on Linux CI. It now plants a real canary; confirmed non-vacuous by checking the same
    canary *is* readable with isolation off.

- **A sixth bug, found while writing the compose file.** The sandbox's `--volume` source was built
  from the *process's* view of the workspace. Docker resolves that path on the host, so a
  containerised API would have mounted a non-existent path and agents would have appeared to write
  files that never reached the real workspace — silently, with no error.
  `AGENTTEAM_HOST_WORKSPACE_ROOT` fixes it.

---

## Phase 2 — Full roster, task lifecycle, live streaming

**Goal:** all four agents exist and can be driven independently over HTTP, with live streaming.

### Built

| Area | Detail |
|---|---|
| Roster | Four agent YAMLs. Each prompt states its lane, what it must refuse, and its handoffs; each has a human name, character bio and voice |
| Lifecycle | `queued → in_progress → needs_review → done`, plus `failed` and `budget_exceeded`; centrally validated |
| REST | Create/dispatch, list, detail with history, ordered trace, review |
| Worker | Per-agent asyncio queues; HTTP returns immediately |
| Events | Seven typed Pydantic events on a sequenced bus, over `/ws` |
| Types | `scripts/export_types.py` generates the frontend's TS; CI checks for drift |
| Tests | 236 passing |

### Decisions

**Status vocabulary renamed.** The spec's Phase 2 lifecycle collides with Phase 1's shipped
`pending/running/completed`. Phase 2's names are authoritative; existing rows are migrated. A
finished agent loop now lands in `needs_review`, not `done` — `done` is the manager's call.

**Handoffs reference roles, never agent names.** An agent whose prompt says "ask Ines" breaks the
moment the roster changes, which would defeat the config-driven requirement from section 1.

**One queue per agent.** Serialises an agent's own lane — one character at one desk cannot do two
things at once — while different agents run concurrently.

**The WebSocket carries notifications, not payloads.** Events carry ids and short previews; clients
fetch bodies over REST. Otherwise one large `write_file` stalls the stream for every subscriber.

**`completed` → `done`, not `needs_review`, in the migration.** Phase 1 runs finished under rules
that had no review step; marking them as awaiting review would invent work that was never pending.

### Bugs found and fixed

1. **Agents could not actually run concurrently.** Each run held one transaction open from its
   first status change until it finished, and SQLite permits a single writer — so the second agent
   blocked until the first finished. Four agents took 1.10s for four 0.2s tasks. The runtime now
   commits at each persistence point, which also makes the trace visible *during* a run and leaves
   it intact after a crash.
2. **Migrations failed with foreign keys enforced.** `batch_alter_table` rebuilds by
   create-copy-drop-rename, and `DROP TABLE agents` fails while `tasks` references it. It failed
   silently — alembic still logged "Running downgrade" — because the first check grepped log text.
   Tests now assert on exit codes and data.
3. **The trace viewer showed steps in the wrong order.** SQLite's `CURRENT_TIMESTAMP` is
   whole-second, so rows in one iteration tied and sorted by per-table id — `write_file` appeared
   before the instruction that caused it.
4. **`POST /review` returned 500.** `updated_at` carries `onupdate=func.now()`, so SQLAlchemy
   invalidates it after the UPDATE regardless of `expire_on_commit=False`, and building the
   response triggered a lazy load outside greenlet context.
5. **`IllegalTransitionError` raised `ValueError` while building its own message** for an
   unrecognised status — exactly the case where the actionable error matters.
6. **A generated-code f-string used 3.12+ syntax** while the project supports 3.11.
7. **A leaked asyncio task per disconnected WebSocket client.**

### Deferred

- **The live API call is still unverified** — no credentials on this machine. Unchanged from Phase 1.
- **The React UI** — Phase 4.
- **Inter-agent messaging.** `agent_to_agent`, `parent_task_id` and the hop counter all exist and
  are still unused; agents describe handoffs in prose but cannot yet dispatch to each other.

---

## Phase 3 — Inter-agent communication

**Goal:** agents collaborate without the manager.

### Built

| Area | Detail |
|---|---|
| Message bus | `agents/bus.py` — routes, persists both directions, enforces limits |
| Delegation | `agents/delegation.py` — tree-wide hop budget, agent chain, one clock |
| Tools | `ask_agent` (blocking, creates a child task), `send_message`, `append_project_context` |
| Shared context | `workspace/PROJECT.md`, append-only, enforced in code |
| Handoffs | Encoded in all four prompts, by role rather than by agent name |
| Rollup | `agents/rollup.py` — recursive CTE; cost per agent and per model |
| Tests | 288 passing |

### Decisions

**`ask_agent` runs the child inline, not through the worker queue.** The caller is
blocked either way, and queuing risks a deadlock: the target's lane may already hold a
task that is itself waiting on this one. The child shares the caller's session — the runs
are strictly sequential, so there is no concurrent-flush hazard.

**Limits belong to the tree, not the run.** A per-run hop counter lets every child reset
the budget, and per-run timeouts multiply. Both are shared by reference so a child cannot
widen a limit its parent was already bound by.

**Cycle detection refuses a target already in the chain.** That catches A→B→A one hop
before it becomes a ping-pong, and it is the same condition that prevents a deadlock —
the target is literally blocked waiting on its caller. It deliberately does *not* block
asking the same teammate from two separate branches, which is legitimate fan-out.

**A refused delegation is a tool error, not an exception.** The agent has to be able to
see the refusal and finish without the answer.

**Notifications cost a hop too.** Letting them be free would be an easy way to spam the
team past any limit.

**`write_file` refuses `PROJECT.md`.** An append-only rule a neighbouring tool can bypass
is not a rule. Checked on the resolved path, so `./PROJECT.md` and `a/../PROJECT.md`
cannot slip past it.

**The shared context is injected, not fetched.** "Read PROJECT.md first" is an instruction
agents skip under pressure, and the entire point is that they build on published
decisions rather than invented ones.

### Bugs found and fixed

1. **Delegated runs used the caller's model client**, so a Database child would have been
   configured and billed as the Backend agent. Caught by driving the exit criterion
   through the real router and worker instead of the runtime directly.
2. **A batch containing a collaboration tool ran concurrently.** Those tools write to the
   session and can start a nested run; an `AsyncSession` is not concurrency-safe. Such a
   batch now executes in order, while pure filesystem batches still run concurrently.
3. **`time.sleep` inside an async test** would have stalled the runtime under test as well
   as the loop.

### Verified

One task — "Build a REST API for a book library with search" — assigned to Backend only,
over real HTTP with a real WebSocket client. Backend asked the Database agent for a
schema, received it as a tool result, built `api/books.py` against it, published the API
contract, and notified the UI agent. Both agents' decisions landed in `PROJECT.md`. Cost
rolled up from $0.1562 (root alone) to $0.1719 (tree), with the child correctly priced at
Sonnet rates rather than the caller's Opus rates.

### Deferred

- **The live API call is still unverified** — no credentials on this machine.
- **The React UI** — Phase 4.
- **Agents cannot yet start work on a notification.** `send_message` is persisted and
  visible in the trace, but the recipient only sees it when its next task begins; nothing
  wakes an idle agent.
