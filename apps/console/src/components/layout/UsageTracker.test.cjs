const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

test("local ventures do not register analytics effects", () => {
  const source = readFileSync(join(__dirname, "UsageTracker.tsx"), "utf8");
  const exports = {};
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, {
    exports,
    require: name => name === "react" ? { useEffect: effect => assert.equal(effect(), undefined) }
      : name === "@/lib/localMode" ? { isLocalConsole: () => true } : {},
  });
  for (const domain of ["procurement", "gardening"]) {
    assert.equal(exports.UsageTracker({ subject: "owner", pathname: `/v/${domain}`, ventureId: domain }), null);
  }
});
