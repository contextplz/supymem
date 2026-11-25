const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const Module = require("node:module");
const { join, dirname } = require("node:path");
const test = require("node:test");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require("typescript");

const componentPath = join(__dirname, "PlanFindingNavigator.tsx");
const source = readFileSync(componentPath, "utf8");
const output = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
} }).outputText;
const compiled = new Module(componentPath, module);
compiled.filename = componentPath;
compiled.paths = Module._nodeModulePaths(dirname(componentPath));
compiled._compile(output, componentPath);
const { PlanEditSection, PlanFindingNavigator } = compiled.exports;

test("rendered editor opens only affected milestones and tasks while preserving other controls", () => {
  const paths = ["proposal.milestones[4].tasks[2].description"];
  const html = renderToStaticMarkup(React.createElement("div", null,
    React.createElement(PlanEditSection, { path: "proposal.milestones[4]", title: "Milestone 5", findingPaths: paths },
      React.createElement(PlanEditSection, { path: "proposal.milestones[4].tasks[2]", title: "Task 3", findingPaths: paths },
        React.createElement("textarea", { defaultValue: "$999" }))),
    React.createElement(PlanEditSection, { path: "proposal.milestones[5]", title: "Milestone 6", findingPaths: paths },
      React.createElement("textarea", { defaultValue: "Other draft" }))));
  assert.match(html, /<details open=""><summary>Milestone 5 · Needs review<\/summary>/);
  assert.match(html, /<details open=""><summary>Task 3 · Needs review<\/summary>/);
  assert.match(html, /<details><summary>Milestone 6<\/summary>/);
  assert.match(html, /\$999/);
  assert.match(html, /Other draft/);
});

test("rendered navigation exposes each saved finding as a button with its message", () => {
  const html = renderToStaticMarkup(React.createElement(PlanFindingNavigator, {
    findings: [
      { code: "unconfirmed_price", path: "proposal.summary", message: "Confirm the price." },
      { code: "unconfirmed_price", path: "proposal.milestones[4].tasks[2].description", message: "Confirm the price." },
    ], note: "", onJump: () => {},
  }));
  assert.equal((html.match(/<button type="button">/g) || []).length, 2);
  assert.match(html, /Location 2: proposal\.milestones\[4\]\.tasks\[2\]\.description/);
  assert.match(html, /Confirm the price/);
});
