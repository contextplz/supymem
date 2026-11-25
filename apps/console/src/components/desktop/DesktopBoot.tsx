"use client";

import { chatNavigation } from "@/lib/chatRouteAction";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { desktopInvoke, desktopListen, waitForDesktopShell } from "@/lib/desktop";
import { externalUrl } from "@/lib/externalLink";

/** Inject gateway URL, track the open venture, deep-link into the SPA. */
export function DesktopBoot() {
  const pathname = usePathname();
  const router = chatNavigation(useRouter());
  const [native, setNative] = useState(false);

  useEffect(() => {
    let pending: AbortController | undefined;
    const detect = () => {
      const started = Date.now();
      pending?.abort();
      pending = new AbortController();
      const signal = pending.signal;
      void waitForDesktopShell(30000, 100, signal).then(found => {
        if (!found || signal.aborted) return;
        console.info("desktop_startup", { phase: "shell_detected", ms: Date.now() - started });
        setNative(true);
        window.dispatchEvent(new Event("supymem-desktop-ready"));
      });
    };
    detect();
    window.addEventListener("focus", detect);
    window.addEventListener("pageshow", detect);
    return () => {
      pending?.abort();
      window.removeEventListener("focus", detect);
      window.removeEventListener("pageshow", detect);
    };
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined" && window.__SUPYMEM__?.gatewayUrl) {
      sessionStorage.setItem("cos_gateway_url", window.__SUPYMEM__.gatewayUrl.replace(/\/$/, ""));
    }
    if (!native) return;
    let disposed = false;
    let unsub: (() => void) | undefined;
    (async () => {
      const gw = await desktopInvoke<string>("gateway_url");
      if (gw && typeof window !== "undefined") {
        sessionStorage.setItem("cos_gateway_url", gw.replace(/\/$/, ""));
      }
      if (disposed) return;
      unsub = await desktopListen("backend-ready", (payload) => {
        if (typeof payload === "string") {
          sessionStorage.setItem("cos_gateway_url", payload.replace(/\/$/, ""));
          window.dispatchEvent(new Event("supymem-reconnect"));
        }
      });
      if (disposed) { unsub(); return; }
      const open = await desktopListen("companyos-open", (payload) => {
        const path = typeof payload === "string" ? payload : "";
        if (path.startsWith("/")) router.push(path);
      });
      const prev = unsub;
      unsub = () => {
        prev?.();
        open();
      };
      if (disposed) unsub();
    })();
    return () => {
      disposed = true;
      unsub?.();
    };
  }, [router, native]);

  // Chat links, citations and target=_blank anchors must never replace the app window.
  useEffect(() => {
    if (!native) return;
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented) return;
      const a = (e.target as Element | null)?.closest?.("a[href]");
      const url = a && externalUrl(a.getAttribute("href"), window.location.origin);
      if (!url) return;
      e.preventDefault();
      void desktopInvoke("plugin:shell|open", { path: url });
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, [native]);

  useEffect(() => {
    if (!native || pathname === "/suggestions/overlay") return;
    const match = pathname.match(/^\/v\/([^/]+)/);
    void desktopInvoke("set_active_venture", { ventureId: match?.[1] || "" });
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") void desktopInvoke("set_active_venture", { ventureId: match?.[1] || "", collapseSuggestions: true });
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [pathname, native]);

  return null;
}
