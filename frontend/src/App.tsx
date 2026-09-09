/**
 * The shell.
 *
 * Two views share one room: the team room, which is the point, and the task
 * board, which is where the work is actually managed. The header carries the
 * things that must always be true — who you are watching, whether the stream is
 * live, and what it is costing.
 */

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useState } from "react";

import { ConnectionPill } from "./components/ConnectionPill";
import { CostMeter } from "./components/CostMeter";
import { AgentPanel } from "./components/AgentPanel";
import { BubbleDialog } from "./components/BubbleDialog";
import { TaskBoard } from "./scenes/TaskBoard";
import { TeamRoom } from "./scenes/TeamRoom";
import { useTeamStore } from "./store/useTeamStore";

type View = "room" | "board";

export function App() {
  const [view, setView] = useState<View>("room");
  const bootstrap = useTeamStore((s) => s.bootstrap);
  const connect = useTeamStore((s) => s.connect);
  const disconnect = useTeamStore((s) => s.disconnect);
  const connection = useTeamStore((s) => s.connection);
  const attempt = useTeamStore((s) => s.reconnectAttempt);
  const sessionCostUsd = useTeamStore((s) => s.sessionCostUsd);
  const sessionTokens = useTeamStore((s) => s.sessionTokens);
  const selectedTask = useTeamStore((s) => s.selectedTask);
  const tasks = useTeamStore((s) => s.tasks);
  const error = useTeamStore((s) => s.error);

  useEffect(() => {
    void bootstrap();
    connect();
    return () => disconnect();
  }, [bootstrap, connect, disconnect]);

  const activeTask = selectedTask !== null ? tasks[selectedTask] : undefined;

  return (
    <div className="flex h-full flex-col">
      <header className="relative z-40 flex shrink-0 items-center justify-between gap-4 border-b border-ink-700 bg-ink-900/80 px-4 py-3 backdrop-blur md:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="font-display text-2xl leading-none tracking-tight text-parchment md:text-[1.75rem]">
            AgentTeam
          </h1>
          <span className="hidden font-display text-lg italic text-parchment-faint sm:block">
            the studio
          </span>
        </div>

        <nav aria-label="Views" className="flex items-center gap-1 rounded-full border border-ink-700 bg-ink-850/60 p-1">
          {(["room", "board"] as const).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => setView(key)}
              aria-current={view === key ? "page" : undefined}
              className="relative rounded-full px-3 py-1 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors"
              style={{ color: view === key ? "var(--color-ink-900)" : "var(--color-parchment-faint)" }}
            >
              {view === key && (
                <motion.span
                  layoutId="view-pill"
                  className="absolute inset-0 rounded-full bg-parchment"
                  transition={{ type: "spring", stiffness: 380, damping: 32 }}
                />
              )}
              <span className="relative">{key === "room" ? "team room" : "task board"}</span>
            </button>
          ))}
        </nav>

        <div className="flex items-center gap-4">
          <CostMeter
            sessionCostUsd={sessionCostUsd}
            sessionTokens={sessionTokens}
            taskCostUsd={activeTask ? activeTask.costUsd : null}
            taskLabel={activeTask ? `task ${activeTask.id}` : null}
          />
          <ConnectionPill state={connection} attempt={attempt} />
        </div>
      </header>

      {error && (
        <div
          role="alert"
          className="shrink-0 border-b border-ink-700 px-4 py-2 font-mono text-[11px] md:px-6"
          style={{ color: "var(--color-state-error)", background: "color-mix(in oklab, var(--color-state-error) 8%, transparent)" }}
        >
          {error}
        </div>
      )}

      <main className="relative flex-1 overflow-hidden">
        <AnimatePresence mode="wait">
          <motion.div
            key={view}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
            className="absolute inset-0"
          >
            {view === "room" ? <TeamRoom /> : <TaskBoard />}
          </motion.div>
        </AnimatePresence>
      </main>

      <AgentPanel />
      <BubbleDialog />
    </div>
  );
}
