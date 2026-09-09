/**
 * The desk component.
 *
 * Two things matter here beyond "it renders": every character is a real
 * focusable control with a name that carries what the pose and glow carry
 * visually, and the state actually changes when the status does.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AgentSummary } from "../../lib/api";
import type { AgentStatus } from "../../lib/events";
import { Desk } from "./Desk";

const AGENT: AgentSummary = {
  key: "backend",
  display_name: "Ada",
  role: "API and business logic",
  model: "claude-opus-5",
  avatar_id: "avatar-backend-01",
  status: "idle",
  status_changed_at: null,
  bio: "Ada writes the contract first.",
  personality: "Precise and contract-first.",
  owns: ["Endpoint design"],
  tools: ["read_file"],
  active_task_id: null,
  queued_tasks: 0,
};

function renderDesk(status: AgentStatus = "idle", onSelect = vi.fn()) {
  render(
    <Desk
      agent={AGENT}
      status={status}
      hue="var(--color-agent-backend)"
      variant={0}
      selected={false}
      onSelect={onSelect}
    />,
  );
  return onSelect;
}

describe("Desk", () => {
  it("is a button, not a clickable div", () => {
    renderDesk();
    expect(screen.getByRole("button")).toBeInTheDocument();
  });

  it("names the agent, the role and what it is doing", () => {
    renderDesk("working");
    const button = screen.getByRole("button");
    const name = button.getAttribute("aria-label") ?? "";
    expect(name).toContain("Ada");
    expect(name).toContain("API and business logic");
    expect(name).toContain("working");
    expect(name).toContain("running tools");
  });

  it("updates its name when the status changes", () => {
    const { rerender } = render(
      <Desk agent={AGENT} status="idle" hue="#fff" variant={0} selected={false} onSelect={vi.fn()} />,
    );
    expect(screen.getByRole("button").getAttribute("aria-label")).toContain("idle");

    rerender(
      <Desk
        agent={AGENT}
        status="waiting_on_human"
        hue="#fff"
        variant={0}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button").getAttribute("aria-label")).toContain("waiting on you");
  });

  it("exposes the status as data, so the room can be asserted on", () => {
    renderDesk("thinking");
    expect(screen.getByRole("button")).toHaveAttribute("data-status", "thinking");
  });

  it("reports selection with aria-pressed", () => {
    render(
      <Desk agent={AGENT} status="idle" hue="#fff" variant={0} selected onSelect={vi.fn()} />,
    );
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
  });

  it("calls back with the agent key when activated", async () => {
    const onSelect = renderDesk("idle");
    await userEvent.click(screen.getByRole("button"));
    expect(onSelect).toHaveBeenCalledWith("backend");
  });

  it("is reachable and activatable from the keyboard", async () => {
    const onSelect = renderDesk("idle");
    await userEvent.tab();
    expect(screen.getByRole("button")).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("backend");
  });

  it("shows the human-readable status on the nameplate", () => {
    renderDesk("blocked");
    expect(screen.getByText("blocked")).toBeInTheDocument();
  });

  it("hides the decorative artwork from assistive technology", () => {
    const { container } = render(
      <Desk agent={AGENT} status="idle" hue="#fff" variant={0} selected={false} onSelect={vi.fn()} />,
    );
    // The character is conveyed by the button's name; the SVG repeating it
    // would just make every desk read twice.
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });
});
