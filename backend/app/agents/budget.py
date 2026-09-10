"""Per-task budget enforcement.

Three independent ceilings, all from spec section 3. Any one of them tripping
halts the task with status budget_exceeded and a reason naming the limit.

The hop counter is wired up here but unused in Phase 1 — there is no
inter-agent messaging yet. It exists so the accounting has somewhere to live
when agent-to-agent delegation lands, rather than being retrofitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.pricing import TokenUsage


class BudgetExceeded(Exception):
    """Raised when a task exceeds one of its ceilings."""

    def __init__(self, limit_name: str, limit: float, actual: float, unit: str = "") -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.actual = actual
        rendered_limit = f"{unit}{limit:,.2f}" if unit else f"{limit:,.0f}"
        rendered_actual = f"{unit}{actual:,.2f}" if unit else f"{actual:,.0f}"
        super().__init__(
            f"budget exceeded: {limit_name} limit of {rendered_limit} reached "
            f"(at {rendered_actual}). The task was halted before making another API call."
        )


@dataclass
class BudgetTracker:
    """Mutable budget state for one task run."""

    max_iterations: int
    max_tokens: int
    max_hops: int
    max_cost_usd: float = 0.0
    max_tree_cost_usd: float = 0.0
    # Set for a delegated run so the tree's spend so far counts against the
    # tree ceiling; a child that only knew its own cost could not enforce one.
    tree_cost_so_far: float = 0.0

    iterations: int = 0
    hops: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.usage.billable_total

    def check_before_iteration(self) -> None:
        """Called before every API call. Raises if any ceiling is already met."""
        if self.iterations >= self.max_iterations:
            raise BudgetExceeded("iteration", self.max_iterations, self.iterations)
        if self.total_tokens >= self.max_tokens:
            raise BudgetExceeded("token", self.max_tokens, self.total_tokens)
        if self.hops >= self.max_hops:
            raise BudgetExceeded("agent hop", self.max_hops, self.hops)
        # Money last, because the message should name the cheapest explanation
        # first when several ceilings are close.
        if self.max_cost_usd and self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded("cost", self.max_cost_usd, self.cost_usd, unit="$")
        if self.max_tree_cost_usd:
            tree_total = self.tree_cost_so_far + self.cost_usd
            if tree_total >= self.max_tree_cost_usd:
                raise BudgetExceeded(
                    "delegation tree cost", self.max_tree_cost_usd, tree_total, unit="$"
                )

    def record_iteration(self) -> int:
        self.iterations += 1
        return self.iterations

    def record_hop(self) -> int:
        self.hops += 1
        return self.hops

    def record_usage(self, usage: TokenUsage, cost_usd: float) -> None:
        self.usage = self.usage + usage
        self.cost_usd += cost_usd

    def summary(self) -> dict[str, float | int]:
        return {
            "iterations": self.iterations,
            "hops": self.hops,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_creation_tokens": self.usage.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.cost_usd, 6),
        }
