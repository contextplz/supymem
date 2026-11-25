// Controlled inputs must retain trailing spaces and empty lines while typing.
export const draftLines = (value: string): string[] => value.split("\n");
const cleanLines = (items: string[]): string[] => items.map(item => item.trim()).filter(Boolean);

type EditablePlan = {
  assumptions: string[]; exclusions: string[]; evidence_gaps: string[];
  milestones: {
    acceptance: string[];
    tasks: { implementation: string; acceptance: string[]; founder_action: { steps: string[] } | null }[];
  }[];
};

// Normalize only editable lists at the save boundary. Build contracts stay intact.
export function preparePlanEdit<T extends EditablePlan>(draft: T): T {
  const result = structuredClone(draft);
  result.assumptions = cleanLines(result.assumptions);
  result.exclusions = cleanLines(result.exclusions);
  result.evidence_gaps = cleanLines(result.evidence_gaps);
  for (const milestone of result.milestones) {
    milestone.acceptance = cleanLines(milestone.acceptance);
    for (const task of milestone.tasks) {
      if (task.implementation !== "founder_action") continue;
      task.acceptance = cleanLines(task.acceptance);
      if (task.founder_action) task.founder_action.steps = cleanLines(task.founder_action.steps);
    }
  }
  return result;
}

export type LandingExpectations = {
  contract: { profile: string; product_name: string; required_text: string[];
    actions: { id: string; [key: string]: unknown }[]; visuals?: { id: string; name: string }[] };
  criteria: { criterion: number; summary: string; text_indices: number[]; action_ids: string[]; visual_ids?: string[] }[];
};

// Explicit founder edit, not an automatic reinterpretation of a saved contract.
// Preserve every acceptance reference while moving one description to image QA.
export function describeImage(value: LandingExpectations, index: number): LandingExpectations {
  const result = structuredClone(value);
  const name = result.contract.required_text[index];
  if (!Number.isInteger(index) || index < 0 || !name || name.length < 12 || name.length > 160 ||
      result.contract.required_text.length <= 1 || (result.contract.visuals?.length ?? 0) >= 6) return result;
  const visuals = result.contract.visuals ?? [];
  const used = new Set([...visuals.map(item => item.id), ...result.contract.actions.map(item => item.id)]);
  let counter = 1;
  while (used.has(`image_${counter}`)) counter++;
  const id = `image_${counter}`;
  result.contract.visuals = [...visuals, { id, name }];
  result.contract.required_text.splice(index, 1);
  for (const criterion of result.criteria) {
    if (criterion.text_indices.includes(index)) criterion.visual_ids = [...(criterion.visual_ids ?? []), id];
    criterion.text_indices = criterion.text_indices.filter(item => item !== index).map(item => item > index ? item - 1 : item);
  }
  return result;
}
