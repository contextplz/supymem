import assert from "node:assert/strict";
import test from "node:test";
import { draftLines, preparePlanEdit, describeImage, type LandingExpectations } from "./planEditing.ts";

test("controlled plan lists retain every typed space and newline until save", () => {
  const intended = "Managed waitlist verified\n\n  Review before publication \n";
  let rendered = "";
  for (const character of intended) rendered = draftLines(rendered + character).join("\n");
  assert.equal(rendered, intended);
  const draft = {
    assumptions: draftLines(rendered), exclusions: ["  "], evidence_gaps: [" Verify demand "],
    milestones: [{ key: "landing", acceptance: [" QA passed ", ""], tasks: [
      { implementation: "new_code", acceptance: ["  immutable contract  ", ""], source_ids: ["source-1"], founder_action: null },
      { implementation: "founder_action", acceptance: [" Receipt ", ""], founder_action: { steps: [" Review QA ", "", " Confirm publication "], verification: " exact receipt " } },
    ] }],
  };
  const before = structuredClone(draft);
  const saved = preparePlanEdit(draft);
  assert.deepEqual(saved.assumptions, ["Managed waitlist verified", "Review before publication"]);
  assert.deepEqual(saved.exclusions, []);
  assert.deepEqual(saved.evidence_gaps, ["Verify demand"]);
  assert.deepEqual(saved.milestones[0].acceptance, ["QA passed"]);
  assert.deepEqual(saved.milestones[0].tasks[1].acceptance, ["Receipt"]);
  assert.deepEqual(saved.milestones[0].tasks[1].founder_action?.steps, ["Review QA", "Confirm publication"]);
  assert.deepEqual(saved.milestones[0].tasks[0], before.milestones[0].tasks[0]);
  assert.equal(saved.milestones[0].tasks[1].founder_action?.verification, " exact receipt ");
  assert.deepEqual(draft, before);
});


test("explicit image conversion preserves acceptance references and original approved object", () => {
  const before: LandingExpectations = {
    contract: { profile: "static_landing_v1", product_name: "Pilot", required_text: ["Offer", "Illustration of a useful workflow", "FAQ"], actions: [{ id: "image_1" }] },
    criteria: [
      { criterion: 0, summary: "Hero", text_indices: [1], action_ids: [] },
      { criterion: 1, summary: "Copy", text_indices: [0, 1, 2], action_ids: ["image_1"] },
    ],
  };
  const saved = structuredClone(before);
  const edited = describeImage(before, 1);
  assert.deepEqual(before, saved);
  assert.deepEqual(edited.contract.required_text, ["Offer", "FAQ"]);
  assert.deepEqual(edited.contract.visuals, [{ id: "image_2", name: "Illustration of a useful workflow" }]);
  assert.deepEqual(edited.criteria.map(item => item.text_indices), [[], [0, 1]]);
  assert.deepEqual(edited.criteria.map(item => item.visual_ids), [["image_2"], ["image_2"]]);
  assert.deepEqual(edited.criteria[1].action_ids, ["image_1"]);
  assert.deepEqual(describeImage(before, -1), before);
});
