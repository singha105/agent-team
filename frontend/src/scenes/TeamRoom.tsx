/**
 * The team room.
 *
 * A shallow stage: back wall, a horizon where wall meets floor, and four desks
 * set along a slight arc so the room has depth without a full isometric
 * projection — which would fight the front-facing characters and make the
 * screen glow, the thing status is carried by, point away from the viewer.
 *
 * Desk positions are measured from the DOM rather than hard-coded, so bubbles
 * fly the real distance between two desks at any viewport width.
 */

import { AnimatePresence } from "framer-motion";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { Desk } from "../components/agent/Desk";
import { FlyingBubble } from "../components/SpeechBubble";
import { themeFor } from "../lib/agentTheme";
import { useTeamStore } from "../store/useTeamStore";

interface Point {
  x: number;
  y: number;
}

export function TeamRoom() {
  const roster = useTeamStore((s) => s.roster);
  const agents = useTeamStore((s) => s.agents);
  const bubbles = useTeamStore((s) => s.bubbles);
  const selectedAgent = useTeamStore((s) => s.selectedAgent);
  const selectAgent = useTeamStore((s) => s.selectAgent);
  const dismissBubble = useTeamStore((s) => s.dismissBubble);
  const openBubbleDetail = useTeamStore((s) => s.openBubbleDetail);

  const stageRef = useRef<HTMLDivElement | null>(null);
  const deskEls = useRef<Map<string, HTMLElement>>(new Map());
  const [anchors, setAnchors] = useState<Record<string, Point>>({});

  const registerDesk = useCallback((key: string, el: HTMLElement | null) => {
    if (el) deskEls.current.set(key, el);
    else deskEls.current.delete(key);
  }, []);

  /**
   * Measure where each desk sits inside the stage. Recomputed on resize because
   * the desks reflow from a row to a grid at tablet width, and a bubble that
   * flies to a stale coordinate lands on nothing.
   */
  const measure = useCallback(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const base = stage.getBoundingClientRect();
    const next: Record<string, Point> = {};
    deskEls.current.forEach((el, key) => {
      const rect = el.getBoundingClientRect();
      // The anchor is a point, not a corner: the bubble centres itself on it.
      //
      // Well above the head rather than over the desk. A resting bubble is a
      // real button, so parking it on the character made the payoff animation
      // swallow clicks meant for the desk underneath — the one interaction the
      // room exists for.
      next[key] = {
        x: rect.left - base.left + rect.width / 2,
        y: rect.top - base.top - 6,
      };
    });
    setAnchors(next);
  }, []);

  useLayoutEffect(() => {
    measure();
  }, [measure, roster.length]);

  useEffect(() => {
    const observer = new ResizeObserver(measure);
    if (stageRef.current) observer.observe(stageRef.current);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure]);

  return (
    <div className="relative flex h-full flex-col overflow-hidden">
      {/* --- the room shell ---------------------------------------------- */}
      <div className="pointer-events-none absolute inset-0">
        {/* back wall */}
        <div
          className="absolute inset-x-0 top-0 h-[62%]"
          style={{
            background:
              "linear-gradient(180deg, var(--color-ink-900) 0%, var(--color-ink-850) 70%, var(--color-ink-800) 100%)",
          }}
        />
        {/* the ceiling wash — one warm source, far above */}
        <div
          className="absolute left-1/2 top-[-28%] h-[70%] w-[120%] -translate-x-1/2 opacity-[0.18]"
          style={{
            background:
              "radial-gradient(ellipse at 50% 0%, var(--color-lamp) 0%, transparent 65%)",
          }}
        />
        {/* horizon */}
        <div
          className="absolute inset-x-0 top-[62%] h-px"
          style={{ background: "linear-gradient(90deg, transparent, var(--color-ink-600), transparent)" }}
        />
        {/* floor, receding */}
        <div
          className="absolute inset-x-0 bottom-0 top-[62%]"
          style={{
            background:
              "linear-gradient(180deg, var(--color-ink-800) 0%, var(--color-ink-900) 100%)",
          }}
        />
      </div>

      {/* --- the stage ---------------------------------------------------- */}
      <div ref={stageRef} className="relative flex flex-1 items-end justify-center px-4 pb-4 pt-20 md:items-center">
        <div className="relative w-full max-w-[1500px]">
          <div className="grid grid-cols-2 gap-x-2 gap-y-6 lg:grid-cols-4 lg:gap-x-6 lg:gap-y-0">
            {roster.map((agent, index) => {
              const theme = themeFor(agent.key);
              const live = agents[agent.key];
              // A slight vertical offset per column reads as an arc, giving the
              // room depth without a projection that would turn the characters
              // away from the viewer.
              const arc = [0, -26, -26, 0][index % 4] ?? 0;
              return (
                <div
                  key={agent.key}
                  style={{ transform: `translateY(${arc}px)` }}
                  className="transition-transform"
                >
                  <Desk
                    agent={agent}
                    status={live?.status ?? agent.status}
                    hue={theme.hue}
                    variant={theme.variant}
                    selected={selectedAgent === agent.key}
                    onSelect={selectAgent}
                    deskRef={registerDesk}
                  />
                </div>
              );
            })}
          </div>

          {/* --- bubbles in flight ---------------------------------------- */}
          <div className="pointer-events-none absolute inset-0 z-30">
            <AnimatePresence>
              {bubbles.map((bubble) => (
                <FlyingBubble
                  key={bubble.id}
                  bubble={bubble}
                  from={anchors[bubble.from] ?? null}
                  to={anchors[bubble.to] ?? null}
                  hue={themeFor(bubble.from).hue}
                  onOpen={openBubbleDetail}
                  onDone={dismissBubble}
                />
              ))}
            </AnimatePresence>
          </div>
        </div>
      </div>

      {roster.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <p className="font-display text-2xl text-parchment-faint">The room is empty.</p>
        </div>
      )}
    </div>
  );
}
