/**
 * One agent, drawn in layered SVG.
 *
 * Composition: the character sits behind the desk facing the viewer, with a
 * monitor low in front of them — small enough that head, shoulders and arms all
 * clear it. That matters more than it sounds. An earlier pass put a large
 * monitor over the torso and every agent read as a head balanced on a box; the
 * figure only becomes a person once the shoulder line and the arms are visible
 * either side of the screen.
 *
 * Screen light is the primary carrier of status, so the panel throws light
 * forward onto the face and chest rather than being something you read directly.
 *
 * Everything is drawn here: no sprite sheets, no stock art. The four differ by
 * hue, hair silhouette and small build changes, so the roster stays config.
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

/**
 * Hair, in the head's own coordinates — centred near (100, 62), radius ~22.
 * Silhouette is the only thing separating the four at a glance across the room,
 * so each is a different outline rather than a different colour.
 */
const HAIR: Record<number, string> = {
  0: "M77 60 C77 40, 123 40, 123 61 C119 49, 111 44, 100 44 C87 44, 79 49, 77 60 Z",
  1: "M76 62 C76 38, 124 38, 124 62 C124 74, 121 85, 118 91 L113 56 L87 56 L82 91 C79 85, 76 74, 76 62 Z",
  2: "M77 61 C77 39, 123 39, 123 61 C116 48, 108 54, 100 45 C92 54, 84 48, 77 61 Z",
  3: "M78 59 C78 36, 122 36, 122 59 C117 46, 110 41, 100 41 C90 41, 83 46, 78 59 Z",
};

