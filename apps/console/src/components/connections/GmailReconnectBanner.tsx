"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { connectHubProvider } from "@/lib/opsApi";
import { desktopInvokeOrThrow, isDesktopShell } from "@/lib/desktop";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export function GmailReconnectBanner({ workspaceId, ventureId }: { workspaceId: string; ventureId: string }) {
  const scope = `${workspaceId}:${ventureId}:${verifiedSessionSubject()}:${verifiedSessionEpoch()}`;
  const current = useRef(scope); current.current = scope;
  const [receipt, setReceipt] = useState<{ scope: string; needsReconnect: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = () => current.current === scope
    && scope === `${workspaceId}:${ventureId}:${verifiedSessionSubject()}:${verifiedSessionEpoch()}`;
  useEffect(() => {
    let alive = true;
    const load = async () => {
      const result = await api(`/api/api/v1/connections/gmail/status?workspace_id=${encodeURIComponent(workspaceId)}`);
      if (alive && active() && result.ok) {
        setReceipt({ scope, needsReconnect: (result.data as { needs_reconnect?: boolean })?.needs_reconnect === true });
      }
    };
    void load();
    setBusy(false); setError("");
    window.addEventListener("focus", load);
    return () => { alive = false; window.removeEventListener("focus", load); };
  }, [scope, workspaceId]);
  if (receipt?.scope !== scope || !receipt.needsReconnect) return null;
  async function reconnect() {
    setBusy(true); setError("");
    try {
      const result = await connectHubProvider("gmail", { return_to: `/v/${ventureId}` }, workspaceId);
      if (!active()) return;
      const url = result.redirect_url || result.authorize_url;
      if (!url) throw new Error("Authorization link unavailable.");
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || parsed.username || parsed.password) throw new Error("Authorization link unavailable.");
      if (isDesktopShell() && parsed.hostname === "connect.composio.dev" && parsed.pathname.startsWith("/link/")) {
        await desktopInvokeOrThrow("open_composio_connect_link", { url });
      } else window.location.assign(url);
    } catch {
      if (active()) setError("Your connection could not start. Try Reconnect again.");
    } finally {
      if (active()) setBusy(false);
    }
  }
  return <section role="status" aria-label="Gmail reconnect required">
    <p>Reconnect your Gmail to keep your emails going</p>
    <button type="button" className="primary" disabled={busy} onClick={() => void reconnect()}>{busy ? "Connecting..." : "Reconnect"}</button>
    <ErrorNotice error={error} source="GmailReconnectBanner.reconnect" />
  </section>;
}
