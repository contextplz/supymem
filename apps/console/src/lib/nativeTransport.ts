/** Native account transport. Credentials never enter this module or the WebView. */
import { desktopInvokeOrThrow, desktopListenOrThrow, isDesktopShell } from "./desktop.ts";
import { isLocalConsole } from "./localMode.ts";
import { announceDiagnostic, apiDiagnosticDetail, apiDiagnosticMessage, apiDiagnosticOperation, native503Origin } from "./uiDiagnostics.ts";

const MAX_BYTES = 32 * 1024 * 1024;
type Field = { name: string; value?: string; filename?: string; contentType?: string; base64?: string };
type Body = { kind: "json"; value: unknown } | { kind: "multipart"; fields: Field[] } |
  { kind: "bytes"; contentType: string; base64: string };
type Reply = { status: number; bodyBase64: string; headers: Record<string, string> };
function encode(bytes: Uint8Array): string {
  if (bytes.byteLength > MAX_BYTES) throw new Error("Files and requests are limited to 32 MiB.");
  const parts: string[] = [];
  for (let i = 0; i < bytes.length; i += 16384) parts.push(String.fromCharCode(...bytes.subarray(i, i + 16384)));
  return btoa(parts.join(""));
}
async function requestBody(init: RequestInit): Promise<Body | undefined> {
  if (init.body == null) return undefined;
  const headers = new Headers(init.headers);
  if (init.body instanceof FormData) {
    const fields: Field[] = [];
    let total = 0;
    for (const [name, value] of init.body.entries()) {
      if (fields.length >= 32) throw new Error("A request can contain at most 32 form fields.");
      if (typeof value === "string") {
        total += new TextEncoder().encode(value).byteLength;
        fields.push({ name, value });
      } else {
        total += value.size;
        if (total > MAX_BYTES) throw new Error("Files and requests are limited to 32 MiB.");
        fields.push({ name, filename: value.name, contentType: value.type || "application/octet-stream", base64: encode(new Uint8Array(await value.arrayBuffer())) });
      }
      if (total > MAX_BYTES) throw new Error("Files and requests are limited to 32 MiB.");
    }
    return { kind: "multipart", fields };
  }
  if (typeof init.body === "string") {
    const bytes = new TextEncoder().encode(init.body);
    if (bytes.byteLength > MAX_BYTES) throw new Error("Requests are limited to 32 MiB.");
    if ((headers.get("Content-Type") || "application/json").includes("json")) {
      return { kind: "json", value: JSON.parse(init.body) };
    }
    return { kind: "bytes", contentType: headers.get("Content-Type") || "text/plain", base64: encode(bytes) };
  }
  if (init.body instanceof Blob) {
    if (init.body.size > MAX_BYTES) throw new Error("Files are limited to 32 MiB.");
    return { kind: "bytes", contentType: headers.get("Content-Type") || init.body.type || "application/octet-stream", base64: encode(new Uint8Array(await init.body.arrayBuffer())) };
  }
  throw new Error("This request body is not supported by the Mac transport.");
}