/** Small build differences, so the four do not read as one person recoloured. */
const BUILD: Record<number, { shoulder: number; headScale: number }> = {
  0: { shoulder: 0, headScale: 1 },
  1: { shoulder: 4, headScale: 0.95 },
  2: { shoulder: -3, headScale: 1.05 },
  3: { shoulder: 6, headScale: 0.97 },
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
  const sh = build.shoulder;

  return (
    <svg
      viewBox="0 26 200 156"
      className={className}
      aria-hidden="true"
      focusable="false"
      style={{ overflow: "visible" }}
    >
      <defs>
        <radialGradient id={`${uid}-spill`} cx="50%" cy="58%" r="60%">
          <stop offset="0%" stopColor={hue} stopOpacity={0.8 * glow} />
          <stop offset="55%" stopColor={hue} stopOpacity={0.2 * glow} />
          <stop offset="100%" stopColor={hue} stopOpacity={0} />
        </radialGradient>

        <radialGradient id={`${uid}-pool`} cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={hue} stopOpacity={0.28 + 0.34 * glow} />
          <stop offset="100%" stopColor={hue} stopOpacity={0} />
        </radialGradient>

        <linearGradient id={`${uid}-torso`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#453c31" />
          <stop offset="100%" stopColor="#282219" />
        </linearGradient>

        <linearGradient id={`${uid}-monitor`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#171310" />
          <stop offset="100%" stopColor="#0c0a07" />
        </linearGradient>

        <filter id={`${uid}-bloom`} x="-70%" y="-70%" width="240%" height="240%">
          <feGaussianBlur stdDeviation="5" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* --- floor pool ---------------------------------------------------- */}
      <motion.ellipse
        cx="100"
        cy="172"
        rx="78"
        ry="15"
        fill={`url(#${uid}-pool)`}
        // scale rather than rx: animating an SVG geometry attribute here made
        // Framer Motion emit rx="undefined" on the first frame, which the
        // browser rejects outright.
        style={{ originX: "100px", originY: "172px" }}
        animate={{ opacity: 0.5 + glow * 0.5, scaleX: 0.92 + glow * 0.18 }}
        transition={transition}
      />

      {/* --- chair, behind everything -------------------------------------- */}
      <motion.g animate={{ y: pose.slump * 0.3 }} transition={transition}>
        <rect x="56" y="84" width="88" height="68" rx="16" fill="#1d1913" />
      </motion.g>

      {/* --- the character --------------------------------------------------- */}
      <motion.g
        style={{ originX: "100px", originY: "150px" }}
        animate={{ rotate: pose.lean, y: pose.slump }}
        transition={transition}
      >
        <motion.g
          animate={reduced || pose.breath === 0 ? { y: 0 } : { y: [0, -pose.breath, 0] }}
          transition={
            reduced
              ? { duration: 0 }
              : { duration: pose.breathDuration, repeat: Infinity, ease: "easeInOut" }
          }
        >
          {/* Shoulder line, wider than the monitor so the body is legible. */}
          <path
            d={`M${54 - sh} 158 L${63 - sh} 112 Q100 94 ${137 + sh} 112 L${146 + sh} 158 Z`}
            fill={`url(#${uid}-torso)`}
          />
          {/* collar, catching the screen light */}
          <path
            d={`M${80 - sh * 0.4} 106 C88 118, 112 118, ${120 + sh * 0.4} 106`}
            stroke={hue}
            strokeOpacity={0.2 + glow * 0.45}
            strokeWidth="2.2"
            fill="none"
          />

          {/* arms, outboard of the monitor — the typing loop lives here */}
          {[-1, 1].map((side) => (
            <motion.g
              key={side}
              style={{ originX: `${100 + side * 36}px`, originY: "116px" }}
              animate={
                pose.typing && !reduced
                  ? { rotate: [0, side * 6, 0, side * 3.5, 0] }
                  : { rotate: pose.lean * -0.35 }
              }
              transition={
                pose.typing && !reduced
                  ? { duration: 0.5, repeat: Infinity, ease: "easeInOut", delay: side < 0 ? 0 : 0.13 }
                  : transition
              }
            >
              <path
                d={`M${100 + side * 34} 116 Q${100 + side * 56} 132, ${100 + side * 42} 148`}
                stroke="#3a3126"
                strokeWidth="13"
                strokeLinecap="round"
                fill="none"
              />
              <motion.circle
                cx={100 + side * 42}
                cy="148"
                r="6.5"
                fill="#5a4e3d"
                animate={{ opacity: 0.65 + glow * 0.35 }}
                transition={transition}
              />
            </motion.g>
          ))}

          {/* head */}
          <motion.g
            style={{ originX: "100px", originY: "88px" }}
            animate={{ rotate: pose.headTurn * 0.35, x: pose.headTurn * 0.3 }}
            transition={transition}
          >
            <g transform={`translate(100 62) scale(${build.headScale}) translate(-100 -62)`}>
              <rect x="92" y="80" width="16" height="18" rx="7" fill="#332b22" />
              <ellipse cx="100" cy="62" rx="22" ry="24" fill="#54483a" />
              {/* screen light on the face — the key read */}
              <motion.ellipse
                cx="100"
                cy="68"
                rx="20"
                ry="21"
                fill={hue}
                animate={{ opacity: 0.08 + glow * 0.4 }}
                transition={transition}
              />
              <motion.g animate={{ scaleY: blink ? 0.08 : 1 }} style={{ originY: "63px" }}>
                <ellipse cx="91" cy="63" rx="2.8" ry="3.4" fill="#14110d" />
                <ellipse cx="109" cy="63" rx="2.8" ry="3.4" fill="#14110d" />
              </motion.g>
              <path d={HAIR[variant] ?? HAIR[0]} fill="#241f18" />
              <path d={HAIR[variant] ?? HAIR[0]} fill={hue} fillOpacity={0.08 + glow * 0.22} />
            </g>
          </motion.g>
        </motion.g>
      </motion.g>

      {/* --- desk ------------------------------------------------------------ */}
      <rect x="18" y="152" width="164" height="8" rx="3" fill="#332b22" />
      <rect x="18" y="152" width="164" height="2" rx="1" fill={hue} fillOpacity={0.16 + glow * 0.32} />
      <rect x="32" y="160" width="7" height="16" rx="2" fill="#241f19" />
      <rect x="161" y="160" width="7" height="16" rx="2" fill="#241f19" />

      {/* --- monitor: low, narrow, and dark enough to read as a separate object */}
      <g>
        <rect x="78" y="112" width="44" height="30" rx="4" fill={`url(#${uid}-monitor)`} />
        <rect x="97" y="142" width="6" height="6" fill="#17130f" />
        <rect x="89" y="147" width="22" height="3" rx="1.5" fill="#241f19" />
        <motion.rect
          x="76.5"
          y="110.5"
          width="47"
          height="33"
          rx="5"
          fill="none"
          stroke={hue}
          strokeWidth="1.8"
          filter={`url(#${uid}-bloom)`}
          animate={{ strokeOpacity: 0.14 + glow * 0.78 }}
          transition={transition}
        />
      </g>

      {/* keyboard, in front of the monitor */}
      <rect x="74" y="149" width="52" height="5" rx="2" fill="#1f1a14" />

      {/* --- screen spill over everything -------------------------------------- */}
      <motion.ellipse
        cx="100"
        cy="106"
        rx="94"
        ry="74"
        fill={`url(#${uid}-spill)`}
        style={{ mixBlendMode: "screen" }}
        animate={{ opacity: 0.3 + glow * 0.7 }}
        transition={transition}
      />

      {/*
        Screen flicker, only while working. A steady glow reads as a poster; the
        irregular tick is what makes it feel like a machine doing something.
      */}
      {status === "working" && !reduced && (
        <motion.rect
          x="78"
          y="112"
          width="44"
          height="30"
          rx="4"
          fill={hue}
          animate={{ opacity: [0.06, 0.16, 0.07, 0.19, 0.08] }}
          transition={{ duration: 1.7, repeat: Infinity, ease: "linear" }}
          style={{ mixBlendMode: "screen" }}
        />
      )}
    </svg>
  );
}

export const AgentCharacter = memo(AgentCharacterImpl);
