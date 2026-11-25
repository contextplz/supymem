import assert from "node:assert/strict";
import test from "node:test";
import { contentBriefFromCompany } from "./contentCompanyBrief.ts";

test("content starts from confirmed company context without inventing proof", () => {
  const template = {
    product: { name: "", one_liner: "", differentiators: [] },
    icp: { segments: [{ name: "", pains: [], watering_holes: [] }] },
    claims: [{ text: "demo claim" }],
  };
  const result = contentBriefFromCompany(template, {
    name: "ScopeSignal", raw_idea: "fallback idea",
  }, {
    one_liner: "Catch scope changes before margin leaks.",
    audience: "Small agency owners",
    problem: "Unbilled work erodes margin",
    mvp_scope: ["compare a request with a statement of work", "draft a change order"],
  });
  assert.deepEqual(result, {
    product: {
      name: "ScopeSignal",
      one_liner: "Catch scope changes before margin leaks.",
      differentiators: ["compare a request with a statement of work", "draft a change order"],
    },
    icp: { segments: [{ name: "Small agency owners", pains: ["Unbilled work erodes margin"], watering_holes: [] }] },
    claims: [],
  });
  assert.equal(template.product.name, "");
});

test("missing charter fields remain blank instead of being guessed", () => {
  const result = contentBriefFromCompany({ product: {}, icp: { segments: [{}] } }, {
    raw_idea: "A founder idea",
  }, {});
  assert.deepEqual(result.claims, []);
  assert.deepEqual(result.product, { name: "", one_liner: "A founder idea", differentiators: [] });
  assert.deepEqual(result.icp, { segments: [{ name: "", pains: [] }] });
});
