import assert from "node:assert/strict";
import test from "node:test";
import { isActivationObjective, objectiveLabel, objectivePageCopy } from "./planObjective.ts";

test("payment exclusion presents the saved beta goal as activation", () => {
  const proposal = {
    exclusions: ["No payment, checkout, pricing display, or revenue recognition."],
  };
  assert.equal(isActivationObjective(proposal), true);
  assert.equal(objectiveLabel(proposal), "Activation objective");
  assert.deepEqual(objectivePageCopy(proposal), {
    subtitle: "Milestones and resources for user activation",
    heading: "Execution path to user activation",
  });
  assert.equal(isActivationObjective({ exclusions: ["Payments are excluded during beta."] }), true);
  assert.equal(isActivationObjective({ exclusions: ["No first paid users are expected in beta."] }), true);
});

test("ordinary plans retain their paid user objective label", () => {
  assert.equal(isActivationObjective({ exclusions: ["No mobile app in this version."] }), false);
  assert.equal(objectiveLabel({ exclusions: ["No mobile app in this version."] }), "First paid user objective");
  const invoicedPlan = { exclusions: ["No Stripe checkout; use invoicing for first paid user."] };
  assert.equal(isActivationObjective(invoicedPlan), false);
  assert.equal(objectiveLabel(invoicedPlan), "First paid user objective");
  assert.equal(isActivationObjective({ exclusions: ["No payment processing; invoice the first customer."] }), false);
  assert.deepEqual(objectivePageCopy(invoicedPlan), {
    subtitle: "Milestones and resources for your first paid user",
    heading: "Execution path to your first paid user",
  });
});

test("plan heading stays neutral before the proposal loads", () => {
  assert.equal(objectiveLabel(null), "First paid user objective");
  assert.deepEqual(objectivePageCopy(null), {
    subtitle: "Execution milestones and resources",
    heading: "Execution path",
  });
});
