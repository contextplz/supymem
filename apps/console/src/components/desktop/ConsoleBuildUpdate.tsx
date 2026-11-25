"use client";

import { useEffect, useRef, useState } from "react";

export function readReloadScroll(scope: string | null) {
  try {
    const saved = JSON.parse(window.sessionStorage.getItem(`console.reload.scroll:${scope}`) || "null");
    return saved && Number.isFinite(saved.top) && saved.top >= 0
      && Number.isSafeInteger(saved.count) && saved.count >= 0
      && Number.isSafeInteger(saved.loaded) && saved.loaded >= 0
      ? saved as { top: number; count: number; loaded: number } : null;
  } catch { return null; }
}

export function ConsoleBuildUpdate() {
  const [available, setAvailable] = useState(false);
  const reload = useRef<() => void>(() => {});
  useEffect(() => {
    const current = process.env.NEXT_PUBLIC_CONSOLE_BUILD_ID;
    if (!current) return;
    let live = true;
    let request: AbortController | null = null;
    let version: string | null = null;
    let reloaded = false;
    let lastActivity = Date.now();
    let idleTimer: number | undefined;
    function attempt(manual = false) {
      if (!live || !version || reloaded || (!manual && !document.hidden && Date.now() - lastActivity < 20_000)) return;
      if (document.querySelector('[data-console-reload-blocked="true"], [aria-busy="true"], dialog[open], [role="dialog"]:not([hidden]), [role="alertdialog"]:not([hidden])')) return;
      const fields = document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | HTMLElement>('input:not([type="hidden"]):not([readonly]), textarea:not([readonly]), form select, [contenteditable="true"]');
      for (const field of fields) {
        if (field.closest('[hidden], [inert], details:not([open])')) continue;
        if (field instanceof HTMLInputElement && ["button", "submit", "reset", "checkbox", "radio", "range", "color"].includes(field.type)) {
          if (["checkbox", "radio"].includes(field.type) && field.checked !== field.defaultChecked) return;
          if (["range", "color"].includes(field.type) && field.value !== field.defaultValue) return;
          continue;
        }
        if (("value" in field ? String(field.value) : field.textContent || "").length) return;
      }
      try {
        const storage = window.sessionStorage;
        const key = `console.reloaded:${version}`;
        if (storage.getItem(key)) return;
        for (const element of document.querySelectorAll<HTMLElement>("[data-console-scroll]")) {
          storage.setItem(`console.reload.scroll:${element.dataset.consoleScroll}`, JSON.stringify({
            top: element.scrollTop, count: Number(element.dataset.consoleScrollCount || 0),
            loaded: Number(element.dataset.consoleScrollLoaded || 0),
          }));
        }
        storage.setItem(key, "1");
      } catch { return; }
      reloaded = true;
      window.location.reload();
    }
    reload.current = () => attempt(true);
    const activity = () => {
      lastActivity = Date.now();
      window.clearTimeout(idleTimer);
      if (version) idleTimer = window.setTimeout(attempt, 20_000);
    };
    const events = ["pointerdown", "pointermove", "keydown", "input", "change", "wheel", "touchstart"];
    for (const event of events) document.addEventListener(event, activity, true);
    async function check() {
      if (document.hidden || request) return;
      const controller = new AbortController();
      request = controller;
      const timeout = window.setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch("/api/health", { cache: "no-store", credentials: "omit", signal: controller.signal });
        if (!response.ok) return;
        const data: unknown = await response.json();
        const health = data as { ok?: unknown; buildId?: unknown } | null;
        if (live && health?.ok === true && typeof health.buildId === "string"
          && /^[a-zA-Z0-9._-]{1,128}$/.test(health.buildId)) {
          version = health.buildId !== current ? health.buildId : null;
          setAvailable(!!version);
          attempt();
          window.clearTimeout(idleTimer);
          if (version && Date.now() - lastActivity < 20_000) idleTimer = window.setTimeout(attempt, 20_000 - (Date.now() - lastActivity));
        }
      } catch { /* Offline, timed out and older servers keep the current app usable. */ }
      finally { window.clearTimeout(timeout); if (request === controller) request = null; }
    }
    const onVisible = () => { attempt(); void check(); };
    void check();
    // A founder who keeps the window focused never fires focus/visibility events; poll too.
    const timer = window.setInterval(onVisible, 5 * 60_000);
    const retry = window.setInterval(attempt, 30_000);
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      live = false;
      request?.abort();
      window.clearInterval(timer);
      window.clearInterval(retry);
      window.clearTimeout(idleTimer);
      reload.current = () => {};
      for (const event of events) document.removeEventListener(event, activity, true);
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  if (!available) return null;
  // Bottom-left sits over the sidebar, clear of the chat composer's Send button.
  return <div role="status" className="panel-card" style={{ position: "fixed", bottom: 16, left: 16,
    zIndex: "var(--z-toast)", width: "min(248px, calc(100vw - 32px))" }}>
    <p>New version available. Your console reloads automatically when your work is saved.</p>
    <button type="button" className="primary sm" onClick={() => reload.current()}>Reload</button>
  </div>;
}
