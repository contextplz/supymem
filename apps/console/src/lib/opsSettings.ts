/** Only the existing config endpoint's explicit absence response means onboarding. */
export function isMissingCompanyConfig(error: unknown): boolean {
  return typeof error === "object" && error !== null &&
    "status" in error && error.status === 404 &&
    "detail" in error && error.detail === "No company config yet";
}

const EDITABLE_FIELDS = ["name", "industry", "product_type", "website_url", "mission", "vision",
  "description", "target_market", "value_prop", "timezone", "daily_cycle_hour"] as const;

/** Send only edited settings, never response metadata or unrelated goals/KPIs. */
export function companySettingsPayload(config: object): Record<string, unknown> {
  const values = config as Record<string, unknown>;
  if (typeof values.name !== "string" || !values.name.trim()) throw new Error("Enter a company name.");
  if (typeof values.timezone !== "string" || !values.timezone.trim()) throw new Error("Enter a timezone.");
  try { new Intl.DateTimeFormat("en", { timeZone: values.timezone.trim() }); }
  catch { throw new Error("Enter a valid timezone, such as UTC or Asia/Kolkata."); }
  if (typeof values.daily_cycle_hour !== "number" || !Number.isInteger(values.daily_cycle_hour) ||
      values.daily_cycle_hour < 0 || values.daily_cycle_hour > 23) throw new Error("Choose a cycle hour from 0 to 23.");
  return { ...Object.fromEntries(EDITABLE_FIELDS.map((key) => [key, values[key]])),
    name: values.name.trim(), timezone: values.timezone.trim() };
}
