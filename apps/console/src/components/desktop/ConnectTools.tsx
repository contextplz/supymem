"use client";

import { useEffect, useState } from "react";
import { desktopInvoke, desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import { localToolNextStep, localToolStatus, type DetectedLocalTool } from "@/lib/localToolGuidance";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { isLocalConsole } from "../../lib/localMode";

export type LocalTool = DetectedLocalTool;

export function ConnectTools() {
  const desktop = typeof window !== "undefined" && isDesktopShell();
  const local = isLocalConsole();
  const [tools, setTools] = useState<LocalTool[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [opened, setOpened] = useState<string | null>(null);
  const [needsAccess, setNeedsAccess] = useState(false);

  async function loadControl() {
    const st = await desktopInvoke<{ ready?: boolean; message?: string }>("macos_control_status");
    setNeedsAccess(Boolean(st && st.ready === false));
  }

  async function reload() {
    const found = await desktopInvoke<LocalTool[]>("list_local_tools");
    if (Array.isArray(found)) setTools(found);
    else {
      setTools([]);
      setErr(local ? "You could not inspect your coding tools. Reopen Supymem Local, then open Settings > Keys and connections > Coding tools and re-detect." : "Could not inspect tools on this Mac. Re-detect after reconnecting Supymem.");
    }
  }

  useEffect(() => {
    if (!desktop) return;
    void reload();
    void loadControl();
  }, [desktop]);

  if (!desktop) {
    return (
      <>
        <section className="panel-card" style={{ marginBottom: 16 }}>
          <h3 style={{ margin: 0 }}>Find local coding tools</h3>
          <p className="muted" style={{ marginTop: 8 }}>
            Open the <strong>{local ? "Supymem Local Mac app" : "Supymem Mac app"}</strong> to detect tools on this computer. The
            website cannot see installed apps or verify your provider sign-in.
          </p>
        </section>
      </>
    );
  }

  const ides = tools.filter((t) => t.name !== "hermes");

  async function connect(name: string) {
    setErr("");
    setBusy(name);
    try {
      await desktopInvokeOrThrow<LocalTool>("connect_local_tool", { name });
      setOpened(name);
      await reload();
      await loadControl();
    } catch (e) {
      const message = e instanceof Error ? e.message : "Could not open that tool.";
      setErr(local ? "Open Settings > Keys and connections > Coding tools, sign in to your selected tool and re-detect before retrying your build." : message);
      if (name === "cursor" && /Accessibility|Automation|System Settings/i.test(message)) {
        if (local) setErr("Turn on Supymem Local in System Settings > Privacy & Security > Accessibility, then open Settings > Keys and connections > Coding tools and re-detect.");
        setNeedsAccess(true);
        void desktopInvoke("show_permission_coach", { tool: name });
      }
    } finally {
      setBusy(null);
    }
  }

  async function openSetup() {
    setErr("");
    try {
      await desktopInvokeOrThrow("open_setup_window");
    } catch (e) {
      setErr(local ? "Open Settings > Keys and connections > Coding tools to connect your coding tool, then retry your build."
        : e instanceof Error ? e.message : "Could not open the Mac setup window.");
    }
  }

  return (
    <>
      <section className="panel-card" style={{ marginBottom: 16 }}>
        <h3 style={{ margin: 0 }}>Coding tools on this Mac</h3>
        <p className="muted" style={{ marginTop: 8 }}>
          {local ? "To build, connect Claude Code, Codex, Cursor or OpenCode in Settings > Keys and connections > Coding tools. Your request starts work after you allow the tool and your subscription login is verified. Sign-in is checked through the CLI without storing your account details. Opening an app does not approve a coding task." : "Supymem can use your signed-in Claude Code or Codex CLI to build a small app on this Mac. You review the plan, approve the coding run and approve each deployment separately. Sign-in is checked through the CLI without storing your account details. Opening an app does not approve a coding task. Cursor editing is unavailable."}
        </p>
        {needsAccess ? (
          <p className="muted" style={{ marginTop: 8 }}>
            Turn on <strong>{local ? "Supymem Local" : "Supymem"}</strong> in the list, then open Cursor again.
          </p>
        ) : null}
        <div
          style={{
            display: "grid",
            gap: 10,
            marginTop: 16,
            gridTemplateColumns: "repeat(auto-fit, minmax(11rem, 1fr))",
          }}
        >
          {ides.map((tool) => {
            return (
              <div
                key={tool.name}
                style={{ padding: "14px", minHeight: 88, textAlign: "left", border: "1px solid var(--line-subtle)", borderRadius: 12 }}
              >
                <strong>{tool.label}</strong>
                <div style={{ fontWeight: 500, opacity: 0.85, marginTop: 6, fontSize: "0.85rem" }}>
                  {busy === tool.name
                    ? "Opening…"
                    : tool.name === "cursor" && needsAccess
                      ? "Needs Accessibility, Settings will open"
                      : localToolStatus(tool, opened === tool.name)}
                </div>
                {tool.installed && (tool.cli_path || tool.app_path) ? <button type="button" onClick={() => void connect(tool.name)} disabled={busy === tool.name} style={{ marginTop: 8 }}>
                  Check {tool.label} connection
                </button> : null}
                <p className="muted" style={{ margin: "8px 0 0", fontSize: "0.8rem" }}>{localToolNextStep(tool)}</p>
                {tool.installed && tool.cli_path ? <small className="muted">CLI: <code>{tool.cli_path}</code></small> : null}
              </div>
            );
          })}
        </div>
        <button type="button" onClick={() => void reload()} disabled={busy !== null} style={{ marginTop: 12 }}>
          Re-detect coding tools
        </button>
        <button type="button" onClick={() => void openSetup()} style={{ marginTop: 12 }}>
          Open Mac setup window
        </button>
        {err ? (
          <ErrorNotice error={err} style={{ color: "var(--danger, #b33)", marginTop: 10 }} source="connecttools.mac_setup">
            {err}{" "}
            {/Accessibility|Automation|System Settings/i.test(err) ? <button
              type="button"
              onClick={() => void desktopInvoke("open_macos_privacy_pane", { pane: "accessibility" })}
              style={{ marginLeft: 8 }}
            >
              Open Accessibility
            </button> : null}
          </ErrorNotice>
        ) : null}
      </section>
    </>
  );
}
