const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

test("saved model usage never displays balances or estimates across domains", () => {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + "/TokenMeter.tsx", "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText, { exports });
  for (const title of ["Clinic managers", "Home bakers"]) {
    for (const available of [0, 100]) {
      assert.equal(exports.TokenMeter({ title, usage: { credits: { available, granted: 100, spent: 100 },
        estimate_usd_total: 17, total_tokens: 1000 } }), null);
    }
  }
});
