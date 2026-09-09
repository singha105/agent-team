/**
 * Demo mode: replay a captured run with no backend.
 *
 * Most people who look at this repo will not have an API key, and asking them
 * to install Python and start two servers to see whether the idea works is a
 * good way to ensure they never find out. `npm run demo` needs Node and nothing
 * else.
 *
 * The fixture is a recording of a real run — the actual event stream and the
 * actual REST responses — captured by scripts/capture_demo.py. Nothing here is
 * hand-written, so what plays back is what happened, including anything that
 * went wrong.
 */

import type { AgentSummary, TaskDetail, TaskSummary, TaskTrace } from "../lib/api";
import type { AgentTeamEvent } from "../lib/events";
import fixture from "./fixture.json";

interface Fixture {
  version: number;
  description: string;
  root_task_id: number;
  agents: AgentSummary[];
  tasks: TaskSummary[];
  task_detail: Record<string, TaskDetail>;
  traces: Record<string, TaskTrace>;
  events: AgentTeamEvent[];
}

const data = fixture as unknown as Fixture;

export const isDemo = import.meta.env.VITE_DEMO === "1";

export const demoDescription = data.description;

/**
 * Pacing.
 *
 * The capture ran with a scripted model at a deliberately fast pace, so its real
 * timestamps would replay as a blur. Events are spaced by kind instead: the ones
 * that carry meaning get room to be read, and bookkeeping goes by quickly.
 */
const DELAYS: Record<string, number> = {
  "task.created": 700,
  "task.status_changed": 500,
  "agent.status_changed": 320,
  "message.created": 620,
  "tool.started": 420,
  "tool.finished": 380,
  "usage.updated": 260,
};

const DEFAULT_DELAY = 400;
const START_DELAY = 900;
/** How long to rest at the end before looping, so the finish is legible. */
const LOOP_PAUSE = 6000;

/** A REST client that answers from the fixture. */
export const demoApi = {
  listAgents: async (): Promise<AgentSummary[]> =>
    // Start everyone idle: the recorded statuses are from the *end* of the run,
    // and seeding those would show the finished room before the story starts.
    data.agents.map((agent) => ({ ...agent, status: "idle", active_task_id: null })),

  listTasks: async (): Promise<TaskSummary[]> => [],

  getTask: async (id: number): Promise<TaskDetail> => {
    const detail = data.task_detail[String(id)];
    if (!detail) throw new Error(`no task ${id} in the demo fixture`);
    return detail;
  },

  getTrace: async (id: number): Promise<TaskTrace> => {
    const trace = data.traces[String(id)];
    if (!trace) throw new Error(`no trace for task ${id} in the demo fixture`);
    return trace;
  },

  createTask: async (): Promise<TaskSummary> => {
    throw new Error(
      "This is a recorded demo, so tasks cannot be assigned. Run the real app to do that — " +
        "see the quickstart in the README.",
    );
  },

  review: async (): Promise<TaskSummary> => {
    throw new Error("This is a recorded demo; reviewing is disabled.");
  },
};

/** Replays the recorded events, looping. */
export class DemoStream {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private index = 0;
  private stopped = false;

  constructor(
    private readonly onEvent: (event: AgentTeamEvent) => void,
    private readonly onReady?: () => void,
  ) {}

  start(): void {
    this.stopped = false;
    this.onReady?.();
    this.timer = setTimeout(() => this.step(), START_DELAY);
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private pass = 0;

  private step(): void {
    if (this.stopped) return;

    if (this.index >= data.events.length) {
      // Loop, so a page left open keeps showing the idea rather than a frozen
      // room. Sequence numbers are rewritten on each pass because the store
      // discards anything at or below what it has already applied — without
      // that, the second loop would be silently ignored as a replay.
      this.index = 0;
      this.pass += 1;
      this.timer = setTimeout(() => this.step(), LOOP_PAUSE);
      return;
    }

    const source = data.events[this.index] as AgentTeamEvent;
    const event = {
      ...source,
      seq: source.seq + this.pass * data.events.length,
    } as AgentTeamEvent;

    this.onEvent(event);
    this.index += 1;

    const delay = DELAYS[event.type] ?? DEFAULT_DELAY;
    this.timer = setTimeout(() => this.step(), delay);
  }

}
