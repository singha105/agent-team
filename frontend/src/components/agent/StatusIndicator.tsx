/**
 * What floats above an agent's head.
 *
 * Deliberately restrained: three of the five states say something is wrong or
 * pending, and a dashboard that shouts about all of them is a dashboard nobody
 * reads. The thinking bubble animates because thinking is ongoing; the others
 * are quiet marks that hold still until you look at them.
 */

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import type { CharacterPose } from "./characterStates";

export function StatusIndicator({ pose }: { pose: CharacterPose }) {
  const reduced = useReducedMotion() ?? false;

  return (
    <AnimatePresence mode="wait">
      {pose.indicator !== "none" && (
        <motion.div
          key={pose.indicator}
          initial={{ opacity: 0, y: 8, scale: 0.9 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -6, scale: 0.94 }}
          transition={reduced ? { duration: 0.12 } : { type: "spring", stiffness: 260, damping: 22 }}
          className="pointer-events-none absolute left-1/2 top-0 -translate-x-1/2"
        >
          {pose.indicator === "thinking" && <ThoughtBubble reduced={reduced} />}
          {pose.indicator === "attention" && <AttentionMark accent={pose.accent} reduced={reduced} />}
          {pose.indicator === "blocked" && <QuietMark accent={pose.accent} glyph="pause" />}
          {pose.indicator === "error" && <QuietMark accent={pose.accent} glyph="alert" />}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function ThoughtBubble({ reduced }: { reduced: boolean }) {
  return (
    <div className="relative flex items-center gap-1 rounded-full border border-ink-600 bg-ink-800/90 px-3 py-2 shadow-lg shadow-black/40 backdrop-blur-sm">
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="block h-1.5 w-1.5 rounded-full"
          style={{ background: "var(--color-state-thinking)" }}
          animate={reduced ? { opacity: 0.8 } : { opacity: [0.25, 1, 0.25], y: [0, -2, 0] }}
          transition={
            reduced ? { duration: 0 } : { duration: 1.1, repeat: Infinity, delay: i * 0.16, ease: "easeInOut" }
          }
        />
      ))}
      {/* the two trailing dots that make it a thought rather than a speech bubble */}
      <span className="absolute -bottom-1.5 left-2 h-1.5 w-1.5 rounded-full border border-ink-600 bg-ink-800" />
      <span className="absolute -bottom-3 left-0.5 h-1 w-1 rounded-full border border-ink-600 bg-ink-800" />
    </div>
  );
}

function AttentionMark({ accent, reduced }: { accent: string; reduced: boolean }) {
  return (
    <div className="relative">
      {/* A slow ring rather than a flash: it should catch the eye on the second
          pass around the room, not demand the first. */}
      {!reduced && (
        <motion.span
          className="absolute inset-0 rounded-full"
          style={{ border: `1px solid ${accent}` }}
          animate={{ scale: [1, 1.55], opacity: [0.5, 0] }}
          transition={{ duration: 2.2, repeat: Infinity, ease: "easeOut" }}
        />
      )}
      <span
        className="relative flex h-7 w-7 items-center justify-center rounded-full border text-[11px] font-mono"
        style={{ borderColor: accent, color: accent, background: "color-mix(in oklab, var(--color-ink-800) 85%, transparent)" }}
      >
        ?
      </span>
    </div>
  );
}

function QuietMark({ accent, glyph }: { accent: string; glyph: "pause" | "alert" }) {
  return (
    <span
      className="flex h-7 w-7 items-center justify-center rounded-full border"
      style={{ borderColor: accent, background: "color-mix(in oklab, var(--color-ink-800) 85%, transparent)" }}
    >
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke={accent} strokeWidth="2.4" strokeLinecap="round">
        {glyph === "pause" ? (
          <>
            <line x1="9" y1="6" x2="9" y2="18" />
            <line x1="15" y1="6" x2="15" y2="18" />
          </>
        ) : (
          <>
            <line x1="12" y1="6" x2="12" y2="13" />
            <circle cx="12" cy="17.5" r="0.6" fill={accent} stroke="none" />
          </>
        )}
      </svg>
    </span>
  );
}
