"""Application settings, loaded from the environment.

Every value has a default except ANTHROPIC_API_KEY, which is deliberately
optional at import time so that non-API code paths (migrations, tests, --help)
work without a key. Code that actually calls the API uses `require_api_key()`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: this file is backend/app/core/config.py, so four parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]


class MissingAPIKeyError(RuntimeError):
    """Raised when an API call is attempted without ANTHROPIC_API_KEY set."""


class Settings(BaseSettings):
    """Runtime configuration. Reads .env then the process environment."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Secrets -----------------------------------------------------------
    # No prefix: the SDK's own convention, and the spec mandates this name.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # --- Paths -------------------------------------------------------------
    db_path: Path = Field(default=Path("data/agentteam.db"), alias="AGENTTEAM_DB_PATH")
    workspace_root: Path = Field(default=Path("workspace"), alias="AGENTTEAM_WORKSPACE_ROOT")
    agent_config_dir: Path = Field(
        default=Path("config/agents"), alias="AGENTTEAM_AGENT_CONFIG_DIR"
    )
    pricing_path: Path = Field(default=Path("config/pricing.yaml"), alias="AGENTTEAM_PRICING_PATH")
    # Only set when AgentTeam itself runs inside a container. The sandbox's
    # --volume flag is interpreted by the *host* Docker daemon, so it must name
    # a path on the host, not the path this process sees. Without it a
    # containerised API mounts the wrong directory and agents appear to write
    # files that never reach the real workspace.
    host_workspace_root: Path | None = Field(default=None, alias="AGENTTEAM_HOST_WORKSPACE_ROOT")

    # --- Sandbox -----------------------------------------------------------
    sandbox_mode: str = Field(default="docker", alias="AGENTTEAM_SANDBOX_MODE")
    sandbox_image: str = Field(default="agentteam-runtime:latest", alias="AGENTTEAM_SANDBOX_IMAGE")
    sandbox_network: bool = Field(default=False, alias="AGENTTEAM_SANDBOX_NETWORK")
    sandbox_memory: str = Field(default="512m", alias="AGENTTEAM_SANDBOX_MEMORY")
    sandbox_pids_limit: int = Field(default=128, alias="AGENTTEAM_SANDBOX_PIDS_LIMIT")
    command_timeout_seconds: int = Field(default=30, alias="AGENTTEAM_COMMAND_TIMEOUT_SECONDS")
    max_output_bytes: int = Field(default=64_000, alias="AGENTTEAM_MAX_OUTPUT_BYTES")

    # --- Budgets -----------------------------------------------------------
    max_iterations: int = Field(default=25, alias="AGENTTEAM_MAX_ITERATIONS")
    max_tokens_per_task: int = Field(default=500_000, alias="AGENTTEAM_MAX_TOKENS_PER_TASK")
    max_agent_hops: int = Field(default=10, alias="AGENTTEAM_MAX_AGENT_HOPS")
    # One wall clock for an entire delegation tree. Per-run timeouts multiply:
    # ten nested runs at 60s each is a ten-minute stall, not a one-minute one.
    task_deadline_seconds: float = Field(
        default=600.0, gt=0, alias="AGENTTEAM_TASK_DEADLINE_SECONDS"
    )
    # Whether send_message queues a follow-up task so an idle recipient acts on
    # it. Off makes notifications inert records, which is the Phase 3 behaviour.
    wake_on_notify: bool = Field(default=True, alias="AGENTTEAM_WAKE_ON_NOTIFY")

    # --- Model request defaults -------------------------------------------
    max_response_tokens: int = Field(default=16_000, alias="AGENTTEAM_MAX_RESPONSE_TOKENS")
    effort: str = Field(default="high", alias="AGENTTEAM_EFFORT")

    # --- Logging -----------------------------------------------------------
    log_level: str = Field(default="INFO", alias="AGENTTEAM_LOG_LEVEL")

    @field_validator("host_workspace_root", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """Treat an empty env var as unset.

        `.env.example` ships AGENTTEAM_HOST_WORKSPACE_ROOT= with no value, so
        anyone following the README's `cp .env.example .env` would otherwise
        get Path('.') here — and the sandbox would ask Docker to bind-mount the
        literal path '.' instead of the workspace.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("sandbox_mode")
    @classmethod
    def _valid_sandbox_mode(cls, v: str) -> str:
        allowed = {"docker", "subprocess"}
        if v not in allowed:
            raise ValueError(f"sandbox_mode must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("effort")
    @classmethod
    def _valid_effort(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if v not in allowed:
            raise ValueError(f"effort must be one of {sorted(allowed)}, got {v!r}")
        return v

    def _resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def db_file(self) -> Path:
        return self._resolve(self.db_path)

    @property
    def workspace_dir(self) -> Path:
        return self._resolve(self.workspace_root)

    @property
    def agent_configs(self) -> Path:
        return self._resolve(self.agent_config_dir)

    @property
    def pricing_file(self) -> Path:
        return self._resolve(self.pricing_path)

    @property
    def sandbox_mount_source(self) -> Path:
        """The path the host daemon should bind-mount as the workspace."""
        if self.host_workspace_root is not None:
            return self.host_workspace_root
        return self.workspace_dir.resolve()

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_file}"

    @property
    def sync_database_url(self) -> str:
        """Used by Alembic, which runs migrations synchronously."""
        return f"sqlite:///{self.db_file}"

    def require_api_key(self) -> str:
        """Return the API key, or raise with an actionable message."""
        if not self.anthropic_api_key:
            raise MissingAPIKeyError(
                "ANTHROPIC_API_KEY is not set. Export it in your shell or add it to .env "
                "(see .env.example). It is never read from any other source."
            )
        return self.anthropic_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
