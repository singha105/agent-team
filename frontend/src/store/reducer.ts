/**
 * Pure event reducer.
 *
 * Kept separate from the Zustand store so the interesting logic — how a stream
 * of events becomes the room you see — can be tested as a function, without a
 * React tree or a socket. The store below is a thin wrapper that applies this.
 */

import type { AgentTeamEvent, AgentStatus, TaskStatus } from "../lib/events";

export interface AgentView {
  key: string;
  status: AgentStatus;
  previousStatus: AgentStatus | null;
  activeTaskId: number | null;
  /** Bumped on every status change so the character can re-key a transition. */
  statusSeq: number;
  lastEventAt: string | null;
}

export interface TaskView {
  id: number;
  agentKey: string;
  title: string;
  status: TaskStatus;
  createdBy: string;
  attempt: number;
  haltReason: string | null;
  costUsd: number;
  tokens: number;
  parentTaskId: number | null;
}

export type LiveEntryKind = "message" | "tool" | "usage";

export interface LiveEntry {
  id: string;
  seq: number;
  kind: LiveEntryKind;
  at: string;
  label: string;
  detail: string | null;
  agentKey: string | null;
  toolCallId?: number;
  isError?: boolean;
  durationMs?: number | null;
  pending?: boolean;
}

/** An agent backing off before a retry — waiting, not stuck. */
export interface Waiting {
  agentKey: string;
  taskId: number;
  reason: string;
  attempt: number;
  maxAttempts: number;
  retryInSeconds: number;
  seq: number;
}

/** One agent's write replaced another's content. */
export interface ConflictNotice {
  id: string;
  taskId: number;
  path: string;
  writer: string;
  replacedWriter: string;
  raced: boolean;
  at: string;
}

export interface Bubble {
  id: string;
  seq: number;
  from: string;
  to: string;
  preview: string;
  taskId: number;
  at: string;
}

export interface RoomState {
  agents: Record<string, AgentView>;
  tasks: Record<number, TaskView>;
  /** Live run entries, newest last, keyed by task id. */
  live: Record<number, LiveEntry[]>;
  /** Speech bubbles currently in flight between desks. */
  bubbles: Bubble[];
  sessionCostUsd: number;
  sessionTokens: number;
  lastSeq: number;
  /** Keyed by agent: who is currently backing off. */
  waiting: Record<string, Waiting>;
  /** Write conflicts, newest last. Kept until dismissed. */
  conflicts: ConflictNotice[];
}

export const initialRoomState: RoomState = {
  agents: {},
  tasks: {},
  live: {},
  bubbles: [],
  sessionCostUsd: 0,
  sessionTokens: 0,
  lastSeq: 0,
  waiting: {},
  conflicts: [],
};

/** Keep per-task live logs bounded; a long run must not grow without limit. */
export const MAX_LIVE_ENTRIES = 300;
/** Bubbles are ephemeral; only the most recent few are ever in flight. */
export const MAX_BUBBLES = 6;
/** Conflicts persist until dismissed, but a runaway must not grow forever. */
export const MAX_CONFLICTS = 20;

function ensureAgent(state: RoomState, key: string): AgentView {
  return (
    state.agents[key] ?? {
      key,
      status: "idle",
      previousStatus: null,
      activeTaskId: null,
      statusSeq: 0,
      lastEventAt: null,
    }
  );
}

function appendLive(state: RoomState, taskId: number, entry: LiveEntry): Record<number, LiveEntry[]> {
  const existing = state.live[taskId] ?? [];
  const next = [...existing, entry];
  return {
    ...state.live,
    [taskId]: next.length > MAX_LIVE_ENTRIES ? next.slice(next.length - MAX_LIVE_ENTRIES) : next,
  };
}

function truncate(text: string, max = 90): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

/**
 * Apply one event. Always returns a new state object; never mutates.
 *
 * Events arriving out of order or twice are possible after a reconnect replay,
 * so anything that would double-count — cost, tokens — is guarded on `seq`.
 */
