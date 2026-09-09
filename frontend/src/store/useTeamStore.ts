/**
 * Zustand store mirroring server state.
 *
 * The store holds two kinds of thing and keeps them apart deliberately:
 * durable records fetched over REST (the roster, task lists, task detail), and
 * live state derived from the event stream (statuses, in-flight tool calls,
 * bubbles, the cost meter). Events are applied through the pure reducer, so the
 * hard part stays testable without React.
 */

import { create } from "zustand";

import { api, type AgentSummary, type TaskDetail, type TaskSummary } from "../lib/api";
import type { AgentTeamEvent } from "../lib/events";
import { EventStream, type ConnectionState } from "../lib/ws";
import {
  initialRoomState,
  reduceEvent,
  type Bubble,
  type LiveEntry,
  type RoomState,
} from "./reducer";

interface TeamState extends RoomState {
  roster: AgentSummary[];
  taskList: TaskSummary[];
  taskDetail: Record<number, TaskDetail>;
  selectedAgent: string | null;
  selectedTask: number | null;
  openBubble: Bubble | null;
  connection: ConnectionState;
  reconnectAttempt: number;
  loading: boolean;
  error: string | null;

  applyEvent: (event: AgentTeamEvent) => void;
  setConnection: (state: ConnectionState, attempt?: number) => void;
  dismissBubble: (id: string) => void;
  openBubbleDetail: (bubble: Bubble | null) => void;
  selectAgent: (key: string | null) => void;
  selectTask: (id: number | null) => void;

  refreshRoster: () => Promise<void>;
  refreshTasks: () => Promise<void>;
  loadTask: (id: number) => Promise<void>;
  assignTask: (agentKey: string, description: string) => Promise<TaskSummary | null>;
  reviewTask: (id: number, decision: "approve" | "reject", feedback?: string) => Promise<void>;
  bootstrap: () => Promise<void>;
  connect: () => void;
  disconnect: () => void;
}

let stream: EventStream | null = null;

export const useTeamStore = create<TeamState>((set, get) => ({
  ...initialRoomState,
  roster: [],
  taskList: [],
  taskDetail: {},
  selectedAgent: null,
  selectedTask: null,
  openBubble: null,
  connection: "closed",
  reconnectAttempt: 0,
  loading: false,
  error: null,

  applyEvent: (event) => set((state) => reduceEvent(state, event)),

  setConnection: (connection, attempt = 0) => set({ connection, reconnectAttempt: attempt }),

  dismissBubble: (id) => set((state) => ({ bubbles: state.bubbles.filter((b) => b.id !== id) })),

  openBubbleDetail: (bubble) => set({ openBubble: bubble }),

  selectAgent: (key) => set({ selectedAgent: key }),

  selectTask: (id) => {
    set({ selectedTask: id });
    if (id !== null) void get().loadTask(id);
  },

  refreshRoster: async () => {
    try {
      const roster = await api.listAgents();
      set((state) => ({
        roster,
        error: null,
        // Seed live status from the server so a page load shows the real room
        // rather than four idle agents until the next event happens to arrive.
        agents: roster.reduce(
          (acc, agent) => ({
            ...acc,
            [agent.key]: state.agents[agent.key] ?? {
              key: agent.key,
              status: agent.status,
              previousStatus: null,
              activeTaskId: agent.active_task_id,
              statusSeq: 0,
              lastEventAt: agent.status_changed_at,
            },
          }),
          { ...state.agents },
        ),
      }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "could not load the roster" });
    }
  },

  refreshTasks: async () => {
    try {
      const taskList = await api.listTasks({ limit: 200 });
      set((state) => ({
        taskList,
        error: null,
        tasks: taskList.reduce(
          (acc, task) => ({
            ...acc,
            [task.id]: {
              id: task.id,
              agentKey: task.agent_key,
              title: task.title,
              status: task.status,
              createdBy: task.created_by,
              attempt: task.attempt,
              haltReason: task.halt_reason,
              costUsd: state.tasks[task.id]?.costUsd ?? 0,
              tokens: state.tasks[task.id]?.tokens ?? 0,
              parentTaskId: task.parent_task_id,
            },
          }),
          { ...state.tasks },
        ),
      }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "could not load tasks" });
    }
  },

  loadTask: async (id) => {
    try {
      const detail = await api.getTask(id);
      set((state) => ({ taskDetail: { ...state.taskDetail, [id]: detail } }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : `could not load task ${id}` });
    }
  },

  assignTask: async (agentKey, description) => {
    try {
      const task = await api.createTask({ agent_key: agentKey, description });
      await get().refreshTasks();
      return task;
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "could not assign the task" });
      return null;
    }
  },

  reviewTask: async (id, decision, feedback) => {
    try {
      await api.review(id, { decision, feedback });
      await Promise.all([get().refreshTasks(), get().loadTask(id)]);
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "could not submit the review" });
    }
  },

  bootstrap: async () => {
    set({ loading: true });
    await Promise.all([get().refreshRoster(), get().refreshTasks()]);
    set({ loading: false });
  },

  connect: () => {
    if (stream) return;
    stream = new EventStream({
      onEvent: (event) => get().applyEvent(event),
      onStateChange: (state, detail) => get().setConnection(state, detail?.attempt ?? 0),
    });
    stream.connect();
  },

  disconnect: () => {
    stream?.close();
    stream = null;
  },
}));

/** Selector: live entries for one task, newest last. */
export const selectLive = (taskId: number | null) => (state: TeamState): LiveEntry[] =>
  taskId === null ? [] : (state.live[taskId] ?? []);
