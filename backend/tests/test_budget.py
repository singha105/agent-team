"""Budget accounting and cost estimation."""

from __future__ import annotations

import pytest

from app.agents.budget import BudgetExceeded, BudgetTracker
from app.core.pricing import TokenUsage, UnknownModelError, estimate_cost_usd, rates_for


def tracker(**kw) -> BudgetTracker:
    return BudgetTracker(
        max_iterations=kw.get("max_iterations", 25),
        max_tokens=kw.get("max_tokens", 500_000),
        max_hops=kw.get("max_hops", 10),
    )


def test_fresh_tracker_permits_the_first_iteration() -> None:
    tracker().check_before_iteration()


def test_iteration_ceiling_trips_at_the_limit() -> None:
    t = tracker(max_iterations=3)
    for _ in range(3):
        t.check_before_iteration()
        t.record_iteration()
    with pytest.raises(BudgetExceeded) as exc:
        t.check_before_iteration()
    assert exc.value.limit_name == "iteration"
    assert exc.value.limit == 3
    assert t.iterations == 3, "must not exceed the ceiling"


def test_token_ceiling_trips() -> None:
    t = tracker(max_tokens=1000)
    t.record_usage(TokenUsage(input_tokens=600, output_tokens=500), 0.01)
    with pytest.raises(BudgetExceeded) as exc:
        t.check_before_iteration()
    assert exc.value.limit_name == "token"
    assert exc.value.actual == 1100


def test_hop_ceiling_trips() -> None:
    t = tracker(max_hops=2)
    t.record_hop()
    t.record_hop()
    with pytest.raises(BudgetExceeded) as exc:
        t.check_before_iteration()
    assert exc.value.limit_name == "agent hop"


def test_usage_accumulates_across_calls() -> None:
    t = tracker()
    t.record_usage(TokenUsage(input_tokens=100, output_tokens=50), 0.001)
    t.record_usage(TokenUsage(input_tokens=200, output_tokens=75), 0.002)
    assert t.usage.input_tokens == 300
    assert t.usage.output_tokens == 125
    assert t.total_tokens == 425
    assert t.cost_usd == pytest.approx(0.003)


def test_cache_tokens_count_toward_the_token_budget() -> None:
    usage = TokenUsage(
        input_tokens=10, output_tokens=20, cache_creation_tokens=30, cache_read_tokens=40
    )
    assert usage.billable_total == 100


def test_token_usage_addition_is_field_wise() -> None:
    a = TokenUsage(input_tokens=1, output_tokens=2, cache_creation_tokens=3, cache_read_tokens=4)
    assert (a + a) == TokenUsage(
        input_tokens=2, output_tokens=4, cache_creation_tokens=6, cache_read_tokens=8
    )


def test_usage_parsed_from_a_response_object() -> None:
    class FakeUsage:
        input_tokens = 1234
        output_tokens = 567
        cache_creation_input_tokens = 89
        cache_read_input_tokens = 10

    usage = TokenUsage.from_response_usage(FakeUsage())
    assert usage.input_tokens == 1234
    assert usage.cache_creation_tokens == 89
    assert usage.cache_read_tokens == 10


def test_usage_parsing_tolerates_absent_cache_fields() -> None:
    """Responses that used no caching omit the cache fields entirely."""

    class Sparse:
        input_tokens = 100
        output_tokens = 20

    usage = TokenUsage.from_response_usage(Sparse())
    assert usage.cache_read_tokens == 0
    assert usage.billable_total == 120


def test_usage_parsing_tolerates_none_values() -> None:
    class Nulls:
        input_tokens = 5
        output_tokens = None
        cache_creation_input_tokens = None
        cache_read_input_tokens = None

    assert TokenUsage.from_response_usage(Nulls()).billable_total == 5


# -- pricing ---------------------------------------------------------------


def test_published_rates_are_loaded() -> None:
    opus = rates_for("claude-opus-5")
    assert (opus.input, opus.output) == (5.00, 25.00)
    sonnet = rates_for("claude-sonnet-5")
    assert (sonnet.input, sonnet.output) == (2.00, 10.00)


def test_cost_of_one_million_tokens_matches_the_published_rate() -> None:
    cost = estimate_cost_usd("claude-opus-5", TokenUsage(input_tokens=1_000_000))
    assert cost == pytest.approx(5.00)
    cost = estimate_cost_usd("claude-opus-5", TokenUsage(output_tokens=1_000_000))
    assert cost == pytest.approx(25.00)


def test_cost_combines_every_token_class() -> None:
    usage = TokenUsage(
        input_tokens=100_000,
        output_tokens=10_000,
        cache_creation_tokens=20_000,
        cache_read_tokens=50_000,
    )
    # 0.1*5 + 0.01*25 + 0.02*6.25 + 0.05*0.50
    assert estimate_cost_usd("claude-opus-5", usage) == pytest.approx(0.5 + 0.25 + 0.125 + 0.025)


def test_sonnet_is_cheaper_than_opus_for_identical_usage() -> None:
    usage = TokenUsage(input_tokens=500_000, output_tokens=100_000)
    assert estimate_cost_usd("claude-sonnet-5", usage) < estimate_cost_usd("claude-opus-5", usage)


def test_zero_usage_costs_nothing() -> None:
    assert estimate_cost_usd("claude-opus-5", TokenUsage()) == 0.0


def test_unpriced_model_raises_with_an_actionable_message() -> None:
    with pytest.raises(UnknownModelError, match="config/pricing.yaml"):
        estimate_cost_usd("claude-imaginary-9", TokenUsage(input_tokens=1))


def test_summary_reports_every_axis() -> None:
    t = tracker()
    t.record_iteration()
    t.record_usage(TokenUsage(input_tokens=10, output_tokens=5), 0.5)
    summary = t.summary()
    assert summary["iterations"] == 1
    assert summary["total_tokens"] == 15
    assert summary["estimated_cost_usd"] == 0.5
