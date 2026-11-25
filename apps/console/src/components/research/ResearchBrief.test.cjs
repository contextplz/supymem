const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function loadResearchBrief() {
  const exported = {};
  const source = readFileSync(join(__dirname, "ResearchBrief.tsx"), "utf8");
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, { exports: exported, require: name => {
    if (name.endsWith(".css")) return { default: {} };
    if (name === "@/lib/researchBrief") return { sourceDomain: value => new URL(value).hostname };
    if (name === "@/lib/cx") return { cx: () => "" };
    return require(name);
  } });
  return exported;
}

test("your sample research source has a label without an outside link or favicon request", () => {
  const exported = loadResearchBrief();
  for (const domain of ["procurement", "gardening"]) {
    const html = renderToStaticMarkup(React.createElement(exported.SourceChip, { url: `https://${domain}.example.invalid/source` }));
    assert.match(html, /Sample source, not a real website/);
    assert.doesNotMatch(html, /<a|<img|href=|src=/);
  }
});

test("research source chips open links on click without fetching external images", () => {
  const exported = loadResearchBrief();
  for (const domain of ["procurement.example", "gardening.example"]) {
    const url = `https://${domain}/source`;
    const html = renderToStaticMarkup(React.createElement(exported.SourceChip, { url }));
    assert.ok(html.includes(`href="${url}"`));
    assert.doesNotMatch(html, /<img|src=|google\.com/);
  }
});

test("the console and bundled setup use no remote font resources", () => {
  const root = join(__dirname, "../../../../..");
  for (const file of ["apps/console/src/styles/tokens.css", "src-tauri/splashscreen/setup.html"]) {
    assert.doesNotMatch(readFileSync(join(root, file), "utf8"), /fonts\.googleapis\.com|fonts\.gstatic\.com/);
  }
});

test("your sample customer source cannot open an outside connection", () => {
  const source = readFileSync(join(__dirname, "../../lib/gtmJourneyApi.ts"), "utf8");
  const tree = ts.createSourceFile("gtmJourneyApi.ts", source, ts.ScriptTarget.Latest, true);
  const declaration = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "safeEvidenceUrl");
  const exported = {};
  vm.runInNewContext(ts.transpileModule(declaration.getText(tree), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText,
    { exports: exported, URL });
  for (const domain of ["procurement", "gardening"]) {
    assert.equal(exported.safeEvidenceUrl(`https://${domain}.example.invalid/source`), undefined);
    assert.equal(exported.safeEvidenceUrl(`https://${domain}.example/source`), `https://${domain}.example/source`);
  }
});
