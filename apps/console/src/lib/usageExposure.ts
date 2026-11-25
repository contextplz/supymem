/** Keep a live control observed until it is exposed, disabled, or leaves the route.
 * Re-observing on every unrelated DOM mutation can discard a pending callback.
 */
export function syncExposureObservation<T extends object>(
  element: T,
  observed: WeakSet<T>,
  observer: { observe(target: T): void; unobserve(target: T): void },
  enabled: boolean,
): void {
  if (!enabled) {
    if (observed.has(element)) {
      observer.unobserve(element);
      observed.delete(element);
    }
    return;
  }
  if (!observed.has(element)) {
    observer.observe(element);
    observed.add(element);
  }
}

export function qualifiesForExposure(isIntersecting: boolean, ratio: number): boolean {
  return isIntersecting && ratio >= 0.25;
}

/** A real click proves that a viewport-visible control was reachable before its
 * handler can disable or replace it. Passive background observation stays gated.
 */
export function emitVisibleActivation(
  controlId: string,
  exposed: Set<string>,
  trusted: boolean,
  visibleRatio: number,
  emitExposure: () => void,
  emitActivation: () => void,
): void {
  if (controlId !== "mac_start" && trusted && visibleRatio >= 0.25 && !exposed.has(controlId)) {
    exposed.add(controlId);
    emitExposure();
  }
  emitActivation();
}

/** Viewport intersection fraction of the control; no label or content is read. */
export function viewportVisibleRatio(
  rect: { left: number; top: number; right: number; bottom: number; width: number; height: number },
  viewport: { width: number; height: number },
): number {
  const area = rect.width * rect.height;
  if (!Number.isFinite(area) || area <= 0) return 0;
  const width = Math.max(0, Math.min(rect.right, viewport.width) - Math.max(rect.left, 0));
  const height = Math.max(0, Math.min(rect.bottom, viewport.height) - Math.max(rect.top, 0));
  return Math.min(1, width * height / area);
}
