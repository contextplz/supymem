/** Standard `error_event` log line for the Console's Next.js server (same shape as packages/core/error_event.py). */
const slug = (value: string, max = 48) => value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, max) || "other";

export function consoleErrorEvent(error: unknown, method: string, routePath: string, routeType: string): string {
  const name = error instanceof Error && /^[A-Za-z0-9_.:-]{1,64}$/.test(error.name) ? error.name : "Error";
  return "error_event " + JSON.stringify({
    app_version: process.env.APP_VERSION || process.env.IMAGE_TAG || "unknown",
    code: name, component: "console_server", intent: `${slug(method, 8)}_${slug(routePath)}`,
    source: slug(routeType, 24), status: 500,
  });
}
