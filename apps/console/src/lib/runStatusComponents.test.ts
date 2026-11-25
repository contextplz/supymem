import * as chatRoutes from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import * as nextStep from "./nextStep.ts";
import * as presentation from "./runStatusPresentation.ts";
import * as gtmProgress from "./gtmProgress.ts";

function components(pathname: string, key = "work.running", state = "working", ideaStatus?: presentation.IdeaPageStatus, detailsOpen = false, offline = false) {
  const snapshot = { venture: { id: "venture-a", name: "Harbor" }, needs_you: [],
    run_status: { state, stage: "Research", activity: "Checking competitors", action: { label: "View progress", href: "/v/venture-a/now" },
      background_work: [{ job_id: "job_a", stage: "Research", state, href: "/v/venture-a/now" }] },
    next_step: { key, stage: "Research", title: "Checking your idea", detail: "Your work is saved", cta_label: "View progress",
      chat_action: null, payload: null, href: "/v/venture-a/now" },
  };
  const jsx = (type: any, props: any) => typeof type === "function" ? type(props) : ({ type, props });
  const css = { default: new Proxy({}, { get: (_, name) => name }) };
  let stateIndex = 0;
  const mocks: Record<string, any> = {
    react: { createContext: (value: any) => ({ value }), useContext: (context: any) => context.value,
      useState: (initial: any) => {
        const index = stateIndex++;
        return [index === 1 && offline || index === 4 && detailsOpen ? true : typeof initial === "function" ? initial() : initial, () => {}];
      }, useEffect() {}, useRef: (current: any) => ({ current }) },
    "react/jsx-runtime": { jsx, jsxs: jsx }, "next/navigation": { usePathname: () => pathname }, "next/link": { default: "a" },
    "@/lib/client": { api() {}, apiSwr() {}, verifiedSessionSubject: () => "actor-a", verifiedSessionEpoch: () => 1 },
    "@/lib/apiCache": { cacheGet: () => ({ data: snapshot }) }, "@/lib/nextStep": nextStep, "@/lib/runStatusPresentation": presentation,
    "@/lib/gtmProgress": gtmProgress,
    "@/components/desktop/EmergencyStopButton": { EmergencyStopButton: () => null },
    "./RunStatusBar.module.css": css, "./NextStepStrip.module.css": css,
  };
  const compile = (file: string) => {
    const source = fs.readFileSync(new URL(`../components/layout/${file}.tsx`, import.meta.url), "utf8");
    const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const exports = {} as Record<string, any>;
    vm.runInNewContext(code, { exports, require: (id: string) => { if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };
    return mocks[id] || {}; }, window: {}, document: {} });
    return exports[file];
  };
  const bar = compile("RunStatusBar")({ ventureId: pathname.startsWith("/v/") || ideaStatus ? "venture-a" : undefined, ideaStatus });
  const strip = compile("NextStepStrip")({ ventureId: "venture-a", ideaStatus });
  return { bar, strip };
}
const nodes = (value: any): any[] => !value || typeof value !== "object" ? []
  : [value, ...[value.props?.children].flat(6).flatMap(nodes)];

test("U51 the current Idea question or ready review overrides a stale preparing header", () => {
  for (const pathname of ["/v/venture-a/idea", "/new"]) for (const activity of ["Answer the next question about your idea", "Review your saved idea"]) {
    const { strip, bar } = components(pathname, "work.running", "queued", { state: "needs_you", activity });
    assert.equal(nodes(strip).some(node => node.type === "strong" && JSON.stringify(node.props.children).includes(activity)), true);
    assert.doesNotMatch(JSON.stringify(strip), /Preparing your first question|Checking your idea/);
    assert.ok(JSON.stringify(bar).includes(activity));
  }
  const shell = fs.readFileSync(new URL("../components/layout/AppShell.tsx", import.meta.url), "utf8");
  assert.match(shell, /nextStep=\{\(!local \|\| isVentureOverview \|\| entryPage\) && <NextStepStrip ventureId=\{effectiveVentureId\} ideaStatus=\{ideaStatus\} inline/);
});
test("running work renders one bar with one progress link, including background jobs", () => {
  for (const [key, state] of [["work.running", "working"], ["plan.review", "working"], ["work.running", "queued"]]) {
    const { bar, strip } = components("/v/venture-a/research", key, state);
    assert.equal(strip, null);
    assert.equal(nodes(bar).filter(node => node.type === "a" && node.props.href === "/v/venture-a/now").length, 1);
  }
});
test("the current progress screen never links back to itself", () => {
  const { bar, strip } = components("/v/venture-a/now");
  assert.equal(strip.props["aria-hidden"], "true");
  assert.match(strip.props.className, /placeholder/);
  assert.equal(nodes(bar).filter(node => node.type === "a" && node.props.href === "/v/venture-a/now").length, 0);
});

test("the shell reserves the same next-step height while fresh receipts are unavailable", () => {
  const { strip } = components("/v/venture-a/idea");
  assert.equal(strip.props["aria-hidden"], "true");
  assert.equal(nodes(strip).some(node => node.type === "button" || node.type === "a"), false);
  const css = fs.readFileSync(new URL("../components/layout/NextStepStrip.module.css", import.meta.url), "utf8");
  assert.match(css, /\.strip\s*\{[^}]*min-height: 60px;[^}]*flex: none;/);
  assert.doesNotMatch(css, /\.strip\s*\{[^}]*(?<!min-)height: 60px/, "a fixed height clips two-button steps");
  assert.match(css, /\.placeholder\s*\{[^}]*visibility: hidden;[^}]*pointer-events: none;/);
});

test("blocker details keep a working review link even when the bar CTA is deduplicated", () => {
  const { bar } = components("/v/venture-a/research", "handoff.needs_you", "needs_you", undefined, true);
  const details = nodes(bar).find(node => node.props?.id === "run-status-details");
  assert.ok(details);
  assert.ok(nodes(details).find(node => node.type === "a" && node.props.href === "/v/venture-a/now"));
});

test("home and global screens have no empty run-status banner; connection failures stay actionable", () => {
  for (const pathname of ["/", "/new", "/connections"]) {
    assert.equal(components(pathname).bar, null);
    const { bar } = components(pathname, undefined, undefined, undefined, false, true);
    assert.ok(nodes(bar).find(node => node.props?.["data-usage-control"] === "run_status_reconnect"));
  }
});

test("the idea banner uses the page state and invokes the card's continue handler", () => {
  let continued = 0;
  const ideaStatus = { state: "failed", activity: "Credits are available now.", action: {
    label: "Continue", disabled: false, onClick: () => { continued++; } } };
  const { bar } = components("/v/venture-a/idea", "work.running", "working", ideaStatus);
  const rendered = nodes(bar);
  const button = rendered.find(node => node.type === "button" && node.props.children === "Continue");
  assert.ok(button);
  button.props.onClick();
  assert.equal(continued, 1);
  assert.equal(rendered.some(node => node.props.children === "Checking competitors"), false);
  const ready = components("/v/venture-a/idea", "work.running", "working", { state: "needs_you", activity: "Answer the next question" });
  assert.equal(nodes(ready.bar).find(node => node.props?.["aria-label"] === "Run status").props["data-working"], false);
});
