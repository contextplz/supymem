export function ideaJourneyActivity({ busy, jobId, reconciling, hasNextStep = false }: { busy: boolean; jobId: string | null; reconciling: boolean; hasNextStep?: boolean }): "saving" | "working" | "checking" | "idle" {
  return busy ? "saving" : hasNextStep ? "idle" : jobId ? "working" : reconciling ? "checking" : "idle";
}
