import { UI_DIAGNOSTIC_EVENT } from "./uiDiagnostics.ts";

/** Every error a founder can SEE (inline text, banner, toast, voice) is reported through reportUserError:
 *  what they saw (scrubbed), where it came from (source), what they were doing (intent) and when.
 *  It rides the same UI_DIAGNOSTIC_EVENT -> UsageTracker -> /api/telemetry ui.error path as api() failures. */
export type UserErrorInput = {
  source: string;               // component/page, e.g. "tasks.run", "voice.realtime"
  message: string;              // the text the user saw
  intent?: string | null;       // what they were trying to do, e.g. "task_run"; defaults to the last control they used
  kind?: "ui" | "voice";
  severity?: "error" | "transient"; // transient: recorded, excluded from user-facing error counts
  serverCode?: string | null;   // machine code (service or realtime), never a message
  status?: number | null;
  requestId?: string | null;    // request/response/event id when present
};
export type UserErrorDetail = { code: "ui_error" | "voice_error"; context: "ui" | "voice"; text: string; source: string;
  intent: string; severity?: "transient"; serverCode?: string; status?: number; requestId?: string; occurredAt: string };

const MAX_TEXT = 300;
const DEDUPE_MS = 5_000;
const INTENT_MS = 15_000;

/** Emails and credential-shaped strings never leave the device. */
export function scrubErrorText(value: unknown, max = MAX_TEXT): string {
  const text = typeof value === "string" ? value : value instanceof Error ? value.message : "";
  return text
    .replace(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g, "[email]")
    .replace(/\b(?:Bearer|Basic)\s+[\w.~+/=-]{8,}/gi, "[token]")
    .replace(/\b(?:sk|pk|rk|ek|ghp|gho|ghs|xox[a-z]|AKIA)[-_][\w-]{8,}/g, "[token]")
    .replace(/\beyJ[\w-]{8,}\.[\w-]{4,}\.[\w-]*/g, "[token]")
    .replace(/\b(?:api[_-]?key|token|secret|password|passwd|authorization)(["']?\s*[:=]\s*["']?)[^\s"',;]{4,}/gi, "$1[token]")
    .replace(/\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/_-]{40,}={0,2}/g, "[token]")
    .replace(/\s+/g, " ").trim().slice(0, max);
}

/** Closed-shape label for source/intent so one bad caller cannot put free text in a dimension column. */
export function slug(value: unknown, fallback: string): string {
  const s = String(value ?? "").toLowerCase().replace(/[^a-z0-9_.-]+/g, "_").replace(/^[^a-z]+/, "").slice(0, 64);
  return s || fallback;
}

