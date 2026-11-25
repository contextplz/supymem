"use client";

import { Fragment, useEffect, useState, type ReactNode } from "react";
import { usePathname, useSelectedLayoutSegments } from "next/navigation";
import { desktopListen, isDesktopShell, waitForDesktopShell } from "@/lib/desktop";
import { desktopDownloadLink, desktopLinkError, desktopVentureLinkAtLocation } from "@/lib/desktopVentureLink";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { isWebPortalPath } from "@/lib/webPortal";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";

export function DesktopProductBoundary({ children }: { children: ReactNode }) {
  const local = useLocalMode();
  const pathname = usePathname();
  const segments = useSelectedLayoutSegments();
  const authEntry = pathname === "/auth" || pathname.startsWith("/auth/");
  const browserEntry = isWebPortalPath(pathname) || segments.includes("_not-found");
  const [desktop, setDesktop] = useState<boolean | null>(null);
  const [appLink, setAppLink] = useState<string | null>(null);
  const [downloadLink, setDownloadLink] = useState<string | null>(null);
  const [launch, setLaunch] = useState<"ready" | "requested" | "blocked">("ready");
  const [linkError, setLinkError] = useState<string | null>(null);
  useEffect(() => {
    const refresh = () => {
      setAppLink(desktopVentureLinkAtLocation(window.location)
        || desktopVentureLinkAtLocation({ origin: window.location.origin, pathname: "/", search: "", hash: "" }));
      setDownloadLink(desktopDownloadLink(window.location.origin));
      setLaunch("ready");
      setLinkError(null);
    };
    refresh();
    window.addEventListener("popstate", refresh);
    window.addEventListener("hashchange", refresh);
    return () => { window.removeEventListener("popstate", refresh); window.removeEventListener("hashchange", refresh); };
  }, [pathname]);
  useEffect(() => {
    if (desktop !== true) return;
    let disposed = false;
    let stop: (() => void) | undefined;
    void desktopListen("supymem-link-error", value => {
      if (!disposed) setLinkError(desktopLinkError(value));
    }).then(unlisten => { if (disposed) unlisten?.(); else stop = unlisten; });
    return () => { disposed = true; stop?.(); };
  }, [desktop]);
  useEffect(() => {
    let pending: AbortController | null = null;
    const detect = () => {
      pending?.abort();
      if (isDesktopShell()) {
        setDesktop(true);
        return;
      }
      setDesktop(current => current === false ? false : null);
      pending = new AbortController();
      const signal = pending.signal;
      void waitForDesktopShell(1500, 100, signal).then(found => {
        if (!signal.aborted) setDesktop(found);
      });
    };
    const detectWhenVisible = () => {
      if (document.visibilityState === "visible") detect();
    };
    detect();
    window.addEventListener("supymem-desktop-ready", detect);
    window.addEventListener("pageshow", detect);
    window.addEventListener("focus", detect);
    document.addEventListener("visibilitychange", detectWhenVisible);
    return () => {
      pending?.abort();
      window.removeEventListener("supymem-desktop-ready", detect);
      window.removeEventListener("pageshow", detect);
      window.removeEventListener("focus", detect);
      document.removeEventListener("visibilitychange", detectWhenVisible);
    };
  }, [pathname]);
  if (local) return children;
  if ((pathname === "/auth/device" || pathname === "/auth/device/approve") && isDesktopShell()) return (
    <div className="login-gate">
      <main className="login-card">
        <h1>Approve this Mac in your browser</h1>
        <p>You approve Mac connections in your browser. Copy this address and open it in your browser to continue.</p>
        <label>Connection page address<input readOnly value={window.location.href} onFocus={event => event.target.select()} /></label>
        <p><a href="/">Return to Supymem</a></p>
      </main>
    </div>
  );
  if (desktop === null && !browserEntry) return <div role="status">Opening Supymem…</div>;
  if (desktop !== true && !browserEntry) return (
    <div className="login-gate">
      <main className="login-card">
        <p className="login-kicker">Supymem</p>
        <h1>Supymem runs on your Mac</h1>
        <p>Open the Supymem Mac app to work on your venture. Supymem is not available in your browser.</p>
        {appLink && <p><button type="button" className="primary" onClick={() => {
          const current = desktopVentureLinkAtLocation(window.location)
            || desktopVentureLinkAtLocation({ origin: window.location.origin, pathname: "/", search: "", hash: "" });
          if (!current) { setLaunch("blocked"); return; }
          try { window.location.assign(current); setLaunch("requested"); } catch { setLaunch("blocked"); }
        }}>{launch === "requested" ? "Open Supymem again" : "Open Supymem"}</button></p>}
        <p role="status" aria-live="polite" className="muted">{launch === "requested"
          ? "Launch requested. If your browser asks, allow Supymem to open. This page cannot confirm whether the app opened."
          : launch === "blocked" ? "Your browser could not launch this link. Open Supymem from Applications, or install it below."
          : "If Supymem does not open, open it from Applications or install it below."}</p>
        {downloadLink && <p><a href={downloadLink}>Install Supymem for Mac</a></p>}
      </main>
    </div>
  );
  return <Fragment key={authEntry ? "auth" : desktop ? "mac" : "web"}>
    <ErrorNotice error={linkError ?? ""} source="DesktopProductBoundary.ventureLink" />{children}
  </Fragment>;
}
