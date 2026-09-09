/**
 * The task board.
 *
 * Kanban columns matching the lifecycle the backend enforces, so the board
 * cannot show a state the state machine does not have. Cards drag between
 * columns, but only where the lifecycle allows it: the backend rejects an
 * illegal transition, and offering a drop target that will be refused is worse
 * than not offering it.
 *
 * In practice that means exactly one draggable move — needs_review to done —
 * which is approval. Everything else is driven by the agents themselves, and
 * the board says so rather than pretending otherwise.
 */

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useMemo, useState } from "react";

import { themeFor } from "../lib/agentTheme";
import type { TaskStatus } from "../lib/events";
import type { TaskView } from "../store/reducer";
import { useTeamStore } from "../store/useTeamStore";
import { TraceViewer } from "../components/TraceViewer";

type BoardCard = TaskView;

interface Column {
  status: TaskStatus | "failed_any";
  label: string;
  tone: string;
  hint: string;
}

const COLUMNS: Column[] = [
  { status: "queued", label: "queued", tone: "var(--color-task-queued)", hint: "waiting for a worker" },
  { status: "in_progress", label: "in progress", tone: "var(--color-task-progress)", hint: "the agent is running" },
  { status: "needs_review", label: "needs review", tone: "var(--color-task-review)", hint: "your call" },
  { status: "done", label: "done", tone: "var(--color-task-done)", hint: "approved" },
  { status: "failed_any", label: "halted", tone: "var(--color-task-failed)", hint: "failed or over budget" },
];

/** Which drops the backend will actually accept. Approval is the only one. */
const DROPPABLE: Partial<Record<string, TaskStatus[]>> = {
  done: ["needs_review"],
};

