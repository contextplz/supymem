import type { DetectedLocalTool } from "./localToolGuidance.ts";

export const CODING_AGENTS = [
  { name: "claude", label: "Claude Code", plan: "Claude" },
  { name: "codex", label: "Codex", plan: "ChatGPT" },
  { name: "cursor", label: "Cursor", plan: "Cursor" },
  { name: "opencode", label: "OpenCode", plan: "connected provider" },
] as const;

export const CODING_AGENT_USES = "build a small tool for your experiment, fix your landing page, or write a data scraper";
export const OPEN_MAC_APP = "Open or update your Supymem desktop app, then open Settings > Keys and connections > Coding tools and re-detect.";

export type CodingAgentState = "Not installed" | "Installed" | "Signed in" | "Ready" | "Builds unavailable";

export function codingAgentState(tool: DetectedLocalTool): CodingAgentState {
  if (!tool.installed) return "Not installed";
  if (tool.name === "cursor" && tool.coding_available !== true) return "Builds unavailable";
  if (CODING_AGENTS.some(agent => agent.name === tool.name) && tool.cli_path && tool.signed_in === true && tool.coding_available === true) return "Ready";
  if (tool.signed_in === true) return "Signed in";
  return "Installed";
}

/** Tools from list_local_tools, or null when this Mac app does not report sign-in status (web, older Macs). */
export function macReportedTools(value: unknown): Record<string, DetectedLocalTool> | null {
  if (!Array.isArray(value)) return null;
  const found: Record<string, DetectedLocalTool> = {};
  for (const agent of CODING_AGENTS) {
    const tool = value.find(item => item && typeof item === "object" && item.name === agent.name);
    if (!tool && agent.name === "opencode") {
      found[agent.name] = { name: agent.name, label: agent.label, kind: "ide", installed: false,
        connected: false, signed_in: null, coding_available: false, cli_path: null };
      continue;
    }
    if (!tool || typeof tool.installed !== "boolean" || !("signed_in" in tool) ||
      (tool.signed_in !== null && typeof tool.signed_in !== "boolean") || typeof tool.coding_available !== "boolean") return null;
    found[agent.name] = tool as DetectedLocalTool;
  }
  return found;
}
