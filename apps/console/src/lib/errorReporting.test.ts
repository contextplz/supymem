import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";
import { UI_DIAGNOSTIC_EVENT } from "./uiDiagnostics.ts";
import { reportUserError, resetUserErrorState, scrubErrorText, slug, templateRoute, currentIntent, noteIntent, startErrorObserver } from "./userError.ts";

const SRC = join(import.meta.dirname, "..");

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? files(join(dir, e.name)) : e.name.endsWith(".tsx") && !e.name.endsWith(".test.tsx") ? [join(dir, e.name)] : []);
}

// A file that renders an error to the user: an alert region or an error-styled element.
const RENDERS_ERROR = /role="alert"|styles\.(?:fail|err|error|inlineError|bannerDanger|errorBanner)\b|className="(?:[^"]* )?(?:error|err|fail)(?: [^"]*)?"|color: "var\(--danger/;
// ...must route it through the shared reporter: <ErrorNotice>, useReportedError() or reportUserError().
const REPORTS = /\bErrorNotice\b|\buseReportedError\b|\breportUserError\b/;

/** Files that predate the reporter. The runtime DOM observer (startErrorObserver) still reports what they show,
 *  with a generic "dom.*" source. This list may only shrink: migrating a file makes the test below demand its removal. */
const LEGACY_UNREPORTED = new Set<string>([
  "components/layout/AppShellErrorBoundary.tsx", // render crash: already reported as render_error by announceDiagnostic
  "components/outreach/DraftReview.tsx",         // advisory "flags" shown in the error colour, not a failure
  "components/outreach/InterviewCapture.tsx",    // same
]);

test("every component that renders an error goes through the shared reporter", () => {
  const offenders: string[] = [];
  const stale: string[] = [];
  for (const file of files(SRC)) {
    const rel = relative(SRC, file);
    if (rel === "components/primitives/ErrorNotice.tsx") continue;
    const text = readFileSync(file, "utf8");
    const bad = RENDERS_ERROR.test(text) && !REPORTS.test(text);
    if (bad && !LEGACY_UNREPORTED.has(rel)) offenders.push(rel);
    if (!bad && LEGACY_UNREPORTED.has(rel)) stale.push(rel);
  }
  assert.deepEqual(offenders, [], "render errors via <ErrorNotice>/useReportedError/reportUserError (see components/primitives/ErrorNotice.tsx)");
  assert.deepEqual(stale, [], "these files now report their errors; remove them from LEGACY_UNREPORTED");
});

test("no raw role=\"alert\" outside the shared notice (mark a non-error with data-not-error)", () => {
  const raw: string[] = [];
  for (const file of files(SRC)) {
    const rel = relative(SRC, file);
    if (rel === "components/primitives/ErrorNotice.tsx" || LEGACY_UNREPORTED.has(rel)) continue;
    readFileSync(file, "utf8").split("\n").forEach((line, i) => {
      if (/role="alert"/.test(line) && !/data-not-error/.test(line)) raw.push(`${rel}:${i + 1}`);
    });
  }
  assert.deepEqual(raw, [], "use <ErrorNotice> so the error is reported");
});

test("the shared notice reports on display and renders an alert", () => {
  const src = readFileSync(join(SRC, "components/primitives/ErrorNotice.tsx"), "utf8");
  assert.match(src, /reportUserError\(/);
  assert.match(src, /role: "alert"/);
  assert.match(src, /"data-error-source": source/);
  assert.match(src, /"data-intent": intent/);
});

test("background greeting fetches cannot become a research UI error intent", () => {
  const originalWindow = globalThis.window, originalDocument = globalThis.document, originalObserver = globalThis.MutationObserver;
  const target = new EventTarget();
  Object.assign(globalThis, {window: target, document: Object.assign(new EventTarget(), {body: {}}),
    MutationObserver: class {observe() {} disconnect() {}}});
  resetUserErrorState();
  const stop = startErrorObserver();
  try {
    noteIntent("research_run");
    for (const controlId of ["greeting_fetch_failed", "greeting_suppressed_greeted"]) {
      target.dispatchEvent(new CustomEvent("supymem-usage-control-activate", {detail: {controlId}}));
      assert.equal(currentIntent(), "research_run");
    }
    target.dispatchEvent(new CustomEvent("supymem-usage-control-activate", {detail: {controlId: "greeting_skip"}}));
    assert.equal(currentIntent(), "greeting_skip");
  } finally {
    stop(); Object.assign(globalThis, {window: originalWindow, document: originalDocument, MutationObserver: originalObserver});
    resetUserErrorState();
  }
});

function capture() {
  const seen: Record<string, unknown>[] = [];
  const target = new EventTarget();
  (globalThis as { window?: unknown }).window = Object.assign(target, {
    dispatchEvent: (e: Event) => { seen.push((e as CustomEvent).detail); return true; },
  });
  resetUserErrorState();
  return seen;
}

test("reportUserError carries source, intent, time and ids, and scrubs emails and tokens", () => {
  const seen = capture();
  noteIntent("task_run", 1_000);
  assert.equal(reportUserError({ source: "tasks.run", message: "Failed for jo@example.com with Bearer abcdefghijklmnop and sk-live_abcdef123456",
    status: 409, serverCode: "venture_paused", requestId: "req_abcdefghjkmn" }, 2_000), true);
  const d = seen[0] as Record<string, unknown>;
  assert.equal(d.code, "ui_error");
  assert.equal(d.context, "ui");
  assert.equal(d.source, "tasks.run");
  assert.equal(d.intent, "task_run"); // falls back to the control the user just used
  assert.equal(d.occurredAt, new Date(2_000).toISOString());
  assert.equal(d.requestId, "req_abcdefghjkmn");
  assert.equal(d.status, 409);
  assert.equal(d.serverCode, "venture_paused");
  for (const secret of ["jo@example.com", "abcdefghijklmnop", "sk-live"]) assert.ok(!String(d.text).includes(secret), secret);
  assert.match(String(d.text), /^Failed for \[email\] with/);
  assert.equal(currentIntent(1_000 + 20_000), "unknown");
});

test("identical repeats inside five seconds are reported once, later ones again", () => {
  const seen = capture();
  const m = { source: "tasks.run", message: "request outcome unknown", intent: "task_run" };
  assert.equal(reportUserError(m, 10_000), true);
  assert.equal(reportUserError(m, 12_000), false);
  assert.equal(reportUserError({ ...m, source: "dom.p" }, 12_500), false); // the DOM observer sees the same text
  assert.equal(reportUserError(m, 16_000), true);
  assert.equal(reportUserError({ ...m, message: "something else" }, 16_001), true);
  assert.equal(seen.length, 3);
});

test("voice errors report as voice_error with the realtime code and response id", () => {
  const seen = capture();
  reportUserError({ kind: "voice", source: "voice.realtime", intent: "voice_turn",
    message: "Conversation already has an active response in progress: resp_D4x9. Wait until the response is finished before creating a new one.",
    serverCode: "conversation_already_has_active_response", requestId: "resp_D4x9" });
  const d = seen[0] as Record<string, unknown>;
  assert.deepEqual([d.code, d.context, d.source, d.intent, d.requestId],
    ["voice_error", "voice", "voice.realtime", "voice_turn", "resp_D4x9"]);
  assert.match(String(d.text), /already has an active response/);
  assert.equal(UI_DIAGNOSTIC_EVENT, "supymem-ui-diagnostic");
});

test("labels and routes are closed shapes", () => {
  assert.equal(slug("Bad Source!!", "ui"), "bad_source_");
  assert.equal(slug("", "ui"), "ui");
  assert.equal(templateRoute("/v/ven_abc123def456/tasks?x=1#h"), "/v/{id}/tasks");
  assert.equal(templateRoute("/ops/tasks"), "/ops/tasks");
  assert.equal(scrubErrorText("word ".repeat(200)).length, 300);
});

test("voice chat shows errors only through its reporting setError (realtime, mic, mint, SDP, data channel, WebRTC)", () => {
  const src = readFileSync(join(SRC, "components/chat/useVoiceChat.ts"), "utf8");
  assert.doesNotMatch(src, /setErrorState\((?!message)/, "show errors via setError(), which reports");
  assert.match(src, /reportUserError\(\{[^}]*kind: "voice"/);
  for (const source of ["voice.realtime", "voice.session_mint", "voice.mic", "voice.sdp_exchange", "voice.datachannel", "voice.webrtc"]) {
    assert.ok(src.includes(`"${source}"`), source);
  }
});
