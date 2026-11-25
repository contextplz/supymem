import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import vm from "node:vm";

test("console runner recursively passes component, library and bracketed app tests as literal paths", () => {
  const source = readFileSync(new URL("../../scripts/test.cjs", import.meta.url), "utf8");
  const files = ["components/office/nested/Shell.test.cjs", "components/founder/FounderPage.test.cjs",
    "lib/nested/client.test.ts", "app/v/[ventureId]/page.test.cjs", "app/nested/page.test.ts",
    "app/page.tsx", "components/layout/AppShell.tsx", "browser/test_browser.py"];
  let received: string[] = [], exitCode: number | undefined;
  vm.runInNewContext(source, {console, process: {execPath: "node", exit: (code: number) => {exitCode = code;}}, require(id: string) {
    if (id === "node:fs") return {readdirSync: (root: string, options: {recursive: boolean}) => {
      assert.equal(root, "src"); assert.equal(options.recursive, true); return files;
    }};
    if (id === "node:path") return {join};
    if (id === "node:child_process") return {spawnSync: (command: string, args: string[], options: {stdio: string}) => {
      assert.equal(command, "node"); assert.equal(options.stdio, "inherit"); received = Array.from(args); return {status: 7};
    }};
    throw new Error(`Unexpected import: ${id}`);
  }});
  assert.deepEqual(received.slice(0, 3), ["--experimental-strip-types", "--test", "--test-concurrency=2"]);
  // node --test treats its arguments as globs, so literal brackets must arrive escaped as [[] and []].
  assert.deepEqual(received.slice(3), files.slice(0, 5).map(file => join("src", file).replace(/[[\]]/g, c => `[${c}]`)).sort());
  assert.ok(received.includes("src/app/v/[[]ventureId[]]/page.test.cjs"));
  assert.equal(exitCode, 7);
});
