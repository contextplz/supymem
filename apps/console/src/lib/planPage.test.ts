import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";
import { nextStepButton } from "./nextStep.ts";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const customerStep = { key: "gtm.outreach", stage: "Customers", title: "Find your first customers",
  detail: "A plan is optional.", cta_label: "Find customers", researchReportReady: true, runState: "completed",
  href: "/gtm?venture=venture", chat_action: "outreach.draft", payload: null };

async function render(route: "charter" | "plan", step: typeof customerStep & { alternatives?: unknown[] } | null) {
  let cursor = 0;
  const hooks: any[] = [], effects: (() => void)[] = [], requests: string[] = [];
  const jsx = (type: unknown, props: Record<string, any>) => ({ type, props });
  function compile(path: string): any {
    const exports = {};
    const output = ts.transpileModule(readFileSync(path, "utf8"), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
    } }).outputText;
    vm.runInNewContext(output, { exports, require: resolve, window: { location: { hash: "" } } });
    return exports;
  }
  function resolve(id: string): unknown {
    if (id === "react") return {
      useState(initial: unknown) { const index = cursor++; if (!(index in hooks)) hooks[index] = initial;
        return [hooks[index], (value: unknown) => { hooks[index] = value; }]; },
      useRef(initial: unknown) { const index = cursor++; return hooks[index] ||= { current: initial }; },
      useEffect(effect: () => void) { const index = cursor++; if (!(index in hooks)) { hooks[index] = true; effects.push(effect); } },
    };
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
    if (id === "next/link") return { default: "a" };
    if (id === "next/navigation") return { useParams: () => ({ ventureId: "venture" }), useRouter: () => ({ push() {} }) };
    if (id === "@/components/layout/NextStepStrip") return { useNextStep: () => step, NextStepButton: "NextStepButton", NextStepStrip: "NextStepStrip" };
    if (id === "@/lib/client") return { ensureSession: async () => {}, api: async (path: string) => {
      requests.push(path);
      return { ok: true, data: path.endsWith("/plans") ? { items: [] }
        : path.endsWith("/resources") ? { inventory: { complete: true } } : {} };
    } };
    if (id === "@/lib/durableJobs") return { restoreActiveJob: async () => ({ id: null, error: "" }), reportJobFailure() {} };
    if (id === "@/hooks/useDurableJob") return { useDurableJob: () => ({ job: null }) };
    if (id === "@/lib/detailLevel") return { useDetailLevel: () => ["brief"] };
    if (id === "@/lib/format") return { shortTitle: () => "Plan", stageLabel: (value: string) => value };
    if (["charterPresentation", "planObjective", "planTimeline", "executionNextAction", "planReview", "planningFailure"].some(name => id === `@/lib/${name}`))
      return compile(fileURLToPath(new URL(`./${id.split("/").at(-1)}.ts`, import.meta.url)));
    if (id.endsWith(".module.css")) return { default: new Proxy({}, { get: (_, key) => key }) };
    return Object.fromEntries([id.split("/").at(-1)!, "FounderResourceInventory", "InlineSpinner", "ErrorNotice"].map(name => [name, name]));
  }
  const Page = compile(fileURLToPath(new URL(`../app/v/[ventureId]/${route}/page.tsx`, import.meta.url))).default;
  Page(); effects.splice(0).forEach(effect => effect());
  await new Promise(setImmediate);
  cursor = 0;
  return { tree: Page(), requests };
}

function nodes(tree: any): any[] {
  if (!tree || typeof tree !== "object") return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return tree.props ? [tree, ...nodes(tree.props.children)] : [];
}

test("Plan and Execution empty states offer optional planning independently of the next-step key", async () => {
  for (const route of ["charter", "plan"] as const) {
    const pageStep = { ...customerStep, key: "site.build", href: "/v/venture/site?build=1", alternatives: [
      { choice: "build", label: "Build the landing page", href: "/v/venture/site?build=1" },
      { choice: "skip", label: "Find customers", href: "/gtm?venture=venture" },
    ] };
    const pageAction = nodes((await render(route, pageStep)).tree).find(node => node.type === "NextStepButton");
    assert.equal(pageAction?.props.step.key, "plan.generate");
    const view = await render(route, customerStep), rendered = nodes(view.tree);
    const action = rendered.find(node => node.type === "NextStepButton" && node.props.step.cta_label === "Write my plan");
    assert.ok(action, route);
    assert.deepEqual(nextStepButton(action.props.step, `/v/venture/${route}`),
      { kind: "action", action: "plan.generate", payload: {}, label: "Write my plan" });
    assert.ok(!rendered.some(node => node.props.children === "Continue the idea journey →"), route);
    assert.ok(!view.requests.some(path => path.endsWith("/compile") || path.endsWith("/estimate-generation")), route);
    assert.ok(!nodes((await render(route, null)).tree).some(node => node.type === "NextStepButton"), route);
    for (const step of [{ ...customerStep, researchReportReady: false },
      ...["working", "queued", "paused"].map(runState => ({ ...customerStep, runState }))]) {
      const unavailable = nodes((await render(route, step)).tree);
      assert.ok(!unavailable.some(node => node.type === "NextStepButton"), `${route}: ${step.runState}`);
      if (route === "charter") assert.ok(unavailable.some(node => node.props.children === "Continue the idea journey →"));
    }
  }
});

test("Plan introduction describes the work without internal stage jargon", async () => {
  const tree = (await render("charter", customerStep)).tree;
  const introduction = nodes(tree).find(node => node.type === "p" && node.props.className === "lede");
  assert.ok(introduction);
  assert.doesNotMatch(JSON.stringify(introduction.props.children), /Stage:|Launch review|charter_review/);
});
