/**
 * Per-agent visual identity.
 *
 * The four shipped agents get hand-picked hues chosen to stay legible together
 * at low luminance. Anything else is derived from the key, because the roster
 * is meant to be swappable in config — a fifth agent should get a coherent desk
 * without a frontend change.
 */

export interface AgentTheme {
  hue: string;
  variant: 0 | 1 | 2 | 3;
}

const KNOWN: Record<string, AgentTheme> = {
  backend: { hue: "var(--color-agent-backend)", variant: 0 },
  database: { hue: "var(--color-agent-database)", variant: 1 },
  frontend: { hue: "var(--color-agent-frontend)", variant: 2 },
  devops: { hue: "var(--color-agent-devops)", variant: 3 },
};

/** Stable hash so an unknown agent keeps the same hue between reloads. */
function hash(key: string): number {
  let value = 0;
  for (let i = 0; i < key.length; i += 1) {
    value = (value * 31 + key.charCodeAt(i)) % 100_000;
  }
  return value;
}

export function themeFor(key: string): AgentTheme {
  const known = KNOWN[key];
  if (known) return known;

  // Restricted to warm-to-cool mid hues at fixed chroma and lightness, so a
  // generated colour cannot land somewhere that breaks the room's palette.
  const h = hash(key) % 360;
  return {
    hue: `oklch(0.78 0.12 ${h})`,
    variant: (hash(key) % 4) as 0 | 1 | 2 | 3,
  };
}
