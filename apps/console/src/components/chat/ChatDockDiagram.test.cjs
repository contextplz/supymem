const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const jsx = (type, props) => ({ type, props });
const transpile = source => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const dock = readFileSync(path.join(__dirname, "ChatDock.tsx"), "utf8");
const card = { exports: {} };
vm.runInNewContext(transpile(readFileSync(path.join(__dirname, "DiagramCard.tsx"), "utf8")), {
  exports: card.exports,
  require(name) {
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (name === "react") return {};
    if (name === "@/lib/diagramSanitize") return { sanitizeDiagramSvg: svg => svg };
    if (name === "./DiagramCard.module.css") return { default: {} };
    throw new Error(name);
  },
});
const fragment = dock.match(/\{(!own && \(m\.parts \|\| \[\]\)[^\n]*)\}\n/)[1];

function render(m, own, prompts) {
  const context = { exports: {}, require: () => ({ jsx, jsxs: jsx }), m, own, styles: { diagram: "diagram" },
    submit: text => prompts.push(text), renderDiagramPart: card.exports.renderDiagramPart };
  vm.runInNewContext(transpile(`exports.out = ${fragment};`), context);
  return context.exports.out;
}

test("ChatDock renders accepted diagram parts in assistant messages through DiagramCard", () => {
  assert.match(dock, /import \{ renderDiagramPart \} from "\.\/DiagramCard";/);
  const prompts = [];
  const svg = '<svg width="100%" viewBox="0 0 680 120" role="img"><title>Journey</title></svg>';
  const message = { id: "m1", role: "assistant", body_md: "Here is the journey.",
    parts: [{ type: "text", text: "Here is the journey." }, { type: "diagram", title: "customer_journey", svg }] };
  const out = render(message, false, prompts);
  assert.equal(out.length, 1);
  const element = out[0].props.children;
  assert.equal(element.type, card.exports.DiagramCard);
  assert.equal(element.props.title, "customer_journey");
  assert.equal(element.props.svg, svg);
  element.props.onPrompt("Tell me more about Pilot");
  assert.deepEqual(prompts, ["Tell me more about Pilot"]);
  assert.equal(render(message, true, prompts), false);
  assert.equal(render({ id: "m2", role: "assistant", body_md: "Plain" }, false, prompts).length, 0);
});
