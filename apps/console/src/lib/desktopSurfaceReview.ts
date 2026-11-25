export type ReviewedDesktopSurface = {
  application_bundle: string; process_id: number; process_start_identity: string;
  window_id: number; window_title: string;
};
export type SurfaceReviewReceipt = { ventureId: string; epoch: number; surfaces: ReviewedDesktopSurface[] };
const bundle = /^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/;
const start = /^[0-9]{1,24}$/;
export function validDesktopSurface(value: unknown): value is ReviewedDesktopSurface {
  if (!value || typeof value !== "object") return false;
  const s = value as ReviewedDesktopSurface;
  if (Object.keys(value).sort().join(",") !== "application_bundle,process_id,process_start_identity,window_id,window_title") return false;
  return typeof s.application_bundle === "string" && s.application_bundle.length <= 200 && bundle.test(s.application_bundle)
    && Number.isSafeInteger(s.process_id) && s.process_id > 0
    && typeof s.process_start_identity === "string" && start.test(s.process_start_identity)
    && Number.isSafeInteger(s.window_id) && s.window_id > 0
    && typeof s.window_title === "string" && s.window_title.length <= 200;
}
export function surfaceKey(s: ReviewedDesktopSurface): string {
  return JSON.stringify([s.application_bundle, s.process_id, s.process_start_identity, s.window_id]);
}
/** The caller supplies only checkbox-selected windows, never task prose. */
export function reviewedSurfaceBody(receipt: SurfaceReviewReceipt | null, ventureId: string, epoch: number):
    { reviewed_surfaces: ReviewedDesktopSurface[] } | undefined {
  if (!receipt) return undefined;
  if (receipt.ventureId !== ventureId || receipt.epoch !== epoch) throw new Error("The selected windows belong to an earlier session. Select them again.");
  const surfaces = receipt.surfaces;
  if (!surfaces.length || surfaces.length > 8 || !surfaces.every(validDesktopSurface)) throw new Error("Select one to eight exact existing windows.");
  const keys = new Set<string>(), processes = new Map<number, string>(), bundles = new Map<string, string>();
  for (const s of surfaces) {
    const identity = JSON.stringify([s.application_bundle, s.process_start_identity]);
    const process = JSON.stringify([s.process_id, s.process_start_identity]);
    const key = JSON.stringify([s.process_id, s.window_id]);
    if (keys.has(key) || (processes.has(s.process_id) && processes.get(s.process_id) !== identity)
        || (bundles.has(s.application_bundle) && bundles.get(s.application_bundle) !== process)) throw new Error("A selected window identity changed. Select it again.");
    keys.add(key); processes.set(s.process_id, identity); bundles.set(s.application_bundle, process);
  }
  return { reviewed_surfaces: surfaces.map(s => ({...s})) };
}
