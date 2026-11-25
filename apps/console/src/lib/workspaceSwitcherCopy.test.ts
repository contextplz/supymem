import assert from "node:assert/strict";
import test from "node:test";
import { workspaceSwitcherCopy } from "./workspaceSwitcherCopy.ts";

test("workspace switcher labels Venture vs All ventures from shell id", () => {
  assert.deepEqual(workspaceSwitcherCopy(undefined, undefined), {
    eyebrow: "Ventures",
    label: "All ventures",
  });
  assert.deepEqual(workspaceSwitcherCopy("ven_x", undefined), {
    eyebrow: "Venture",
    label: "Loading venture…",
  });
  assert.deepEqual(workspaceSwitcherCopy("ven_x", "LabHire"), {
    eyebrow: "Venture",
    label: "LabHire",
  });
});
