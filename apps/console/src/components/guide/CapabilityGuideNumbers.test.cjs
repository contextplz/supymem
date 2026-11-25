const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const exportsObject = {};
vm.runInNewContext(ts.transpileModule(readFileSync(require.resolve("../../lib/capabilityGuide.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: exportsObject });

test("founder number cards work for unrelated business and consumer ventures without invented inputs", () => {
  for (const venture of ["Equipment leasing for factories", "Dance lessons for families"]) {
    for (const [id, prompt] of [["price-band", "Work out my price range"], ["unit-economics", "Do the numbers for my venture"]]) {
      const card = exportsObject.GUIDE_CAPABILITIES.find(item => item.id === id);
      assert.equal(card.prompt, prompt);
      assert.equal(card.destination, "overview");
      assert.ok(!card.comingNext && !card.description.includes(venture));
      assert.ok(exportsObject.filterGuideCapabilities(card.title, "Start & shape", "Customers").some(item => item.id === id));
      assert.doesNotMatch([card.title, card.description, card.result, card.note].join(" "), /\u2014/);
    }
  }
  const price = exportsObject.GUIDE_CAPABILITIES.find(item => item.id === "price-band");
  assert.match(price.note, /at least 5 valid answers/);
  assert.match(price.description, /real interviews or survey replies/);
  const economics = exportsObject.GUIDE_CAPABILITIES.find(item => item.id === "unit-economics");
  assert.match(economics.note, /missing inputs, one at a time/);
  assert.match(economics.description, /Starting cash is optional/);
});

test("number cards coexist with the live leads and market map cards across unrelated domains", () => {
  for (const audience of ["Equipment maintenance for factories", "Weekend cooking classes for families"]) {
    const cards = exportsObject.GUIDE_CAPABILITIES;
    for (const id of ["price-band", "unit-economics", "leads", "market-beliefs"]) {
      assert.equal(cards.filter(card => card.id === id).length, 1);
      assert.doesNotMatch(cards.find(card => card.id === id).description, new RegExp(audience));
    }
    assert.equal(cards.find(card => card.id === "leads").destination, "leads");
    assert.equal(cards.find(card => card.id === "market-beliefs").destination, "market-map");
  }
});
