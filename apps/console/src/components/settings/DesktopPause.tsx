"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, verifiedSessionEpoch } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { desktopExecutionAvailability, desktopExecutionMessage, type DesktopExecutionAvailability } from "@/lib/desktopExecutionAvailability";

export function DesktopPause({ ventureId, children }: { ventureId: string; children?: ReactNode }) {
  const serial = useRef(0);
  const epoch = verifiedSessionEpoch();
  const ownerRef = useRef({ ventureId, epoch });
  if (ownerRef.current.ventureId !== ventureId || ownerRef.current.epoch !== epoch) ownerRef.current = { ventureId, epoch };
  const renderedOwner = ownerRef.current;
  const [receipt, setReceipt] = useState<{ owner: typeof renderedOwner; status: DesktopExecutionAvailability } | null>(null);
  const availability = receipt?.owner === renderedOwner ? receipt.status : "unknown";
  const setAvailability = (status: DesktopExecutionAvailability) => setReceipt({ owner: renderedOwner, status });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path = `/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`;
  useEffect(() => {
    const refresh = () => {
    const generation = ++serial.current;
    setAvailability("unknown"); setBusy(false); setError("");
    void api(path).then((r) => {
      if (generation !== serial.current || renderedOwner !== ownerRef.current || renderedOwner.epoch !== verifiedSessionEpoch()) return;
      if (r.ok) setAvailability(desktopExecutionAvailability(r));
      else setError("Could not confirm this device's pause status. Refresh Settings before continuing.");
    });
    };
    refresh();
    window.addEventListener("supymem-session-changed", refresh);
    return () => { serial.current++; window.removeEventListener("supymem-session-changed", refresh); };
  }, [path, renderedOwner]);
  async function toggle() {
    if ((availability !== "available" && availability !== "paused") || busy) return;
    if (renderedOwner !== ownerRef.current || renderedOwner.epoch !== verifiedSessionEpoch()) return;
    const generation = ++serial.current;
    const requestedPause = availability === "available";
    setBusy(true); setError(""); setAvailability("unknown");
    const r = await api(path, { method: "PUT", body: JSON.stringify({ paused: requestedPause }) });
    if (generation !== serial.current || renderedOwner !== ownerRef.current || renderedOwner.epoch !== verifiedSessionEpoch()) return;
    if (r.ok) {
      // PUT saves the pause only; GET is the authority for execution availability.
      const status = await api(path);
      if (generation !== serial.current || renderedOwner !== ownerRef.current || renderedOwner.epoch !== verifiedSessionEpoch()) return;
      setAvailability(desktopExecutionAvailability(status));
      window.dispatchEvent(new Event("supymem-desktop-pause-changed"));
    }
    else setError("Could not save the Mac app control pause. Refresh and review its status.");
    setBusy(false);
  }
  return <><div className="panel-card" aria-label="Mac app control pause">
    <h3>Mac app control</h3>
    <p role="status">{desktopExecutionMessage(availability)}</p>
    <button type="button" disabled={(availability !== "available" && availability !== "paused") || busy} onClick={() => void toggle()}>
      {busy ? "Saving…" : availability === "paused" ? "Resume Mac app control" : availability === "available" ? "Pause Mac app control" : "Native app control unavailable"}
    </button>
    <ErrorNotice error={error} source="settings.desktop-pause.error" />
  </div>{availability !== "unknown" ? children : null}</>;
}
