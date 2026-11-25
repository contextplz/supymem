import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { localToolStatus } from "./localToolGuidance.ts";
import { macTaskPermissionsGranted, screenPermissionAction, statusOf } from "./macPermissions.ts";

// Exercise the shipped renderer and explicit check/request callbacks. Native
// permission/tool responses are owned fixtures; no permission is requested.
const flush = () => new Promise<void>(done => setImmediate(done));
function fixture(path: string, name: string, invoke: (command: string, args: any) => Promise<any>,
                 webHealth?: unknown) {
  const source = fs.readFileSync(new URL(path, import.meta.url), "utf8");
  const tree = ts.createSourceFile(name + ".tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = tree.statements.filter(node => !ts.isImportDeclaration(node))
    .map(node => node.getText(tree).replace(/^export /, "")).join("\n");
  const code = ts.transpileModule(body + `\nglobalThis.component=${name};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  let cursor = 0;
  const slots: any[] = [], elements: any[] = [], calls: { command: string; args: any }[] = [], healthCalls: string[] = [];
  const native = async (command: string, args: any) => { calls.push({ command, args }); return invoke(command, args); };
  const context = vm.createContext({
    React: { createElement: (type: any, props: any, ...children: any[]) => {
      const element = { type, props, children }; elements.push(element); return element;
    } },
    useState: (initial: unknown) => {
      const index = cursor++; slots[index] ??= { value: initial };
      return [slots[index].value, (value: unknown) => {
        slots[index].value = typeof value === "function" ? value(slots[index].value) : value;
      }];
    },
    useCallback: (callback: unknown) => callback, useEffect: () => undefined, useRef: (initial: unknown) => ({current: initial}),
    macTaskPermissionsGranted, screenPermissionAction, statusOf,
    permissionRequestedBefore: () => false, markPermissionRequested: () => undefined,
    isDesktopShell: () => webHealth === undefined, desktopInvoke: native, desktopInvokeOrThrow: native,
    api: async (path: string) => {
      if (webHealth === undefined) throw new Error("desktop detection must not query cloud tools");
      healthCalls.push(path); return { ok: true, data: webHealth };
    },
    localToolStatus, ErrorNotice: "ErrorNotice", permissionsSetupDone: () => true,
    shouldAutoOpenHelperSetup: () => false, STATUS_RETRY_MAX: 20, STATUS_RETRY_MS: 3000,
    sessionStorage: { getItem: () => null, setItem: () => undefined },
  });
  vm.runInContext(code, context);
  const render = () => { cursor = 0; elements.length = 0; context.component({ inline: true }); return elements; };
  const text = () => render().flatMap(element => element.children.flat(Infinity)
    .filter((value: unknown) => typeof value === "string")).join(" ");
  const click = async (control: string) => {
    const button = render().find(element => element.props?.["data-usage-control"] === control);
    assert.ok(button, control); button.props.onClick(); await flush();
  };
  return { render, text, click, calls, healthCalls };
}

test("detected computer use is installed, while Codex keeps its separate CLI/account guidance", async () => {
  const f = fixture("../components/settings/LocalToolsHealth.tsx", "LocalToolsHealth", async command => {
    assert.equal(command, "list_local_tools");
    return [
      { name: "hermes", label: "Supymem Computer Use", installed: true, kind: "computer_use", connected: false },
      { name: "codex", label: "Codex", installed: true, kind: "coding", connected: false, cli_path: "/owned-fixture/codex" },
    ];
  });
  await f.click("settings_local_tools_redetect");
  assert.equal(f.calls.length, 1);
  assert.match(f.text(), /Detected tools/);
  assert.match(f.text(), /Supymem Computer Use.*installed; task availability is checked in its saved review/);
  assert.match(f.text(), /Codex CLI found.*CLI sign-in unverified/);
  assert.match(f.text(), /Supported browser checks and coding tasks/);
  assert.match(f.text(), /Native Mac control is unavailable by default/);
  assert.equal((f.text().match(/not checked on this Mac/g) || []).length, 2);
  assert.equal(f.healthCalls.length, 0);
  assert.doesNotMatch(f.text(), /not running|not reachable/);
  assert.doesNotMatch(f.text(), /ready on this Mac|Coding agents/);
});

for (const reachable of [true, false]) test(`web health preserves the actually checked endpoint ${reachable ? "success" : "failure"}`, async () => {
  const f = fixture("../components/settings/LocalToolsHealth.tsx", "LocalToolsHealth", async () => {
    throw new Error("web health must not invoke native tool detection");
  }, { coding_agents: [], local_llm: {
    ollama: { ok: reachable, base_url: "http://owned-probe.invalid", models: reachable ? ["fixture-model"] : [] },
    lm_studio: { ok: reachable, base_url: "http://owned-probe.invalid", models: reachable ? ["fixture-model"] : [] },
    any_available: reachable,
  } });
  await f.click("settings_local_tools_redetect");
  assert.deepEqual(f.healthCalls, ["/api/local-tools/health"]);
  assert.equal(f.calls.length, 0);
  const expected = reachable ? /reachable \(1 models\)/g : /not reachable/g;
  assert.equal((f.text().match(expected) || []).length, 2);
  assert.doesNotMatch(f.text(), /not checked on this Mac|not running/);
});

test("a refused native tool read does not display any capability-ready claim", async () => {
  const f = fixture("../components/settings/LocalToolsHealth.tsx", "LocalToolsHealth", async () => null);
  await f.click("settings_local_tools_redetect");
  const notice = f.render().find(element => element.type === "ErrorNotice");
  assert.match(notice.props.error, /Could not inspect tools/);
  assert.doesNotMatch(f.text(), /ready on this Mac|installed; task availability/);
});

test("granted native preflight describes status without verifying task capability", async () => {
  const f = fixture("../components/desktop/DesktopPermissionSetup.tsx", "DesktopPermissionSetup", async command => {
    assert.equal(command, "permissions_status");
    return {"screen-recording": "granted", accessibility: "granted"};
  });
  await f.click("settings_permissions_refresh");
  assert.match(f.text(), /Screen Recording\s*,.*Granted/);
  assert.match(f.text(), /Accessibility\s*,.*Granted/);
  assert.match(f.text(), /Granting a permission does not make a task available/);
  assert.doesNotMatch(f.text(), /Ready|Lets Supymem work in another app/);
});

test("unknown native status stays unchecked and never offers a prompt", async () => {
  const f = fixture("../components/desktop/DesktopPermissionSetup.tsx", "DesktopPermissionSetup", async command => {
    assert.equal(command, "permissions_status"); return {};
  });
  await f.click("settings_permissions_refresh");
  assert.match(f.text(), /Screen Recording\s*,.*Not checked/);
  assert.doesNotMatch(f.text(), /Grant permission|Ready/);
  assert.deepEqual(f.calls.map(call => call.command), ["permissions_status"]);
});

test("an unavailable permission check stays actionable without a ready status", async () => {
  const f = fixture("../components/desktop/DesktopPermissionSetup.tsx", "DesktopPermissionSetup", async () => {
    throw new Error("owned fixture bridge unavailable");
  });
  await f.click("settings_permissions_refresh");
  const notice = f.render().find(element => element.type === "ErrorNotice");
  assert.match(notice.props.error, /could not check your Mac settings.*Retry/);
  assert.doesNotMatch(f.text(), /Granted|Ready/);
});
