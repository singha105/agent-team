"""Which agent is currently running, as ambient context.

The filesystem tools are deliberately context-free — that is what keeps them
testable in isolation and safe to run concurrently — but a write still needs to
record *who* wrote it, so a later replacement by a different agent can be
reported as a lost update.

A ContextVar carries that without changing any tool's signature. It follows the
task across await boundaries, and each asyncio task gets its own copy, so two
agents running concurrently never see each other's value.
"""

from __future__ import annotations

from contextvars import ContextVar

_current_agent: ContextVar[str] = ContextVar("current_agent", default="unknown")


def set_current_agent(agent_key: str) -> None:
    _current_agent.set(agent_key)


def current_agent() -> str:
    return _current_agent.get()
