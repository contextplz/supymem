import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(new URL("../app/v/[ventureId]/plan/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("plan.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let predicate = "";
let callback = "";
function visit(node: ts.Node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === "editLanding") callback = node.getText(tree);
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
      && ts.isJsxElement(node.right) && node.right.getText(tree).startsWith("<details key={`landing-")) {
    predicate = node.left.getText(tree);
  }
  ts.forEachChild(node, visit);
}
visit(tree); assert.ok(predicate, "actual landing editor visibility predicate is present");
assert.ok(callback, "actual landing edit callback is present");

function visible(capability_id: string, implementation = "new_code", expectations: object | null = {}) {
  const context = vm.createContext({ task: { capability_id, implementation, landing_expectations: expectations } });
  vm.runInContext(ts.transpileModule(`globalThis.visible=Boolean(${predicate});`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  return context.visible;
}

test("actual plan editor exposes approved static contract controls for both supported source paths", () => {
  assert.equal(visible("build.run"), true);
  assert.equal(visible("coding.subscription_static"), true);
});

test("actual static editor excludes untyped, product, founder and reused task scope", () => {
  for (const capability of ["api.fastapi", "coding.subscription_product_source", "founder.action", "unknown"]) {
    assert.equal(visible(capability), false);
  }
  assert.equal(visible("coding.subscription_static", "new_code", null), false);
  assert.equal(visible("coding.subscription_static", "founder_action"), false);
  assert.equal(visible("coding.subscription_static", "reusable_code"), false);
});

function edited(capability_id: string, implementation = "new_code") {
  const original = { milestones: [
    { tasks: [{ capability_id, implementation, landing_expectations: { contract: { required_text: ["Before"] } } },
      { capability_id: "build.run", implementation: "new_code", landing_expectations: { contract: { required_text: ["Other task"] } } }] },
    { tasks: [{ capability_id: "build.run", implementation: "new_code", landing_expectations: { contract: { required_text: ["Other milestone"] } } }] },
  ] };
  const next = { contract: { required_text: ["After"], visuals: [{ id: "logo", name: "A reviewed image description" }] } };
  let value = original;
  const context = vm.createContext({ next, setEdit: (update: (current: typeof original) => typeof original) => { value = update(value); } });
  vm.runInContext(ts.transpileModule(`${callback}\neditLanding(0, 0, next);`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  return { original, value, next };
}

test("actual edit callback saves copy and visual changes only to the selected supported static task", () => {
  for (const capability of ["build.run", "coding.subscription_static"]) {
    const { original, value, next } = edited(capability);
    assert.equal(value.milestones[0].tasks[0].landing_expectations, next);
    assert.equal(value.milestones[0].tasks[1], original.milestones[0].tasks[1]);
    assert.equal(value.milestones[1], original.milestones[1]);
    assert.equal(original.milestones[0].tasks[0].landing_expectations.contract.required_text[0], "Before");
  }
});

test("actual edit callback cannot mutate product, unknown, founder or reused scope", () => {
  for (const [capability, implementation] of [
    ["api.fastapi", "new_code"], ["coding.subscription_product_source", "new_code"],
    ["unknown", "new_code"], ["coding.subscription_static", "founder_action"],
    ["coding.subscription_static", "reusable_code"],
  ]) {
    const { original, value } = edited(capability, implementation);
    assert.equal(value.milestones[0].tasks[0], original.milestones[0].tasks[0]);
  }
});
