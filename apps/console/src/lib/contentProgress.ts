export function runProgress(run: { kind: string; state: string; phase: string }): string {
  const what = run.kind === "topics" ? "post ideas" : "draft";
  if (run.state === "completed") return run.kind === "topics" ? "Your post ideas are ready." : "Your draft is ready.";
  if (run.state === "refused") return "The draft did not pass the quality check.";
  if (run.state === "failed" || run.state === "failed_dead") return `Your ${what} could not be finished.`;
  if (run.state === "needs_input") return `Your ${what} needs your attention.`;
  if (run.state === "queued" || run.phase === "queued") return `Getting ready to write your ${what}…`;
  if (run.phase.startsWith("critic")) return "Checking quality…";
  return run.kind === "topics" ? "Planning your post ideas…" : "Writing your draft…";
}
