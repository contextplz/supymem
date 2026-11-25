import assert from "node:assert/strict";
import test from "node:test";
import { defaultImportProductId } from "./importPresentation.ts";

test("an existing product is selected when import opens", () => {
  assert.equal(defaultImportProductId("", [{ id: "app_scope" }]), "app_scope");
});

test("a founder selection is retained while it remains available", () => {
  assert.equal(defaultImportProductId("app_two", [{ id: "app_one" }, { id: "app_two" }]), "app_two");
});

test("a removed selection falls back to the first available product", () => {
  assert.equal(defaultImportProductId("app_old", [{ id: "app_new" }]), "app_new");
});
