"""Contract tests: the real Anthropic SDK over real HTTP.

Every other suite replaces `client.messages.create` with a scripted object.
That proves the loop logic and nothing about the request we would actually
send — a wrong parameter name or a malformed tool schema passes all of them and
fails on the first live call.

Here the runtime drives a genuine `anthropic.AsyncAnthropic` against a local
server speaking the Messages API wire format, so the SDK does the real encoding
and the real response parsing.

What this still cannot prove is that Anthropic's service accepts the request.
Only a live call does that; `python -m app.cli preflight` makes exactly one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import anthropic
import pytest
import pytest_asyncio

from app.agents.runtime import AgentRuntime, sync_agent_row
from app.core import db as db_module
from app.models import HUMAN, Task, TaskStatus
from tests.wire_server import (
    refusal_response,
    running_wire_server,
    text_response,
    tool_use_response,
)

REAL_AGENT_CONFIGS = Path(__file__).resolve().parents[2] / "config" / "agents"


@pytest.fixture
def wire_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "data").mkdir()
    agents = tmp_path / "agents"
    shutil.copytree(REAL_AGENT_CONFIGS, agents)

    monkeypatch.setenv("AGENTTEAM_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("AGENTTEAM_DB_PATH", str(tmp_path / "data" / "wire.db"))
    monkeypatch.setenv("AGENTTEAM_AGENT_CONFIG_DIR", str(agents))
    monkeypatch.setenv("AGENTTEAM_SANDBOX_MODE", "subprocess")

    from app.agents import config_loader
    from app.core import config as cfg

    cfg.get_settings.cache_clear()
    config_loader._cached.cache_clear()
    db_module._engine = None
    db_module._session_factory = None
    yield cfg.get_settings()
    config_loader._cached.cache_clear()
    cfg.get_settings.cache_clear()
    db_module._engine = None
    db_module._session_factory = None


@pytest_asyncio.fixture
async def session(wire_settings):
    import app.models  # noqa: F401

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_module.Base.metadata.create_all)
    factory = db_module.get_session_factory()
    async with factory() as s:
        yield s
    await db_module.dispose_engine()


async def make_task(session, agent_key: str, description: str) -> Task:
    from app.agents.config_loader import get_agent_config

    row = await sync_agent_row(session, get_agent_config(agent_key))
    task = Task(
        title=description[:80],
        description=description,
        assigned_agent_id=row.id,
        created_by=HUMAN,
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.flush()
    await session.commit()
    return task


async def run_against(wire_base_url: str, session, settings, agent_key: str, task: Task):
    from app.agents.config_loader import get_agent_config

    client = anthropic.AsyncAnthropic(api_key="sk-ant-test-not-a-real-key", base_url=wire_base_url)
    runtime = AgentRuntime(get_agent_config(agent_key), session, settings, client=client)
    return await runtime.run(task)


# -- the request we actually send -----------------------------------------


async def test_a_real_request_is_accepted_by_the_wire_format(session, wire_settings) -> None:
    """The whole point: the SDK encodes our kwargs and the server accepts them."""
    async with running_wire_server([text_response("All done.")]) as (wire, url):
        task = await make_task(session, "backend", "Do something small")
        result = await run_against(url, session, wire_settings, "backend", task)

    assert result.status == TaskStatus.NEEDS_REVIEW, result.halt_reason
    assert result.final_text == "All done."
    assert len(wire.requests) == 1


async def test_the_request_carries_the_documented_fields(session, wire_settings) -> None:
    async with running_wire_server([text_response("ok")]) as (wire, url):
        task = await make_task(session, "backend", "Build an endpoint")
        await run_against(url, session, wire_settings, "backend", task)

    sent = wire.requests[0]
    assert sent["model"] == "claude-opus-5"
    assert isinstance(sent["max_tokens"], int) and sent["max_tokens"] > 0
    assert sent["system"], "the agent's system prompt must be sent"
    assert sent["messages"][0]["role"] == "user"
    assert sent["output_config"] == {"effort": wire_settings.effort}
    # budget_tokens is rejected on current models; we must never send thinking config.
    assert "thinking" not in sent


async def test_the_api_key_is_sent_as_a_header_not_in_the_body(session, wire_settings) -> None:
    async with running_wire_server([text_response("ok")]) as (wire, url):
        task = await make_task(session, "backend", "x")
        await run_against(url, session, wire_settings, "backend", task)

    assert "x-api-key" in wire.headers[0]
    assert "sk-ant-test-not-a-real-key" not in str(wire.requests[0])


async def test_every_tool_schema_is_accepted(session, wire_settings) -> None:
    """A malformed input_schema is a 400 on the first live call. The shipped
    agents grant all seven tools, so this covers every one."""
    async with running_wire_server([text_response("ok")]) as (wire, url):
        task = await make_task(session, "backend", "x")
        await run_against(url, session, wire_settings, "backend", task)

    tools = wire.requests[0]["tools"]
    assert len(tools) == 7
    for tool in tools:
        assert tool["name"]
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
        assert isinstance(tool["input_schema"]["properties"], dict)


@pytest.mark.parametrize("agent_key", ["backend", "database", "devops", "frontend"])
async def test_every_shipped_agent_produces_an_accepted_request(
    session, wire_settings, agent_key: str
) -> None:
    """A typo in any agent's model id or tool grant is a 400 in production."""
    async with running_wire_server([text_response("ok")]) as (wire, url):
        task = await make_task(session, agent_key, "x")
        result = await run_against(url, session, wire_settings, agent_key, task)

    assert result.status == TaskStatus.NEEDS_REVIEW, result.halt_reason
    assert wire.requests[0]["model"] in {"claude-opus-5", "claude-sonnet-5"}


