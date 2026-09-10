# Creating a team

A team is a directory of YAML files under `config/teams/`. Nothing in
`backend/` knows how many agents there are, what they are called, or what they
do — so adding a team means adding files, not touching Python.

Two teams ship with the project:

| Team | Agents | What it is for |
|---|---|---|
| `software` | Ada (backend), Ines (database), Mira (frontend), Kai (devops) | The default: building software |
| `research` | Sena (researcher), Dorian (analyst), Vero (writer) | The reusability proof — a different size, different roles, different tool grants |

Select one with `AGENTTEAM_TEAM`:

```bash
AGENTTEAM_TEAM=research python -m app.cli agents
```

## The shortest possible team

One file is a valid team.

```yaml
# config/teams/solo/helper.yaml
key: helper
display_name: Wren
role: Does the one thing
model: claude-sonnet-5
avatar_id: avatar-solo-01
bio: Wren does the one thing, and stops.
owns: [The one thing]
tools: [read_file, write_file, list_files]
system_prompt: |
  You do the one thing and stop.
```

```bash
AGENTTEAM_TEAM=solo python -m app.cli agents
```

## Every field

| Field | Required | Notes |
|---|---|---|
| `key` | yes | Stable id, must match the filename. Used in handoffs and the API. |
| `display_name` | yes | A human first name. The UI shows this. |
| `role` | yes | One line. Appears in the accessible name of the character. |
| `model` | yes | An exact Claude model id. Must have an entry in `config/pricing.yaml`. |
| `avatar_id` | yes | Any stable string; the UI derives a hue and silhouette from `key`. |
| `tools` | yes | Which tools this agent gets. See below. |
| `system_prompt` | one of | Inline prompt text. |
| `system_prompt_file` | one of | Path to a prompt file, relative to the team directory. |
| `bio` | no | A short character bio. The UI shows it; write one. |
| `personality` | no | Voice notes, shown in the panel. |
| `owns` | no | What this agent is responsible for. Rendered as tags. |
| `does_not_own` | no | Explicit lane boundary. |
| `hands_off_to` | no | Map of *role* → what belongs to it. See the warning below. |
| `max_iterations` | no | Per-agent override of the global ceiling. |
| `max_tokens` | no | Per-agent token ceiling. |
| `effort` | no | `low`–`max`, or `off` to omit `output_config` for models that reject it. |

Unknown fields are rejected rather than ignored, so a typo fails at load with the
file and the field named.

## Tools

| Tool | What it does |
|---|---|
| `read_file` | Read a file, with `offset`/`limit` for paging |
| `write_file` | Write a file; serialised per path, and reports replacing another agent's work |
| `list_files` | Recursive listing |
| `run_command` | One command in a disposable container |
| `send_message` | One-way note to a teammate; wakes them with a follow-up task |
| `ask_agent` | Blocking question; the teammate runs a child task and its answer is the result |
| `append_project_context` | Publish a decision to the shared `PROJECT.md` |

**Grant only what the role needs.** This is a real control, not decoration —
an agent without `run_command` cannot execute anything, whatever its prompt
says. In the research team, the writer and the researcher have no
`run_command`, because neither has any business executing code.

Tool grants have a limit, though, and it is worth being honest about it: every
agent on a *software* team needs to write files. The grant is all-or-nothing, so
it cannot tell the difference between the backend agent writing `api/books.py`
— its job — and the same agent writing `db/schema.sql`, which is it taking the
data agent's work. That is what `writes` is for.

## Lanes: `writes`

```yaml
writes:
  - 'api/*'
  - 'api/**'
  - 'services/**'
  - '*.py'
```

Glob patterns this agent may write to. A write outside them is **refused**, and
the refusal names the teammate who owns the path, so the agent asks rather than
retrying under a different filename.

Three rules make this usable rather than obstructive:

- **An agent that declares no `writes` may write anywhere.** The restriction is
  opt-in, so a one-agent team needs no configuration at all.
- **An unclaimed path is allowed.** Only crossing into a lane someone else has
  claimed is refused — otherwise agents would deadlock on any file nobody
  thought to declare.
- **Say so in the prompt.** All four software agents have a "Your lane is
  enforced" section. An agent that hits an unexplained refusal will try to work
  around it; one that was told the rule hands off instead.

## Handoffs must reference roles, never names

```yaml
# Right — survives a roster change
hands_off_to:
  data layer: "Schema shape, migrations, indexes."

# Wrong — breaks the moment Ines is replaced
hands_off_to:
  Ines: "Schema shape, migrations, indexes."
```

The same rule applies inside `system_prompt`: write "ask the agent that owns the
data layer", not "ask Ines". A test enforces this for every shipped team — an
agent's prompt may not contain another agent's display name.

The reason is the point of the whole layout: if prompts name each other, the
roster is no longer swappable and the config-driven claim is false.

## Writing a good system prompt

The four things every prompt in this project states, in this order:

1. **The workspace.** Paths are relative to a sandbox; there is no network; there
   is no shell. Agents waste turns discovering this otherwise.
2. **The lane.** What this agent owns, and explicitly what it does not. Agents
   drift into each other's work without it.
3. **How to work.** Look before writing, no placeholder code, verify by running.
4. **Handoffs.** What to do when something it needs is missing — ask the owner
   rather than inventing it, and publish decisions others build on.

Then a closing instruction to reply with a plain-text summary and no tool call,
so the loop has a clean stopping condition.

## Checking your team before running it

```bash
AGENTTEAM_TEAM=yourteam python -m app.cli agents
```

That loads and validates every file, and prints the roster with models and tool
grants. A malformed team fails here with the file and the problem named, rather
than partway through a run.

To verify the models are accepted without spending meaningfully:

```bash
AGENTTEAM_TEAM=yourteam python -m app.cli preflight --agent <key> \
  --model claude-haiku-4-5 --effort off
```

## What the UI does automatically

Nothing needs registering. The frontend reads the roster from `/api/agents`, and
an agent it has never seen gets a coherent desk: the hue is derived from a stable
hash restricted to mid hues at fixed chroma, so a generated colour cannot land
somewhere that breaks the room's palette, and the silhouette is picked from the
same hash.
