import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const exports = {} as any;
const output = ts.transpileModule(readFileSync(new URL("../components/research/StartupGraveyard.tsx", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;
vm.runInNewContext(output, { exports, URL, require(id: string) {
  return id.endsWith(".module.css") ? { default: {} } : require(id);
} });
const startup = { name: "CrumbQueue", founded: "2018", ended: "2022", cash_burned: "$2 million",
  why_failed: "Too few paying bakeries covered onboarding costs.", suggestion: "Test paid order intake with independent bakery owners.",
  entry_url: "https://www.loot-drop.io/startup/42-crumbqueue" };
const render = (startups?: unknown) => renderToStaticMarkup(React.createElement(exports.StartupGraveyard, { startups }));

test("similar failed startup renders short facts, suggestion label and source links", () => {
  const html = render([startup]);
  for (const text of ["Startup graveyard", "CrumbQueue", "2018-2022", "$2 million", "Why it failed",
    "What to do differently", "Our suggestion", "via Loot Drop", "not independently verified"]) assert.ok(html.includes(text), text);
  assert.ok(html.includes('data-usage-control="research_graveyard_source"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
});

test("empty or invalid matches render nothing, optional facts stay hidden and entries are capped", () => {
  for (const entries of [undefined, null, [], {}, [null], [{ ...startup, entry_url: "javascript:alert(1)" }],
    [{ ...startup, entry_url: "https://loot-drop.io/startup.html" }]]) assert.equal(render(entries), "");
  const optional = render([{ ...startup, cash_burned: null, founded: null }]);
  assert.ok(!optional.includes("Cash burned") && !optional.includes("Years active"));
  assert.equal((render(Array.from({ length: 4 }, (_, index) => ({ ...startup, entry_url: startup.entry_url.replace("42", String(index)) })))
    .match(/<article/g) || []).length, 3);
});
