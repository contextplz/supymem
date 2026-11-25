/** "Report a problem": the body for POST /api/problem-reports. The page is a route template (ids -> {id}); the
 *  screenshot is only included when the founder picked a file AND ticked the consent box. */
export const MAX_SCREENSHOT_BYTES = 1_500_000;

export type ProblemInput = {
  message: string;
  pathname: string;
  appVersion?: string | null;
  ventureId?: string | null;
  screenshotB64?: string | null;
  consent?: boolean;
};

export function pageTemplate(pathname: string): string {
  const path = pathname.split(/[?#]/)[0].replace(/\/[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*(?=\/|$)/g, "/{id}");
  return /^\/[A-Za-z0-9_{}./-]{0,119}$/.test(path) ? path : "";
}

export function buildProblemBody(input: ProblemInput): Record<string, unknown> {
  const body: Record<string, unknown> = { message: input.message.trim().slice(0, 2000), page: pageTemplate(input.pathname) };
  if (input.appVersion && /^[A-Za-z0-9_.-]{1,32}$/.test(input.appVersion)) body.app_version = input.appVersion;
  if (input.ventureId) body.venture_id = input.ventureId;
  if (input.screenshotB64 && input.consent) {
    body.screenshot_b64 = input.screenshotB64;
    body.screenshot_consent = true;
  }
  return body;
}

export function screenshotProblem(file: { type: string; size: number }): string | null {
  if (!["image/png", "image/jpeg"].includes(file.type)) return "Attach a PNG or JPEG screenshot.";
  if (file.size > MAX_SCREENSHOT_BYTES) return "That screenshot is over 1.5 MB. Attach a smaller one.";
  return null;
}
