// Pure helpers for Charlie's first-launch greeting (Mac app, once per account).

export type Greeting = { text: string; firstName: string | null; greetedAt: string | null };
export type GreetingOutcome = "completed" | "skipped" | "failed";

export async function loadGreeting(
  request: () => Promise<{ ok: boolean; status: number; data?: unknown }>, signal: AbortSignal,
  observed: (ok: boolean, status: number) => void,
): Promise<Greeting | null> {
  let delay = 1000;
  for (let attempt = 0; attempt < 3 && !signal.aborted; attempt++) {
    let greeting: Greeting | null = null;
    let status = 0;
    try {
      const result = await request();
      status = result.status;
      greeting = result.ok ? parseGreeting(result.data) : null;
    } catch { /* retry transport failures */ }
    if (signal.aborted) return null;
    if (greeting) { observed(true, status); return greeting; }
    if (attempt === 2 || (status >= 400 && status < 500 && status !== 429)) {
      observed(false, status);
      return null;
    }
    await new Promise<void>((resolve) => {
      const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
      const timer = setTimeout(done, delay);
      signal.addEventListener("abort", done, { once: true });
    });
    delay *= 2;
  }
  return null;
}

/** Validate the /api/me/greeting payload before deciding or retrying. */
export function parseGreeting(data: unknown): Greeting | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.text !== "string" || !d.text.trim()) return null;
  if (d.greeted_at != null && typeof d.greeted_at !== "string") return null;
  if (d.first_name != null && typeof d.first_name !== "string") return null;
  return {
    text: d.text,
    firstName: typeof d.first_name === "string" ? d.first_name : null,
    greetedAt: typeof d.greeted_at === "string" && d.greeted_at ? d.greeted_at : null,
  };
}

export function shouldShowIntro(desktop: boolean, greeting: Greeting | null, replay = false): boolean {
  return desktop && greeting !== null && (replay || greeting.greetedAt === null);
}

/** Dev/test override: ?replayIntro=1 or localStorage supymem.replayIntro=1. Never touches server state. */
export function replayRequested(search: string, stored: string | null): boolean {
  return new URLSearchParams(search).get("replayIntro") === "1" || stored === "1";
}

/** RMS of a WebAudio time-domain byte buffer (128 = silence), scaled so speech reaches about 0..1. */
export function audioLevel(samples: Uint8Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (const s of samples) {
    const v = (s - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / samples.length) * 3);
}

export function smoothLevel(prev: number, next: number): number {
  return prev + (next - prev) * (next > prev ? 0.5 : 0.15); // quick attack, slow release
}

/** Gentle scale and bob for a level in 0..1. */
export function mascotPose(level: number): { scale: number; bobPx: number } {
  const l = Math.max(0, Math.min(1, level));
  return { scale: 1 + 0.1 * l, bobPx: -8 * l };
}

/** Translate/scale that moves a centered element of `size` px to the bottom-right corner at `cornerSize` px. */
export function cornerTransform(
  viewport: { width: number; height: number },
  size: number,
  cornerSize = 64,
  inset = 24,
): { dx: number; dy: number; scale: number } {
  const scale = cornerSize / size;
  return {
    dx: viewport.width / 2 - inset - cornerSize / 2,
    dy: viewport.height / 2 - inset - cornerSize / 2,
    scale,
  };
}

export function decodeBase64(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

type Synth = { speak(u: unknown): void; cancel(): void } | undefined;

/** Last-resort voice: the OS speech engine, no server, no microphone. Call inside a user gesture.
 *  Returns false when unavailable so the caller can keep the caption and report it. */
export function speakWithSystemVoice(
  text: string, synth: Synth, Utter: (new (t: string) => { onend: unknown; onerror: unknown; onstart?: unknown }) | undefined,
  onEnd: () => void, onError: () => void = () => {}, onStart: () => void = () => {},
): boolean {
  if (!synth || !Utter || !text.trim()) return false;
  try {
    const u = new Utter(text);
    u.onend = onEnd;
    u.onerror = onError;
    u.onstart = onStart;
    synth.cancel();
    synth.speak(u);
    return true;
  } catch { return false; }
}

/** Why playback cannot start: a context that is not running is silent (WKWebView) and an undecodable buffer is too. */
export function playbackFailure(ctxState: string, decoded: boolean): "ctx_suspended" | "decode_failed" | null {
  if (ctxState !== "running") return "ctx_suspended";
  return decoded ? null : "decode_failed";
}
