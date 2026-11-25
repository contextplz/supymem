import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { revisionBuildHref } from "./siteRevision.ts";

const orderSource = fs.readFileSync(new URL("../app/v/[ventureId]/device-work-orders/[orderId]/page.tsx", import.meta.url), "utf8");
const buildsSource = fs.readFileSync(new URL("../app/v/[ventureId]/builds/page.tsx", import.meta.url), "utf8");

function renderedQaHref(runId: string): string {
  const tree = ts.createSourceFile("order.tsx", orderSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let paragraph = "";
  function visit(node: ts.Node) {
    if (ts.isJsxElement(node) && node.getText(tree).startsWith("<p>Saved QA run:")) paragraph = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(paragraph);
  let href = "";
  const context = vm.createContext({
    qaRun: { app_id: "app_owned", id: runId }, ventureId: "ven_owned", revisionBuildHref, Link: "owned-link",
    React: { createElement(type: unknown, props: { href?: string } | null) {
      if (type === "owned-link") href = props?.href || "";
      return null;
    } },
  });
  vm.runInContext(ts.transpileModule(`(${paragraph});`, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
  } }).outputText, context);
  assert.ok(href); return href;
}

async function actualBuildSelection(href: string) {
  const tree = ts.createSourceFile("builds.tsx", buildsSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let body = "";
  function visit(node: ts.Node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === "loadBuilds") body = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(body);
  const loaded: string[] = [], errors: string[] = [];
  const context = vm.createContext({
    URL, window: { location: { href: new URL(href, "https://console.supymem.com").href } },
    listGeneration: { current: 0 }, currentApp: { current: "app_owned" }, active: () => true,
    build: null, eventAfter: 0, setRuntime() {}, setRuntimeError() {}, setBuilds() {},
    setBuild() {}, setEvents() {}, setSyncError: (message: string) => errors.push(message),
    apiFailure: () => "Read failed", loadBuild: async (id: string) => loaded.push(id),
    api: async (path: string) => ({ ok: true, data: path.endsWith("/builds")
      ? { items: [{ id: "bsi_newer" }, { id: "bsi_older" }] } : { runtime: null } }),
  });
  vm.runInContext(ts.transpileModule(body + ";globalThis.read=loadBuilds;", {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  await context.read("app_owned"); return { loaded, errors };
}

test("actual saved-source QA link opens its exact older run instead of the newest run", async () => {
  const href = renderedQaHref("bsi_older");
  const selected = await actualBuildSelection(href);
  assert.deepEqual(selected.loaded, ["bsi_older"]);
  assert.ok(!selected.errors.some(Boolean));
});

test("actual saved-source QA link never substitutes another run when the requested run is missing", async () => {
  const selected = await actualBuildSelection(renderedQaHref("bsi_missing"));
  assert.deepEqual(selected.loaded, []);
  assert.ok(selected.errors.some(message => message.includes("linked build is not available")));
});
