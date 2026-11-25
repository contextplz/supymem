import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : p.endsWith(".tsx") ? [p] : [];
  });
}

test("no em or en dash in JSX text or string literals", () => {
  const bad: string[] = [];
  for (const file of walk(join(process.cwd(), "src"))) {
    readFileSync(file, "utf8").split("\n").forEach((line, i) => {
      const code = line.replace(/\{\/\*.*?\*\/\}/g, "").replace(/\/\*.*?\*\//g, "").replace(/^\s*(\/\/|\*).*$/, "");
      if (/[\u2013\u2014]/.test(code) && !/\.(test|replace|match)\(|\/[^/]*[\u2013\u2014][^/]*\/[gimsuy]*[.,)]/.test(code)) bad.push(`${file}:${i + 1}`);
    });
  }
  assert.deepEqual(bad, []);
});
