/**
 * Write conflicts, surfaced.
 *
 * The backend already serialises writes and notices when one agent replaces
 * another's file. That is only half the job: a conflict resolved invisibly is
 * one you discover in code review, days later, wondering why the schema someone
 * wrote is gone.
 *
 * So it appears here, and stays until dismissed — unlike a bubble, this is not
 * something to miss by looking away.
 */

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { themeFor } from "../lib/agentTheme";
import { useTeamStore } from "../store/useTeamStore";

export function ConflictBanner() {
  const conflicts = useTeamStore((s) => s.conflicts);
  const dismiss = useTeamStore((s) => s.dismissConflict);
  const selectTask = useTeamStore((s) => s.selectTask);
  const reduced = useReducedMotion() ?? false;

  if (conflicts.length === 0) return null;

  return (
    <div className="pointer-events-none fixed right-4 top-20 z-40 flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-2">
      <AnimatePresence initial={false}>
        {conflicts.slice(-3).map((conflict) => (
          <motion.div
            key={conflict.id}
            layout={!reduced}
            initial={{ opacity: 0, x: 20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            transition={{ duration: reduced ? 0.1 : 0.3, ease: [0.22, 1, 0.36, 1] }}
            className="pointer-events-auto rounded-xl border p-3 shadow-xl shadow-black/50 backdrop-blur"
            style={{
              borderColor: "color-mix(in oklab, var(--color-state-waiting) 45%, var(--color-ink-600))",
              background: "color-mix(in oklab, var(--color-ink-800) 88%, var(--color-state-waiting) 12%)",
            }}
            role="alert"
          >
            <div className="mb-1.5 flex items-center gap-2">
              <svg
                viewBox="0 0 24 24"
                className="h-3.5 w-3.5 shrink-0"
                fill="none"
                stroke="var(--color-state-waiting)"
                strokeWidth="2.2"
                strokeLinecap="round"
              >
                <path d="M12 7v6" />
                <circle cx="12" cy="17" r="0.6" fill="var(--color-state-waiting)" stroke="none" />
              </svg>
              <span className="label-micro" style={{ color: "var(--color-state-waiting)" }}>
                write conflict
              </span>
              <button
                type="button"
                onClick={() => dismiss(conflict.id)}
                aria-label="Dismiss this conflict"
                className="ml-auto rounded p-0.5 text-parchment-faint transition-colors hover:text-parchment"
              >
                <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </div>

            <p className="font-mono text-[11px] leading-relaxed text-parchment">
              <span style={{ color: themeFor(conflict.writer).hue }}>{conflict.writer}</span>
              {" replaced "}
              <span style={{ color: themeFor(conflict.replacedWriter).hue }}>
                {conflict.replacedWriter}
              </span>
              {"'s "}
              <span className="text-parchment-dim">{conflict.path}</span>
            </p>
            <p className="mt-1 text-[11px] leading-snug text-parchment-faint">
              {conflict.raced
                ? "Both wrote at the same moment."
                : "The earlier work is gone unless it was merged."}
            </p>

            <button
              type="button"
              onClick={() => selectTask(conflict.taskId)}
              className="mt-2 rounded border border-ink-600 px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-parchment-dim transition-colors hover:border-ink-500"
            >
              open task {conflict.taskId}
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
