export type CorrectionAvailability = {
  build_id: string; available: boolean; reason: string;
  source_artifact_id?: string; report_digest?: string;
};

export function correctionAvailabilityMatches(value: CorrectionAvailability | null,
  owner: {build: string; artifact: string; report: string}): boolean {
  return value?.available === true && value.build_id === owner.build && !!owner.artifact && !!owner.report &&
    value.source_artifact_id === owner.artifact && value.report_digest === owner.report;
}
