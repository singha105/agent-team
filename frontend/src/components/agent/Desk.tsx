/**
 * A desk: one character, its nameplate, and the button that opens it.
 *
 * The whole desk is a single button rather than a clickable div with a nested
 * control. Keyboard users get one stop per agent with a name that says who they
 * are and what they are doing, which is the same information a sighted user
 * gets from the pose and the glow.
 */

import { motion, useReducedMotion } from "framer-motion";

import type { AgentSummary } from "../../lib/api";
import type { AgentStatus } from "../../lib/events";
import { AgentCharacter } from "./AgentCharacter";
import { StatusIndicator } from "./StatusIndicator";
import { accessibleName, needsAttention, poseFor } from "./characterStates";

export interface DeskProps {
  agent: AgentSummary;
  status: AgentStatus | string;
  /** Set when the agent is backing off before a retry. */
  waiting?: { reason: string; attempt: number; maxAttempts: number } | null;
  hue: string;
  variant: 0 | 1 | 2 | 3;
  selected: boolean;
  onSelect: (key: string) => void;
  /** Set by the room so bubbles know where to fly from and to. */
  deskRef?: (key: string, el: HTMLElement | null) => void;
}

export function Desk({
  agent,
  status,
  waiting,
  hue,
  variant,
  selected,
  onSelect,
  deskRef,
}: DeskProps) {
  const reduced = useReducedMotion() ?? false;
  const pose = poseFor(status);
  const attention = needsAttention(status);

  return (
    <motion.button
      type="button"
      ref={(el) => deskRef?.(agent.key, el)}
      onClick={() => onSelect(agent.key)}
      aria-label={
        waiting
          ? `${agent.display_name}, ${agent.role}. Waiting — ${waiting.reason}, ` +
            `retry ${waiting.attempt} of ${waiting.maxAttempts}. Open details.`
          : accessibleName(agent.display_name, agent.role, status)
      }
      aria-pressed={selected}
      data-agent={agent.key}
      data-status={status}
      className="group relative flex w-full flex-col items-center rounded-2xl px-2 pb-2 pt-12 text-left transition-colors hover:bg-ink-850/60 focus-visible:bg-ink-850/60"
      whileHover={reduced ? undefined : { y: -4 }}
      whileTap={reduced ? undefined : { y: -1 }}
      transition={{ type: "spring", stiffness: 320, damping: 24 }}
    >
      {/* selection is a light change, consistent with the rest of the room */}
      {selected && (
        <motion.span
          layoutId="desk-selection"
          className="pointer-events-none absolute inset-0 rounded-2xl border"
          style={{ borderColor: hue, boxShadow: `inset 0 0 40px color-mix(in oklab, ${hue} 18%, transparent)` }}
          transition={{ type: "spring", stiffness: 300, damping: 30 }}
        />
      )}

      <div className="relative w-full max-w-[300px]">
        <StatusIndicator pose={pose} />
        <AgentCharacter
          agentKey={agent.key}
          status={status}
          hue={hue}
          variant={variant}
          className="h-auto w-full"
        />
      </div>

      {/* nameplate */}
      <div className="mt-1 flex flex-col items-center gap-0.5">
        <span className="font-display text-xl leading-none tracking-tight text-parchment">
          {agent.display_name}
        </span>
        <span className="label-micro">{agent.key}</span>
        {/* A backing-off agent must not read as a frozen one. The status line
            says what it is waiting for and how far through the retries it is. */}
        <span
          className="mt-1 flex items-center gap-1.5 font-mono text-[10px] tracking-wide"
          style={{
            color: waiting
              ? "var(--color-state-waiting)"
              : attention
                ? pose.accent
                : "var(--color-parchment-faint)",
          }}
        >
          <motion.span
            className="block h-1.5 w-1.5 rounded-full"
            style={{ background: waiting ? "var(--color-state-waiting)" : pose.accent }}
            animate={
              reduced || status === "idle"
                ? { opacity: 1 }
                : { opacity: [0.45, 1, 0.45] }
            }
            transition={reduced ? { duration: 0 } : { duration: 2.4, repeat: Infinity, ease: "easeInOut" }}
          />
          {waiting ? `${waiting.reason} · retry ${waiting.attempt}/${waiting.maxAttempts}` : pose.label}
        </span>
      </div>
    </motion.button>
  );
}
