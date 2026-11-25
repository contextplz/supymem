const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const exportsForTest = {};
vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, "IdeaReferences.tsx"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: exportsForTest, require(id) {
  if (id === "react/jsx-runtime") return require(id);
  if (id.endsWith(".css")) return { default: {} };
  if (id === "@/components/planning/CaseReferences") return { safeUrl(value) {
    try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; } catch {}
  } };
  throw new Error(id);
} });
const render = references => renderToStaticMarkup(React.createElement(exportsForTest.IdeaReferences, { references }));
const startup = { id: "case-a", name: "Stripe", what_they_did: "Installed with founders", outcome: "Observed setup friction",
  lesson: "Test onboarding by hand", url: "https://paulgraham.com/ds.html", relevance_reason: "Shared context: developer tooling.", important_difference: "Different customers; test locally." };
const advice = { id: "yc-a", title: "Dev Tools Company", line: "Talk to developers", url: "https://www.youtube.com/watch?v=a" };

test("renders sourced case outcomes, lessons and YC advice with external links", () => {
  const html = render({ startups: [startup, { ...startup, id: "case-b", name: "Segment" }], yc_advice: [advice] });
  for (const text of ["Startups that tried this", "Stripe", "Segment", startup.what_they_did, startup.outcome,
    startup.lesson, startup.relevance_reason, startup.important_difference, "YC advice for your stage", advice.title, advice.line, "not independently verified", "Not affiliated"]) assert.ok(html.includes(text), text);
  assert.equal((html.match(/target="_blank"/g) || []).length, 3);
  assert.equal((html.match(/rel="noopener noreferrer"/g) || []).length, 3);
  assert.ok(html.includes(startup.url) && html.includes(advice.url));
});

test("omits empty sections and rejects unsafe links while escaping corpus text", () => {
  assert.equal(render(undefined), "");
  assert.equal(render({ startups: [], yc_advice: [] }), "");
  const ycOnly = render({ startups: [], yc_advice: [advice] });
  assert.ok(!ycOnly.includes("Startups that tried this") && ycOnly.includes("YC advice for your stage"));
  const html = render({ startups: [{ ...startup, name: "<script>alert(1)</script>", url: "javascript:alert(1)" }],
    yc_advice: [{ ...advice, url: "https://user:password@example.com/" }] });
  assert.ok(!html.includes("<script>") && html.includes("&lt;script&gt;"));
  assert.ok(!html.includes("href=") && !html.includes("YC says:"));
});
