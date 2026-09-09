/**
 * Live spend, in the header.
 *
 * Cost is the thing that turns an agent demo into something you have to
 * supervise, so it sits permanently in view rather than behind a tab. The
 * session figure ticks rather than jumping, because a number that changes
 * silently is a number nobody notices changing.
 */

import { motion, useReducedMotion } from "framer-motion";
import { useEffect, useRef, useState } from "react";

function useTicker(value: number, reduced: boolean) {
  const [shown, setShown] = useState(value);
  const frame = useRef<number>(0);

  useEffect(() => {
    if (reduced) {
      setShown(value);
      return;
    }
    const start = shown;
    const delta = value - start;
    if (Math.abs(delta) < 1e-9) return;
    const began = performance.now();
    const duration = 600;

    const step = (now: number) => {
      const t = Math.min(1, (now - began) / duration);
      const eased = 1 - (1 - t) ** 3;
      setShown(start + delta * eased);
      if (t < 1) frame.current = requestAnimationFrame(step);
    };
    frame.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame.current);
    // `shown` deliberately excluded: including it restarts the tween each frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, reduced]);

  return shown;
}

export interface CostMeterProps {
  sessionCostUsd: number;
  sessionTokens: number;
  taskCostUsd: number | null;
  taskLabel: string | null;
}

export function CostMeter({ sessionCostUsd, sessionTokens, taskCostUsd, taskLabel }: CostMeterProps) {
  const reduced = useReducedMotion() ?? false;
  const session = useTicker(sessionCostUsd, reduced);

  return (
    <div className="flex items-stretch gap-5">
      {taskCostUsd !== null && (
        <Readout
          label={taskLabel ?? "this task"}
          value={`$${taskCostUsd.toFixed(4)}`}
          tone="var(--color-parchment-dim)"
        />
      )}
      <Readout
        label="session"
        value={`$${session.toFixed(4)}`}
        tone="var(--color-lamp)"
        sub={`${sessionTokens.toLocaleString()} tokens`}
        pulse={!reduced && sessionCostUsd > 0}
      />
    </div>
  );
}

function Readout({
  label,
  value,
  tone,
  sub,
  pulse,
}: {
  label: string;
  value: string;
  tone: string;
  sub?: string;
  pulse?: boolean;
}) {
  return (
    <div className="flex flex-col items-end justify-center">
      <span className="label-micro">{label}</span>
      <div className="flex items-baseline gap-1.5">
        {pulse && (
          <motion.span
            className="block h-1 w-1 rounded-full"
            style={{ background: tone }}
            animate={{ opacity: [0.3, 1, 0.3] }}
            transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
          />
        )}
        <span className="font-mono text-sm tabular-nums" style={{ color: tone }}>
          {value}
        </span>
      </div>
      {sub && <span className="font-mono text-[10px] text-parchment-faint">{sub}</span>}
    </div>
  );
}
