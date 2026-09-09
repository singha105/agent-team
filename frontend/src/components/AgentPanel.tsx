/**
 * The slide-in agent panel.
 *
 * Everything about one agent in one place: who they are, what they are on, what
 * they have done, and a way to give them work. The live run view is the part
 * that matters — it is the same trace the backend records, arriving as it
 * happens rather than after the fact.
 */

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect, useMemo, useRef, useState } from "react";

import { themeFor } from "../lib/agentTheme";
import { poseFor } from "./agent/characterStates";
import { agentTasksFrom, useTeamStore } from "../store/useTeamStore";
import { TraceViewer } from "./TraceViewer";

export function AgentPanel() {
  const selectedAgent = useTeamStore((s) => s.selectedAgent);
  const selectAgent = useTeamStore((s) => s.selectAgent);
  const roster = useTeamStore((s) => s.roster);
  const agents = useTeamStore((s) => s.agents);
  const tasks = useTeamStore((s) => s.tasks);
  const live = useTeamStore((s) => s.live);
  const assignTask = useTeamStore((s) => s.assignTask);
  const reviewTask = useTeamStore((s) => s.reviewTask);
  const selectTask = useTeamStore((s) => s.selectTask);

  const reduced = useReducedMotion() ?? false;
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [openTrace, setOpenTrace] = useState<number | null>(null);

  const agent = roster.find((a) => a.key === selectedAgent) ?? null;
  const status = selectedAgent ? (agents[selectedAgent]?.status ?? agent?.status ?? "idle") : "idle";
  const theme = themeFor(selectedAgent ?? "");
  const pose = poseFor(status);

  const history = useMemo(
    () => agentTasksFrom(tasks, selectedAgent).slice(0, 40),
    [tasks, selectedAgent],
  );

  // The task whose run is currently streaming: the newest one not yet finished.
  const activeTaskId = useMemo(() => {
    const running = history.find((t) => t.status === "in_progress" || t.status === "queued");
    return running?.id ?? history[0]?.id ?? null;
  }, [history]);

  const entries = activeTaskId !== null ? (live[activeTaskId] ?? []) : [];

  // Close on Escape, and return focus to the desk that opened the panel.
  useEffect(() => {
    if (!selectedAgent) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        selectAgent(null);
        const desk = document.querySelector<HTMLElement>(`[data-agent="${selectedAgent}"]`);
        desk?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedAgent, selectAgent]);

  // Move focus into the panel when it opens, so a keyboard user is not left
  // behind on the desk button while a dialog is on screen.
  useEffect(() => {
    if (selectedAgent && panelRef.current) {
      panelRef.current.focus();
      setDraft("");
      setOpenTrace(null);
    }
  }, [selectedAgent]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!agent || !draft.trim() || sending) return;
    setSending(true);
    const created = await assignTask(agent.key, draft.trim());
    setSending(false);
    if (created) {
      setDraft("");
      selectTask(created.id);
    }
  };

  return (
    <AnimatePresence>
      {agent && (
        <>
          <motion.div
            className="fixed inset-0 z-40 bg-ink-900/60 backdrop-blur-[2px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reduced ? 0.1 : 0.25 }}
            onClick={() => selectAgent(null)}
            aria-hidden="true"
          />

          <motion.aside
            ref={panelRef}
            tabIndex={-1}
            role="dialog"
            aria-modal="true"
            aria-label={`${agent.display_name} — ${agent.role}`}
            className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[min(34rem,100vw)] flex-col border-l border-ink-700 bg-ink-850 shadow-2xl shadow-black/60 outline-none"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={
              reduced ? { duration: 0.12 } : { type: "spring", stiffness: 260, damping: 32 }
            }
          >
            {/* --- header ------------------------------------------------- */}
            <div
              className="relative shrink-0 border-b border-ink-700 px-5 py-4"
              style={{
                background: `linear-gradient(180deg, color-mix(in oklab, ${theme.hue} 10%, var(--color-ink-800)), var(--color-ink-850))`,
              }}
            >
              <button
                type="button"
                onClick={() => selectAgent(null)}
                className="absolute right-4 top-4 rounded-full border border-ink-600 p-1.5 text-parchment-faint transition-colors hover:text-parchment"
                aria-label="Close agent details"
              >
                <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>

              <div className="flex items-baseline gap-2.5">
                <h2 className="font-display text-3xl leading-none text-parchment">{agent.display_name}</h2>
                <span className="label-micro">{agent.key}</span>
              </div>
              <p className="mt-1 text-sm text-parchment-dim">{agent.role}</p>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <span
                  className="rounded-full border px-2 py-0.5 font-mono text-[10px] tracking-wide"
                  style={{ borderColor: `color-mix(in oklab, ${theme.hue} 40%, var(--color-ink-600))`, color: theme.hue }}
                >
                  {agent.model}
                </span>
                <span
                  className="flex items-center gap-1.5 rounded-full border border-ink-600 px-2 py-0.5 font-mono text-[10px]"
                  style={{ color: pose.accent }}
                >
                  <span className="block h-1.5 w-1.5 rounded-full" style={{ background: pose.accent }} />
                  {pose.label}
                </span>
                {agent.queued_tasks > 0 && (
                  <span className="rounded-full border border-ink-600 px-2 py-0.5 font-mono text-[10px] text-parchment-faint">
                    {agent.queued_tasks} in flight
                  </span>
                )}
              </div>
            </div>

            {/* --- scrolling body ------------------------------------------ */}
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {agent.bio && (
                <section className="mb-6">
                  <h3 className="label-micro mb-2">who they are</h3>
                  <p className="font-display text-[15px] leading-relaxed text-parchment-dim">{agent.bio}</p>
                </section>
              )}

              {agent.owns.length > 0 && (
                <section className="mb-6">
                  <h3 className="label-micro mb-2">owns</h3>
                  <ul className="flex flex-wrap gap-1.5">
                    {agent.owns.map((item) => (
                      <li
                        key={item}
                        className="rounded border border-ink-700 px-2 py-1 font-mono text-[10px] text-parchment-faint"
                      >
                        {item}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              <section className="mb-6">
                <h3 className="label-micro mb-2">
                  live run {activeTaskId !== null ? `· task ${activeTaskId}` : ""}
                </h3>
                <LiveRun entries={entries} hue={theme.hue} />
              </section>

              <section>
                <h3 className="label-micro mb-2">task history</h3>
                {history.length === 0 ? (
                  <p className="font-mono text-[11px] text-parchment-faint">
                    Nothing assigned yet.
                  </p>
                ) : (
                  <ul className="flex flex-col gap-1.5">
                    {history.map((task) => (
                      <li key={task.id}>
                        <div className="rounded-lg border border-ink-700 bg-ink-800/60">
                          <button
                            type="button"
                            onClick={() => setOpenTrace(openTrace === task.id ? null : task.id)}
                            aria-expanded={openTrace === task.id}
                            className="flex w-full items-start gap-2 px-3 py-2 text-left"
                          >
                            <span className="mt-1.5 block h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: `var(--color-task-${statusToken(task.status)})` }} />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-[13px] text-parchment">{task.title}</span>
                              <span className="font-mono text-[10px] text-parchment-faint">
                                #{task.id} · {task.status.replace(/_/g, " ")}
                                {task.attempt > 1 ? ` · attempt ${task.attempt}` : ""}
                              </span>
                            </span>
                          </button>

                          {task.status === "needs_review" && (
                            <ReviewControls taskId={task.id} onReview={reviewTask} />
                          )}

                          <AnimatePresence initial={false}>
                            {openTrace === task.id && (
                              <motion.div
                                initial={{ height: 0, opacity: 0 }}
                                animate={{ height: "auto", opacity: 1 }}
                                exit={{ height: 0, opacity: 0 }}
                                transition={{ duration: reduced ? 0.1 : 0.3, ease: [0.22, 1, 0.36, 1] }}
                                className="overflow-hidden"
                              >
                                <div className="border-t border-ink-700 px-3 py-2">
                                  <TraceViewer taskId={task.id} />
                                </div>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>

            {/* --- assign ------------------------------------------------- */}
            <form onSubmit={submit} className="shrink-0 border-t border-ink-700 bg-ink-800/60 px-5 py-4">
              <label htmlFor="assign-task" className="label-micro mb-2 block">
                assign {agent.display_name} a task
              </label>
              <div className="flex gap-2">
                <textarea
                  id="assign-task"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") void submit(event);
                  }}
                  rows={2}
                  placeholder={`e.g. ${placeholderFor(agent.key)}`}
                  className="min-h-[3.25rem] flex-1 resize-y rounded-lg border border-ink-600 bg-ink-900 px-3 py-2 text-[13px] text-parchment placeholder:text-parchment-faint/60 focus:border-ink-500 focus:outline-none"
                />
                <button
                  type="submit"
                  disabled={!draft.trim() || sending}
                  className="shrink-0 self-end rounded-lg border px-4 py-2 font-mono text-[11px] uppercase tracking-[0.14em] transition-opacity disabled:opacity-35"
                  style={{ borderColor: theme.hue, color: theme.hue }}
                >
                  {sending ? "sending" : "assign"}
                </button>
              </div>
              <p className="mt-1.5 font-mono text-[10px] text-parchment-faint">⌘↵ to send</p>
            </form>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

function statusToken(status: string): string {
  if (status === "in_progress") return "progress";
  if (status === "needs_review") return "review";
  if (status === "failed" || status === "budget_exceeded") return "failed";
  if (status === "done") return "done";
  return "queued";
}

function placeholderFor(key: string): string {
  const hints: Record<string, string> = {
    backend: "Build a REST API for a book library with search",
    database: "Design the schema for a book library with search",
    frontend: "Build the book list view against the published contract",
    devops: "Write the Dockerfile and CI pipeline for the API",
  };
  return hints[key] ?? "describe the work";
}

function ReviewControls({
  taskId,
  onReview,
}: {
  taskId: number;
  onReview: (id: number, decision: "approve" | "reject", feedback?: string) => Promise<void>;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <div className="border-t border-ink-700 px-3 py-2">
      {!rejecting ? (
        <div className="flex gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await onReview(taskId, "approve");
              setBusy(false);
            }}
            className="rounded border px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider disabled:opacity-40"
            style={{ borderColor: "var(--color-task-done)", color: "var(--color-task-done)" }}
          >
            approve
          </button>
          <button
            type="button"
            onClick={() => setRejecting(true)}
            className="rounded border border-ink-600 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider text-parchment-faint"
          >
            request changes
          </button>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <label htmlFor={`feedback-${taskId}`} className="label-micro">
            what needs to change
          </label>
          <textarea
            id={`feedback-${taskId}`}
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            rows={2}
            autoFocus
            className="resize-y rounded border border-ink-600 bg-ink-900 px-2 py-1.5 text-[12px] text-parchment focus:outline-none"
            placeholder="The agent resumes with this and its prior work in context."
          />
          <div className="flex gap-2">
            <button
              type="button"
              disabled={!feedback.trim() || busy}
              onClick={async () => {
                setBusy(true);
                await onReview(taskId, "reject", feedback.trim());
                setBusy(false);
                setRejecting(false);
                setFeedback("");
              }}
              className="rounded border px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider disabled:opacity-40"
              style={{ borderColor: "var(--color-state-waiting)", color: "var(--color-state-waiting)" }}
            >
              send back
            </button>
            <button
              type="button"
              onClick={() => setRejecting(false)}
              className="rounded border border-ink-600 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider text-parchment-faint"
            >
              cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function LiveRun({ entries, hue }: { entries: { id: string; kind: string; label: string; detail: string | null; pending?: boolean; isError?: boolean; durationMs?: number | null }[]; hue: string }) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [entries.length]);

  if (entries.length === 0) {
    return (
      <p className="font-mono text-[11px] text-parchment-faint">
        Nothing streaming. Assign a task and this fills in as it runs.
      </p>
    );
  }

  return (
    <div className="max-h-64 overflow-y-auto rounded-lg border border-ink-700 bg-ink-900/70">
      <ul className="divide-y divide-ink-800">
        <AnimatePresence initial={false}>
          {entries.map((entry) => (
            <motion.li
              key={entry.id}
              layout
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              className="flex items-start gap-2 px-3 py-1.5"
            >
              <span
                className="mt-1 block h-1 w-1 shrink-0 rounded-full"
                style={{
                  background: entry.isError
                    ? "var(--color-state-error)"
                    : entry.kind === "tool"
                      ? hue
                      : "var(--color-parchment-faint)",
                }}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-[11px]" style={{ color: entry.isError ? "var(--color-state-error)" : "var(--color-parchment)" }}>
                    {entry.label}
                  </span>
                  {entry.pending && (
                    <motion.span
                      className="font-mono text-[9px] text-parchment-faint"
                      animate={{ opacity: [0.35, 1, 0.35] }}
                      transition={{ duration: 1.2, repeat: Infinity }}
                    >
                      running
                    </motion.span>
                  )}
                  {entry.durationMs != null && (
                    <span className="font-mono text-[9px] text-parchment-faint">{entry.durationMs}ms</span>
                  )}
                </div>
                {entry.detail && (
                  <p className="truncate font-mono text-[10px] text-parchment-faint">{entry.detail}</p>
                )}
              </div>
            </motion.li>
          ))}
        </AnimatePresence>
      </ul>
      <div ref={endRef} />
    </div>
  );
}
