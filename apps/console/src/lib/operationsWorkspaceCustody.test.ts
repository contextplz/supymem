import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { operationsHref } from "./operationsScope.ts";

const source = fs.readFileSync(new URL("../components/ops/OpsWorkspaceGate.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("OpsWorkspaceGate.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function picker() {
  let body = "";
  function visit(node: ts.Node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === "changeWorkspace") body = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(body);
  let scope: unknown, apiScope: unknown, href = "";
  const change = vm.runInNewContext(ts.transpileModule(`${body}; changeWorkspace`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    URL, operationsHref, selected: "wsp_fixtureworks", getOpsWorkspace: () => "wsp_fixtureworks", loading: false, generation: {current: 1}, loadedOwner: {subject: "usr_owner", receiptEpoch: 1, generation: 1}, ownerCurrent: () => true, setLoadedOwner: () => {}, actorId: "usr_owner", workspaces: [{ id: "wsp_fixtureworks" }, { id: "wsp_gyms" }],
    scope: { workspaceId: "wsp_fixtureworks", ventureId: "ven_fixtureworks", actorId: "usr_owner" },
    setOpsWorkspace: (workspaceId: string, ventureId?: string) => { apiScope = { workspaceId, ventureId }; },
    setSelected: () => {}, setScope: (next: unknown) => { scope = next; }, onScopeChange: () => {}, setError: () => {},
    window: { location: { href: "https://app.supymem.com/connections?venture=ven_fixtureworks" },
      history: { replaceState: (_state: unknown, _title: string, next: string) => { href = next; } } },
  }) as (value: string) => void;
  return { change, result: () => JSON.parse(JSON.stringify({ scope, apiScope, href })) };
}

test("actual company picker removes another venture from both API scope and shell URL", () => {
  const p = picker(); p.change("wsp_gyms");
  assert.deepEqual(p.result(), { scope: { workspaceId: "wsp_gyms", actorId: "usr_owner" },
    apiScope: { workspaceId: "wsp_gyms" }, href: "/connections?workspace=wsp_gyms" });
});

test("actual company picker retains only an already-verified matching venture", () => {
  const p = picker(); p.change("wsp_fixtureworks");
  assert.deepEqual(p.result().apiScope, { workspaceId: "wsp_fixtureworks", ventureId: "ven_fixtureworks" });
  assert.equal(p.result().href, "/connections?workspace=wsp_fixtureworks&venture=ven_fixtureworks");
});

test("actual company picker clears scope and rejects unavailable workspaces", () => {
  const p = picker(); p.change("wsp_foreign"); assert.equal(p.result().href, "");
  p.change(""); assert.equal(p.result().scope, null); assert.equal(p.result().href, "/connections");
});
