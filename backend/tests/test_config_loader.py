"""The config loader must reject invalid agent definitions with a clear reason."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.config_loader import AgentConfig, AgentConfigError, load_agent_config

VALID = {
    "key": "backend",
    "display_name": "Ada",
    "role": "API and business logic",
    "model": "claude-opus-5",
    "avatar_id": "avatar-01",
    "tools": ["read_file", "write_file"],
    "system_prompt": "You are a backend engineer.",
}


def write_agent(directory: Path, name: str, data: dict) -> Path:
    import yaml

    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_valid_config_loads(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "backend", VALID)
    config = load_agent_config("backend", agent_config_dir)
    assert config.key == "backend"
    assert config.model == "claude-opus-5"
    assert config.tools == ["read_file", "write_file"]


@pytest.mark.parametrize(
    "missing",
    ["key", "display_name", "role", "model", "avatar_id", "tools"],
)
def test_missing_required_field_is_rejected(agent_config_dir: Path, missing: str) -> None:
    data = {k: v for k, v in VALID.items() if k != missing}
    write_agent(agent_config_dir, "backend", data)
    with pytest.raises(AgentConfigError) as exc:
        load_agent_config("backend", agent_config_dir)
    assert missing in str(exc.value)


def test_unknown_tool_is_rejected(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "backend", VALID | {"tools": ["read_file", "delete_everything"]})
    with pytest.raises(AgentConfigError, match="unknown tools"):
        load_agent_config("backend", agent_config_dir)


def test_empty_tool_list_is_rejected(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "backend", VALID | {"tools": []})
    with pytest.raises(AgentConfigError):
        load_agent_config("backend", agent_config_dir)


def test_duplicate_tools_are_rejected(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "backend", VALID | {"tools": ["read_file", "read_file"]})
    with pytest.raises(AgentConfigError, match="duplicate"):
        load_agent_config("backend", agent_config_dir)


def test_unknown_field_is_rejected(agent_config_dir: Path) -> None:
    """extra='forbid' catches typo'd keys rather than silently ignoring them."""
    write_agent(agent_config_dir, "backend", VALID | {"modell": "claude-opus-5"})
    with pytest.raises(AgentConfigError):
        load_agent_config("backend", agent_config_dir)


def test_filename_must_match_declared_key(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "frontend", VALID)  # declares key 'backend'
    with pytest.raises(AgentConfigError, match="filename"):
        load_agent_config("frontend", agent_config_dir)


def test_both_prompt_sources_is_rejected(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "backend", VALID | {"system_prompt_file": "p.txt"})
    with pytest.raises(AgentConfigError, match="not both"):
        load_agent_config("backend", agent_config_dir)


def test_no_prompt_source_is_rejected(agent_config_dir: Path) -> None:
    data = {k: v for k, v in VALID.items() if k != "system_prompt"}
    write_agent(agent_config_dir, "backend", data)
    with pytest.raises(AgentConfigError, match="system_prompt"):
        load_agent_config("backend", agent_config_dir)


def test_prompt_from_file_is_read(agent_config_dir: Path) -> None:
    (agent_config_dir / "prompt.txt").write_text("You are a backend engineer.", encoding="utf-8")
    data = {k: v for k, v in VALID.items() if k != "system_prompt"}
    write_agent(agent_config_dir, "backend", data | {"system_prompt_file": "prompt.txt"})
    config = load_agent_config("backend", agent_config_dir)
    assert config.resolve_system_prompt(agent_config_dir) == "You are a backend engineer."


def test_missing_prompt_file_is_reported(agent_config_dir: Path) -> None:
    data = {k: v for k, v in VALID.items() if k != "system_prompt"}
    write_agent(agent_config_dir, "backend", data | {"system_prompt_file": "gone.txt"})
    config = load_agent_config("backend", agent_config_dir)
    with pytest.raises(AgentConfigError, match="cannot read"):
        config.resolve_system_prompt(agent_config_dir)


def test_invalid_effort_is_rejected(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "backend", VALID | {"effort": "extreme"})
    with pytest.raises(AgentConfigError, match="effort"):
        load_agent_config("backend", agent_config_dir)


def test_malformed_yaml_is_reported(agent_config_dir: Path) -> None:
    (agent_config_dir / "backend.yaml").write_text("key: [unclosed", encoding="utf-8")
    with pytest.raises(AgentConfigError, match="not valid YAML"):
        load_agent_config("backend", agent_config_dir)


def test_non_mapping_yaml_is_reported(agent_config_dir: Path) -> None:
    (agent_config_dir / "backend.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(AgentConfigError, match="mapping"):
        load_agent_config("backend", agent_config_dir)


def test_missing_file_lists_available_agents(agent_config_dir: Path) -> None:
    write_agent(agent_config_dir, "backend", VALID)
    with pytest.raises(AgentConfigError, match="backend"):
        load_agent_config("nope", agent_config_dir)


def test_model_is_data_not_code(agent_config_dir: Path) -> None:
    """Changing an agent's model must be a one-line config edit."""
    write_agent(agent_config_dir, "backend", VALID | {"model": "claude-sonnet-5"})
    assert load_agent_config("backend", agent_config_dir).model == "claude-sonnet-5"


def test_config_is_immutable() -> None:
    config = AgentConfig.model_validate(VALID)
    with pytest.raises(Exception):
        config.model = "something-else"  # type: ignore[misc]


def test_shipped_backend_config_is_valid() -> None:
    """The real config/agents/backend.yaml must load and match the spec."""
    config = load_agent_config("backend", Path(__file__).resolve().parents[2] / "config/agents")
    assert config.key == "backend"
    assert config.model == "claude-opus-5"
    assert set(config.tools) == {
        "read_file",
        "write_file",
        "list_files",
        "run_command",
        "send_message",
        "ask_agent",
        "append_project_context",
    }
    assert config.resolve_system_prompt(Path("config/agents")).strip()


def test_every_shipped_agent_encodes_the_handoff_protocol() -> None:
    """Phase 3 requires each prompt to state its dependencies and to say that a
    missing one is asked for, not invented."""
    from app.agents.config_loader import load_all_agent_configs

    directory = Path(__file__).resolve().parents[2] / "config/agents"
    configs = load_all_agent_configs(directory)
    assert set(configs) == {"backend", "database", "devops", "frontend"}

    for key, config in configs.items():
        prompt = config.resolve_system_prompt(directory)
        assert "PROJECT.md" in prompt, f"{key} never mentions the shared context"
        assert "ask_agent" in prompt, f"{key} is not told how to ask a teammate"
        assert "append_project_context" in prompt, f"{key} is not told how to publish"
        assert config.does_not_own, f"{key} has no stated lane boundary"
        assert config.hands_off_to, f"{key} has no handoff targets"
        # Handoffs are keyed by role so a roster swap does not break the prompts.
        for other in configs:
            if other != key:
                assert configs[other].display_name not in prompt, (
                    f"{key} names {configs[other].display_name} directly; "
                    "handoffs must reference roles, not agent names"
                )
