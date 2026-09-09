/** Typed REST client. Mirrors backend/app/api. */

import type { AgentStatus, TaskStatus } from "./events";

export interface AgentSummary {
  key: string;
  display_name: string;
  role: string;
  model: string;
  avatar_id: string;
  status: AgentStatus;
  status_changed_at: string | null;
  bio: string | null;
  personality: string | null;
  owns: string[];
  tools: string[];
  active_task_id: number | null;
  queued_tasks: number;
}

export interface TaskSummary {
  id: number;
  title: string;
  description: string;
  status: TaskStatus;
  agent_key: string;
  created_by: string;
  parent_task_id: number | null;
  halt_reason: string | null;
  review_feedback: string | null;
  attempt: number;
  created_at: string;
  updated_at: string;
}

export interface MessageRecord {
  id: number;
  task_id: number;
  from_agent: string | null;
  to_agent: string | null;
  role: string;
  message_type: string;
  content: unknown;
  iteration: number;
  created_at: string;
}

export interface ToolCallRecord {
  id: number;
  task_id: number;
  agent_key: string;
  tool_name: string;
  tool_use_id: string | null;
  arguments: Record<string, unknown>;
  result: Record<string, unknown> | null;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
}

export interface UsageRecord {
  id: number;
  iteration: number;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  estimated_cost_usd: number;
  created_at: string;
}

export interface AgentSpend {
  agent_key: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  task_count: number;
}

export interface TreeUsage {
  root_task_id: number;
  task_ids: number[];
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  delegated_task_count: number;
  by_agent: AgentSpend[];
}

export interface DelegationNode {
  task_id: number;
  parent_task_id: number | null;
  depth: number;
  title: string;
  status: TaskStatus;
  created_by: string;
  agent_key: string;
}

export interface TaskDetail extends TaskSummary {
  messages: MessageRecord[];
  tool_calls: ToolCallRecord[];
  usage: UsageRecord[];
  total_cost_usd: number;
  total_tokens: number;
  tree: TreeUsage | null;
  delegation: DelegationNode[];
}

export interface TraceEntry {
  kind: "message" | "tool_call" | "usage";
  at: string;
  iteration: number;
  ref_id: number;
  summary: string;
  detail: Record<string, unknown>;
}

export interface TaskTrace {
  task_id: number;
  status: TaskStatus;
  agent_key: string;
  attempt: number;
  entries: TraceEntry[];
  total_cost_usd: number;
  total_tokens: number;
  tree: TreeUsage | null;
  delegation: DelegationNode[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  });

  if (!response.ok) {
    // FastAPI puts the reason in `detail`; surfacing it beats "Request failed",
    // because the API's errors are written to be actionable.
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail)) detail = body.detail.map((d: { msg?: string }) => d.msg).join("; ");
    } catch {
      /* a non-JSON error body is not worth failing over */
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  listAgents: () => request<AgentSummary[]>("/api/agents"),

  listTasks: (params?: { agent_key?: string; status?: string; limit?: number }) => {
    const query = new URLSearchParams();
    if (params?.agent_key) query.set("agent_key", params.agent_key);
    if (params?.status) query.set("status", params.status);
    if (params?.limit) query.set("limit", String(params.limit));
    const suffix = query.toString() ? `?${query}` : "";
    return request<TaskSummary[]>(`/api/tasks${suffix}`);
  },

  getTask: (id: number) => request<TaskDetail>(`/api/tasks/${id}`),

  getTrace: (id: number) => request<TaskTrace>(`/api/tasks/${id}/trace`),

  createTask: (payload: { agent_key: string; description: string; title?: string }) =>
    request<TaskSummary>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  review: (id: number, payload: { decision: "approve" | "reject"; feedback?: string }) =>
    request<TaskSummary>(`/api/tasks/${id}/review`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};
