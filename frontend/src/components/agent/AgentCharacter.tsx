/**
 * One agent, drawn in layered SVG.
 *
 * Composition: the character sits behind a desk facing the viewer, with the
 * monitor between them — so we see the slab of its back while the screen light
 * washes forward onto the face and chest. That is the whole reason for the
 * arrangement: it makes screen brightness the primary carrier of status, which
 * is what lets the room be read from across it.
 *
 * Everything is drawn here rather than imported: no sprite sheets, no stock
 * art. Each agent differs only by hue, hair silhouette and a couple of
 * proportions, so the roster stays a config change.
 */

import { motion, useReducedMotion } from "framer-motion";
import { memo, useEffect, useState } from "react";

import type { AgentStatus } from "../../lib/events";
import { poseFor, transitionFor } from "./characterStates";

export interface AgentCharacterProps {
  agentKey: string;
  status: AgentStatus | string;
  /** Signature hue for this desk, a CSS colour. */
  hue: string;
  /** Distinguishes the four silhouettes. */
  variant: 0 | 1 | 2 | 3;
  className?: string;
}

/** Hair shapes, so the four are distinguishable in silhouette alone. */
const HAIR: Record<number, string> = {
  0: "M32 30 C32 12, 68 12, 68 31 C68 22, 60 18, 50 18 C40 18, 32 22, 32 30 Z",
  1: "M30 32 C30 10, 70 10, 70 32 C70 26, 66 40, 62 44 L60 24 L40 24 L38 44 C34 40, 30 26, 30 32 Z",
  2: "M31 31 C31 11, 69 11, 69 31 C64 20, 56 26, 50 20 C44 26, 36 20, 31 31 Z",
  3: "M33 29 C33 13, 67 13, 67 29 C63 21, 58 17, 50 17 C42 17, 37 21, 33 29 Z",
};

/** A little variation so the four do not read as one person recoloured. */
const BUILD: Record<number, { shoulder: number; headScale: number }> = {
  0: { shoulder: 0, headScale: 1 },
  1: { shoulder: 3, headScale: 0.96 },
  2: { shoulder: -2, headScale: 1.04 },
  3: { shoulder: 4, headScale: 0.98 },
};

