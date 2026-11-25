import type { CSSProperties } from "react";

const PALETTE = [
  "#6b2fa0",
  "#0b8fc7",
  "#1a9a55",
  "#c45a0a",
  "#c43b32",
  "#8a8a2a",
  "#8b4cb8",
  "#2f6f8f",
];

const SKIN = ["#f3d0b0", "#e0b089", "#c68642", "#8d5524", "#f6ddcc", "#d1a37a"];
const HAIR = ["#1f1a17", "#4a2c14", "#8a5a2b", "#2c3a6b", "#6b2fa0", "#222"];
const SHIRT = ["#7a3aad", "#1f6f8f", "#1a7a4c", "#c45a0a", "#3d4a8a", "#8a2f4a"];

function hashSeed(seed: string): number {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash * 31 + seed.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

export function prettyRole(role: string): string {
  return role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function prettyName(name: string): string {
  return name.replace(/_/g, " ");
}

export type AgentActivity = {
  key: "shipping" | "plotting" | "talking" | "stuck" | "lurking" | "idle";
  label: string;
};

export function agentActivity(status?: string | null): AgentActivity {
  const s = (status || "").toLowerCase();
  if (s === "running") return { key: "shipping", label: "shipping" };
  if (s === "active") return { key: "talking", label: "on the roster" };
  if (s === "paused") return { key: "stuck", label: "paused" };
  if (s === "quarantined" || s === "shadow") return { key: "lurking", label: "watching" };
  if (s === "ready" || s === "draft" || s === "pending") return { key: "idle", label: "ready" };
  if (s === "retired") return { key: "lurking", label: "retired" };
  return { key: "lurking", label: s ? s.replace(/_/g, " ") : "idle" };
}

type PortraitBits = {
  bg: string;
  skin: string;
  hair: string;
  shirt: string;
  cells: boolean[];
};

function portraitBits(seed: string): PortraitBits {
  const h = hashSeed(seed || "agent");
  const cells: boolean[] = [];
  for (let i = 0; i < 16; i += 1) {
    cells.push(((h >> i) & 1) === 1);
  }
  return {
    bg: PALETTE[(h >> 3) % PALETTE.length],
    skin: SKIN[(h >> 5) % SKIN.length],
    hair: HAIR[(h >> 7) % HAIR.length],
    shirt: SHIRT[(h >> 9) % SHIRT.length],
    cells,
  };
}

/** Original 8×8 employee portrait — identicon face, not a copied sprite. */
export function EmployeePortrait({
  seed,
  size = 24,
  className,
}: {
  seed: string;
  size?: number;
  className?: string;
}) {
  const p = portraitBits(seed);
  const cell = (x: number, y: number, fill: string) => (
    <rect key={`${x}-${y}-${fill}`} x={x} y={y} width={1} height={1} fill={fill} />
  );
  const hairOn = p.cells[0] || p.cells[1];
  const bangs = p.cells[2];
  const smile = p.cells[3];
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 8 8"
      aria-hidden
      style={{ borderRadius: 7, display: "block", flexShrink: 0 } as CSSProperties}
    >
      <rect width="8" height="8" fill={p.bg} />
      {hairOn && (
        <>
          {cell(1, 0, p.hair)}
          {cell(2, 0, p.hair)}
          {cell(3, 0, p.hair)}
          {cell(4, 0, p.hair)}
          {cell(5, 0, p.hair)}
          {cell(6, 0, p.hair)}
          {cell(1, 1, p.hair)}
          {cell(6, 1, p.hair)}
          {bangs && cell(2, 1, p.hair)}
          {bangs && cell(5, 1, p.hair)}
        </>
      )}
      {cell(2, 2, p.skin)}
      {cell(3, 2, p.skin)}
      {cell(4, 2, p.skin)}
      {cell(5, 2, p.skin)}
      {cell(2, 3, p.skin)}
      {cell(3, 3, p.skin)}
      {cell(4, 3, p.skin)}
      {cell(5, 3, p.skin)}
      {cell(2, 4, p.skin)}
      {cell(3, 4, p.skin)}
      {cell(4, 4, p.skin)}
      {cell(5, 4, p.skin)}
      {cell(3, 3, "#1c1917")}
      {cell(5, 3, "#1c1917")}
      {smile ? cell(3, 5, "#7a3a32") : cell(4, 5, "#7a3a32")}
      {smile && cell(4, 5, "#7a3a32")}
      {cell(2, 6, p.shirt)}
      {cell(3, 6, p.shirt)}
      {cell(4, 6, p.shirt)}
      {cell(5, 6, p.shirt)}
      {cell(1, 7, p.shirt)}
      {cell(2, 7, p.shirt)}
      {cell(3, 7, p.shirt)}
      {cell(4, 7, p.shirt)}
      {cell(5, 7, p.shirt)}
      {cell(6, 7, p.shirt)}
    </svg>
  );
}

const SPEAKER_MARK = /^<!--employee:([^:]+):([^:]*):([^>]*)-->\s*/i;

export function parseEmployeeStamp(body: string): {
  speaker: { name: string; title: string; id: string } | null;
  text: string;
} {
  const match = body.match(SPEAKER_MARK);
  if (!match) return { speaker: null, text: body };
  return {
    speaker: { name: match[1], title: match[2], id: match[3] },
    text: body.slice(match[0].length),
  };
}
