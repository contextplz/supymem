export function chatFlight() {
  let active: AbortController | null = null;
  return {
    busy() { return active !== null; },
    begin() { const controller = new AbortController(); active = controller; return controller; },
    owns(controller: AbortController) { return active === controller && !controller.signal.aborted; },
    finish(controller: AbortController) { if (active === controller) active = null; },
    stop() { active?.abort(); active = null; },
  };
}

export async function waitForChatRequest<Result>(request: Promise<Result>, signal?: AbortSignal | null): Promise<Result> {
  if (!signal) return request;
  let abort: () => void = () => {};
  try {
    return await Promise.race([request, new Promise<never>((_, reject) => {
      abort = () => reject(new DOMException("Request stopped; already sent work may still finish", "AbortError"));
      signal.addEventListener("abort", abort, {once: true});
      if (signal.aborted) abort();
    })]);
  } finally {
    signal.removeEventListener("abort", abort);
  }
}
