export type SurfaceCapability = {
  state: "ready" | "needs_setup" | "off" | "not_in_this_version";
  surfaces?: Record<string, string[]>;
};

const RECOVERY_SETTINGS = new Set(["providers", "search", "smtp", "publishing", "composio", "spend", "data"]);

export function surfaceCapabilities(value: unknown): SurfaceCapability[] | null {
  if (!Array.isArray(value)) return null;
  const states = ["ready", "needs_setup", "off", "not_in_this_version"];
  if (!value.every(item => item && typeof item === "object" && states.includes(item.state)
    && (item.surfaces === undefined || item.surfaces && typeof item.surfaces === "object"
      && !Array.isArray(item.surfaces) && Object.values(item.surfaces).every(matches =>
        Array.isArray(matches) && matches.every(match => typeof match === "string"))))) return null;
  return value;
}

export function surfaceVisible(capabilities: SurfaceCapability[] | null, surface: string, value: string, local: boolean): boolean {
  if (!local) return true;
  if (!capabilities) return surface === "settings" && RECOVERY_SETTINGS.has(value)
    || surface === "actions" && value === "status";
  const target = surface === "paths" ? value.split(/[?#]/, 1)[0].replace(/^\/v\/[^/]+/, "") : value;
  return !capabilities.some(item => item.state === "not_in_this_version"
    && item.surfaces?.[surface]?.some(match => target === match || surface === "paths" && target.startsWith(`${match}/`)));
}