export function TaskBoard() {
  const tasks = useTeamStore((s) => s.tasks);
  const reviewTask = useTeamStore((s) => s.reviewTask);
  const reduced = useReducedMotion() ?? false;

  const [dragging, setDragging] = useState<number | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [openTrace, setOpenTrace] = useState<number | null>(null);

  /**
   * Grouped from the event-mirrored store rather than the REST list, so a task
   * an agent created by delegating appears on the board as it happens instead
   * of on the next refetch.
   */
  const byColumn = useMemo(() => {
    const grouped: Record<string, BoardCard[]> = {};
    for (const column of COLUMNS) grouped[column.status] = [];
    const cards = Object.values(tasks).sort((a, b) => b.id - a.id);
    for (const task of cards) {
      const key =
        task.status === "failed" || task.status === "budget_exceeded"
          ? "failed_any"
          : task.status;
      (grouped[key] ??= []).push(task);
    }
    return grouped;
  }, [tasks]);

  const draggedStatus = dragging !== null ? (tasks[dragging]?.status ?? null) : null;

  return (
    <div className="h-full overflow-x-auto overflow-y-hidden px-4 py-4 md:px-6">
      <div className="flex h-full min-w-[52rem] gap-3">
        {COLUMNS.map((column) => {
          const accepts = draggedStatus
            ? (DROPPABLE[column.status] ?? []).includes(draggedStatus as TaskStatus)
            : false;
          const items = byColumn[column.status] ?? [];

          return (
            <section
              key={column.status}
              aria-label={`${column.label}, ${items.length} tasks`}
              onDragOver={(event) => {
                if (!accepts) return;
                event.preventDefault();
                setHovered(column.status);
              }}
              onDragLeave={() => setHovered((h) => (h === column.status ? null : h))}
              onDrop={async (event) => {
                event.preventDefault();
                setHovered(null);
                if (accepts && dragging !== null) await reviewTask(dragging, "approve");
                setDragging(null);
              }}
              className="flex min-w-[15rem] flex-1 flex-col rounded-xl border transition-colors"
              style={{
                borderColor: hovered === column.status && accepts ? column.tone : "var(--color-ink-700)",
                background:
                  hovered === column.status && accepts
                    ? `color-mix(in oklab, ${column.tone} 8%, var(--color-ink-850))`
                    : "color-mix(in oklab, var(--color-ink-850) 70%, transparent)",
              }}
            >
              <header className="flex items-baseline justify-between gap-2 border-b border-ink-700 px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <span className="block h-1.5 w-1.5 rounded-full" style={{ background: column.tone }} />
                  <h2 className="font-mono text-[10px] uppercase tracking-[0.16em]" style={{ color: column.tone }}>
                    {column.label}
                  </h2>
                </div>
                <span className="font-mono text-[10px] text-parchment-faint">{items.length}</span>
              </header>

              <div className="flex-1 overflow-y-auto p-2">
                {items.length === 0 ? (
                  <p className="px-1 py-2 font-mono text-[10px] text-parchment-faint/70">{column.hint}</p>
                ) : (
                  <ul className="flex flex-col gap-2">
                    <AnimatePresence initial={false}>
                      {items.map((task) => {
                        const theme = themeFor(task.agentKey);
                        const canDrag = task.status === "needs_review";
                        return (
                          <motion.li
                            key={task.id}
                            layout={!reduced}
                            initial={{ opacity: 0, y: 6 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0, scale: 0.97 }}
                            transition={{ duration: reduced ? 0.1 : 0.24 }}
                          >
                            <article
                              draggable={canDrag}
                              onDragStart={() => setDragging(task.id)}
                              onDragEnd={() => {
                                setDragging(null);
                                setHovered(null);
                              }}
                              className="rounded-lg border border-ink-700 bg-ink-800/80"
                              style={{ cursor: canDrag ? "grab" : "default" }}
                            >
                              <button
                                type="button"
                                onClick={() => setOpenTrace(openTrace === task.id ? null : task.id)}
                                aria-expanded={openTrace === task.id}
                                className="w-full px-3 py-2 text-left"
                              >
                                <div className="mb-1 flex items-center gap-1.5">
                                  <span className="block h-1.5 w-1.5 rounded-full" style={{ background: theme.hue }} />
                                  <span className="font-mono text-[10px]" style={{ color: theme.hue }}>
                                    {task.agentKey}
                                  </span>
                                  <span className="ml-auto font-mono text-[10px] text-parchment-faint">#{task.id}</span>
                                </div>
                                <p className="line-clamp-2 text-[13px] leading-snug text-parchment">{task.title}</p>
                                <div className="mt-1.5 flex items-center gap-2 font-mono text-[10px] text-parchment-faint">
                                  {task.attempt > 1 && <span>attempt {task.attempt}</span>}
                                  {task.costUsd > 0 && <span>${task.costUsd.toFixed(4)}</span>}
                                  {task.createdBy !== "human" && <span>by {task.createdBy}</span>}
                                </div>
                                {task.haltReason && (
                                  <p className="mt-1.5 line-clamp-2 font-mono text-[10px]" style={{ color: "var(--color-state-error)" }}>
                                    {task.haltReason}
                                  </p>
                                )}
                              </button>

                              {task.status === "needs_review" && (
                                <div className="flex gap-1.5 border-t border-ink-700 px-3 py-1.5">
                                  <button
                                    type="button"
                                    onClick={() => void reviewTask(task.id, "approve")}
                                    className="rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider"
                                    style={{ borderColor: "var(--color-task-done)", color: "var(--color-task-done)" }}
                                  >
                                    approve
                                  </button>
                                  <RejectInline taskId={task.id} onReview={reviewTask} />
                                </div>
                              )}

                              <AnimatePresence initial={false}>
                                {openTrace === task.id && (
                                  <motion.div
                                    initial={{ height: 0, opacity: 0 }}
                                    animate={{ height: "auto", opacity: 1 }}
                                    exit={{ height: 0, opacity: 0 }}
                                    transition={{ duration: reduced ? 0.1 : 0.28, ease: [0.22, 1, 0.36, 1] }}
                                    className="overflow-hidden"
                                  >
                                    <div className="border-t border-ink-700 px-3 py-2">
                                      <TraceViewer taskId={task.id} />
                                    </div>
                                  </motion.div>
                                )}
                              </AnimatePresence>
                            </article>
                          </motion.li>
                        );
                      })}
                    </AnimatePresence>
                  </ul>
                )}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

function RejectInline({
  taskId,
  onReview,
}: {
  taskId: number;
  onReview: (id: number, decision: "approve" | "reject", feedback?: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState("");

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded border border-ink-600 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-parchment-faint"
      >
        changes
      </button>
    );
  }

  return (
    <div className="flex w-full flex-col gap-1.5 py-1">
      <label htmlFor={`board-feedback-${taskId}`} className="sr-only">
        What needs to change on task {taskId}
      </label>
      <textarea
        id={`board-feedback-${taskId}`}
        value={feedback}
        onChange={(event) => setFeedback(event.target.value)}
        rows={2}
        autoFocus
        placeholder="what needs to change"
        className="resize-y rounded border border-ink-600 bg-ink-900 px-2 py-1 text-[11px] text-parchment focus:outline-none"
      />
      <div className="flex gap-1.5">
        <button
          type="button"
          disabled={!feedback.trim()}
          onClick={async () => {
            await onReview(taskId, "reject", feedback.trim());
            setOpen(false);
            setFeedback("");
          }}
          className="rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider disabled:opacity-40"
          style={{ borderColor: "var(--color-state-waiting)", color: "var(--color-state-waiting)" }}
        >
          send back
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded border border-ink-600 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-parchment-faint"
        >
          cancel
        </button>
      </div>
    </div>
  );
}
