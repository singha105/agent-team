"""Task lifecycle state machine.

The happy path is queued → in_progress → needs_review → done. Two terminal
failure states, failed and budget_exceeded, are reachable from in_progress.
Rejection at review sends the task back to queued with the manager's feedback,
which is the only backward edge.

Transitions are validated centrally rather than by scattered assignments, so an
illegal move raises instead of silently corrupting the trace — a task that
jumps from queued straight to done has a history that cannot be reconstructed.
"""

from __future__ import annotations

from app.models.enums import TaskStatus

# Every legal edge. Anything absent here raises.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {
            TaskStatus.IN_PROGRESS,
            # A task can fail before the loop starts — an unpriced model, an
            # unreadable prompt file — without ever running.
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.IN_PROGRESS: frozenset(
        {
            TaskStatus.NEEDS_REVIEW,
            TaskStatus.FAILED,
            TaskStatus.BUDGET_EXCEEDED,
        }
    ),
    TaskStatus.NEEDS_REVIEW: frozenset(
        {
            TaskStatus.DONE,
            # Rejection re-queues with feedback appended.
            TaskStatus.QUEUED,
        }
    ),
    # Terminal.
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.BUDGET_EXCEEDED: frozenset(),
}

TERMINAL_STATES = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BUDGET_EXCEEDED})


class IllegalTransitionError(ValueError):
    """Raised when a task is moved between states that have no edge."""

    def __init__(self, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested

        # Building the message must not itself raise. A status can arrive from a
        # database row written by an older version of the code, and a bare
        # ValueError from the enum lookup would hide which transition was
        # attempted — exactly the information needed to debug it.
        try:
            allowed = sorted(ALLOWED_TRANSITIONS[TaskStatus(current)])
            targets = [str(s) for s in allowed] or "(none — terminal state)"
        except (KeyError, ValueError):
            targets = (
                f"(unknown status {current!r}; expected one of {[str(s) for s in TaskStatus]})"
            )

        super().__init__(
            f"illegal task transition {current!r} → {requested!r}. "
            f"From {current!r} the only legal targets are {targets}."
        )


def can_transition(current: str, requested: str) -> bool:
    try:
        return TaskStatus(requested) in ALLOWED_TRANSITIONS[TaskStatus(current)]
    except (KeyError, ValueError):
        return False


def validate_transition(current: str, requested: str) -> TaskStatus:
    """Return the target status, or raise IllegalTransitionError."""
    try:
        target = TaskStatus(requested)
    except ValueError as exc:
        raise IllegalTransitionError(current, requested) from exc

    try:
        current_status = TaskStatus(current)
    except ValueError as exc:
        raise IllegalTransitionError(current, requested) from exc

    if target not in ALLOWED_TRANSITIONS[current_status]:
        raise IllegalTransitionError(current, requested)
    return target


def is_terminal(status: str) -> bool:
    try:
        return TaskStatus(status) in TERMINAL_STATES
    except ValueError:
        return False
