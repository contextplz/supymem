export function idleWorkMessage(input: {
  nextTitle?: string;
  statusCode?: string;
}): string {
  if (!input.nextTitle) return "The queue is empty.";
  if (input.statusCode === "paused") {
    return `The venture is paused. “${input.nextTitle}” remains in the saved plan.`;
  }
  return `“${input.nextTitle}” is next in the saved plan. No task is running now.`;
}
