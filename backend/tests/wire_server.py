"""A local server that speaks the Messages API wire format.

Why this exists: every other test replaces `client.messages.create` with a
scripted object, which proves the loop logic but proves nothing about the
request we would actually send. A wrong parameter name, a malformed tool
schema, or a misread `usage` field would pass all of them and fail on the first
real call.

Pointing the real `anthropic.AsyncAnthropic` at this server exercises the SDK's
own encoding, real HTTP, and real response parsing. What it cannot prove is that
Anthropic's service accepts the request — only the live API can do that. It
narrows the gap; it does not close it.

Request validation here deliberately mirrors the documented constraints the
runtime must respect, so a regression shows up as a 400 with a reason rather
than a silently accepted call.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Models this fake knows about, mirroring config/pricing.yaml.
KNOWN_MODELS = {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"}
VALID_EFFORT = {"low", "medium", "high", "xhigh", "max"}


class RecordingWireServer:
    """Records every request and replies with scripted Messages API responses."""

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.script = script or []
        self._index = 0
        self.app = self._build_app()

    def _next_response(self) -> dict[str, Any]:
        if self._index < len(self.script):
            body = self.script[self._index]
            self._index += 1
            return body
        return text_response("Done.")

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/messages")
        async def messages(request: Request) -> JSONResponse:  # noqa: ANN202
            body = await request.json()
            self.requests.append(body)
            self.headers.append(dict(request.headers))

            error = validate_request(body)
            if error is not None:
                return JSONResponse(
                    status_code=400,
                    content={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": error},
                    },
                )
            return JSONResponse(status_code=200, content=self._next_response())

        return app


def validate_request(body: dict[str, Any]) -> str | None:
    """Return an error string if the request would be rejected."""
    if body.get("model") not in KNOWN_MODELS:
        return f"model: {body.get('model')!r} is not a recognised model id"

    if not isinstance(body.get("max_tokens"), int) or body["max_tokens"] < 1:
        return "max_tokens: must be a positive integer"

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages: must be a non-empty array"
    if messages[0].get("role") != "user":
        return "messages: the first message must have role 'user'"
    if messages[-1].get("role") == "assistant":
        return (
            "messages: assistant prefill is not supported on this model; the final "
            "message must not be an assistant turn"
        )

    # budget_tokens was removed on current models and returns 400.
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and "budget_tokens" in thinking:
        return "thinking.budget_tokens: not supported on this model"

    output_config = body.get("output_config")
    if output_config is not None:
        if not isinstance(output_config, dict):
            return "output_config: must be an object"
        effort = output_config.get("effort")
        if effort is not None and effort not in VALID_EFFORT:
            return f"output_config.effort: {effort!r} is not a valid effort level"

    for tool in body.get("tools") or []:
        if not tool.get("name"):
            return "tools: every tool needs a name"
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return f"tools.{tool.get('name')}: input_schema must be an object schema"
        if not isinstance(schema.get("properties"), dict):
            return f"tools.{tool.get('name')}: input_schema needs a properties object"

    # tool_result blocks must reference a tool_use id from an earlier turn.
    offered: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                offered.add(block.get("id"))
            if block.get("type") == "tool_result":
                if "tool_use_id" not in block:
                    return "tool_result: missing tool_use_id"
                if block["tool_use_id"] not in offered:
                    return (
                        f"tool_result: tool_use_id {block['tool_use_id']!r} does not "
                        "match any tool_use block in the conversation"
                    )
    return None


def _usage(input_tokens: int = 120, output_tokens: int = 40) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def text_response(text: str, model: str = "claude-opus-5", **usage: int) -> dict[str, Any]:
    return {
        "id": "msg_wire_text",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": _usage(**usage),
    }


def tool_use_response(
    name: str, tool_input: dict[str, Any], block_id: str = "toolu_wire_1", **usage: int
) -> dict[str, Any]:
    return {
        "id": "msg_wire_tool",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "tool_use", "id": block_id, "name": name, "input": tool_input}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": _usage(**usage),
    }


def refusal_response(category: str = "cyber") -> dict[str, Any]:
    return {
        "id": "msg_wire_refusal",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": ""}],
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": category, "explanation": "declined"},
        "stop_sequence": None,
        "usage": _usage(),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def running_wire_server(script: list[dict[str, Any]] | None = None):
    """Start the server on a free port and yield (server, base_url)."""
    wire = RecordingWireServer(script)
    port = _free_port()
    config = uvicorn.Config(wire.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover
            raise RuntimeError("wire server did not start")
        yield wire, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
