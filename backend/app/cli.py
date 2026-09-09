"""Command line entry point.

python -m app.cli run --agent backend --task "..."
python -m app.cli show --task 1
python -m app.cli agents
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap

from sqlalchemy import select

from app.agents.config_loader import AgentConfigError, get_agent_config, load_all_agent_configs
from app.agents.runtime import AgentRuntime, sync_agent_row
from app.core.config import MissingAPIKeyError, get_settings
from app.core.db import dispose_engine, session_scope
from app.core.logging import configure_logging
from app.models import HUMAN, Message, Task, ToolCall, Usage


def _rule(title: str = "") -> str:
    return f"── {title} ".ljust(72, "─") if title else "─" * 72


async def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        config = get_agent_config(args.agent)
    except AgentConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        settings.require_api_key()
    except MissingAPIKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(_rule(f"{config.display_name} ({config.key})"))
    print(f"model     {config.model}")
    print(f"role      {config.role}")
    print(f"sandbox   {settings.sandbox_mode}")
    print(
        f"budgets   {config.max_iterations or settings.max_iterations} iterations, "
        f"{config.max_tokens or settings.max_tokens_per_task:,} tokens"
    )
    print(f"workspace {settings.workspace_dir}")
    print(_rule())

    async with session_scope() as session:
        agent_row = await sync_agent_row(session, config)
        task = Task(
            title=textwrap.shorten(args.task, width=120, placeholder="…"),
            description=args.task,
            assigned_agent_id=agent_row.id,
            created_by=HUMAN,
        )
        session.add(task)
        await session.flush()
        task_id = task.id

        runtime = AgentRuntime(config, session, settings=settings)
        result = await runtime.run(task)

    print(_rule("result"))
    print(f"task      {task_id}")
    print(f"status    {result.status}")
    if result.halt_reason:
        print(f"halted    {result.halt_reason}")
    b = result.budget
    print(
        f"usage     {b['iterations']} iterations, {b['total_tokens']:,} tokens "
        f"(in {b['input_tokens']:,} / out {b['output_tokens']:,}), "
        f"est. ${b['estimated_cost_usd']:.4f}"
    )
    if result.final_text:
        print(_rule("agent summary"))
        print(result.final_text.strip())
    print(_rule())
    print(f"trace: python -m app.cli show --task {task_id}")

    return 0 if result.ok else 1


async def cmd_show(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        task = (
            await session.execute(select(Task).where(Task.id == args.task))
        ).scalar_one_or_none()
        if task is None:
            print(f"error: no task with id {args.task}", file=sys.stderr)
            return 2

        messages = (
            (
                await session.execute(
                    select(Message).where(Message.task_id == task.id).order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        tool_calls = (
            (
                await session.execute(
                    select(ToolCall).where(ToolCall.task_id == task.id).order_by(ToolCall.id)
                )
            )
            .scalars()
            .all()
        )
        usage_rows = (
            (
                await session.execute(
                    select(Usage).where(Usage.task_id == task.id).order_by(Usage.id)
                )
            )
            .scalars()
            .all()
        )

        print(_rule(f"task {task.id}"))
        print(f"title     {task.title}")
        print(f"status    {task.status}")
        print(f"created   by {task.created_by} at {task.created_at}")
        if task.halt_reason:
            print(f"halted    {task.halt_reason}")

        print(_rule("messages"))
        for m in messages:
            frm = m.from_agent or "human"
            to = m.to_agent or "human"
            print(f"  #{m.id:<4} it={m.iteration:<3} {m.message_type:<12} {frm} → {to}")

        print(_rule("tool calls"))
        for c in tool_calls:
            flag = "ERR " if c.error else "    "
            args_preview = str(c.arguments)[:60]
            print(f"  #{c.id:<4} {flag}{c.tool_name:<13} {c.duration_ms or 0:>6}ms  {args_preview}")

        print(_rule("usage"))
        total_cost = 0.0
        total_tokens = 0
        for u in usage_rows:
            total_cost += u.estimated_cost_usd
            total_tokens += u.input_tokens + u.output_tokens + u.cache_read_tokens
            print(
                f"  it={u.iteration:<3} in {u.input_tokens:>7,}  out {u.output_tokens:>6,}  "
                f"cache_r {u.cache_read_tokens:>7,}  ${u.estimated_cost_usd:.4f}"
            )
        print(
            f"  {'total':<8} {total_tokens:,} tokens across {len(usage_rows)} calls, "
            f"${total_cost:.4f}"
        )
        print(_rule())
    return 0


async def cmd_preflight(args: argparse.Namespace) -> int:
    """Verify the live API end to end with one small, real call.

    Everything else in this project is tested against a scripted model or a
    local server speaking the wire format. Those prove the request we build is
    well formed; only a real call proves Anthropic accepts it. This makes that
    check one command, with the cost stated before and after.
    """
    import anthropic

    from app.agents.tools import build_toolset
    from app.core.pricing import TokenUsage, estimate_cost_usd, rates_for

    settings = get_settings()
    try:
        config = get_agent_config(args.agent)
    except AgentConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    system = config.resolve_system_prompt(settings.agent_configs)
    schemas, _ = build_toolset(config.tools)

    # The point of this check is that the *request shape* is accepted, which is
    # model-independent. Overriding the model lets that be proven on the
    # cheapest one available — roughly a fifth of a cent on Haiku against about
    # a penny on Opus — without weakening what is verified: same system prompt,
    # same seven tool schemas, same wire format.
    model = args.model or config.model
    request: dict = {
        "model": model,
        "max_tokens": args.max_tokens,
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": "Reply with the single word: ready. Do not call any tool.",
            }
        ],
        "tools": schemas,
    }
    # output_config is rejected outright by older and smaller models, so it is
    # omitted rather than sent and hoped for.
    if args.effort != "off":
        request["output_config"] = {"effort": args.effort}

    try:
        rates = rates_for(model)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Characters per token, deliberately conservative. The first live run showed
    # 2,736 actual input tokens against 2,164 estimated at four characters per
    # token — the estimate undershot by a quarter, so the word "under" printed
    # beside it was false. A number whose whole job is to bound spend before you
    # commit to it must never read low.
    CHARS_PER_TOKEN = 3
    approx_input = (len(system) + sum(len(str(t)) for t in schemas)) // CHARS_PER_TOKEN
    est = (approx_input * rates.input + args.max_tokens * rates.output) / 1_000_000

    print(_rule("preflight"))
    print(f"agent      {config.display_name} ({config.key})")
    print(f"model      {model}" + ("  (override)" if args.model else ""))
    print(f"tools      {len(schemas)} schemas: {', '.join(t['name'] for t in schemas)}")
    print(f"effort     {args.effort}   max_tokens {args.max_tokens}")
    print(f"est. cost  under ${est:.4f} (~{approx_input:,} input tokens)")
    print(_rule())

    if args.dry_run:
        print("dry run: nothing was sent. Drop --dry-run to make the call.")
        return 0

    try:
        settings.require_api_key()
    except MissingAPIKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = anthropic.AsyncAnthropic(api_key=settings.require_api_key())
    try:
        response = await client.messages.create(**request)
    except anthropic.AuthenticationError:
        print("FAILED  the API key was rejected. Check ANTHROPIC_API_KEY.", file=sys.stderr)
        return 1
    except anthropic.NotFoundError:
        print(
            f"FAILED  model {model!r} was not found. Check --model, or the `model` "
            f"field in config/agents/{config.key}.yaml.",
            file=sys.stderr,
        )
        return 1
    except anthropic.BadRequestError as exc:
        print(f"FAILED  the request was rejected: {exc.message}", file=sys.stderr)
        print("        This is the failure the offline tests cannot catch.", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError as exc:
        print(f"FAILED  could not reach the API: {exc}", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as exc:
        print(f"FAILED  API error {exc.status_code}: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await client.close()

    usage = TokenUsage.from_response_usage(response.usage)
    cost = estimate_cost_usd(model, usage)
    text = "".join(b.text for b in response.content if b.type == "text").strip()

    print("OK   request accepted and response parsed")
    print(f"  model returned   {response.model}")
    print(f"  stop_reason      {response.stop_reason}")
    print(f"  content blocks   {[b.type for b in response.content]}")
    print(f"  reply            {text[:80]!r}")
    print(
        f"  usage            in {usage.input_tokens:,} / out {usage.output_tokens:,} "
        f"(cache read {usage.cache_read_tokens:,})"
    )
    print(f"  actual cost      ${cost:.6f}")
    print(_rule())
    print("The live API path is verified: the request this project builds is accepted,")
    print("and the response parses into the fields the budget and cost accounting read.")
    return 0


async def cmd_agents(_args: argparse.Namespace) -> int:
    try:
        configs = load_all_agent_configs()
    except AgentConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_rule("configured agents"))
    for key, c in configs.items():
        print(f"  {key:<12} {c.display_name:<10} {c.model:<18} {c.role}")
        print(f"  {'':<12} tools: {', '.join(c.tools)}")
    print(_rule())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="AgentTeam — run an agent against a task.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="assign a task to an agent and run it")
    run.add_argument("--agent", required=True, help="agent key, e.g. 'backend'")
    run.add_argument("--task", required=True, help="task description")
    run.set_defaults(func=cmd_run)

    show = sub.add_parser("show", help="print the full trace for a task")
    show.add_argument("--task", required=True, type=int, help="task id")
    show.set_defaults(func=cmd_show)

    agents = sub.add_parser("agents", help="list configured agents")
    agents.set_defaults(func=cmd_agents)

    preflight = sub.add_parser(
        "preflight",
        help="verify the live API with one small real call",
        description=(
            "Sends one minimal request using a real agent's model, system prompt and "
            "tool schemas, then reports what came back. This is the only check that "
            "proves Anthropic accepts the request this project builds."
        ),
    )
    preflight.add_argument("--agent", default="backend", help="agent to test with")
    preflight.add_argument(
        "--model",
        default=None,
        help=(
            "override the agent's model. The request shape is what is being verified "
            "and that is model-independent, so the cheapest model proves the same "
            "thing: --model claude-haiku-4-5 --effort off costs about $0.002."
        ),
    )
    preflight.add_argument(
        "--effort",
        default="low",
        choices=["low", "medium", "high", "xhigh", "max", "off"],
        help="'off' omits output_config, which smaller and older models reject",
    )
    preflight.add_argument(
        "--max-tokens", type=int, default=32, help="response cap, kept small to bound cost"
    )
    preflight.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be sent and the cost estimate, without calling",
    )
    preflight.set_defaults(func=cmd_preflight)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)

    async def _run() -> int:
        try:
            return await args.func(args)
        finally:
            await dispose_engine()

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