/** A normal Response for JSON, text, files and multipart uploads; no cookie fallback or mutation retry. */
export async function nativeFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!isDesktopShell()) throw new Error("Native transport requires the Supymem app.");
  if (init.signal?.aborted) throw new DOMException("Request cancelled", "AbortError");
  const supplied = new Headers(init.headers);
  const headers: Record<string, string> = {};
  // No X-Request-ID: released Mac apps deny headers outside their allowlist.
  for (const name of ["Idempotency-Key", "If-Match", "X-Workspace-ID"]) {
    const value = supplied.get(name);
    if (value != null) headers[name] = value;
  }
  const body = await requestBody(init);
  if (init.signal?.aborted) throw new DOMException("Request cancelled", "AbortError");
  const reply = await desktopInvokeOrThrow<Reply>("desktop_http_request", {
    method: (init.method || "GET").toUpperCase(), path, headers, body,
  });
  if (init.signal?.aborted) throw new DOMException("Request cancelled; any change already sent may still have completed", "AbortError");
  try {
    if (!reply || !Number.isInteger(reply.status) || typeof reply.bodyBase64 !== "string" || reply.bodyBase64.length > Math.ceil(MAX_BYTES / 3) * 4) {
      throw new Error("The Mac transport returned an invalid response.");
    }
    if (reply.status === 0) throw new Error("The request outcome is unconfirmed. Check its status before repeating a change.");
    if (reply.status < 200 || reply.status > 599) throw new Error("The Mac transport returned an invalid HTTP status.");
    const decoded = atob(reply.bodyBase64);
    if (decoded.length > MAX_BYTES) throw new Error("The response exceeds 32 MiB.");
    const bytes = Uint8Array.from(decoded, (char) => char.charCodeAt(0));
    const safeHeaders = new Headers();
    for (const name of ["content-type", "content-disposition", "x-request-id", "retry-after"]) {
      if (typeof reply.headers?.[name] === "string") safeHeaders.set(name, reply.headers[name]);
    }
    return new Response([204, 205, 304].includes(reply.status) || init.method?.toUpperCase() === "HEAD" ? null : bytes, { status: reply.status, headers: safeHeaders });
  } catch (error) {
    // IPC succeeded, but the transport could not turn its reply into a usable
    // response. The exception may include a private header or body; report only
    // the closed native failure label and keep the original caller error.
    announceDiagnostic("native_error", "native", "Mac command_failed", null, null, null, null, null, "desktop_http_request");
    throw error;
  }
}

type StreamFrame = { id: string; kind: "open" | "message" | "error" | "closed"; event: string; data: string; last_event_id: string; status: number; origin?: string };
function streamId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
/** EventSource-compatible native stream. The native reader owns bearer refresh and reconnect. */
export class NativeEventSource extends EventTarget {
  static readonly DROPS_BEFORE_ERROR = 4;
  readonly id = streamId();
  readyState = 0;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  private closed = false;
  private dropped = 0;
  private unlisten: (() => void) | undefined;
  private stopAuth: (() => void) | undefined;
  constructor(path: string, lastEventId?: string) {
    super();
    void this.start(path, lastEventId);
  }
  private fail() {
    if (this.closed) return;
    this.readyState = 0;
    const event = new Event("error"); this.dispatchEvent(event); this.onerror?.(event);
  }
  private async start(path: string, lastEventId?: string) {
    try {
      const stop = await desktopListenOrThrow("supymem-native-sse", (payload) => {
        if (this.closed || !payload || typeof payload !== "object") return;
        const frame = payload as StreamFrame;
        if (frame.id !== this.id) return;
        if (frame.kind === "open") { this.readyState = 1; this.dropped = 0; this.dispatchEvent(new Event("open")); }
        else if (frame.kind === "message" && typeof frame.data === "string" && frame.data.length <= 1024 * 1024) {
          const event = new MessageEvent(frame.event || "message", { data: frame.data, lastEventId: frame.last_event_id });
          this.dispatchEvent(event); if (event.type === "message") this.onmessage?.(event);
        } else if (frame.kind === "error") {
          const status = Number.isInteger(frame.status) && frame.status >= 400 && frame.status <= 599 ? frame.status : 0;
          const retryable = status === 0 || status === 408 || status === 429 || status >= 500;
          const retrying = retryable && ++this.dropped < NativeEventSource.DROPS_BEFORE_ERROR;
          announceDiagnostic(status >= 500 ? "server_error" : status >= 400 ? "request_error" : "network_error", "api", apiDiagnosticMessage(status),
            null, apiDiagnosticOperation(path), native503Origin(status, { error: { code: frame.origin } }), "native_stream", null, null, apiDiagnosticDetail(path, status),
            retrying ? "transient" : "error");
          if (!retrying) this.fail();
        }
        else if (frame.kind === "closed") { this.fail(); this.close(); }
      });
      if (this.closed) { stop(); return; } this.unlisten = stop;
      const stopAuth = await desktopListenOrThrow("supymem-native-auth", (payload) => {
        const state = (payload as { state?: string } | null)?.state;
        if (state && ["signed_out", "reauth_required", "account_mismatch", "revocation_pending", "keychain_unavailable"].includes(state)) {
          this.fail(); this.close();
        }
      });
      if (this.closed) { stopAuth(); return; } this.stopAuth = stopAuth;
      await desktopInvokeOrThrow("desktop_events_subscribe", { id: this.id, path, lastEventId }, { quiet: true });
      if (this.closed) await desktopInvokeOrThrow("desktop_events_unsubscribe", { id: this.id }, { quiet: true });
    } catch { this.fail(); this.close(); }
  }
  close() {
    if (this.closed) return;
    this.closed = true; this.readyState = 2; this.unlisten?.(); this.stopAuth?.();
    void desktopInvokeOrThrow("desktop_events_unsubscribe", { id: this.id }, { quiet: true }).catch(() => undefined);
  }
}
class LocalEventStream extends EventTarget {
  readyState = 0;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  private readonly controller = new AbortController();

