/**
 * The store reducer.
 *
 * This is how a stream of events becomes the room you see, so these tests cover
 * the cases that would quietly show you the wrong room: duplicates after a
 * reconnect replay, a tool result arriving without its start, unbounded growth
 * during a long run.
 */

import { describe, expect, it } from "vitest";

import type {
  AgentStatusChanged,
  MessageCreated,
  TaskCreated,
  TaskStatusChanged,
  ToolFinished,
  ToolStarted,
  UsageUpdated,
} from "../lib/events";
import {
  MAX_BUBBLES,
  MAX_LIVE_ENTRIES,
  initialRoomState,
  reduceEvent,
  reduceEvents,
} from "./reducer";

let seq = 0;
const at = "2026-09-09T10:00:00Z";

function agentStatus(agent: string, status: string, taskId?: number): AgentStatusChanged {
  return { seq: ++seq, at, type: "agent.status_changed", agent_key: agent, status,
    previous_status: null, task_id: taskId ?? null };
}
function taskCreated(id: number, agent = "backend"): TaskCreated {
  return { seq: ++seq, at, type: "task.created", task_id: id, agent_key: agent,
    title: `Task ${id}`, status: "queued", created_by: "human" };
}
function taskStatus(id: number, status: string, agent = "backend"): TaskStatusChanged {
  return { seq: ++seq, at, type: "task.status_changed", task_id: id, agent_key: agent,
    status, previous_status: "queued", halt_reason: null, attempt: 1 };
}
function message(id: number, taskId = 1, overrides: Partial<MessageCreated> = {}): MessageCreated {
  return { seq: ++seq, at, type: "message.created", task_id: taskId, message_id: id,
    message_type: "assistant", role: "assistant", from_agent: "backend", to_agent: null,
    iteration: 1, preview: "hello", ...overrides };
}
function toolStarted(id: number, taskId = 1): ToolStarted {
  return { seq: ++seq, at, type: "tool.started", task_id: taskId, agent_key: "backend",
    tool_call_id: id, tool_name: "write_file", arguments_preview: "{path: a.py}" };
}
function toolFinished(id: number, taskId = 1, isError = false): ToolFinished {
  return { seq: ++seq, at, type: "tool.finished", task_id: taskId, agent_key: "backend",
    tool_call_id: id, tool_name: "write_file", duration_ms: 42, is_error: isError,
    error: isError ? "denied" : null };
}
function usage(taskId = 1, cost = 0.01, tokens = 100, iteration = 1): UsageUpdated {
  return { seq: ++seq, at, type: "usage.updated", task_id: taskId, agent_key: "backend",
    model: "claude-opus-5", iteration, input_tokens: 80, output_tokens: 20,
    total_tokens: tokens, estimated_cost_usd: cost, task_total_cost_usd: cost };
}

describe("agent status", () => {
  it("records the new status and remembers the previous one", () => {
    const state = reduceEvents(initialRoomState, [
      agentStatus("backend", "thinking", 1),
      agentStatus("backend", "working", 1),
    ]);
    expect(state.agents.backend?.status).toBe("working");
    expect(state.agents.backend?.previousStatus).toBe("thinking");
  });

  it("bumps a sequence so the character can re-key its transition", () => {
    const state = reduceEvents(initialRoomState, [
      agentStatus("backend", "thinking"),
      agentStatus("backend", "working"),
      agentStatus("backend", "idle"),
    ]);
    expect(state.agents.backend?.statusSeq).toBe(3);
  });

  it("creates an agent it has not seen before", () => {
    const state = reduceEvent(initialRoomState, agentStatus("newcomer", "working"));
    expect(state.agents.newcomer?.status).toBe("working");
  });
});

describe("tasks", () => {
  it("records a created task", () => {
    const state = reduceEvent(initialRoomState, taskCreated(7));
    expect(state.tasks[7]?.status).toBe("queued");
    expect(state.tasks[7]?.agentKey).toBe("backend");
  });

  it("updates status without losing the title", () => {
    const state = reduceEvents(initialRoomState, [taskCreated(7), taskStatus(7, "in_progress")]);
    expect(state.tasks[7]?.status).toBe("in_progress");
    expect(state.tasks[7]?.title).toBe("Task 7");
  });

  it("materialises a task from a status change alone", () => {
    // Possible after a reconnect where the creation event fell outside the
    // replay window. Dropping it would hide a running task entirely.
    const state = reduceEvent(initialRoomState, taskStatus(9, "in_progress"));
    expect(state.tasks[9]?.status).toBe("in_progress");
  });

  it("keeps the halt reason so the board can explain a failure", () => {
    const event = { ...taskStatus(7, "budget_exceeded"), halt_reason: "hop limit reached" };
    const state = reduceEvents(initialRoomState, [taskCreated(7), event]);
    expect(state.tasks[7]?.haltReason).toBe("hop limit reached");
  });
});

