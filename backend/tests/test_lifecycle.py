"""Task lifecycle state machine."""

from __future__ import annotations

import pytest

from app.agents.lifecycle import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    can_transition,
    is_terminal,
    validate_transition,
)
from app.models import TaskStatus

LEGAL = [
    (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS),
    (TaskStatus.QUEUED, TaskStatus.FAILED),
    (TaskStatus.IN_PROGRESS, TaskStatus.NEEDS_REVIEW),
    (TaskStatus.IN_PROGRESS, TaskStatus.FAILED),
    (TaskStatus.IN_PROGRESS, TaskStatus.BUDGET_EXCEEDED),
    (TaskStatus.NEEDS_REVIEW, TaskStatus.DONE),
    (TaskStatus.NEEDS_REVIEW, TaskStatus.QUEUED),
]

ILLEGAL = [
    # Skipping the work entirely.
    (TaskStatus.QUEUED, TaskStatus.DONE),
    (TaskStatus.QUEUED, TaskStatus.NEEDS_REVIEW),
    (TaskStatus.QUEUED, TaskStatus.BUDGET_EXCEEDED),
    # Approving work that was never submitted for review.
    (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
    # Terminal states are terminal.
    (TaskStatus.DONE, TaskStatus.QUEUED),
    (TaskStatus.DONE, TaskStatus.IN_PROGRESS),
    (TaskStatus.FAILED, TaskStatus.IN_PROGRESS),
    (TaskStatus.FAILED, TaskStatus.DONE),
    (TaskStatus.BUDGET_EXCEEDED, TaskStatus.QUEUED),
    # Reviewing a task that already failed.
    (TaskStatus.FAILED, TaskStatus.NEEDS_REVIEW),
    # No self-loops.
    (TaskStatus.IN_PROGRESS, TaskStatus.IN_PROGRESS),
    (TaskStatus.QUEUED, TaskStatus.QUEUED),
]


@pytest.mark.parametrize(("start", "target"), LEGAL)
def test_legal_transitions_are_allowed(start: TaskStatus, target: TaskStatus) -> None:
    assert validate_transition(start, target) == target
    assert can_transition(start, target)


@pytest.mark.parametrize(("start", "target"), ILLEGAL)
def test_illegal_transitions_raise(start: TaskStatus, target: TaskStatus) -> None:
    assert not can_transition(start, target)
    with pytest.raises(IllegalTransitionError) as exc:
        validate_transition(start, target)
    assert str(start) in str(exc.value)
    assert str(target) in str(exc.value)


def test_error_names_the_legal_targets() -> None:
    """The message has to be actionable, not just a refusal."""
    with pytest.raises(IllegalTransitionError, match="in_progress"):
        validate_transition(TaskStatus.QUEUED, TaskStatus.DONE)


def test_unknown_status_raises_rather_than_passing_through() -> None:
    with pytest.raises(IllegalTransitionError):
        validate_transition(TaskStatus.QUEUED, "banana")
    with pytest.raises(IllegalTransitionError):
        validate_transition("banana", TaskStatus.DONE)


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BUDGET_EXCEEDED])
def test_terminal_states_have_no_outgoing_edges(status: TaskStatus) -> None:
    assert is_terminal(status)
    assert ALLOWED_TRANSITIONS[status] == frozenset()


@pytest.mark.parametrize(
    "status", [TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.NEEDS_REVIEW]
)
def test_non_terminal_states_can_progress(status: TaskStatus) -> None:
    assert not is_terminal(status)
    assert ALLOWED_TRANSITIONS[status]


def test_every_status_appears_in_the_table() -> None:
    """A status with no entry would raise KeyError instead of a clear error."""
    assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)


def test_the_only_backward_edge_is_review_rejection() -> None:
    """Rejection is the one way a task moves back toward the start."""
    order = {
        TaskStatus.QUEUED: 0,
        TaskStatus.IN_PROGRESS: 1,
        TaskStatus.NEEDS_REVIEW: 2,
        TaskStatus.DONE: 3,
    }
    backward = [
        (src, dst)
        for src, targets in ALLOWED_TRANSITIONS.items()
        for dst in targets
        if src in order and dst in order and order[dst] < order[src]
    ]
    assert backward == [(TaskStatus.NEEDS_REVIEW, TaskStatus.QUEUED)]