  constructor(path: string, lastEventId = "") {
    super();
    const disconnected = () => {
      if (this.controller.signal.aborted) return;
      this.close();
      const error = new Event("error");
      this.dispatchEvent(error);
      this.onerror?.(error);
    };
    void this.read(path, lastEventId).then(disconnected, disconnected);
  }

  private async read(path: string, lastEventId: string) {
    const { signal } = this.controller;
    const headers = new Headers({ Accept: "text/event-stream" });
    if (lastEventId) headers.set("Last-Event-ID", lastEventId);
    const response = await fetch(path, { headers, signal, credentials: "same-origin", cache: "no-store", redirect: "error" });
    const contentType = response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase();
    if (signal.aborted || !response.ok || !response.body || contentType !== "text/event-stream") return;
    this.readyState = 1;
    this.dispatchEvent(new Event("open"));
    const reader = response.body.pipeThrough(new TextDecoderStream(), { signal }).getReader();
    try {
      let buffer = "", event = "", data = "", skipLineFeed = false;
      while (!signal.aborted) {
        const { done, value } = await reader.read();
        if (done || signal.aborted) break;
        if (!value) continue;
        buffer += (skipLineFeed && value.startsWith("\n") ? value.slice(1) : value).replace(/\r\n?/g, "\n");
        skipLineFeed = value.endsWith("\r");
        let newline: number;
        while (!signal.aborted && (newline = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          if (line === "") {
            if (data) {
              const message = new MessageEvent(event || "message", { data: data.slice(0, -1), lastEventId });
              this.dispatchEvent(message);
              if (message.type === "message") this.onmessage?.(message);
            }
            event = data = "";
          } else {
            const colon = line.indexOf(":");
            const field = colon < 0 ? line : line.slice(0, colon);
            const fieldValue = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
            if (field === "event") event = fieldValue;
            else if (field === "data") data += `${fieldValue}\n`;
            else if (field === "id" && !fieldValue.includes("\0")) lastEventId = fieldValue;
          }
          if (data.length > 1024 * 1024) return;
        }
        if (buffer.length > 1024 * 1024) return;
      }
    } finally {
      reader.releaseLock();
    }
  }

  close() {
    this.readyState = 2;
    this.controller.abort();
  }
}

export type AuthenticatedEventSource = NativeEventSource | LocalEventStream | EventSource;
export function createAuthenticatedEventSource(path: string, gateway: string, lastEventId?: string): AuthenticatedEventSource {
  if (isDesktopShell()) return new NativeEventSource(path, lastEventId);
  if (isLocalConsole()) return new LocalEventStream(path, lastEventId);
  return new EventSource(`${gateway}${path}`, { withCredentials: true });
}