describe("live entries", () => {
  it("appends messages in order", () => {
    const state = reduceEvents(initialRoomState, [message(1), message(2)]);
    expect(state.live[1]?.map((e) => e.id)).toEqual(["m1", "m2"]);
  });

  it("resolves a tool call in place rather than adding a second row", () => {
    // A tool call is one thing that took time, not two events.
    const state = reduceEvents(initialRoomState, [toolStarted(5), toolFinished(5)]);
    const tools = state.live[1]?.filter((e) => e.kind === "tool") ?? [];
    expect(tools).toHaveLength(1);
    expect(tools[0]?.pending).toBe(false);
    expect(tools[0]?.durationMs).toBe(42);
  });

  it("marks a failed tool call as an error", () => {
    const state = reduceEvents(initialRoomState, [toolStarted(5), toolFinished(5, 1, true)]);
    expect(state.live[1]?.[0]?.isError).toBe(true);
    expect(state.live[1]?.[0]?.detail).toBe("denied");
  });

  it("still records a finish whose start it never saw", () => {
    const state = reduceEvent(initialRoomState, toolFinished(5));
    expect(state.live[1]).toHaveLength(1);
    expect(state.live[1]?.[0]?.pending).toBe(false);
  });

  it("keeps a long run bounded", () => {
    const events = Array.from({ length: MAX_LIVE_ENTRIES + 60 }, (_, i) => message(i + 1));
    const state = reduceEvents(initialRoomState, events);
    expect(state.live[1]).toHaveLength(MAX_LIVE_ENTRIES);
    // The newest survive; a trimmed log should show the present, not the past.
    expect(state.live[1]?.at(-1)?.id).toBe(`m${MAX_LIVE_ENTRIES + 60}`);
  });

  it("keeps each task's entries separate", () => {
    const state = reduceEvents(initialRoomState, [message(1, 1), message(2, 2)]);
    expect(state.live[1]).toHaveLength(1);
    expect(state.live[2]).toHaveLength(1);
  });
});

describe("bubbles", () => {
  it("raises one only for a crossing between two agents", () => {
    const state = reduceEvents(initialRoomState, [
      message(1, 1, { message_type: "assistant", to_agent: null }),
      message(2, 1, { message_type: "agent_to_agent", from_agent: "backend", to_agent: "database" }),
    ]);
    expect(state.bubbles).toHaveLength(1);
    expect(state.bubbles[0]?.from).toBe("backend");
    expect(state.bubbles[0]?.to).toBe("database");
  });

  it("raises none for a human message", () => {
    const state = reduceEvent(
      initialRoomState,
      message(1, 1, { message_type: "agent_to_agent", from_agent: null, to_agent: "backend" }),
    );
    expect(state.bubbles).toHaveLength(0);
  });

  it("truncates the preview", () => {
    const long = "x".repeat(400);
    const state = reduceEvent(
      initialRoomState,
      message(1, 1, {
        message_type: "agent_to_agent",
        from_agent: "backend",
        to_agent: "database",
        preview: long,
      }),
    );
    expect(state.bubbles[0]?.preview.length).toBeLessThanOrEqual(91);
    expect(state.bubbles[0]?.preview.endsWith("…")).toBe(true);
  });

  it("keeps only the most recent few in flight", () => {
    const events = Array.from({ length: MAX_BUBBLES + 5 }, (_, i) =>
      message(i + 1, 1, {
        message_type: "agent_to_agent",
        from_agent: "backend",
        to_agent: "database",
      }),
    );
    const state = reduceEvents(initialRoomState, events);
    expect(state.bubbles).toHaveLength(MAX_BUBBLES);
  });
});

describe("cost accounting", () => {
  it("accumulates session cost and tokens", () => {
    const state = reduceEvents(initialRoomState, [usage(1, 0.02, 100), usage(1, 0.03, 150)]);
    expect(state.sessionCostUsd).toBeCloseTo(0.05, 6);
    expect(state.sessionTokens).toBe(250);
  });

  it("tracks the task total the server reports", () => {
    const state = reduceEvents(initialRoomState, [taskCreated(1), usage(1, 0.07)]);
    expect(state.tasks[1]?.costUsd).toBeCloseTo(0.07, 6);
  });

  it("does not double-count a replayed usage event", () => {
    // Reconnecting with ?since_seq=N replays. Without the seq guard the meter
    // inflates every time the socket drops, which is routine in development.
    const first = reduceEvent(initialRoomState, usage(1, 0.05, 100));
    // seq 1 is below what has already been applied, which is what a replay
    // after a reconnect looks like.
    const replayEvent = { ...usage(1, 0.05, 100), seq: 1 };
    const second = reduceEvent(first, replayEvent);
    expect(second.sessionCostUsd).toBeCloseTo(first.sessionCostUsd, 6);
    expect(second.sessionTokens).toBe(first.sessionTokens);
  });

  it("advances lastSeq monotonically", () => {
    const state = reduceEvents(initialRoomState, [usage(1), usage(1), usage(1)]);
    expect(state.lastSeq).toBe(seq);
    const older = reduceEvent(state, { ...usage(1), seq: 2 });
    expect(older.lastSeq).toBe(state.lastSeq);
  });
});

describe("purity", () => {
  it("never mutates the state it is given", () => {
    const before = initialRoomState;
    const snapshot = JSON.stringify(before);
    reduceEvents(before, [taskCreated(1), agentStatus("backend", "working"), usage(1)]);
    expect(JSON.stringify(before)).toBe(snapshot);
  });

  it("ignores an event type it does not know", () => {
    const unknown = { seq: 999, at, type: "something.new" } as never;
    const state = reduceEvent(initialRoomState, unknown);
    expect(state.lastSeq).toBe(999);
    expect(state.agents).toEqual({});
  });
});