function AgentCharacterImpl({ agentKey, status, hue, variant, className }: AgentCharacterProps) {
  const reduced = useReducedMotion() ?? false;
  const pose = poseFor(status);
  const transition = transitionFor(status, reduced);
  const build = BUILD[variant] ?? BUILD[0]!;
  const [blink, setBlink] = useState(false);

  // Blinking is what separates "animated" from "alive". Randomised so four
  // characters on screen never blink in unison, which reads as a glitch.
  useEffect(() => {
    if (reduced) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => {
      timer = setTimeout(
        () => {
          if (cancelled) return;
          setBlink(true);
          setTimeout(() => !cancelled && setBlink(false), 130);
          schedule();
        },
        2600 + Math.random() * 4200,
      );
    };
    schedule();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [reduced]);

  const uid = `ch-${agentKey}`;
  const glow = pose.glow;

  return (
    <svg
      viewBox="0 0 200 200"
      className={className}
      aria-hidden="true"
      focusable="false"
      style={{ overflow: "visible" }}
    >
      <defs>
        {/* Light thrown forward off the screen onto the character. */}
        <radialGradient id={`${uid}-spill`} cx="50%" cy="62%" r="58%">
          <stop offset="0%" stopColor={hue} stopOpacity={0.85 * glow} />
          <stop offset="55%" stopColor={hue} stopOpacity={0.22 * glow} />
          <stop offset="100%" stopColor={hue} stopOpacity={0} />
        </radialGradient>

        {/* The pool the desk lamp lays on the floor. */}
        <radialGradient id={`${uid}-pool`} cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={hue} stopOpacity={0.3 + 0.35 * glow} />
          <stop offset="100%" stopColor={hue} stopOpacity={0} />
        </radialGradient>

        <linearGradient id={`${uid}-torso`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#3b342b" />
          <stop offset="100%" stopColor="#241f19" />
        </linearGradient>

        <linearGradient id={`${uid}-monitor`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#2b251d" />
          <stop offset="100%" stopColor="#191510" />
        </linearGradient>

        <filter id={`${uid}-bloom`} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="6" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* --- floor pool ------------------------------------------------- */}
      <motion.ellipse
        cx="100"
        cy="176"
        rx="76"
        ry="16"
        fill={`url(#${uid}-pool)`}
        animate={{ opacity: 0.5 + glow * 0.5, rx: 70 + glow * 12 }}
        transition={transition}
      />

      {/* --- chair ------------------------------------------------------- */}
      <motion.g animate={{ y: pose.slump * 0.3 }} transition={transition}>
        <rect x="66" y="96" width="68" height="62" rx="14" fill="#221d17" />
        <rect x="72" y="102" width="56" height="50" rx="11" fill="#2a241c" />
      </motion.g>

      {/* --- the character ------------------------------------------------ */}
      <motion.g
        style={{ originX: "100px", originY: "150px" }}
        animate={{ rotate: pose.lean, y: pose.slump }}
        transition={transition}
      >
        {/* Breathing: the whole upper body, not just the chest. */}
        <motion.g
          animate={reduced || pose.breath === 0 ? { y: 0 } : { y: [0, -pose.breath, 0] }}
          transition={
            reduced
              ? { duration: 0 }
              : { duration: pose.breathDuration, repeat: Infinity, ease: "easeInOut" }
          }
        >
          {/* torso */}
          <path
            d={`M${72 - build.shoulder} 152 C${72 - build.shoulder} 108, ${128 + build.shoulder} 108, ${128 + build.shoulder} 152 Z`}
            fill={`url(#${uid}-torso)`}
          />
          {/* collar catches the screen light */}
          <path
            d={`M${84 - build.shoulder * 0.5} 118 C92 128, 108 128, ${116 + build.shoulder * 0.5} 118`}
            stroke={hue}
            strokeOpacity={0.25 + glow * 0.4}
            strokeWidth="2"
            fill="none"
          />

          {/* arms — the typing loop lives here */}
          {[-1, 1].map((side) => (
            <motion.g
              key={side}
              style={{ originX: `${100 + side * 22}px`, originY: "122px" }}
              animate={
                pose.typing && !reduced
                  ? { rotate: [0, side * 5, 0, side * 3, 0] }
                  : { rotate: pose.lean * -0.3 }
              }
              transition={
                pose.typing && !reduced
                  ? { duration: 0.55, repeat: Infinity, ease: "easeInOut", delay: side < 0 ? 0 : 0.14 }
                  : transition
              }
            >
              <path
                d={`M${100 + side * 20} 122 Q${100 + side * 34} 136, ${100 + side * 26} 150`}
                stroke="#332c23"
                strokeWidth="11"
                strokeLinecap="round"
                fill="none"
              />
              {/* hand, lit from the screen */}
              <circle
                cx={100 + side * 26}
                cy="150"
                r="6"
                fill="#4a4033"
                opacity={0.7 + glow * 0.3}
              />
            </motion.g>
          ))}

          {/* head */}
          <motion.g
            style={{ originX: "100px", originY: "96px" }}
            animate={{ rotate: pose.headTurn * 0.35, x: pose.headTurn * 0.28 }}
            transition={transition}
          >
            <g transform={`translate(100 74) scale(${build.headScale}) translate(-100 -74)`}>
              {/* neck */}
              <rect x="94" y="92" width="12" height="14" rx="5" fill="#2e2820" />
              {/* face */}
              <ellipse cx="100" cy="76" rx="19" ry="21" fill="#4d4234" />
              {/* screen light on the face — the key read */}
              <motion.ellipse
                cx="100"
                cy="80"
                rx="17"
                ry="18"
                fill={hue}
                animate={{ opacity: 0.1 + glow * 0.42 }}
                transition={transition}
              />
              {/* eyes */}
              <motion.g animate={{ scaleY: blink ? 0.08 : 1 }} style={{ originY: "76px" }}>
                <ellipse cx="93" cy="76" rx="2.4" ry="3" fill="#15120e" />
                <ellipse cx="107" cy="76" rx="2.4" ry="3" fill="#15120e" />
              </motion.g>
              {/* hair */}
              <path d={HAIR[variant] ?? HAIR[0]} fill="#241f19" transform="translate(50 44) scale(0.62) translate(-50 -44)" />
              <path
                d={HAIR[variant] ?? HAIR[0]}
                fill={hue}
                fillOpacity={0.12 + glow * 0.2}
                transform="translate(50 44) scale(0.62) translate(-50 -44)"
              />
            </g>
          </motion.g>
        </motion.g>
      </motion.g>

      {/* --- desk --------------------------------------------------------- */}
      <rect x="24" y="152" width="152" height="8" rx="3" fill="#2f2820" />
      <rect x="24" y="152" width="152" height="2" rx="1" fill={hue} fillOpacity={0.18 + glow * 0.3} />
      <rect x="36" y="160" width="7" height="18" rx="2" fill="#241f19" />
      <rect x="157" y="160" width="7" height="18" rx="2" fill="#241f19" />

      {/* keyboard */}
      <rect x="76" y="146" width="48" height="7" rx="2" fill="#241f19" />

      {/* --- monitor, seen from behind ------------------------------------ */}
      <g>
        <rect x="58" y="86" width="84" height="56" rx="6" fill={`url(#${uid}-monitor)`} />
        <rect x="94" y="142" width="12" height="10" fill="#221d17" />
        <rect x="82" y="151" width="36" height="4" rx="2" fill="#2a241c" />
        {/* light escaping around the panel edges */}
        <motion.rect
          x="56"
          y="84"
          width="88"
          height="60"
          rx="8"
          fill="none"
          stroke={hue}
          strokeWidth="2"
          filter={`url(#${uid}-bloom)`}
          animate={{ strokeOpacity: 0.15 + glow * 0.75 }}
          transition={transition}
        />
      </g>

      {/* --- screen spill over everything --------------------------------- */}
      <motion.ellipse
        cx="100"
        cy="112"
        rx="96"
        ry="72"
        fill={`url(#${uid}-spill)`}
        style={{ mixBlendMode: "screen" }}
        animate={{ opacity: 0.35 + glow * 0.65 }}
        transition={transition}
      />

      {/*
        Screen flicker, only while working. A steady glow reads as a poster;
        the irregular tick is what makes it feel like a machine doing something.
      */}
      {status === "working" && !reduced && (
        <motion.rect
          x="58"
          y="86"
          width="84"
          height="56"
          rx="6"
          fill={hue}
          animate={{ opacity: [0.05, 0.13, 0.06, 0.16, 0.07] }}
          transition={{ duration: 1.7, repeat: Infinity, ease: "linear" }}
          style={{ mixBlendMode: "screen" }}
        />
      )}
    </svg>
  );
}

export const AgentCharacter = memo(AgentCharacterImpl);
