/** A message typed while the previous reply is still "Sending…" waits its turn; it is never dropped. */
export function planSend(pending: boolean, queue: readonly string[], body: string): { sendNow: boolean; queue: string[] } {
  const text = body.trim();
  if (!text) return { sendNow: false, queue: [...queue] };
  return pending ? { sendNow: false, queue: [...queue, text] } : { sendNow: true, queue: [...queue] };
}

/** The next waiting message once nothing is in flight, and what remains. */
export function takeNext(queue: readonly string[]): { next: string | null; queue: string[] } {
  return queue.length ? { next: queue[0], queue: queue.slice(1) } : { next: null, queue: [] };
}
