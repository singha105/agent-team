/**
 * Connection state.
 *
 * A dashboard that silently goes stale is worse than one that admits it, so a
 * dropped socket is always visible — but quietly, since reconnects are routine
 * in development and a red banner every time the backend reloads trains you to
 * ignore it.
 */

import { motion } from "framer-motion";

import type { ConnectionState } from "../lib/ws";

const COPY: Record<ConnectionState, { text: string; tone: string }> = {
  connecting: { text: "connecting", tone: "var(--color-state-thinking)" },
  open: { text: "live", tone: "var(--color-task-done)" },
  reconnecting: { text: "reconnecting", tone: "var(--color-state-waiting)" },
  closed: { text: "offline", tone: "var(--color-state-error)" },
};

export function ConnectionPill({ state, attempt }: { state: ConnectionState; attempt: number }) {
  const copy = COPY[state];
  return (
    <div
      className="flex items-center gap-2 rounded-full border border-ink-700 bg-ink-850/70 px-3 py-1"
      role="status"
      aria-live="polite"
    >
      <motion.span
        className="block h-1.5 w-1.5 rounded-full"
        style={{ background: copy.tone }}
        animate={state === "open" ? { opacity: 1 } : { opacity: [0.3, 1, 0.3] }}
        transition={state === "open" ? { duration: 0 } : { duration: 1.4, repeat: Infinity }}
      />
      <span className="font-mono text-[10px] uppercase tracking-[0.16em]" style={{ color: copy.tone }}>
        {copy.text}
        {state === "reconnecting" && attempt > 1 ? ` ·${attempt}` : ""}
      </span>
    </div>
  );
}