/** Route template for the page the error showed on: ids become {id}; no query or hash. */
export function templateRoute(pathname: string): string {
  const path = pathname.split(/[?#]/, 1)[0] || "/";
  const out = path.split("/").map((seg, i, all) =>
    seg && (all[i - 1] === "v" || /^(?:[0-9a-f]{8}-[0-9a-f-]{27}|dwo_[\w-]+|[0-9a-f]{12,}|(?=[^\d]*\d)[A-Za-z0-9_-]{10,})$/i.test(seg)) ? "{id}" : seg).join("/");
  return out.replace(/[^A-Za-z0-9_{}./-]/g, "_").slice(0, 120) || "/";
}

const recent = new Map<string, number>();
let lastControl: { id: string; at: number } | null = null;

export function wasRecentlyReported(message: string, now = Date.now()): boolean {
  const t = recent.get(scrubErrorText(message));
  return t !== undefined && now - t < 60_000;
}
/** Test hook. */
export function resetUserErrorState() { recent.clear(); lastControl = null; }

export function noteIntent(id: string | null | undefined, at = Date.now()) { if (id) lastControl = { id, at }; }
export function currentIntent(now = Date.now()): string {
  return lastControl && now - lastControl.at < INTENT_MS ? lastControl.id : "unknown";
}

/** Returns true when an event was dispatched (false: empty, duplicate within 5s, or no window). */
export function reportUserError(input: UserErrorInput, now = Date.now()): boolean {
  if (typeof window === "undefined") return false;
  const text = scrubErrorText(input.message);
  if (!text) return false;
  const source = slug(input.source, "ui");
  // Keyed by the text alone: the hook, the voice path and the DOM observer can all see the same message.
  const prev = recent.get(text);
  if (prev !== undefined && now - prev < DEDUPE_MS) return false;
  recent.set(text, now);
  if (recent.size > 200) recent.delete(recent.keys().next().value!);
  const kind = input.kind ?? "ui";
  const detail: UserErrorDetail = { code: kind === "voice" ? "voice_error" : "ui_error", context: kind, text, source,
    intent: slug(input.intent ?? currentIntent(now), "unknown"), occurredAt: new Date(now).toISOString() };
  const code = input.serverCode && /^[a-z][a-z0-9_]{0,39}$/.test(input.serverCode) ? input.serverCode : null;
  if (input.severity === "transient") detail.severity = "transient";
  if (code) detail.serverCode = code;
  if (Number.isInteger(input.status) && input.status! >= 0 && input.status! <= 599) detail.status = input.status!;
  if (input.requestId && /^[A-Za-z0-9_.:-]{1,80}$/.test(input.requestId)) detail.requestId = input.requestId;
  window.dispatchEvent(new CustomEvent(UI_DIAGNOSTIC_EVENT, { detail }));
  return true;
}

const ERROR_CLASS = /(?:^|[\s_-])(?:error|err|fail|failed|inlineError|bannerDanger|errorBanner|errorText)(?:$|[\s_-])/i;
export const isErrorElement = (el: Element) => !el.hasAttribute("data-not-error") && (
  el.getAttribute("role") === "alert" || el.hasAttribute("data-error-notice") || ERROR_CLASS.test(el.getAttribute("class") || ""));

/** Safety net: any role="alert" / error-styled element that shows up and was not already reported through
 *  reportUserError (hook, ErrorNotice, voice) is reported here, with the nearest data-error-source if any. */
export function startErrorObserver(): () => void {
  if (typeof document === "undefined" || typeof MutationObserver === "undefined") return () => undefined;
  const click = (e: Event) => {
    const id = e.target instanceof Element ? e.target.closest("[data-usage-control],[data-intent]") : null;
    noteIntent(id?.getAttribute("data-intent") || id?.getAttribute("data-usage-control"));
  };
  const control = (e: Event) => {
    const id = (e as CustomEvent<{ controlId?: string }>).detail?.controlId;
    if (id && !/^greeting_(?:fetch_|suppressed_)/.test(id)) noteIntent(id);
  };
  const pending = new Set<Element>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const scan = () => {
    timer = undefined;
    for (const el of pending) {
      if (!el.isConnected || !isErrorElement(el)) continue;
      const text = scrubErrorText(el.textContent);
      // Skip empty elements, big containers and anything the hook/voice path already reported.
      if (!text || el.children.length > 6 || wasRecentlyReported(text)) continue;
      const host = el.closest("[data-error-source]");
      reportUserError({ message: text, source: host?.getAttribute("data-error-source") || `dom.${el.tagName.toLowerCase()}`,
        intent: el.getAttribute("data-intent") || host?.getAttribute("data-intent") });
    }
    pending.clear();
  };
  const mo = new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "childList") r.addedNodes.forEach((n) => {
        if (!(n instanceof Element)) return;
        if (isErrorElement(n)) pending.add(n);
        n.querySelectorAll("[role='alert'],[data-error-notice],[class*='error'],[class*='fail'],[class*='err']").forEach((c) => pending.add(c));
      });
      else if (r.target.parentElement) pending.add(r.target.parentElement);
    }
    // Wait past React's passive effects so a hook-reported message is already recorded.
    if (pending.size && timer === undefined) timer = setTimeout(scan, 300);
  });
  mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  document.addEventListener("click", click, { capture: true, passive: true });
  window.addEventListener("supymem-usage-control-activate", control);
  return () => { mo.disconnect(); if (timer) clearTimeout(timer); document.removeEventListener("click", click, true);
    window.removeEventListener("supymem-usage-control-activate", control); };
}
