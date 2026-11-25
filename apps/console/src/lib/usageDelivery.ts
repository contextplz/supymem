/** Keep transient telemetry failures for a later authenticated retry. */
export type QueuedTelemetry = { body: Record<string, unknown>; attempts: number; nextAttemptAt?: number };

export function enqueueTelemetry(queue: QueuedTelemetry[], item: QueuedTelemetry, maxItems: number): void {
  if (maxItems < 1) return;
  if (queue.length >= maxItems) {
    // A long offline session should not replace a diagnostic with a passive
    // page view. Keep the queue bounded and favor actionable failures.
    const oldestOrdinary = queue.findIndex(entry => !["ui.error", "ui.dead_end"].includes(String(entry.body.event)));
    if (oldestOrdinary >= 0) queue.splice(oldestOrdinary, 1);
    else if (["ui.error", "ui.dead_end"].includes(String(item.body.event))) queue.shift();
    else return;
  }
  queue.push(item);
}

export function terminalTelemetryStatus(status: number): boolean {
  return status >= 400 && status < 500 && ![401, 408, 425, 429].includes(status);
}

export function telemetryRetryDelayMs(attempts: number): number {
  return Math.min(300_000, 15_000 * 2 ** Math.min(Math.max(attempts - 1, 0), 5));
}

type FlushOptions = {
  queue: () => QueuedTelemetry[];
  owner: () => string | undefined;
  persist: () => void;
  send: (body: Record<string, unknown>) => Promise<{ ok: boolean; status: number }>;
  now: () => number;
  online: () => boolean;
};

export async function flushQueuedTelemetry(options: FlushOptions): Promise<void> {
  if (!options.online()) return;
  while (options.queue().length) {
    const changedAccount = options.queue().findIndex(item => item.body.context_subject !== options.owner());
    if (changedAccount >= 0) {
      options.queue().splice(changedAccount, 1);
      options.persist();
      continue;
    }
    // A backoff on a passive view must not delay a fresh error report.
    const eligible = (item: QueuedTelemetry) => (item.nextAttemptAt || 0) <= options.now();
    const urgent = options.queue().findIndex(item => ["ui.error", "ui.dead_end"].includes(String(item.body.event)) && eligible(item));
    const index = urgent >= 0 ? urgent : options.queue().findIndex(eligible);
    if (index < 0) break;
    const item = options.queue()[index];
    item.attempts++;
    let result: { ok: boolean; status: number };
    try {
      result = await options.send(item.body);
    } catch {
      if (options.queue().includes(item)) {
        item.nextAttemptAt = options.now() + telemetryRetryDelayMs(item.attempts);
        options.persist();
      }
      continue;
    }
    // A changed account can replace the queue while the network request is in flight.
    if (!options.queue().includes(item)) continue;
    if (result.ok || terminalTelemetryStatus(result.status)) {
      options.queue().splice(options.queue().indexOf(item), 1);
      options.persist();
    } else {
      item.nextAttemptAt = options.now() + telemetryRetryDelayMs(item.attempts);
      options.persist();
    }
  }
}

export function retryQueuedOnReconnect(queue: QueuedTelemetry[], persist: () => void): void {
  if (queue.length) {
    for (const item of queue) item.nextAttemptAt = 0;
    persist();
  }
}
