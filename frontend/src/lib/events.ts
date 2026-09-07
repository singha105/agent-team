// AUTO-GENERATED — do not edit by hand.
// Source: backend/app/events/schemas.py
// Regenerate: python scripts/export_types.py

export type TaskStatus = "queued" | "in_progress" | "needs_review" | "done" | "failed" | "budget_exceeded";
export type AgentStatus = "idle" | "thinking" | "working" | "waiting_on_human" | "blocked" | "error";

export interface AgentStatusChanged {
  /** Monotonic per-process sequence number. */
  seq: number;
  at: string;
  type: "agent.status_changed";
  agent_key: string;
  status: string;
  previous_status: string | null;
  task_id: number | null;
}

export interface TaskCreated {
  /** Monotonic per-process sequence number. */
  seq: number;
  at: string;
  type: "task.created";
  task_id: number;
  agent_key: string;
  title: string;
  status: string;
  created_by: string;
}

export interface TaskStatusChanged {
  /** Monotonic per-process sequence number. */
  seq: number;
  at: string;
  type: "task.status_changed";
  task_id: number;
  agent_key: string;
  status: string;
  previous_status: string;
  halt_reason: string | null;
  attempt: number;
}

export interface MessageCreated {
  /** Monotonic per-process sequence number. */
  seq: number;
  at: string;
  type: "message.created";
  task_id: number;
  message_id: number;
  message_type: string;
  role: string;
  from_agent: string | null;
  to_agent: string | null;
  iteration: number;
  preview: string | null;
}

export interface ToolStarted {
  /** Monotonic per-process sequence number. */
  seq: number;
  at: string;
  type: "tool.started";
  task_id: number;
  agent_key: string;
  tool_call_id: number;
  tool_name: string;
  arguments_preview: string | null;
}

export interface ToolFinished {
  /** Monotonic per-process sequence number. */
  seq: number;
  at: string;
  type: "tool.finished";
  task_id: number;
  agent_key: string;
  tool_call_id: number;
  tool_name: string;
  duration_ms: number | null;
  is_error: boolean;
  error: string | null;
}

export interface UsageUpdated {
  /** Monotonic per-process sequence number. */
  seq: number;
  at: string;
  type: "usage.updated";
  task_id: number;
  agent_key: string;
  model: string;
  iteration: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  task_total_cost_usd: number;
}

export type AgentTeamEvent =
  | AgentStatusChanged
  | TaskCreated
  | TaskStatusChanged
  | MessageCreated
  | ToolStarted
  | ToolFinished
  | UsageUpdated;

export const EVENT_TYPES = [
  "agent.status_changed",
  "task.created",
  "task.status_changed",
  "message.created",
  "tool.started",
  "tool.finished",
  "usage.updated",
] as const;

/** Narrow an event by its discriminant. */
export function isEvent<T extends AgentTeamEvent["type"]>(
  event: AgentTeamEvent,
  type: T,
): event is Extract<AgentTeamEvent, { type: T }> {
  return event.type === type;
}
