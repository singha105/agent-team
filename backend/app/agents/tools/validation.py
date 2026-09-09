"""Validation of model-supplied tool arguments.

The model occasionally emits arguments that do not match a tool's schema — a
missing required field, a string where an integer belongs, an invented
parameter. That is normal, not exceptional, and the run must survive it.

The contract here: never raise into the loop. Return a message precise enough
that the agent can correct itself on the next turn, which is what turns a
malformed call into one wasted iteration rather than a dead task.

A small validator rather than jsonschema: the schemas in this project are a
single flat object with typed properties, and depending on a library to check
six of those is more surface than it is worth.
"""

from __future__ import annotations

from typing import Any

JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class ArgumentError(ValueError):
    """Raised with a message written to be read by the model, not a developer."""


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null" if value is None else type(value).__name__


def validate_arguments(tool_name: str, schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
    """Check `arguments` against a tool's input_schema.

    Returns the arguments unchanged when valid; raises ArgumentError with a
    correction the model can act on otherwise.
    """
    if not isinstance(arguments, dict):
        raise ArgumentError(
            f"{tool_name} expects an object of arguments, got {_type_name(arguments)}."
        )

    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = list(schema.get("required") or [])

    problems: list[str] = []

    missing = [name for name in required if name not in arguments]
    if missing:
        problems.append(
            f"missing required {'argument' if len(missing) == 1 else 'arguments'}: "
            + ", ".join(repr(name) for name in missing)
        )

    unknown = [name for name in arguments if name not in properties]
    if unknown:
        problems.append(
            f"unknown {'argument' if len(unknown) == 1 else 'arguments'}: "
            + ", ".join(repr(name) for name in unknown)
            + f". {tool_name} accepts: {', '.join(sorted(properties)) or '(none)'}"
        )

    for name, value in arguments.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if not expected:
            continue
        allowed = JSON_TYPES.get(expected)
        if allowed is None:
            continue
        # A bool is an int in Python; the model meaning one for the other is a
        # real mistake worth reporting rather than silently accepting.
        if isinstance(value, bool) and expected in {"integer", "number"}:
            problems.append(f"{name!r} must be {expected}, got boolean")
            continue
        if not isinstance(value, allowed):
            problems.append(f"{name!r} must be {expected}, got {_type_name(value)}")
            continue
        if expected == "integer" and "minimum" in spec and value < spec["minimum"]:
            problems.append(f"{name!r} must be at least {spec['minimum']}, got {value}")

    if problems:
        raise ArgumentError(
            f"Invalid arguments for {tool_name}: "
            + "; ".join(problems)
            + ". Correct them and try again."
        )

    return arguments
