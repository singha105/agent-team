/**
 * The trace viewer.
 *
 * The spec calls this as important as the agents, and it is: it is the only
 * place the whole run is reconstructable — every message, every tool call with
 * its arguments and result, every delegated child, and what each step cost.
 *
 * Entries arrive already ordered from the API, interleaved across kinds in real
 * time. Nothing is re-sorted here; the order is the evidence.
 */

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect, useState } from "react";

import { api, type TaskTrace, type TraceEntry } from "../lib/api";
import { themeFor } from "../lib/agentTheme";
import { useTeamStore } from "../store/useTeamStore";

export function TraceViewer({ taskId }: { taskId: number }) {
  const [trace, setTrace] = useState<TaskTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectTask = useTeamStore((s) => s.selectTask);

  useEffect(() => {
    let cancelled = false;
    api
      .getTrace(taskId)
      .then((result) => !cancelled && setTrace(result))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : "could not load the trace"));
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  if (error) return <p className="font-mono text-[11px]" style={{ color: "var(--color-state-error)" }}>{error}</p>;
  if (!trace) return <p className="font-mono text-[11px] text-parchment-faint">loading trace…</p>;

  const children = trace.delegation.filter((node) => node.depth > 0);

  return (
    <div className="flex flex-col gap-3">
      {/* --- what the whole tree cost -------------------------------------- */}
      {trace.tree && (
        <div className="rounded border border-ink-700 bg-ink-800/50 px-3 py-2">
          <div className="flex items-baseline justify-between">
            <span className="label-micro">this task</span>
            <span className="font-mono text-[11px] tabular-nums text-parchment">
              ${trace.total_cost_usd.toFixed(4)} · {trace.total_tokens.toLocaleString()} tok
            </span>
          </div>
          {trace.tree.delegated_task_count > 0 && (
            <>
              <div className="mt-1 flex items-baseline justify-between">
                <span className="label-micro">
                  with {trace.tree.delegated_task_count} delegated
                </span>
                <span className="font-mono text-[11px] tabular-nums" style={{ color: "var(--color-lamp)" }}>
                  ${trace.tree.estimated_cost_usd.toFixed(4)} · {trace.tree.total_tokens.toLocaleString()} tok
                </span>
              </div>
              <ul className="mt-2 flex flex-col gap-0.5 border-t border-ink-700 pt-2">
                {trace.tree.by_agent.map((spend) => (
                  <li key={`${spend.agent_key}-${spend.model}`} className="flex items-baseline justify-between gap-2">
                    <span className="flex items-center gap-1.5 font-mono text-[10px] text-parchment-dim">
                      <span className="block h-1.5 w-1.5 rounded-full" style={{ background: themeFor(spend.agent_key).hue }} />
                      {spend.agent_key}
                      <span className="text-parchment-faint">{spend.model}</span>
                    </span>
                    <span className="font-mono text-[10px] tabular-nums text-parchment-faint">
                      ${spend.estimated_cost_usd.toFixed(4)}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {/* --- delegated children ------------------------------------------- */}
      {children.length > 0 && (
        <div>
          <h4 className="label-micro mb-1.5">delegated to</h4>
          <ul className="flex flex-col gap-1">
            {children.map((node) => (
              <li key={node.task_id} style={{ paddingLeft: `${(node.depth - 1) * 12}px` }}>
                <button
                  type="button"
                  onClick={() => selectTask(node.task_id)}
                  className="flex w-full items-center gap-2 rounded border border-ink-700 bg-ink-800/40 px-2 py-1.5 text-left transition-colors hover:border-ink-600"
                >
                  <span className="font-mono text-[10px] text-parchment-faint">↳</span>
                  <span className="block h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: themeFor(node.agent_key).hue }} />
                  <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-parchment-dim">
                    #{node.task_id} {node.agent_key} · {node.status.replace(/_/g, " ")}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* --- the steps ------------------------------------------------------ */}
      <div>
        <h4 className="label-micro mb-1.5">{trace.entries.length} steps</h4>
        <ol className="relative flex flex-col">
          {/* the spine, so the run reads as one thread */}
          <span className="absolute bottom-2 left-[5px] top-2 w-px" style={{ background: "var(--color-ink-700)" }} />
          {trace.entries.map((entry) => (
            <TraceStep key={`${entry.kind}-${entry.ref_id}`} entry={entry} />
          ))}
        </ol>
      </div>
    </div>
  );
}

const KIND_TONE: Record<TraceEntry["kind"], string> = {
  message: "var(--color-parchment-faint)",
  tool_call: "var(--color-state-working)",
  usage: "var(--color-state-thinking)",
};

function TraceStep({ entry }: { entry: TraceEntry }) {
  const [open, setOpen] = useState(false);
  const reduced = useReducedMotion() ?? false;
  const isError = Boolean(entry.detail?.error);

  return (
    <li className="relative pl-5">
      <span
        className="absolute left-0 top-[9px] block h-[11px] w-[11px] rounded-full border-2"
        style={{
          borderColor: isError ? "var(--color-state-error)" : KIND_TONE[entry.kind],
          background: "var(--color-ink-850)",
        }}
      />
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-baseline gap-2 py-1 text-left"
      >
        <span className="font-mono text-[9px] uppercase tracking-wider text-parchment-faint">
          {entry.kind === "tool_call" ? "tool" : entry.kind}
        </span>
        <span
          className="min-w-0 flex-1 truncate font-mono text-[11px]"
          style={{ color: isError ? "var(--color-state-error)" : "var(--color-parchment-dim)" }}
        >
          {entry.summary}
        </span>
        <svg
          viewBox="0 0 24 24"
          className="h-3 w-3 shrink-0 text-parchment-faint transition-transform"
          style={{ transform: open ? "rotate(90deg)" : "none" }}
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
        >
          <path d="M9 5l7 7-7 7" />
        </svg>
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: reduced ? 0.1 : 0.24, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
          >
            <pre className="mb-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded border border-ink-700 bg-ink-900 px-2.5 py-2 font-mono text-[10px] leading-relaxed text-parchment-dim">
              {JSON.stringify(entry.detail, null, 2)}
            </pre>
          </motion.div>
        )}
      </AnimatePresence>
    </li>
  );
}
