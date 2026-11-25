"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/client";
import { desktopInvoke, isDesktopShell } from "@/lib/desktop";
import { localToolStatus, type DetectedLocalTool } from "@/lib/localToolGuidance";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Agent = {
  name: string;
  binary: string;
  version: string;
  auth_mode: string;
  consented: boolean;
  local_tool?: DetectedLocalTool;
};

type Health = {
  coding_agents: Agent[];
  local_llm: {
    status_checked?: boolean;
    ollama: { ok: boolean; base_url: string; models: string[]; error?: string };
    lm_studio: { ok: boolean; base_url: string; models: string[]; error?: string };
    any_available: boolean;
  };
};

export function LocalToolsHealth() {
  const [data, setData] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function reload() {
    setBusy(true);
    setError(null);
    if (isDesktopShell()) {
      const tools = await desktopInvoke<DetectedLocalTool[]>("list_local_tools");
      if (Array.isArray(tools)) {
        setData({
          coding_agents: tools
            .filter((t) => t.installed)
            .map((t) => ({
              name: t.name,
              binary: t.cli_path || t.app_path || t.name,
              version: "detected",
              auth_mode: "unknown",
              consented: t.name === "hermes",
              local_tool: t,
            })),
          local_llm: {
            status_checked: false,
            ollama: { ok: false, base_url: "", models: [] },
            lm_studio: { ok: false, base_url: "", models: [] },
            any_available: false,
          },
        });
        setError(null);
        setBusy(false);
        return;
      }
      setError("Could not inspect tools on this Mac. Re-detect after reconnecting Supymem.");
      setData(null);
      setBusy(false);
      return;
    }
    const r = await api("/api/local-tools/health");
    if (!r.ok) {
      setError(r.error || `Couldn’t load local tools (${r.status})`);
      setData(null);
    } else {
      setData(r.data as Health);
    }
    setBusy(false);
  }

  useEffect(() => {
    void reload();
  }, []);

  return (
    <section className="panel-card" style={{ marginTop: "1rem" }}>
      <header style={{ display: "flex", alignItems: "baseline", gap: "1rem", flexWrap: "wrap" }}>
        <h3 style={{ margin: 0 }}>Local tools</h3>
        <button type="button" data-usage-control="settings_local_tools_redetect" onClick={() => void reload()} disabled={busy}>
          {busy ? "Checking…" : "Re-detect"}
        </button>
      </header>
      <p className="muted" style={{ marginTop: 8 }}>
        Claude Code and Codex sign-in is checked through their installed CLIs.
        Every coding run still needs your approval and local preflight. Detection does not prove
        a successful task. Cursor editing remains unavailable.
      </p>
      <ErrorNotice error={error} source="LocalToolsHealth.error" style={{ color: "var(--danger, #b33)" }} />
      {data && (
        <div style={{ display: "grid", gap: "1rem", marginTop: "0.75rem" }}>
          <div>
            <h4 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>Detected tools</h4>
            {data.coding_agents.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>
                No local coding tools were detected. Use the setup card on this page to install or re-detect them.
              </p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
                {data.coding_agents.map((a) => (
                  <li key={a.name} style={{ marginBottom: 6 }}>
                    <strong>{a.name === "hermes" ? "Supymem Computer Use" : a.name}</strong>
                    {a.name === "hermes" ? (
                      <span className="muted"> · installed; task availability is checked in its saved review</span>
                    ) : a.local_tool ? (
                      <span className="muted"> · {localToolStatus(a.local_tool)}</span>
                    ) : a.name.toLowerCase() === "codex" ? (
                      <span className="muted"> · detected; account access is checked when a reviewed coding task starts</span>
                    ) : a.name.toLowerCase() === "cursor" ? (
                      <span className="muted"> · read-only inspection adapter available; sign-in unverified; editing adapter pending</span>
                    ) : a.name.toLowerCase() === "claude" ? (
                      <span className="muted"> · detected; sign-in unverified; reviewed coding requires Claude Code 2.1.284 or newer</span>
                    ) : a.name.toLowerCase() === "opencode" ? (
                      <span className="muted"> · detected; sign-in unverified; reviewed background editing adapter pending</span>
                    ) : (
                      <span className="muted">
                        {" "}
                        · detected, not used by Supymem. You can still open that IDE yourself.
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <p className="muted" style={{ marginTop: 8 }}>
              Native Mac control is unavailable by default. Supported browser checks and coding tasks
              have their own reviews and checks; tool detection does not verify background control.
            </p>
            {(() => {
              const expected = [{ name: "hermes", label: "Supymem Computer Use" }];
              const missing = expected.filter(
                (item) => !data.coding_agents.some((a) => a.name === item.name),
              );
              if (!missing.length) return null;
              return (
                <p className="muted" style={{ marginTop: 8 }}>
                  Not installed yet:{" "}
                  {missing.map((item, i) => (
                    <span key={item.name}>
                      {i ? ", " : ""}
                      <strong>{item.label}</strong>
                    </span>
                  ))}
                  . Click Install on this page.
                </p>
              );
            })()}
          </div>
          <div>
            <h4 style={{ margin: "0 0 0.4rem", fontSize: "0.95rem" }}>Local LLM</h4>
            <p style={{ margin: 0 }} className="muted">
              Ollama:{" "}
              {data.local_llm.status_checked === false
                ? "not checked on this Mac"
                : data.local_llm.ollama.ok
                  ? `reachable (${data.local_llm.ollama.models.length} models)`
                  : "not reachable"}
              {" · "}
              LM Studio:{" "}
              {data.local_llm.status_checked === false
                ? "not checked on this Mac"
                : data.local_llm.lm_studio.ok
                  ? `reachable (${data.local_llm.lm_studio.models.length} models)`
                  : "not reachable"}
            </p>
            <p className="muted" style={{ marginTop: 6, fontSize: "0.85rem" }}>
              Tip: pick provider “Local (Ollama / LM Studio)” above and leave base URL empty for
              Ollama’s default.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
