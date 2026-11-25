type Result = { ok: boolean; data: unknown };
type Options = {
  read: (path: string, onData: (result: Result) => void) => Promise<unknown>;
  scope: () => string | null;
  accept: (name: string) => void;
  schedule?: (callback: () => void, milliseconds: number) => ReturnType<typeof setTimeout>;
  cancelTimer?: (timer: ReturnType<typeof setTimeout>) => void;
};

/** Read-only missing-name recovery. Every delivery is fenced to its requesting account.
 * Backoff caps at 30s and stops once a name is known, navigation or a local rename wins.
 */
export function startVentureNameRecovery(id: string, options: Options) {
  const schedule = options.schedule || setTimeout;
  const cancelTimer = options.cancelTimer || clearTimeout;
  let disposed = false, pending = false, resolved = false, attempt = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const clear = () => { if (timer !== undefined) cancelTimer(timer); timer = undefined; };
  async function refresh() {
    if (disposed || pending || resolved) return;
    clear();
    pending = true;
    const owner = options.scope();
    try {
      await options.read(`/api/ventures/${encodeURIComponent(id)}`, result => {
        if (disposed || !owner || owner !== options.scope() || !result.ok) return;
        const venture = result.data as { id?: unknown; name?: unknown } | null;
        if (venture?.id !== id || typeof venture.name !== "string" || !venture.name.trim()) return;
        resolved = true;
        clear();
        options.accept(venture.name.trim());
      });
    } catch { /* A failed read retains existing UI state; never repeat a mutation. */ }
    finally {
      pending = false;
      if (!disposed && !resolved) {
        const delay = [1000, 3000, 8000, 15000, 30000][Math.min(attempt++, 4)];
        timer = schedule(() => { void refresh(); }, delay);
      }
    }
  }
  return { refresh, stop: () => { disposed = true; clear(); } };
}
