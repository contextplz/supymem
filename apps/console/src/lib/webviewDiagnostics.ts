import { isChunkLoadError } from "./chunkLoadError.ts";
import { desktopInvokeQuiet, isDesktopShell } from "./desktop.ts";
import { consoleRoute } from "./observability.ts";
import { sanitizeDiagnostic } from "./uiDiagnostics.ts";

const ERROR_TEXT = { runtime_error: "Runtime failure", promise_error: "Promise failure", render_error: "Render failure" };
const ERROR_TYPES = new Set(["Error", "TypeError", "ReferenceError", "SyntaxError", "RangeError", "URIError", "EvalError", "AggregateError"]);
const GATES = new Set(["local_capabilities", "local_provider", "venture_load", "conversation_load", "workspace_session", "route_load"]);
const CHUNK_FRAME = /\/_next\/static\/chunks\/(?:[^\s():?#]*[-/.])?([a-f0-9]{16})\.js:(\d{1,9}):(\d{1,9})/;
const BLANK_MS = 15_000;
const MAX_ERRORS_PER_MINUTE = 19;
const recent: number[] = [];

function send(kind: string, fields: Record<string, unknown>): void {
  const now = window.performance.now();
  while (recent.length && now - recent[0] >= 60_000) recent.shift();
  if (recent.length >= MAX_ERRORS_PER_MINUTE + (kind === "blank_render" ? 1 : 0)) return;
  recent.push(now);
  void desktopInvokeQuiet("append_webview_error", { record: { kind, route: consoleRoute(window.location.pathname), ...fields } });
}

export function recordWebviewError(kind: keyof typeof ERROR_TEXT, error: unknown, location?: ErrorEvent): void {
  if (!isDesktopShell()) return;
  let errorType = "Unknown";
  let frame: RegExpMatchArray | null = null;
  try {
    const name = error instanceof Error ? error.name : "";
    if (isChunkLoadError(error)) errorType = "ChunkLoadError";
    else if (ERROR_TYPES.has(name)) errorType = name;
    const stack = error instanceof Error ? error.stack : undefined;
    frame = (location && `${location.filename}:${location.lineno}:${location.colno}`.match(CHUNK_FRAME))
      || (typeof stack === "string" ? stack.slice(0, 4_096).match(CHUNK_FRAME) : null);
  } catch {}
  send(kind, { text: sanitizeDiagnostic(ERROR_TEXT[kind]), errorType,
    ...(frame ? { source: { chunk: frame[1], line: Number(frame[2]), column: Number(frame[3]) } } : {}) });
}

function shown(element: HTMLElement): boolean {
  if (!element.getClientRects().length || element.closest("[hidden],[inert],[aria-hidden='true']")) return false;
  const style = window.getComputedStyle(element);
  return style.visibility === "visible" && style.opacity !== "0";
}

function blankRender(): void {
  const gates = Array.from(document.querySelectorAll<HTMLElement>("[data-webview-pending]"))
    .filter(gate => GATES.has(gate.dataset.webviewPending ?? "") && !gate.closest("[hidden],[inert],[aria-hidden='true']"));
  const primary = document.querySelectorAll<HTMLElement>("[data-webview-main]:not([data-webview-main='chat']),.chat-first-home [data-webview-main='chat']");
  const main = primary.length ? primary : document.querySelectorAll<HTMLElement>(".workspace-body,main,[role='main']");
  const regions = Array.from(main.length ? main : document.querySelectorAll<HTMLElement>("[role='dialog'][aria-modal='true']")).filter(shown);
  // shortcut: visible text and controls approximate readiness; mark content explicitly if placeholders are mistaken for pages.
  if (regions.some(region => !gates.some(gate => region === gate || region.contains(gate) || gate.contains(region)) &&
    (region.innerText.trim() || Array.from(region.querySelectorAll<HTMLElement>("input,textarea,select,img,canvas,video")).some(shown)))) return;
  const pending = [...new Set(gates.map(gate => gate.dataset.webviewPending))];
  send("blank_render", { pending });
}

export function installWebviewDiagnostics(): () => void {
  if (!isDesktopShell()) return () => {};
  const runtime = (event: ErrorEvent) => recordWebviewError("runtime_error", event.error ?? event.message, event);
  const rejection = (event: PromiseRejectionEvent) => recordWebviewError("promise_error", event.reason);
  window.addEventListener("error", runtime);
  window.addEventListener("unhandledrejection", rejection);
  const routeKey = () => window.location.pathname + window.location.search;
  let route = routeKey();
  let deadline = window.performance.now() + BLANK_MS;
  // shortcut: polling can miss sub-250ms route hops; use native navigation events if those hops need tracing.
  const timer = window.setInterval(() => {
    const current = routeKey();
    const now = window.performance.now();
    if (current !== route) {
      route = current;
      deadline = now + BLANK_MS;
    }
    if (now >= deadline) {
      deadline = Infinity;
      blankRender();
    }
  }, 250);
  return () => {
    window.removeEventListener("error", runtime);
    window.removeEventListener("unhandledrejection", rejection);
    window.clearInterval(timer);
    recent.length = 0;
  };
}
