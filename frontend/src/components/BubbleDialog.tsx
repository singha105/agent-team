/**
 * The full text behind a speech bubble.
 *
 * A bubble in flight can only carry a preview, and the interesting part of an
 * inter-agent exchange is usually the part that does not fit — a schema, a
 * contract. Clicking opens the whole thing.
 */

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect } from "react";

import { themeFor } from "../lib/agentTheme";
import { useTeamStore } from "../store/useTeamStore";

export function BubbleDialog() {
  const bubble = useTeamStore((s) => s.openBubble);
  const close = useTeamStore((s) => s.openBubbleDetail);
  const selectTask = useTeamStore((s) => s.selectTask);
  const reduced = useReducedMotion() ?? false;

  useEffect(() => {
    if (!bubble) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && close(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [bubble, close]);

  return (
    <AnimatePresence>
      {bubble && (
        <motion.div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-ink-900/70 p-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduced ? 0.1 : 0.2 }}
          onClick={() => close(null)}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={`Message from ${bubble.from} to ${bubble.to}`}
            className="w-full max-w-lg rounded-2xl border border-ink-600 bg-ink-850 p-5 shadow-2xl shadow-black/60"
            initial={{ scale: 0.94, y: 12 }}
            animate={{ scale: 1, y: 0 }}
            exit={{ scale: 0.96, y: 8 }}
            transition={reduced ? { duration: 0.12 } : { type: "spring", stiffness: 300, damping: 28 }}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-3 flex items-center gap-2 font-mono text-[11px]">
              <span style={{ color: themeFor(bubble.from).hue }}>{bubble.from}</span>
              <span className="text-parchment-faint">→</span>
              <span style={{ color: themeFor(bubble.to).hue }}>{bubble.to}</span>
              <span className="ml-auto text-parchment-faint">task {bubble.taskId}</span>
            </div>
            <p className="whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-parchment-dim">
              {bubble.preview}
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  selectTask(bubble.taskId);
                  close(null);
                }}
                className="rounded border border-ink-600 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-parchment-dim"
              >
                open the trace
              </button>
              <button
                type="button"
                onClick={() => close(null)}
                className="rounded border border-ink-600 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-parchment-faint"
                autoFocus
              >
                close
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
