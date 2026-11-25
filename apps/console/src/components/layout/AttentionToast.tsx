"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { useEffect, useState } from "react";
import { UI_ATTENTION_EVENT, type AttentionKind } from "@/lib/uiAttention";
import { nativePermissionsSupported } from "@/lib/macPermissions";
import { useLocalMode } from "./LocalModeBoundary";
import styles from "./AttentionToast.module.css";

export function AttentionToast({ ventureId }: { ventureId?: string }) {
  const local = useLocalMode();
  const [kind, setKind] = useState<AttentionKind | null>(null);
  useEffect(() => {
    let disposed = false;
    const show = (event: Event) => {
      const next = (event as CustomEvent<{ kind?: AttentionKind }>).detail?.kind;
      if (next === "provider_connection" || next === "mac_permission") setKind(next);
    };
    const showLocal = async (event: Event) => {
      const next = (event as CustomEvent<{ kind?: AttentionKind }>).detail?.kind;
      if (next === "mac_permission" && (!/Mac/i.test(navigator.platform) || !(await nativePermissionsSupported()))) return;
      if (!disposed) show(event);
    };
    const listener = local ? showLocal : show;
    window.addEventListener(UI_ATTENTION_EVENT, listener);
    return () => { disposed = true; window.removeEventListener(UI_ATTENTION_EVENT, listener); };
  }, [local]);
  if (local && !kind) return null;
  const provider = kind === "provider_connection";
  const href = provider
    ? ventureId ? `/v/${encodeURIComponent(ventureId)}/connections` : "/connections"
    : ventureId ? `/v/${encodeURIComponent(ventureId)}/settings#settings-computer-use` : "/ops/settings";
  return <aside className={styles.toast} hidden={!kind} inert={!kind} role="status" aria-label="Supymem needs a setup action">
    <strong>{provider ? "Connect a service to continue" : "Allow Mac access to continue"}</strong>
    <p>{provider
      ? "This action needs a connected account or API key. Supymem has paused it until setup is complete."
      : "This action needs a macOS permission. Review the requested access before trying again."}</p>
    <div className={styles.actions}>
      <ChatAnchor className="primary" href={href}>{provider ? "Open Connections" : "Open Mac settings"}</ChatAnchor>
      <button type="button" onClick={() => setKind(null)}>Dismiss</button>
    </div>
  </aside>;
}
