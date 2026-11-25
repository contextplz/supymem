export type DetectedLocalTool = {
  name: string;
  label: string;
  kind: string;
  installed: boolean;
  connected: boolean;
  signed_in?: boolean | null;
  coding_available?: boolean;
  app_path?: string | null;
  cli_path?: string | null;
};

export function localToolStatus(tool: DetectedLocalTool, opened = false): string {
  if (!tool.installed) return "Not found on this Mac";
  if (!tool.cli_path) {
    return `${tool.label} app found; coding CLI not found. Reviewed coding tasks need the CLI.`;
  }
  const location = tool.name === "codex" && /ChatGPT\.app/i.test(tool.cli_path)
    ? "Codex CLI found inside ChatGPT"
    : `${tool.label} CLI found`;
  if (tool.signed_in === true && !localToolCanStartBuilder(tool)) return `${location}; signed in; build connection not verified`;
  if (tool.signed_in === false) return `${location}; sign-in not confirmed`;
  if (tool.signed_in === true) return `${location}; signed in${tool.coding_available ? "; subscription login verified" : "; subscription login not verified"}`;
  return `${location}; ${opened ? "app opened; " : ""}CLI sign-in unverified`;
}

export function localToolNextStep(tool: DetectedLocalTool): string {
  if (tool.name === "cursor" && tool.installed && tool.cli_path && (tool.coding_available !== true || tool.signed_in !== true)) {
    return "Update Cursor and sign in with cursor-agent login in Terminal, then choose Check connection under Settings > Keys and connections > Coding tools.";
  }
  if (!tool.installed) return `Install ${tool.label}, then re-detect. Open Settings > Keys and connections > Coding tools to connect it.`;
  if (!tool.cli_path) {
    return `Install the ${tool.label} CLI and sign in there. Signing in to the app alone does not enable Supymem coding tasks. Then open Settings > Keys and connections > Coding tools and re-detect.`;
  }
  if (tool.name === "cursor") {
    return "In Settings > Keys and connections > Coding tools, allow Charlie to use Cursor. Then ask Charlie to build your app. Your request starts the local build; nothing is deployed. Check your usage limits in Cursor.";
  }
  if (tool.name === "opencode") {
    return tool.coding_available && tool.signed_in === true
      ? "In Settings > Keys and connections > Coding tools, allow Charlie to use OpenCode, then ask Charlie to build."
      : "Install or update to OpenCode 2.2.5 or newer in the 2.x series. Run opencode auth login in Terminal and choose a subscription login, not an API key. Then select Check connection in Settings > Keys and connections > Coding tools.";
  }
  if (tool.name === "claude") {
    if (tool.signed_in === false) return "Use Claude Code 2.1.284 or newer. In Terminal, run claude, then /login with your Claude subscription. Then open Settings > Keys and connections > Coding tools and re-detect. Your model API key does not connect Claude Code.";
    if (tool.signed_in === true && !tool.coding_available) return "Use a Claude subscription login and Claude Code 2.1.284 or newer. In Terminal, run claude, then /login. Then open Settings > Keys and connections > Coding tools and re-detect. Your model API key does not connect Claude Code.";
    if (tool.coding_available && tool.signed_in === true) return "In Settings > Keys and connections > Coding tools, allow Charlie to use Claude Code. Then ask Charlie to build your app. Your request starts the local build; nothing is deployed. Check your usage limits in Claude Code.";
    return "In Terminal, run claude, then /login with your Claude subscription. Use Claude Code 2.1.284 or newer. Then open Settings > Keys and connections > Coding tools and re-detect. Your model API key does not connect Claude Code.";
  }
  if (tool.name === "codex" && tool.signed_in === false) return "Use Codex CLI 0.138.0 or newer. In Terminal, run codex login with your ChatGPT subscription. Then open Settings > Keys and connections > Coding tools and re-detect. Your model API key does not connect Codex.";
  if (tool.name === "codex" && tool.signed_in === true && !tool.coding_available) return "Use a ChatGPT login and Codex CLI 0.138.0 or newer. In Terminal, run codex login. Then open Settings > Keys and connections > Coding tools and re-detect. Your model API key does not connect Codex.";
  if (tool.name === "codex" && tool.coding_available && tool.signed_in === true) return "In Settings > Keys and connections > Coding tools, allow Charlie to use Codex. Then ask Charlie to build your app. Your request starts the local build; nothing is deployed. Check your usage limits in Codex.";
  return "In Terminal, run codex login with your ChatGPT subscription. Use Codex CLI 0.138.0 or newer. Then open Settings > Keys and connections > Coding tools and re-detect. Your model API key does not connect Codex.";
}

export function localToolCanStartBuilder(tool: DetectedLocalTool | undefined): boolean {
  if (!tool?.installed) return false;
  return Boolean(tool.cli_path) && (["claude", "codex"].includes(tool.name)
    || ["cursor", "opencode"].includes(tool.name) && tool.signed_in === true && tool.coding_available === true);
}
