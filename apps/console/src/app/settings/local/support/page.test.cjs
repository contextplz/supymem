const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

test("the support bundle is downloaded locally only after you ask", async () => {
  const source = readFileSync(join(__dirname, "page.tsx"), "utf8");
  for (const local of [true, false]) {
    for (const domain of ["procurement.example", "gardening.example"]) {
      const exports = {}, clicks = [], requests = [];
      const bundle = new Blob([JSON.stringify({ desktop_crashes: [{ process: "rust" }] })]);
      const build = (type, props) => ({ type, props });
      vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
        module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
      } }).outputText, {
        exports, setTimeout() {},
        URL: { createObjectURL: value => { assert.equal(value, bundle); return `blob:${domain}`; } },
        fetch: async (path, init) => { requests.push([path, init.method]); return { ok: true, blob: async () => bundle }; },
        document: { createElement: () => ({ click() { clicks.push({ href: this.href, filename: this.download }); } }) },
        require: name => name === "react" ? { useState: initial => [initial, () => {}] }
          : name === "react/jsx-runtime" ? { jsx: build, jsxs: build } : { useLocalMode: () => local },
      });
      const tree = exports.default();
      assert.equal(requests.length, 0);
      if (!local) { assert.equal(tree, null); continue; }
      const button = tree.props.children.find(node => node.type === "button");
      button.props.onClick();
      await new Promise(resolve => setImmediate(resolve));
      assert.deepEqual(requests, [["/api/local/support-bundle", undefined]]);
      assert.deepEqual(clicks, [{ href: `blob:${domain}`, filename: "supymem-support.json" }]);
    }
  }
});
