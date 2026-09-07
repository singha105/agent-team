"""Load and validate agent definitions from config/agents/*.yaml.

The runtime never hardcodes an agent. Everything that distinguishes one agent
from another — name, role, model, prompt, tool grants, avatar — comes from here.
Adding an agent is a new file; changing a model is a one-line edit.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.config import get_settings

# Tool names the runtime can actually wire up. A config granting anything else
# is a typo, and failing loudly here beats a silent no-op at runtime.
KNOWN_TOOLS = frozenset({"read_file", "write_file", "list_files", "run_command"})


class AgentConfigError(ValueError):
    """Raised when an agent YAML file is missing or invalid."""


class AgentConfig(BaseModel):
    """One agent's complete identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=64, description="Stable id, e.g. 'backend'.")
    display_name: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=128, description="Exact Claude model id.")
    avatar_id: str = Field(min_length=1, max_length=64)
    tools: list[str] = Field(min_length=1)

    owns: list[str] = Field(default_factory=list)
    personality: str | None = None

    # Exactly one of these must be set.
    system_prompt: str | None = None
    system_prompt_file: str | None = None

    # Optional per-agent budget overrides; None means use the global default.
    max_iterations: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    effort: str | None = None

    @field_validator("key")
    @classmethod
    def _key_is_slug(cls, v: str) -> str:
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"key must be alphanumeric with - or _, got {v!r}")
        return v

    @field_validator("tools")
    @classmethod
    def _tools_are_known(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - KNOWN_TOOLS)
        if unknown:
            raise ValueError(f"unknown tools {unknown}; known tools are {sorted(KNOWN_TOOLS)}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate entries in tools")
        return v

    @field_validator("effort")
    @classmethod
    def _effort_is_valid(cls, v: str | None) -> str | None:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if v is not None and v not in allowed:
            raise ValueError(f"effort must be one of {sorted(allowed)}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> AgentConfig:
        has_inline = self.system_prompt is not None and self.system_prompt.strip()
        has_file = self.system_prompt_file is not None
        if has_inline and has_file:
            raise ValueError("set system_prompt or system_prompt_file, not both")
        if not has_inline and not has_file:
            raise ValueError("one of system_prompt or system_prompt_file is required")
        return self

    def resolve_system_prompt(self, config_dir: Path) -> str:
        """Return the prompt text, reading the referenced file if needed."""
        if self.system_prompt is not None and self.system_prompt.strip():
            return self.system_prompt.strip()

        path = Path(self.system_prompt_file)  # type: ignore[arg-type]
        if not path.is_absolute():
            path = config_dir / path
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AgentConfigError(
                f"agent {self.key!r}: cannot read system_prompt_file {path}: {exc}"
            ) from exc
        if not text:
            raise AgentConfigError(f"agent {self.key!r}: system_prompt_file {path} is empty")
        return text


def load_agent_config(key: str, config_dir: Path | None = None) -> AgentConfig:
    """Load one agent by key. Raises AgentConfigError with an actionable message."""
    directory = config_dir or get_settings().agent_configs
    path = directory / f"{key}.yaml"

    if not path.is_file():
        available = sorted(p.stem for p in directory.glob("*.yaml")) if directory.is_dir() else []
        raise AgentConfigError(
            f"no agent config at {path}. Available agents: {available or '(none)'}"
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise AgentConfigError(f"{path} must contain a YAML mapping, got {type(raw).__name__}")

    try:
        config = AgentConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        raise AgentConfigError(f"{path} is invalid — {problems}") from exc

    if config.key != key:
        raise AgentConfigError(
            f"{path} declares key {config.key!r} but the filename says {key!r}; they must match"
        )
    return config


def load_all_agent_configs(config_dir: Path | None = None) -> dict[str, AgentConfig]:
    """Load every agent in the config directory, keyed by agent key."""
    directory = config_dir or get_settings().agent_configs
    if not directory.is_dir():
        raise AgentConfigError(f"agent config directory not found: {directory}")
    return {p.stem: load_agent_config(p.stem, directory) for p in sorted(directory.glob("*.yaml"))}


@lru_cache(maxsize=32)
def _cached(key: str, directory_str: str) -> AgentConfig:
    return load_agent_config(key, Path(directory_str))


def get_agent_config(key: str) -> AgentConfig:
    """Cached lookup for the hot path."""
    return _cached(key, str(get_settings().agent_configs))
