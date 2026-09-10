"""Which agent may write where.

Agents drift into each other's work. The system prompts say not to, and prompts
are advice — an agent under pressure to finish will write the schema itself
rather than wait for the data agent.

Tool grants cannot express this. Every agent on a software team legitimately
needs to write files; the question is *which* files, and a grant is all-or-
nothing. Path ownership is the control that actually matches the lane: Ada
writing api/books.py is her job, Ada writing db/schema.sql is her taking Ines's.

An agent that declares no paths may write anywhere, so this stays opt-in and a
one-agent team needs no configuration at all.
"""

from __future__ import annotations

from fnmatch import fnmatch

from app.agents.config_loader import AgentConfigError, load_all_agent_configs
from app.core.config import Settings, get_settings


def _rosters(settings: Settings) -> dict[str, list[str]]:
    try:
        configs = load_all_agent_configs(settings.agent_configs)
    except AgentConfigError:
        # A roster that will not load is a louder problem than this one, and it
        # is reported where it is loaded. Do not compound it by refusing writes.
        return {}
    return {key: list(config.writes) for key, config in configs.items()}


def owner_of(path: str, settings: Settings | None = None) -> str | None:
    """Which agent claims this path, if any."""
    resolved = settings or get_settings()
    for key, patterns in _rosters(resolved).items():
        if any(fnmatch(path, pattern) for pattern in patterns):
            return key
    return None


def may_write(agent_key: str, path: str, settings: Settings | None = None) -> tuple[bool, str]:
    """Whether `agent_key` may write `path`, and why not.

    The refusal names the owner, because an agent told only "no" will try a
    different filename rather than hand the work over.
    """
    resolved = settings or get_settings()
    rosters = _rosters(resolved)

    own = rosters.get(agent_key) or []
    if own and any(fnmatch(path, pattern) for pattern in own):
        return True, ""

    claimant = owner_of(path, resolved)
    if claimant is not None and claimant != agent_key:
        return False, (
            f"{path} belongs to the {claimant!r} agent, not to you. Ask them for it with "
            f"ask_agent, or publish what you need to PROJECT.md and let them build it. "
            f"You own: {', '.join(own) if own else '(nothing declared)'}."
        )

    # Unclaimed path, or this agent declared no lane: allowed.
    return True, ""