# -- the tool-use round trip ----------------------------------------------


async def test_the_tool_result_round_trip_is_wire_valid(session, wire_settings) -> None:
    """The second request must echo the assistant turn and attach a
    tool_result whose id matches the tool_use the server sent."""
    script = [
        tool_use_response("list_files", {"path": "."}, "toolu_abc"),
        text_response("Listed the workspace."),
    ]
    async with running_wire_server(script) as (wire, url):
        task = await make_task(session, "backend", "Look around")
        result = await run_against(url, session, wire_settings, "backend", task)

    assert result.status == TaskStatus.NEEDS_REVIEW, result.halt_reason
    assert len(wire.requests) == 2

    second = wire.requests[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert any(b.get("type") == "tool_use" for b in second[-2]["content"])

    results = [b for b in second[-1]["content"] if b.get("type") == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_use_id"] == "toolu_abc"


async def test_a_mismatched_tool_result_id_would_be_rejected(session, wire_settings) -> None:
    """Proves the previous test is not vacuous: the server does check."""
    from tests.wire_server import validate_request

    bad = {
        "model": "claude-opus-5",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "nope", "content": "x"}],
            },
        ],
    }
    assert "does not match any tool_use" in (validate_request(bad) or "")


# -- response parsing ------------------------------------------------------


async def test_usage_is_parsed_from_a_real_response_body(session, wire_settings) -> None:
    """Token accounting drives the budget and the cost estimate, and it is read
    off a field name that only a real response shape can confirm."""
    async with running_wire_server([text_response("ok", input_tokens=1234, output_tokens=567)]) as (
        _wire,
        url,
    ):
        task = await make_task(session, "backend", "x")
        result = await run_against(url, session, wire_settings, "backend", task)

    assert result.budget["input_tokens"] == 1234
    assert result.budget["output_tokens"] == 567
    # 1234 in + 567 out at opus-5 rates.
    expected = (1234 * 5.00 + 567 * 25.00) / 1_000_000
    assert result.budget["estimated_cost_usd"] == pytest.approx(expected, abs=1e-6)


async def test_a_refusal_response_is_handled(session, wire_settings) -> None:
    async with running_wire_server([refusal_response("cyber")]) as (_wire, url):
        task = await make_task(session, "backend", "x")
        result = await run_against(url, session, wire_settings, "backend", task)

    assert result.status == TaskStatus.FAILED
    assert "declined" in result.halt_reason
    assert "cyber" in result.halt_reason


async def test_an_api_error_becomes_an_actionable_failure(
    session, wire_settings, monkeypatch
) -> None:
    """A 400 must fail the task with the server's reason, not a traceback."""
    # max_tokens below 1 is rejected by the wire contract, so this exercises the
    # real SDK's error path rather than a synthesised exception.
    monkeypatch.setattr(wire_settings, "max_response_tokens", 0)

    async with running_wire_server([]) as (_wire, url):
        task = await make_task(session, "backend", "x")
        result = await run_against(url, session, wire_settings, "backend", task)

    assert result.status == TaskStatus.FAILED
    assert "max_tokens" in result.halt_reason
    assert "400" in result.halt_reason


async def test_an_unreachable_api_fails_cleanly(session, wire_settings) -> None:
    """A connection failure must name the cause, not surface as a traceback."""
    from app.agents.config_loader import get_agent_config

    # Nothing is listening here; the SDK retries, then raises a connection error.
    client = anthropic.AsyncAnthropic(
        api_key="k", base_url="http://127.0.0.1:1", max_retries=0, timeout=2.0
    )
    task = await make_task(session, "backend", "x")
    runtime = AgentRuntime(get_agent_config("backend"), session, wire_settings, client=client)

    result = await runtime.run(task)

    assert result.status == TaskStatus.FAILED
    assert "could not reach the API" in result.halt_reason


# -- the preflight command -------------------------------------------------


async def test_preflight_dry_run_makes_no_call(wire_settings) -> None:
    from app.cli import build_parser, cmd_preflight

    args = build_parser().parse_args(["preflight", "--dry-run"])
    async with running_wire_server([]) as (wire, _url):
        assert await cmd_preflight(args) == 0
        assert wire.requests == [], "a dry run must not send anything"


async def test_preflight_reports_success_against_a_real_http_endpoint(
    wire_settings, monkeypatch
) -> None:
    """The command exists so that verifying the live path is one bounded call.
    This proves the command itself works; only a real key proves the service
    accepts it."""
    from app.cli import build_parser, cmd_preflight

    async with running_wire_server(
        [text_response("ready", input_tokens=2000, output_tokens=3)]
    ) as (
        wire,
        url,
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-local-wire-server")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        from app.core import config as cfg

        cfg.get_settings.cache_clear()
        try:
            args = build_parser().parse_args(["preflight", "--max-tokens", "32"])
            assert await cmd_preflight(args) == 0
        finally:
            cfg.get_settings.cache_clear()

    assert len(wire.requests) == 1
    sent = wire.requests[0]
    assert sent["model"] == "claude-opus-5"
    assert len(sent["tools"]) == 7
    assert sent["output_config"] == {"effort": "low"}
    assert sent["max_tokens"] == 32


async def test_preflight_without_a_key_exits_two(wire_settings, monkeypatch) -> None:
    from app.cli import build_parser, cmd_preflight

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app.core import config as cfg

    cfg.get_settings.cache_clear()
    try:
        args = build_parser().parse_args(["preflight"])
        assert await cmd_preflight(args) == 2
    finally:
        cfg.get_settings.cache_clear()