export function reduceEvent(state: RoomState, event: AgentTeamEvent): RoomState {
  // A replayed event must not be counted twice. `seq` is monotonic per process,
  // so anything at or below what we have already applied is a duplicate.
  const isReplay = event.seq > 0 && event.seq <= state.lastSeq;
  const lastSeq = Math.max(state.lastSeq, event.seq);

  switch (event.type) {
    case "agent.status_changed": {
      const agent = ensureAgent(state, event.agent_key);
      // Any status change means the agent got through — clear the wait, or the
      // room would keep claiming it is backing off long after it recovered.
      const { [event.agent_key]: _cleared, ...waiting } = state.waiting;
      return {
        ...state,
        lastSeq,
        waiting,
        agents: {
          ...state.agents,
          [event.agent_key]: {
            ...agent,
            status: event.status as AgentStatus,
            previousStatus: agent.status,
            activeTaskId: event.task_id ?? agent.activeTaskId,
            statusSeq: agent.statusSeq + 1,
            lastEventAt: event.at,
          },
        },
      };
    }

    case "agent.waiting": {
      return {
        ...state,
        lastSeq,
        waiting: {
          ...state.waiting,
          [event.agent_key]: {
            agentKey: event.agent_key,
            taskId: event.task_id,
            reason: event.reason,
            attempt: event.attempt,
            maxAttempts: event.max_attempts,
            retryInSeconds: event.retry_in_seconds,
            seq: event.seq,
          },
        },
      };
    }

    case "workspace.conflict": {
      const notice = {
        id: `c${event.seq}`,
        taskId: event.task_id,
        path: event.path,
        writer: event.writer,
        replacedWriter: event.replaced_writer,
        raced: event.raced,
        at: event.at,
      };
      return {
        ...state,
        lastSeq,
        conflicts: [...state.conflicts, notice].slice(-MAX_CONFLICTS),
      };
    }

    case "task.created": {
      return {
        ...state,
        lastSeq,
        tasks: {
          ...state.tasks,
          [event.task_id]: {
            id: event.task_id,
            agentKey: event.agent_key,
            title: event.title,
            status: event.status as TaskStatus,
            createdBy: event.created_by,
            attempt: 1,
            haltReason: null,
            costUsd: 0,
            tokens: 0,
            // A task created by an agent rather than the human is a delegation;
            // the parent is filled in when the detail is fetched.
            parentTaskId: null,
          },
        },
      };
    }

    case "task.status_changed": {
      const task = state.tasks[event.task_id];
      if (!task) {
        return {
          ...state,
          lastSeq,
          tasks: {
            ...state.tasks,
            [event.task_id]: {
              id: event.task_id,
              agentKey: event.agent_key,
              title: `Task ${event.task_id}`,
              status: event.status as TaskStatus,
              createdBy: "unknown",
              attempt: event.attempt,
              haltReason: event.halt_reason,
              costUsd: 0,
              tokens: 0,
              parentTaskId: null,
            },
          },
        };
      }
      return {
        ...state,
        lastSeq,
        tasks: {
          ...state.tasks,
          [event.task_id]: {
            ...task,
            status: event.status as TaskStatus,
            attempt: event.attempt,
            haltReason: event.halt_reason,
          },
        },
      };
    }

    case "message.created": {
      const entry: LiveEntry = {
        id: `m${event.message_id}`,
        seq: event.seq,
        kind: "message",
        at: event.at,
        label: event.message_type,
        detail: event.preview,
        agentKey: event.from_agent,
      };

      // An agent-to-agent message is the one thing that gets a bubble: it is a
      // crossing between two desks, which is exactly what the room should show.
      const crossing =
        event.message_type === "agent_to_agent" && event.from_agent && event.to_agent;

      const bubbles = crossing
        ? [
            ...state.bubbles,
            {
              id: `b${event.message_id}`,
              seq: event.seq,
              from: event.from_agent as string,
              to: event.to_agent as string,
              preview: truncate(event.preview ?? ""),
              taskId: event.task_id,
              at: event.at,
            },
          ].slice(-MAX_BUBBLES)
        : state.bubbles;

      return {
        ...state,
        lastSeq,
        live: appendLive(state, event.task_id, entry),
        bubbles,
      };
    }

    case "tool.started": {
      return {
        ...state,
        lastSeq,
        live: appendLive(state, event.task_id, {
          id: `t${event.tool_call_id}`,
          seq: event.seq,
          kind: "tool",
          at: event.at,
          label: event.tool_name,
          detail: event.arguments_preview,
          agentKey: event.agent_key,
          toolCallId: event.tool_call_id,
          pending: true,
        }),
      };
    }

    case "tool.finished": {
      // Resolve the entry the matching tool.started created, rather than adding
      // a second row: a tool call is one thing that took time, not two events.
      const entries = state.live[event.task_id] ?? [];
      const index = entries.findIndex(
        (entry) => entry.kind === "tool" && entry.toolCallId === event.tool_call_id,
      );
      if (index === -1) {
        return {
          ...state,
          lastSeq,
          live: appendLive(state, event.task_id, {
            id: `t${event.tool_call_id}`,
            seq: event.seq,
            kind: "tool",
            at: event.at,
            label: event.tool_name,
            detail: event.error,
            agentKey: event.agent_key,
            toolCallId: event.tool_call_id,
            isError: event.is_error,
            durationMs: event.duration_ms,
            pending: false,
          }),
        };
      }
      const updated = [...entries];
      updated[index] = {
        ...(updated[index] as LiveEntry),
        pending: false,
        isError: event.is_error,
        durationMs: event.duration_ms,
        detail: event.error ?? (updated[index] as LiveEntry).detail,
      };
      return { ...state, lastSeq, live: { ...state.live, [event.task_id]: updated } };
    }

    case "usage.updated": {
      const task = state.tasks[event.task_id];
      const entry: LiveEntry = {
        id: `u${event.task_id}-${event.iteration}`,
        seq: event.seq,
        kind: "usage",
        at: event.at,
        label: `iteration ${event.iteration}`,
        detail: `${event.input_tokens.toLocaleString()} in · ${event.output_tokens.toLocaleString()} out`,
        agentKey: event.agent_key,
      };

      return {
        ...state,
        lastSeq,
        // A replayed usage event must not inflate the meter.
        sessionCostUsd: isReplay
          ? state.sessionCostUsd
          : state.sessionCostUsd + event.estimated_cost_usd,
        sessionTokens: isReplay ? state.sessionTokens : state.sessionTokens + event.total_tokens,
        tasks: task
          ? {
              ...state.tasks,
              [event.task_id]: {
                ...task,
                costUsd: event.task_total_cost_usd,
                tokens: isReplay ? task.tokens : task.tokens + event.total_tokens,
              },
            }
          : state.tasks,
        live: appendLive(state, event.task_id, entry),
      };
    }

    default:
      return { ...state, lastSeq };
  }
}

export function reduceEvents(state: RoomState, events: AgentTeamEvent[]): RoomState {
  return events.reduce(reduceEvent, state);
}
