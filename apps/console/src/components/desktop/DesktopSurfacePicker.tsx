"use client";
import { useEffect, useRef, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { desktopInvoke, desktopInvokeOrThrow } from "@/lib/desktop";
import { MAC_TASK_PERMISSION_EVENT, macTaskPermissionsGranted, type PermissionSnapshot } from "@/lib/macPermissions";
import { verifiedSessionEpoch } from "@/lib/client";
import { surfaceKey, validDesktopSurface, type ReviewedDesktopSurface, type SurfaceReviewReceipt } from "@/lib/desktopSurfaceReview";

type App = { name: string; bundle_id: string; pid: number; process_start_identity: string };
export function DesktopSurfacePicker({ ventureId, disabled, onChange }: {
  ventureId: string; disabled: boolean; onChange: (receipt: SurfaceReviewReceipt | null) => void;
}) {
  const epoch = verifiedSessionEpoch();
  const current = useRef({ventureId, epoch});
  if (current.current.ventureId !== ventureId || current.current.epoch !== epoch) current.current = {ventureId, epoch};
  const renderedOwner = current.current;
  const [apps, setApps] = useState<App[]>([]);
  const [windows, setWindows] = useState<ReviewedDesktopSurface[]>([]);
  const [selected, setSelected] = useState<ReviewedDesktopSurface[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    generation.current++; setApps([]); setWindows([]); setSelected([]); setEnabled(false); setLoading(false); onChange(null);
    // onChange is deliberately excluded: scope transitions invalidate custody.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId, epoch]);
  const alive = (owner: typeof current.current, token: number) => owner === current.current && token === generation.current
    && owner.ventureId === current.current.ventureId && owner.epoch === current.current.epoch
    && owner.epoch === verifiedSessionEpoch();
  async function load(bundleId?: string) {
    if (renderedOwner !== current.current || epoch !== verifiedSessionEpoch()) return;
    const owner = current.current, token = ++generation.current;
    setLoading(true); setError("");
    try {
      const permissions = await desktopInvoke<PermissionSnapshot>("permissions_status");
      if (!alive(owner, token)) return;
      if (!macTaskPermissionsGranted(permissions)) {
        window.dispatchEvent(new CustomEvent(MAC_TASK_PERMISSION_EVENT, { detail: { resume: async () => {
          if (alive(owner, token)) await load(bundleId);
        } } }));
        return;
      }
      const data = await desktopInvokeOrThrow<{items?: App[]; windows?: {window_id: number; title: string}[]}>(
        "desktop_app_inventory", bundleId ? {bundleId} : undefined);
      if (!alive(owner, token)) return;
      const found = (Array.isArray(data.items) ? data.items : []).filter(app => app && typeof app === "object" && typeof app.name === "string" && validDesktopSurface({
        application_bundle: app.bundle_id, process_id: app.pid, process_start_identity: app.process_start_identity,
        window_id: 1, window_title: "",
      }));
      if (bundleId) {
        const matches = found.filter(app => app.bundle_id === bundleId);
        if (matches.length !== 1) throw new Error("This app identity changed. Refresh the list and select it again.");
        const app = matches[0];
        const surfaces = (Array.isArray(data.windows) ? data.windows : []).map(window => ({
          application_bundle: app.bundle_id, process_id: app.pid, process_start_identity: app.process_start_identity,
          window_id: window.window_id, window_title: window.title,
        })).filter(validDesktopSurface);
        setWindows(surfaces);
        if (!surfaces.length) setError("No existing window is available. Open the app window, then refresh.");
      } else { setApps(found); setWindows([]); }
    } catch (err) { if (alive(owner, token)) setError(err instanceof Error ? err.message : "Could not read the selected app windows."); }
    finally { if (alive(owner, token)) setLoading(false); }
  }
  function change(surface: ReviewedDesktopSurface, checked: boolean) {
    if (disabled || loading || renderedOwner !== current.current || epoch !== verifiedSessionEpoch()) return;
    const next = checked ? [...selected, surface] : selected.filter(s => surfaceKey(s) !== surfaceKey(surface));
    if (next.length > 8) { setError("Select at most eight windows."); return; }
    setSelected(next); onChange({ventureId, epoch, surfaces: next});
  }
  return <fieldset disabled={disabled || loading} style={{marginTop: 12}}>
    <legend>Explicit window selection</legend>
    <label><input type="checkbox" checked={enabled} onChange={event => {
      if (renderedOwner !== current.current || epoch !== verifiedSessionEpoch()) return;
      setEnabled(event.target.checked); setSelected([]); setWindows([]); onChange(event.target.checked ? {ventureId, epoch, surfaces: []} : null);
      if (event.target.checked) void load();
    }} /> Choose several existing windows for a separate review</label>
    {enabled && <>
      <p className="muted">Multi-window execution is staged until the runtime supports this reviewed scope. Select exact windows yourself; mentioning an app in the task does not grant access. The first selected window is the primary window.</p>
      <button type="button" onClick={() => void load()}>Refresh running apps</button>
      <div>{apps.map(app => <button type="button" key={app.bundle_id + app.pid} onClick={() => void load(app.bundle_id)}>Show {app.name} windows</button>)}</div>
      <div>{windows.map(surface => <label key={surfaceKey(surface)} style={{display: "block"}}>
        <input type="checkbox" checked={selected.some(s => surfaceKey(s) === surfaceKey(surface))}
          onChange={event => change(surface, event.target.checked)} />
        {surface.application_bundle}: {surface.window_title || "Untitled"} (window {surface.window_id})
      </label>)}</div>
      {!!selected.length && <ol>{selected.map(surface => <li key={surfaceKey(surface)}>
        {surface.application_bundle}: {surface.window_title || "Untitled"}
        <button type="button" onClick={() => change(surface, false)}>Remove</button>
      </li>)}</ol>}
      <ErrorNotice error={error} source="DesktopSurfacePicker.error" />
    </>}
  </fieldset>;
}
