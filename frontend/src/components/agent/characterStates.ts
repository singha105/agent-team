/**
 * The character state machine.
 *
 * Every visual property of an agent — how it sits, how its screen burns, what
 * hovers over its head — is derived here from a single AgentStatus, so the
 * character component stays declarative and the mapping can be tested without
 * rendering anything.
 *
 * Two rules shape the numbers below:
 *
 *   Nothing hard-cuts. Every state carries its own transition, and the
 *   component keys off the value rather than remounting, so Framer Motion
 *   tweens between poses.
 *
 *   Status is legible as light before it is legible as shape. A glance across
 *   the room should tell you who is working from screen brightness alone; the
 *   posture and the overhead indicator are the second and third readings.
 */

import type { AgentStatus } from "../../lib/events";

export interface CharacterPose {
  /** Degrees. Negative leans back from the desk. */
  lean: number;
  /** Pixels. Positive sinks toward the desk. */
  slump: number;
  /** Head rotation; positive turns toward the viewer. */
  headTurn: number;
  /** Screen brightness, 0–1, driving both the monitor and the light it casts. */
  glow: number;
  /** Whether the hands are on the keys. */
  typing: boolean;
  /** Chest rise/fall, in pixels. 0 disables the loop. */
  breath: number;
  /** Seconds for one breath cycle. */
  breathDuration: number;
  /** What floats above the head, if anything. */
  indicator: "none" | "thinking" | "attention" | "blocked" | "error";
  /** Token name for this state's accent, resolved against the theme. */
  accent: string;
  /** Human-readable, used for the accessible name and the tooltip. */
  label: string;
  description: string;
}

export const CHARACTER_STATES: Record<AgentStatus, CharacterPose> = {
  idle: {
    lean: 0,
    slump: 1,
    headTurn: 0,
    // Not zero: a dark monitor reads as broken rather than waiting.
    glow: 0.22,
    typing: false,
    breath: 1.6,
    breathDuration: 4.6,
    indicator: "none",
    accent: "var(--color-state-idle)",
    label: "idle",
    description: "waiting for work",
  },
  thinking: {
    // Leaning back and away from the keyboard is what reading looks like.
    lean: -7,
    slump: -2,
    headTurn: -4,
    glow: 0.55,
    typing: false,
    breath: 1.1,
    breathDuration: 3.4,
    indicator: "thinking",
    accent: "var(--color-state-thinking)",
    label: "thinking",
    description: "reasoning about the task",
  },
  working: {
    // Forward over the keys, screen at full burn.
    lean: 5,
    slump: 3,
    headTurn: -2,
    glow: 1,
    typing: true,
    breath: 0.7,
    breathDuration: 2.4,
    indicator: "none",
    accent: "var(--color-state-working)",
    label: "working",
    description: "running tools",
  },
  waiting_on_human: {
    lean: -3,
    slump: 0,
    // The one state that addresses you directly.
    headTurn: 18,
    glow: 0.45,
    typing: false,
    breath: 1.3,
    breathDuration: 4.0,
    indicator: "attention",
    accent: "var(--color-state-waiting)",
    label: "waiting on you",
    description: "work is ready for review",
  },
  blocked: {
    lean: -2,
    slump: 6,
    headTurn: 6,
    glow: 0.3,
    typing: false,
    breath: 1.2,
    breathDuration: 5.2,
    indicator: "blocked",
    accent: "var(--color-state-blocked)",
    label: "blocked",
    description: "stopped by a limit",
  },
  error: {
    lean: -1,
    slump: 8,
    headTurn: 10,
    // Dimmed rather than red-alarmed: the spec asks for visible, not alarming.
    glow: 0.26,
    typing: false,
    breath: 1.4,
    breathDuration: 5.6,
    indicator: "error",
    accent: "var(--color-state-error)",
    label: "error",
    description: "the run failed",
  },
};

export const DEFAULT_POSE = CHARACTER_STATES.idle;

export function poseFor(status: AgentStatus | string | undefined): CharacterPose {
  if (!status) return DEFAULT_POSE;
  return CHARACTER_STATES[status as AgentStatus] ?? DEFAULT_POSE;
}

/** States where the agent is doing something and the desk should feel alive. */
export function isActive(status: AgentStatus | string | undefined): boolean {
  return status === "thinking" || status === "working";
}

/** States that want the manager's attention. */
export function needsAttention(status: AgentStatus | string | undefined): boolean {
  return status === "waiting_on_human" || status === "blocked" || status === "error";
}

/**
 * The transition used to move between two poses.
 *
 * Leaving a state matters as much as entering one: dropping out of `working`
 * should settle rather than snap, while turning to face the viewer should feel
 * deliberate and slightly slower than the rest.
 */
export function transitionFor(next: AgentStatus | string | undefined, reducedMotion: boolean) {
  if (reducedMotion) return { duration: 0.12, ease: "linear" as const };
  if (next === "waiting_on_human") return { type: "spring" as const, stiffness: 90, damping: 16 };
  if (next === "working") return { type: "spring" as const, stiffness: 140, damping: 18 };
  return { duration: 0.7, ease: [0.22, 1, 0.36, 1] as const };
}

/** Accessible name for the character button. */
export function accessibleName(displayName: string, role: string, status: AgentStatus | string): string {
  const pose = poseFor(status);
  return `${displayName}, ${role}. Currently ${pose.label} — ${pose.description}. Open details.`;
}
