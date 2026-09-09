/**
 * The event stream client.
 *
 * The backend restarts constantly in development, so reconnect behaviour is
 * exercised far more often than the happy path, and a dashboard that silently
 * goes stale is worse than one that admits it is offline.
 */

import { describe, expect, it, vi } from "vitest";

import type { AgentTeamEvent } from "./events";
import { EventStream, backoffDelay, type ConnectionState } from "./ws";

/** A socket we can drive by hand. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  close() {
    this.closed = true;
    this.onclose?.();
  }

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }

  emitRaw(data: string) {
    this.onmessage?.({ data } as MessageEvent);
  }
}

function makeStream(onEvent = vi.fn()) {
  FakeSocket.instances = [];
  const states: ConnectionState[] = [];
  const stream = new EventStream(
    { onEvent, onStateChange: (state) => states.push(state) },
    { url: "ws://test/ws", socketFactory: (url) => new FakeSocket(url) as unknown as WebSocket },
  );
  return { stream, states, onEvent };
}

describe("backoffDelay", () => {
  it("grows with the attempt number", () => {
    // Jitter is random, so compare the ceilings over many samples.
    const ceiling = (attempt: number) =>
      Math.max(...Array.from({ length: 200 }, () => backoffDelay(attempt, 100, 60_000)));
    expect(ceiling(1)).toBeLessThan(ceiling(4));
  });

  it("never exceeds the cap", () => {
    for (let attempt = 1; attempt < 40; attempt += 1) {
      expect(backoffDelay(attempt, 500, 15_000)).toBeLessThanOrEqual(15_000);
    }
  });

  it("is never negative", () => {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      expect(backoffDelay(attempt, 500, 15_000)).toBeGreaterThanOrEqual(0);
    }
  });

  it("jitters, so tabs do not all reconnect in the same millisecond", () => {
    const samples = new Set(Array.from({ length: 60 }, () => backoffDelay(6, 500, 15_000)));
    expect(samples.size).toBeGreaterThan(5);
  });
});

describe("EventStream", () => {
  it("reports connecting then open", () => {
    const { stream, states } = makeStream();
    stream.connect();
    FakeSocket.instances[0]!.onopen?.();
    expect(states).toEqual(["connecting", "open"]);
  });

  it("delivers events and tracks the sequence number", () => {
    const onEvent = vi.fn();
    const { stream } = makeStream(onEvent);
    stream.connect();
    const socket = FakeSocket.instances[0]!;
    socket.onopen?.();
    socket.emit({ type: "task.created", seq: 4, task_id: 1 } as unknown as AgentTeamEvent);
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(stream.seq).toBe(4);
  });

  it("swallows the handshake frame rather than passing it on as an event", () => {
    const onEvent = vi.fn();
    const { stream } = makeStream(onEvent);
    stream.connect();
    FakeSocket.instances[0]!.emit({ type: "stream.ready", event_types: ["task.created"] });
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("survives a malformed frame", () => {
    const onEvent = vi.fn();
    const { stream } = makeStream(onEvent);
    stream.connect();
    const socket = FakeSocket.instances[0]!;
    expect(() => socket.emitRaw("not json{")).not.toThrow();
    socket.emit({ type: "task.created", seq: 1 } as unknown as AgentTeamEvent);
    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("reconnects after an unexpected close", async () => {
    vi.useFakeTimers();
    const { stream, states } = makeStream();
    stream.connect();
    FakeSocket.instances[0]!.onopen?.();
    FakeSocket.instances[0]!.onclose?.();
    expect(states).toContain("reconnecting");
    await vi.advanceTimersByTimeAsync(20_000);
    expect(FakeSocket.instances.length).toBeGreaterThan(1);
    vi.useRealTimers();
  });

  it("resumes from the last sequence number so a reconnect loses nothing", async () => {
    vi.useFakeTimers();
    const { stream } = makeStream();
    stream.connect();
    const first = FakeSocket.instances[0]!;
    first.onopen?.();
    first.emit({ type: "task.created", seq: 11 } as unknown as AgentTeamEvent);
    first.onclose?.();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(FakeSocket.instances[1]!.url).toContain("since_seq=11");
    vi.useRealTimers();
  });

  it("does not reconnect after a deliberate close", async () => {
    vi.useFakeTimers();
    const { stream, states } = makeStream();
    stream.connect();
    FakeSocket.instances[0]!.onopen?.();
    stream.close();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(states.at(-1)).toBe("closed");
    vi.useRealTimers();
  });

  it("only schedules one reconnect when error and close both fire", async () => {
    vi.useFakeTimers();
    const { stream } = makeStream();
    stream.connect();
    const socket = FakeSocket.instances[0]!;
    socket.onopen?.();
    socket.onerror?.();
    socket.onclose?.();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.useRealTimers();
  });
});
