const STORAGE_REASONS = new Set([
  "import_store_configuration_unavailable",
  "import_cleanup_not_accepted",
  "import_storage_not_accepted",
]);

const ISOLATION_REASONS = new Set([
  "import_parser_isolation_unavailable",
  "import_parser_platform_unsupported",
]);

export function importAvailabilityMessage(reasons: string[] | undefined): string {
  const unique = new Set((reasons || []).filter(Boolean));
  const missing: string[] = [];
  if ([...unique].some(reason => STORAGE_REASONS.has(reason))) missing.push("encrypted source storage and cleanup");
  if ([...unique].some(reason => ISOLATION_REASONS.has(reason))) missing.push("an isolated project parser");
  if (unique.has("import_intake_not_accepted")) missing.push("the production safety review");

  if (!missing.length) {
    return "Project import is not available in this environment yet. Your source has not been uploaded or executed.";
  }
  const joined = missing.length === 1
    ? missing[0]
    : `${missing.slice(0, -1).join(", ")} and ${missing[missing.length - 1]}`;
  return `Project import will become available after Supymem finishes ${joined}. Your source has not been uploaded or executed.`;
}
