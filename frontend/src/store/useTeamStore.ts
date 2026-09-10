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
import { DemoStream, demoApi, isDemo } from "../demo/replay";
import {
  initialRoomState,
  reduceEvent,
  type Bubble,
  type LiveEntry,
  type RoomState,
  type TaskView,
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
  dismissConflict: (id: string) => void;
  openBubbleDetail: (bubble: Bubble | null) => void;
  selectAgent: (key: string | null) => void;
  selectTask: (id: number | null) => void;

  refreshRoster: () => Promise<void>;
  refreshTasks: () => Promise<void>;
  loadTask: (id: number) => Promise<void>;
  assignTask: (agentKey: string, description: string) => Promise<TaskSummary | null>;
  reviewTask: (id: number, decision: "approve" | "reject", feedback?: string) => Promise<void>;
  bootstrap: () => Promise<void>;
  reconcile: () => Promise<void>;
  connect: () => void;
  disconnect: () => void;
}

let stream: EventStream | null = null;
let demoStream: DemoStream | null = null;

// In demo mode every network call is answered from the recording, so the
// store below needs no branching beyond this one swap.
const client = isDemo ? { ...api, ...demoApi } : api;

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

  dismissConflict: (id) =>
    set((state) => ({ conflicts: state.conflicts.filter((c) => c.id !== id) })),

  openBubbleDetail: (bubble) => set({ openBubble: bubble }),

  selectAgent: (key) => set({ selectedAgent: key }),

  selectTask: (id) => {
    set({ selectedTask: id });
    if (id !== null) void get().loadTask(id);
  },

  refreshRoster: async () => {
    try {
      const roster = await client.listAgents();
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
      const taskList = await client.listTasks({ limit: 200 });
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
      const detail = await client.getTask(id);
      set((state) => ({ taskDetail: { ...state.taskDetail, [id]: detail } }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : `could not load task ${id}` });
    }
  },

  assignTask: async (agentKey, description) => {
    try {
      const task = await client.createTask({ agent_key: agentKey, description });
      await get().refreshTasks();
      return task;
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "could not assign the task" });
      return null;
    }
  },

  reviewTask: async (id, decision, feedback) => {
    try {
      await client.review(id, { decision, feedback });
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

  /**
   * Re-read authoritative state after a gap in the stream.
   *
   * Unlike bootstrap this does not set `loading`: the room is already on screen
   * and blanking it to a spinner because a socket blinked would be worse than
   * the brief inconsistency it is fixing. Agent statuses are overwritten from
   * the server rather than merged, because a status we inferred from events we
   * may not have all of is exactly what cannot be trusted here.
   */
  reconcile: async () => {
    try {
      const [roster, taskList] = await Promise.all([api.listAgents(), api.listTasks({ limit: 200 })]);
      set((state) => ({
        roster,
        taskList,
        error: null,
        agents: roster.reduce(
          (acc, agent) => ({
            ...acc,
            [agent.key]: {
              key: agent.key,
              status: agent.status,
              previousStatus: state.agents[agent.key]?.status ?? null,
              activeTaskId: agent.active_task_id,
              statusSeq: (state.agents[agent.key]?.statusSeq ?? 0) + 1,
              lastEventAt: agent.status_changed_at,
            },
          }),
          {} as typeof state.agents,
        ),
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
              // Cost accumulates from events; keep what we have rather than
              // resetting a meter that was counting correctly.
              costUsd: state.tasks[task.id]?.costUsd ?? 0,
              tokens: state.tasks[task.id]?.tokens ?? 0,
              parentTaskId: task.parent_task_id,
            },
          }),
          {} as typeof state.tasks,
        ),
      }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "could not reconcile after reconnecting" });
    }
  },

  connect: () => {
    if (isDemo) {
      if (demoStream) return;
      demoStream = new DemoStream(
        (event) => get().applyEvent(event),
        () => get().setConnection("open"),
      );
      demoStream.start();
      return;
    }
    if (stream) return;
    stream = new EventStream({
      onEvent: (event) => get().applyEvent(event),
      onStateChange: (state, detail) => {
        const previous = get().connection;
        get().setConnection(state, detail?.attempt ?? 0);

        // Reconcile on every reconnect, not just replay.
        //
        // `?since_seq=N` replays what the server still has buffered, which is
        // bounded — a long disconnection silently falls off the end of that
        // buffer and the room would then show a state that stopped being true
        // minutes ago. Refetching is the only way to know the current truth,
        // and it is cheap next to being confidently wrong.
        if (state === "open" && previous !== "connecting") {
          void get().reconcile();
        }
      },
    });
    stream.connect();
  },

  disconnect: () => {
    demoStream?.stop();
    demoStream = null;
    stream?.close();
    stream = null;
  },
}));

/**
 * Tasks for one agent, newest first.
 *
 * A plain function over an already-selected record, not a Zustand selector.
 * Selecting a freshly built array from the store would give React a new
 * snapshot on every render and loop forever; callers select the stable `tasks`
 * record and derive with this inside a useMemo.
 *
 * Reads the event-mirrored record rather than the REST list because the list is
 * only refetched on demand — a task an agent creates by delegating would
 * otherwise never appear, and the panel would claim the agent had done nothing.
 */
export function agentTasksFrom(
  tasks: Record<number, TaskView>,
  agentKey: string | null,
): TaskView[] {
  if (agentKey === null) return [];
  return Object.values(tasks)
    .filter((task) => task.agentKey === agentKey)
    .sort((a, b) => b.id - a.id);
}

/** Selector: live entries for one task, newest last. */
export const selectLive = (taskId: number | null) => (state: TeamState): LiveEntry[] =>
  taskId === null ? [] : (state.live[taskId] ?? []);
