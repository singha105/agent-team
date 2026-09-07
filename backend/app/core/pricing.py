"""Cost estimation from the `usage` field of Messages API responses.

Rates live in config/pricing.yaml so they can be updated without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.config import get_settings


class UnknownModelError(KeyError):
    """Raised when a model has no entry in the pricing table."""


@dataclass(frozen=True)
class ModelRates:
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float


@dataclass(frozen=True)
class TokenUsage:
    """Token counts from one API response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def billable_total(self) -> int:
        """Total tokens counted against the per-task token budget."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    @classmethod
    def from_response_usage(cls, usage: object) -> TokenUsage:
        """Build from an SDK `response.usage` object.

        Cache fields are absent on responses that used no caching, so every
        field is read defensively and coerced through `int(... or 0)`.
        """
        return cls(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@lru_cache(maxsize=1)
def _load_rates(path_str: str) -> dict[str, ModelRates]:
    data = yaml.safe_load(Path(path_str).read_text(encoding="utf-8")) or {}
    models = data.get("models") or {}
    return {
        name: ModelRates(
            input=float(cfg["input"]),
            output=float(cfg["output"]),
            cache_write=float(cfg["cache_write"]),
            cache_read=float(cfg["cache_read"]),
        )
        for name, cfg in models.items()
    }


def rates_for(model: str) -> ModelRates:
    """Look up per-MTok rates for a model."""
    table = _load_rates(str(get_settings().pricing_file))
    try:
        return table[model]
    except KeyError as exc:
        raise UnknownModelError(
            f"No pricing entry for model {model!r}. Add it to config/pricing.yaml — "
            f"known models: {sorted(table)}"
        ) from exc


def estimate_cost_usd(model: str, usage: TokenUsage) -> float:
    """Estimate the USD cost of one API response."""
    r = rates_for(model)
    per_token = 1_000_000
    return (
        usage.input_tokens * r.input
        + usage.output_tokens * r.output
        + usage.cache_creation_tokens * r.cache_write
        + usage.cache_read_tokens * r.cache_read
    ) / per_token
