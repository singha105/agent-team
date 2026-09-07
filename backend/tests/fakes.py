"""A scripted stand-in for anthropic.AsyncAnthropic.

This is a test double, not a stub implementation: the production path in
app.agents.runtime always calls the real SDK. It exists so loop termination,
budget trips and persistence can be asserted deterministically and without
spending money — the real API cannot be made to reliably emit 25 consecutive
tool calls on demand.

Block shapes mirror the Messages API: text blocks carry .text, tool_use blocks
carry .id/.name/.input, and every block serialises via model_dump_json() the
way SDK pydantic models do.
"""

from __future__ import annotations

import itertools
import json
from typing import Any


class Block:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def model_dump_json(self) -> str:
        return json.dumps(self.__dict__)


def text_block(text: str) -> Block:
    return Block(type="text", text=text)


def tool_use_block(name: str, tool_input: dict, block_id: str = "toolu_1") -> Block:
    return Block(type="tool_use", id=block_id, name=name, input=tool_input)


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int, cache_read: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = cache_read


class FakeResponse:
    def __init__(
        self,
        content: list[Block],
        stop_reason: str,
        input_tokens: int = 100,
        output_tokens: int = 50,
        stop_details: Any = None,
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = FakeUsage(input_tokens, output_tokens)
        self.stop_details = stop_details


class FakeMessages:
    def __init__(self, responses: list[FakeResponse], repeat_last: bool) -> None:
        self._responses = responses
        self._repeat_last = repeat_last
        self._iter = iter(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        # Snapshot `messages`: the runtime keeps appending to the same list, so
        # storing it by reference would make every recorded call look identical.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        try:
            return next(self._iter)
        except StopIteration:
            if self._repeat_last and self._responses:
                return self._responses[-1]
            raise AssertionError(
                f"the runtime made {len(self.calls)} API calls but only "
                f"{len(self._responses)} responses were scripted"
            ) from None


class FakeClient:
    """Quacks like anthropic.AsyncAnthropic for the one method the runtime uses."""

    def __init__(self, responses: list[FakeResponse], repeat_last: bool = False) -> None:
        self.messages = FakeMessages(responses, repeat_last)

    @property
    def call_count(self) -> int:
        return len(self.messages.calls)

    @property
    def calls(self) -> list[dict]:
        return self.messages.calls


def always_calls_a_tool(tool: str = "list_files", args: dict | None = None) -> FakeClient:
    """A client that never stops calling tools — used to force budget trips."""
    ids = itertools.count()
    return FakeClient(
        [
            FakeResponse(
                content=[tool_use_block(tool, args or {"path": "."}, f"toolu_{next(ids)}")],
                stop_reason="tool_use",
            )
        ],
        repeat_last=True,
    )
