export type Capability = {
  name: string;
  title: string;
  state: "ready" | "needs_setup" | "off" | "not_in_this_version";
  missing: string[];
  next_step: string;
  background_jobs?: { description: string; schedule: string }[];
  surfaces?: Record<string, string[]>;
};

export const SETTINGS: Record<string, string[]> = {
  research: ["search", "providers"], find_customers: ["search", "providers"],
  send_email: ["smtp"], read_replies: ["smtp"], landing_pages: ["providers"],
  publish_pages: ["publishing"], page_measurement: [], background_work: [],
  run_for_me_experiments: ["providers"], routines: [], connected_apps: ["composio"], content: ["providers"],
  build_with_my_tool: [],
};

export const SECTION_TITLES: Record<string, string> = {
  search: "Your search and reader", providers: "Your AI provider", smtp: "Your outgoing mail",
  publishing: "Your page publishing accounts", composio: "Your app connections",
};

export const STATE_TITLES = {
  ready: "Ready", needs_setup: "Needs setup", off: "Off", not_in_this_version: "Not in this version yet",
};

export function capabilityView(value: unknown): Capability | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Capability;
  const jobs = item.background_jobs === undefined ? [] : item.background_jobs;
  return (Object.hasOwn(SETTINGS, item.name) || item.state === "not_in_this_version") && typeof item.name === "string" && typeof item.title === "string"
    && Object.hasOwn(STATE_TITLES, item.state) && Array.isArray(item.missing)
    && item.missing.every(field => typeof field === "string") && typeof item.next_step === "string"
    && Array.isArray(jobs) && jobs.every(job => job && typeof job.description === "string" && typeof job.schedule === "string")
    ? { name: item.name, title: item.title, state: item.state, missing: item.missing, next_step: item.next_step,
        background_jobs: jobs.map(job => ({ description: job.description, schedule: job.schedule })), surfaces: item.surfaces }
    : null;
}
