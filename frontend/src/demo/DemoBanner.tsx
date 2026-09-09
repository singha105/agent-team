/**
 * Says plainly that this is a recording.
 *
 * Without it a viewer assumes they are driving live agents and only finds out
 * otherwise when assigning a task throws. Better to say so up front than to let
 * someone conclude the app is broken.
 */

import { motion, useReducedMotion } from "framer-motion";

import { demoDescription, isDemo } from "./replay";

export function DemoBanner() {
  const reduced = useReducedMotion() ?? false;
  if (!isDemo) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduced ? 0.1 : 0.4, delay: reduced ? 0 : 0.6 }}
      className="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-4"
    >
      <div
        className="pointer-events-auto flex max-w-2xl items-center gap-3 rounded-full border px-4 py-2 backdrop-blur"
        style={{
          borderColor: "color-mix(in oklab, var(--color-lamp) 35%, var(--color-ink-600))",
          background: "color-mix(in oklab, var(--color-ink-850) 90%, var(--color-lamp) 10%)",
        }}
      >
        <span className="label-micro shrink-0" style={{ color: "var(--color-lamp)" }}>
          recording
        </span>
        <p className="text-[12px] leading-snug text-parchment-dim">
          A real run, replayed — no API key needed.{" "}
          <span className="text-parchment-faint">“{demoDescription}”</span> Assigning and
          reviewing are disabled here; see the README quickstart to run it live.
        </p>
      </div>
    </motion.div>
  );
}
