/**
 * WebSocket client for the event stream.
 *
 * Two things it has to get right beyond opening a socket:
 *
 *   Reconnect — the backend restarts constantly in development, and a dashboard
 *   that silently goes dead is worse than one that says it is offline. Backoff
 *   is exponential with jitter so a server coming back up is not hit by every
 *   open tab at the same instant.
 *
 *   Gaps — every event carries a monotonic `seq`. Reconnecting with
 *   `?since_seq=N` replays what was missed, so a dropped connection does not
 *   silently lose an agent's whole run.
 */

import type { AgentTeamEvent } from "./events";

export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed";

export interface StreamHandlers {
  onEvent: (event: AgentTeamEvent) => void;
  onStateChange?: (state: ConnectionState, detail?: { attempt: number; nextRetryMs?: number }) => void;
  onReady?: (info: { eventTypes: string[] }) => void;
}

export interface StreamOptions {
  url?: string;
  baseDelayMs?: number;
  maxDelayMs?: number;
  /** Injected in tests; defaults to the platform WebSocket. */
  socketFactory?: (url: string) => WebSocket;
}

const DEFAULT_BASE_DELAY = 500;
const DEFAULT_MAX_DELAY = 15_000;

function defaultUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws`;
}

/** Exponential backoff with full jitter, capped. */
export function backoffDelay(attempt: number, base = DEFAULT_BASE_DELAY, max = DEFAULT_MAX_DELAY): number {
  const ceiling = Math.min(max, base * 2 ** Math.max(0, attempt - 1));
  // Full jitter rather than a fixed delay: without it, every tab that dropped
  // when the server went down reconnects in the same millisecond when it
  // returns, and knocks it over again.
  return Math.round(Math.random() * ceiling);
}

export class EventStream {
  private socket: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private lastSeq = 0;

  constructor(
    private readonly handlers: StreamHandlers,
    private readonly options: StreamOptions = {},
  ) {}

  get seq(): number {
    return this.lastSeq;
  }

  connect(): void {
    this.stopped = false;
    this.open();
  }

  close(): void {
    this.stopped = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.socket?.close();
    this.socket = null;
    this.handlers.onStateChange?.("closed", { attempt: this.attempt });
  }

  private open(): void {
    const base = this.options.url ?? defaultUrl();
    // Resuming from the last seen sequence number is what makes a reconnect
    // lossless rather than merely quiet.
    const url = `${base}?since_seq=${this.lastSeq}`;

    this.handlers.onStateChange?.(this.attempt === 0 ? "connecting" : "reconnecting", {
      attempt: this.attempt,
    });

    const factory = this.options.socketFactory ?? ((u: string) => new WebSocket(u));
    const socket = factory(url);
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.handlers.onStateChange?.("open", { attempt: 0 });
    };

    socket.onmessage = (event: MessageEvent) => {
      let payload: unknown;
      try {
        payload = JSON.parse(String(event.data));
      } catch {
        return; // a malformed frame must not kill the stream
      }
      if (!payload || typeof payload !== "object") return;

      const record = payload as Record<string, unknown>;
      if (record.type === "stream.ready") {
        this.handlers.onReady?.({ eventTypes: (record.event_types as string[]) ?? [] });
        return;
      }
      if (typeof record.seq === "number") {
        this.lastSeq = Math.max(this.lastSeq, record.seq);
      }
      this.handlers.onEvent(payload as AgentTeamEvent);
    };

    socket.onerror = () => {
      // `onclose` always follows, and that is where the retry lives — handling
      // both would schedule two reconnects for one failure.
    };

    socket.onclose = () => {
      this.socket = null;
      if (this.stopped) return;
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    this.attempt += 1;
    const delay = backoffDelay(this.attempt, this.options.baseDelayMs, this.options.maxDelayMs);
    this.handlers.onStateChange?.("reconnecting", { attempt: this.attempt, nextRetryMs: delay });
    this.timer = setTimeout(() => this.open(), delay);
  }
}
