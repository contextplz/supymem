const { readdirSync } = require("node:fs");
const { spawnSync } = require("node:child_process");
const { join } = require("node:path");

const files = readdirSync("src", { recursive: true })
  .filter(file => /^(?:lib\/.*\.test\.ts|components\/.*\.test\.cjs|app\/.*\.test\.(?:cjs|ts))$/.test(file))
  .map(file => join("src", file).replace(/[[\]]/g, c => `[${c}]`)).sort(); // node --test globs its args: [x] escapes literal brackets
const result = spawnSync(process.execPath, ["--experimental-strip-types", "--test", "--test-concurrency=2", ...files], { stdio: "inherit" });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
