"""Reusability: a team is a directory of files, not a code path.

The claim the project makes is that swapping the roster is configuration. These
tests are what makes that a claim rather than an aspiration — they load a team
the runtime has never been told about and run it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.agents.config_loader import load_all_agent_configs
from app.agents.runtime import AgentRuntime, sync_agent_row
from app.models import HUMAN, Message, MessageType, Task, TaskStatus
from tests.fakes import FakeClient, FakeResponse, text_block, tool_use_block

TEAMS = Path(__file__).resolve().parents[2] / "config" / "teams"


def test_both_shipped_teams_load() -> None:
    assert sorted(p.name for p in TEAMS.iterdir() if p.is_dir()) == ["research", "software"]


@pytest.mark.parametrize("team", ["software", "research"])
def test_every_agent_in_a_team_is_valid(team: str) -> None:
    configs = load_all_agent_configs(TEAMS / team)
    assert configs
    for key, config in configs.items():
        assert config.key == key
        assert config.display_name
        assert config.bio, f"{key} needs a bio for the UI"
        assert config.owns
        assert config.tools
        assert config.resolve_system_prompt(TEAMS / team).strip()


def test_the_research_team_is_a_different_shape() -> None:
    """Three agents, different names, different roles, different tool grants —
    proving nothing in the runtime assumes four agents called backend, database,
    frontend and devops."""
    research = load_all_agent_configs(TEAMS / "research")
    software = load_all_agent_configs(TEAMS / "software")

    assert len(research) == 3
    assert len(software) == 4
    assert set(research) == {"researcher", "analyst", "writer"}
    assert not set(research) & set(software)

    # Tool grants differ per agent: a writer has no business running commands.
    assert "run_command" not in research["writer"].tools
    assert "run_command" not in research["researcher"].tools
    assert "run_command" in research["analyst"].tools


@pytest.mark.parametrize("team", ["software", "research"])
def test_handoffs_reference_roles_not_names(team: str) -> None:
    """An agent whose prompt names a teammate breaks the moment the roster
    changes — which would defeat the entire point of this layout."""
    configs = load_all_agent_configs(TEAMS / team)
    for key, config in configs.items():
        prompt = config.resolve_system_prompt(TEAMS / team)
        for other_key, other in configs.items():
            if other_key != key:
                assert (
                    other.display_name not in prompt
                ), f"{team}/{key} names {other.display_name} directly"


def test_selecting_a_team_is_an_environment_variable(monkeypatch, tmp_path) -> None:
    from app.core import config as cfg

    monkeypatch.setenv("AGENTTEAM_TEAM", "research")
    monkeypatch.delenv("AGENTTEAM_AGENT_CONFIG_DIR", raising=False)
    cfg.get_settings.cache_clear()
    try:
        settings = cfg.get_settings()
        assert settings.agent_configs.name == "research"
        assert "research" in settings.available_teams
        assert "software" in settings.available_teams
    finally:
        cfg.get_settings.cache_clear()


async def test_a_team_the_runtime_has_never_seen_actually_runs(
    session, settings, monkeypatch, tmp_path
) -> None:
    """The proof, rather than the promise: load the research team, give one of
    its agents a task, and let it delegate — with no code change anywhere."""
    import shutil

    directory = tmp_path / "research"
    shutil.copytree(TEAMS / "research", directory)
    monkeypatch.setattr(settings, "agent_config_dir", directory)

    from app.agents import config_loader

    config_loader._cached.cache_clear()

    configs = load_all_agent_configs(directory)
    analyst = configs["analyst"]

    scripts = {
        "analyst": [
            FakeResponse(
                [
                    tool_use_block(
                        "ask_agent",
                        {"to_agent": "researcher", "question": "What sources do we have?"},
                        "a1",
                    )
                ],
                "tool_use",
            ),
            FakeResponse([text_block("Analysed the sources Sena verified.")], "end_turn"),
        ],
        "researcher": [
            FakeResponse(
                [text_block("Three verified primary sources, dated 2024-2026.")], "end_turn"
            )
        ],
    }

    class Scripts:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def __call__(self, key: str) -> FakeClient:
            self.seen.append(key)
            return FakeClient(scripts.get(key, [FakeResponse([text_block("ok")], "end_turn")]))

    factory = Scripts()

    row = await sync_agent_row(session, analyst)
    task = Task(
        title="Analyse the sources",
        description="Work out what the gathered material supports.",
        assigned_agent_id=row.id,
        created_by=HUMAN,
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.flush()
    await session.commit()

    result = await AgentRuntime(
        analyst, session, settings, client=factory("analyst"), client_for=factory
    ).run(task)

    assert result.status == TaskStatus.NEEDS_REVIEW
    assert "researcher" in factory.seen, "the analyst never delegated"

    # The delegation is recorded exactly as it is for the software team.
    exchanges = (
        (
            await session.execute(
                select(Message).where(Message.message_type == MessageType.AGENT_TO_AGENT)
            )
        )
        .scalars()
        .all()
    )
    assert len(exchanges) == 2
    assert (exchanges[0].from_agent, exchanges[0].to_agent) == ("analyst", "researcher")

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id))).scalars().all()
    )
    assert len(children) == 1


def test_adding_a_team_needs_no_code(tmp_path) -> None:
    """A directory with one valid file is a team. If this ever requires touching
    Python, the config-driven claim is false."""
    directory = tmp_path / "solo"
    directory.mkdir()
    (directory / "helper.yaml").write_text(
        yaml.safe_dump(
            {
                "key": "helper",
                "display_name": "Wren",
                "role": "Does the one thing",
                "model": "claude-sonnet-5",
                "avatar_id": "avatar-solo-01",
                "bio": "Wren does the one thing, and stops.",
                "owns": ["The one thing"],
                "tools": ["read_file", "write_file", "list_files"],
                "system_prompt": "You do the one thing and stop.",
            }
        ),
        encoding="utf-8",
    )

    configs = load_all_agent_configs(directory)
    assert list(configs) == ["helper"]
    assert configs["helper"].display_name == "Wren"
