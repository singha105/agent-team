/**
 * The character state machine.
 *
 * Every visual property an agent has comes from this mapping, so these tests
 * are the ones that stop a status silently rendering as something it is not.
 */

import { describe, expect, it } from "vitest";

import type { AgentStatus } from "../../lib/events";
import {
  CHARACTER_STATES,
  accessibleName,
  isActive,
  needsAttention,
  poseFor,
  transitionFor,
} from "./characterStates";

const ALL_STATUSES: AgentStatus[] = [
  "idle",
  "thinking",
  "working",
  "waiting_on_human",
  "blocked",
  "error",
];

describe("the state table", () => {
  it("covers every status the backend can emit", () => {
    expect(Object.keys(CHARACTER_STATES).sort()).toEqual([...ALL_STATUSES].sort());
  });

  it.each(ALL_STATUSES)("gives %s a complete pose", (status) => {
    const pose = CHARACTER_STATES[status];
    expect(pose.label).toBeTruthy();
    expect(pose.description).toBeTruthy();
    expect(pose.accent).toMatch(/^var\(--/);
    expect(pose.glow).toBeGreaterThan(0);
    expect(pose.glow).toBeLessThanOrEqual(1);
  });

  it("never lets a monitor go fully dark", () => {
    // A black screen reads as broken hardware rather than an agent waiting.
    for (const status of ALL_STATUSES) {
      expect(CHARACTER_STATES[status].glow).toBeGreaterThan(0.1);
    }
  });

  it("burns brightest while working", () => {
    const working = CHARACTER_STATES.working.glow;
    for (const status of ALL_STATUSES.filter((s) => s !== "working")) {
      expect(CHARACTER_STATES[status].glow).toBeLessThan(working);
    }
  });

  it("only types while working", () => {
    for (const status of ALL_STATUSES) {
      expect(CHARACTER_STATES[status].typing).toBe(status === "working");
    }
  });

  it("leans forward to work and back to think", () => {
    expect(CHARACTER_STATES.working.lean).toBeGreaterThan(0);
    expect(CHARACTER_STATES.thinking.lean).toBeLessThan(0);
  });

  it("turns toward the viewer only when it needs one", () => {
    const turn = CHARACTER_STATES.waiting_on_human.headTurn;
    expect(turn).toBeGreaterThan(10);
    for (const status of ["idle", "thinking", "working"] as AgentStatus[]) {
      expect(CHARACTER_STATES[status].headTurn).toBeLessThan(turn);
    }
  });

  it("breathes faster when busy than when idle", () => {
    expect(CHARACTER_STATES.working.breathDuration).toBeLessThan(
      CHARACTER_STATES.idle.breathDuration,
    );
  });

  it("keeps blocked and error visible but not alarming", () => {
    // The spec asks for a visible, non-alarming change: dimmed and slumped,
    // not a bright red flash.
    for (const status of ["blocked", "error"] as AgentStatus[]) {
      const pose = CHARACTER_STATES[status];
      expect(pose.glow).toBeLessThan(CHARACTER_STATES.thinking.glow);
      expect(pose.slump).toBeGreaterThan(0);
      expect(pose.indicator).toBe(status);
    }
  });

  it("shows a thought bubble only while thinking", () => {
    for (const status of ALL_STATUSES) {
      const hasThought = CHARACTER_STATES[status].indicator === "thinking";
      expect(hasThought).toBe(status === "thinking");
    }
  });
});

describe("poseFor", () => {
  it("returns the matching pose", () => {
    expect(poseFor("working").label).toBe("working");
  });

  it("falls back to idle for anything unrecognised", () => {
    // A status from a newer backend must not blank the character out.
    expect(poseFor("teleporting").label).toBe("idle");
    expect(poseFor(undefined).label).toBe("idle");
    expect(poseFor("").label).toBe("idle");
  });
});

describe("classifiers", () => {
  it("treats thinking and working as active", () => {
    expect(isActive("thinking")).toBe(true);
    expect(isActive("working")).toBe(true);
    expect(isActive("idle")).toBe(false);
    expect(isActive("waiting_on_human")).toBe(false);
  });

  it("treats the three stopped states as wanting attention", () => {
    expect(needsAttention("waiting_on_human")).toBe(true);
    expect(needsAttention("blocked")).toBe(true);
    expect(needsAttention("error")).toBe(true);
    expect(needsAttention("working")).toBe(false);
  });
});

describe("transitions", () => {
  it("never hard-cuts", () => {
    for (const status of ALL_STATUSES) {
      const transition = transitionFor(status, false) as Record<string, unknown>;
      const isSpring = transition.type === "spring";
      const hasDuration = typeof transition.duration === "number" && transition.duration > 0;
      expect(isSpring || hasDuration).toBe(true);
    }
  });

  it("collapses to a short tween under reduced motion", () => {
    for (const status of ALL_STATUSES) {
      const transition = transitionFor(status, true) as { duration: number };
      expect(transition.duration).toBeLessThanOrEqual(0.15);
    }
  });

  it("uses a distinct, softer spring for turning to face the viewer", () => {
    const attention = transitionFor("waiting_on_human", false) as { stiffness: number };
    const working = transitionFor("working", false) as { stiffness: number };
    expect(attention.stiffness).toBeLessThan(working.stiffness);
  });
});

describe("accessibleName", () => {
  it("carries the same information the pose and glow carry visually", () => {
    const name = accessibleName("Ada", "API and business logic", "working");
    expect(name).toContain("Ada");
    expect(name).toContain("API and business logic");
    expect(name).toContain("working");
    expect(name).toContain("running tools");
  });

  it("stays sensible for an unknown status", () => {
    expect(accessibleName("Ada", "backend", "sleeping")).toContain("idle");
  });
});
