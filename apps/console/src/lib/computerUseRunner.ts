import { desktopInvoke, desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import { acpLine, extractAcpSessionId, parseControlChunk } from "@/lib/codingControl";

type LocalCli = { name: string; bin: string; path: string };

async function waitFor(slot: { current: string | null }, ms: number): Promise<string | null> {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (slot.current) return slot.current;
    await new Promise((r) => setTimeout(r, 80));
  }
  return slot.current;
}

/** Start Supymem Computer Use with a prompt. No founder picker. */
export async function startComputerUse(
  ventureId: string,
  prompt: string,
): Promise<{ started: boolean; detail: string }> {
  if (!isDesktopShell()) return { started: false, detail: "Supymem for Mac is required." };
  const text = prompt.trim();
  if (!text) return { started: false, detail: "The computer-use job had no instructions." };
  const found = await desktopInvoke<LocalCli[]>("list_local_clis");
  const ready = Array.isArray(found) && found.some((c) => c.name === "hermes");
  if (!ready) return { started: false, detail: "Supymem Computer Use is not installed." };

  const id = await desktopInvokeOrThrow<string>("spawn_coding_control", {
    provider: "hermes",
    ventureId,
    autoApprove: true,
    mode: "acp",
  });
  let rpc = 1;
  const send = async (method: string, params: unknown) => {
    const n = rpc++;
    await desktopInvokeOrThrow("stdio_write", { id, data: acpLine(n, method, params) });
  };
  await send("initialize", {
    protocolVersion: 1,
    clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
    clientInfo: { name: "supymem-desktop", version: "0.1.0" },
  });
  let cwd = ".";
  try {
    const path = await desktopInvoke<string>("venture_workspace_path", { ventureId });
    if (path && path.trim()) cwd = path.trim();
  } catch {
    /* keep process cwd */
  }
  await send("session/new", { cwd, mcpServers: [] });
  const slot: { current: string | null } = { current: null };
  const unsub = await import("@/lib/desktop").then((m) =>
    m.desktopListen("stdio-output", (payload) => {
      const rec = payload as { id?: string; data?: string };
      if (rec.id && rec.id !== id) return;
      const chunk = rec.data || "";
      const parsed = parseControlChunk("hermes", chunk);
      slot.current = parsed.sessionId || extractAcpSessionId(chunk) || slot.current;
    }),
  );
  const sid = await waitFor(slot, 45_000);
  unsub();
  if (!sid) throw new Error("Supymem Computer Use did not open a session within 45 seconds.");
  await desktopInvokeOrThrow("stdio_write", {
    id,
    data: acpLine(rpc++, "session/prompt", {
      sessionId: sid,
      prompt: [{ type: "text", text }],
    }),
  });
  return { started: true, detail: `Computer Use session ${sid} started.` };
}
