const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const { pageBuildView } = require("../../lib/pageBuild.ts");

function sourceTree() {
  return ts.createSourceFile("BuildPageCard.tsx", readFileSync(join(__dirname, "BuildPageCard.tsx"), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

for (const venture of ["maple-ledger", "winter-picnic"]) {
  test(`${venture}: a rejected heading links directly to its venture chat`, () => {
    const tree = sourceTree();
    let link;
    function visit(node) {
      if (ts.isJsxElement(node) && node.children.some(child => child.getText(tree).trim() === "Open venture chat")) {
        const href = node.openingElement.attributes.properties.find(attribute => attribute.name?.getText(tree) === "href");
        link = href?.initializer.expression.getText(tree);
      }
      ts.forEachChild(node, visit);
    }
    visit(tree);
    assert.ok(link);
    assert.equal(vm.runInNewContext(link, { ventureId: venture }), `/new?venture=${venture}`);
  });
}

for (const domain of ["industrial-parts", "weekend-fitness"]) {
  test(`${domain}: local build, retry and new versions prioritize current page orders over legacy cards`, () => {
    const tree = sourceTree();
    let expression;
    function visit(node) {
      if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "nativeOrder") expression = node.initializer.getText(tree);
      ts.forEachChild(node, visit);
    }
    visit(tree);
    for (const state of ["pending", "approved", "stopped", "cancelled"]) {
      const orders = [{ id: "dwo_old", task_id: domain, artifact_profile: "static_landing_v1", state }];
      const context = { view: pageBuildView("codex", orders, domain), profile: { current: { job_id: "current-page" } } };
      const local = vm.runInNewContext(expression, { ...context, local: true });
      assert.equal(local, false, "current local page orders keep their checked preview and watch link");
      const hosted = vm.runInNewContext(expression, { ...context, local: false });
      assert.equal(hosted, true);
      assert.equal(vm.runInNewContext(expression, { ...context, local: true, profile: null }), true);
    }
  });
  test(`${domain}: no connected tool offers your exact settings screen, not an internal alternative`, () => {
    const tree = sourceTree();
    let card, settings;
    function visit(node) {
      if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'CODING_TOOLS_SETTINGS') settings = node.getText(tree);
      if (ts.isJsxElement(node) && node.openingElement.attributes.properties.some(attribute =>
        ts.isJsxAttribute(attribute) && attribute.name.getText(tree) === "title" && attribute.initializer?.getText(tree) === '"Connect a coding tool"')) card = node.getText(tree);
      ts.forEachChild(node, visit);
    }
    visit(tree);
    const output = ts.transpileModule(`const ${settings}; const result = (${card});`, { compilerOptions: {
      target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, jsxFactory: "jsx",
    } }).outputText;
    const context = { jsx: (type, props, ...children) => ({ type, props, children }), OneCard: "card", ChatAnchor: "a",
      ventureId: domain, local: true, serverAlternative: "internal-generation-alternative" };
    const result = vm.runInNewContext(`${output}; result`, context);
    assert.match(result.props.body, /Settings > Keys and connections > Coding tools/);
    assert.equal(result.props.actions.props.href, "/settings/local#coding-tools");
    assert.ok(!result.children.includes(context.serverAlternative));
    const hosted = vm.runInNewContext(`${output}; result`, { ...context, local: false });
    assert.equal(hosted.props.actions.props.href, `/connections?venture=${domain}`);
    assert.ok(hosted.children.includes(context.serverAlternative));
  });
}
