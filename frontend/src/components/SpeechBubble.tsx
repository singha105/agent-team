/**
 * A message crossing the room.
 *
 * This is the visual payoff of inter-agent delegation: when one agent asks
 * another for something you watch it happen, rather than reading about it in a
 * log afterwards. The bubble travels the real measured distance between the two
 * desks and arcs on the way, because a straight line across four desks reads as
 * a progress bar rather than something being carried.
 */

import { motion, useReducedMotion } from "framer-motion";

import type { Bubble } from "../store/reducer";

export interface FlyingBubbleProps {
  bubble: Bubble;
  from: { x: number; y: number } | null;
  to: { x: number; y: number } | null;
  hue: string;
  onOpen: (bubble: Bubble) => void;
  onDone: (id: string) => void;
}

/** How long a bubble takes to cross, then how long it rests before fading. */
const TRAVEL_SECONDS = 2.4;
const LINGER_SECONDS = 1.1;

export function FlyingBubble({ bubble, from, to, hue, onOpen, onDone }: FlyingBubbleProps) {
  const reduced = useReducedMotion() ?? false;

  if (!from || !to) return null;

  // Arc height scales with distance: neighbouring desks get a gentle hop, the
  // full width of the room gets a real arc.
  const distance = Math.abs(to.x - from.x);
  const lift = Math.min(96, 30 + distance * 0.16);

  return (
    // Two elements on purpose. The outer one is animated to an anchor *point*;
    // the inner one centres itself on that point. Animating a single element
    // meant guessing the bubble's own width in order to centre it, and the
    // guess put it off the desk entirely.
    <motion.div
      className="pointer-events-none absolute left-0 top-0 z-30"
      initial={{ x: from.x, y: from.y, opacity: 0 }}
      animate={
        reduced
          ? // Reduced motion: the spec asks for bubble travel to stop, so it
            // appears at the destination instead of crossing the room. The
            // information is kept; only the movement is dropped.
            { x: to.x, y: to.y, opacity: 1 }
          : {
              x: [from.x, (from.x + to.x) / 2, to.x],
              y: [from.y, Math.min(from.y, to.y) - lift, to.y],
              opacity: [0, 1, 1],
            }
      }
      exit={{ opacity: 0, transition: { duration: 0.35 } }}
      transition={
        reduced
          ? { duration: 0.15 }
          : { duration: TRAVEL_SECONDS, times: [0, 0.5, 1], ease: [0.33, 0, 0.2, 1] }
      }
      onAnimationComplete={() => {
        window.setTimeout(() => onDone(bubble.id), (reduced ? 0.4 : LINGER_SECONDS) * 1000);
      }}
    >
      <motion.button
        type="button"
        onClick={() => onOpen(bubble)}
        aria-label={`Message from ${bubble.from} to ${bubble.to}: ${bubble.preview}. Open the full message.`}
        className="pointer-events-auto block w-[15rem] -translate-x-1/2 -translate-y-1/2 rounded-xl border px-3 py-2 text-left shadow-xl shadow-black/60 backdrop-blur-sm transition-[filter] hover:brightness-125"
        style={{
          borderColor: `color-mix(in oklab, ${hue} 55%, var(--color-ink-500))`,
          background: `color-mix(in oklab, var(--color-ink-800) 80%, ${hue} 20%)`,
        }}
        initial={{ scale: 0.7 }}
        animate={{ scale: 1 }}
        transition={reduced ? { duration: 0.12 } : { type: "spring", stiffness: 260, damping: 20 }}
      >
        <span className="mb-1 flex items-center gap-1.5 font-mono text-[10px] tracking-wider">
          <span style={{ color: hue }}>{bubble.from}</span>
          <svg
            viewBox="0 0 24 12"
            className="h-2 w-4 text-parchment-faint"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
          >
            <path d="M1 6h20M16 1l5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="text-parchment-dim">{bubble.to}</span>
        </span>
        <span className="block font-sans text-[13px] leading-snug text-parchment">
          {bubble.preview}
        </span>
      </motion.button>
    </motion.div>
  );
}
