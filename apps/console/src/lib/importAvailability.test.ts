import test from "node:test";
import assert from "node:assert/strict";
import { importAvailabilityMessage } from "./importAvailability.ts";

test("import capability failures are deduplicated into founder-facing prerequisites", () => {
  const message = importAvailabilityMessage([
    "import_store_configuration_unavailable",
    "import_parser_isolation_unavailable",
    "import_store_configuration_unavailable",
    "import_intake_not_accepted",
  ]);
  assert.match(message, /encrypted source storage and cleanup/);
  assert.match(message, /isolated project parser/);
  assert.match(message, /production safety review/);
  assert.doesNotMatch(message, /import_|configuration unavailable/);
});

test("unknown capability failures remain honest without exposing internal codes", () => {
  assert.equal(
    importAvailabilityMessage(["future_internal_reason"]),
    "Project import is not available in this environment yet. Your source has not been uploaded or executed.",
  );
});
