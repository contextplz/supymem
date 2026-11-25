"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, ensureSession, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { desktopListen, isDesktopShell } from "@/lib/desktop";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { setCharlieMascot } from "@/lib/charlieMascot";
import { CONTROL_OUTCOME_EVENT } from "@/lib/controlOutcome";
import { announceUsageControl } from "@/lib/usageControl";
import {
  audioLevel, cornerTransform, decodeBase64, loadGreeting, mascotPose, replayRequested, shouldShowIntro,
  playbackFailure, smoothLevel, speakWithSystemVoice, type Greeting, type GreetingOutcome,
} from "@/lib/charlieGreeting";
import styles from "./CharlieIntro.module.css";

const SIZE = 320;
const LEAVE_MS = 900;

type Phase = "loading" | "tap" | "retry" | "speaking" | "saving" | "leaving";
type Audio = { ctx: AudioContext; analyser: AnalyserNode; buffer: AudioBuffer };

const log = (msg: string, extra?: unknown) => console.info(`[charlie-intro] ${msg}`, extra ?? "");

export async function mountMascot(host: HTMLElement, reduced: boolean) {
  const { mountMascot } = await import("@/lib/mascotPlayer.js");
  return mountMascot(host, reduced, SIZE);
}

function mascotPoster() {
  return <>
    <img src="/charlie/poster.png" alt="" onLoad={event => { event.currentTarget.dataset.loaded = "true"; }} onError={event => { event.currentTarget.style.display = "none"; }} />
    <span className={styles.fallback}>C</span>
  </>;
}

async function decode(ctx: AudioContext, b64: string): Promise<AudioBuffer> {
  const bytes = decodeBase64(b64);
  const ab = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  // Older WebKit only supports the callback form.
  return new Promise((res, rej) => { const p = ctx.decodeAudioData(ab, res, rej); p?.then?.(res, rej); });
}

export function CharlieIntroWelcome({ onConnect, sampleEntry }: { onConnect: () => void; sampleEntry?: ReactNode }) {
  const hostRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    let unmount: (() => void) | undefined;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    void mountMascot(host, reduced).then(mounted => {
      if (disposed) mounted.dispose();
      else unmount = mounted.dispose;
    }).catch(() => {});
    return () => { disposed = true; unmount?.(); };
  }, []);

  return (
    <div className={styles.root} style={{ colorScheme: "light", background: "#fff", color: "#111827" }} role="dialog" aria-modal={true} aria-label="Meet Charlie" aria-describedby="charlie-local-description">
      <div className={styles.backdrop} style={{ background: "#fff" }} />
      <div className={styles.stage}>
        <div className={styles.mascot} style={{ width: SIZE, height: SIZE }}>
          <div ref={hostRef} className={styles.renderer} aria-hidden="true">{mascotPoster()}</div>
        </div>
        <div className={styles.caption} style={{ color: "inherit" }}>
          <p id="charlie-local-description">Supymem runs on your machine to help you research, create content, and grow your venture. You choose the work and connect your own model with your own keys.</p>
          <button type="button" className={styles.tap} style={{ background: "#111827", color: "#fff" }} autoFocus onClick={onConnect}>Connect a model</button>
          {sampleEntry}
        </div>
      </div>
    </div>
  );
}

export function CharlieIntro() {
  const local = useLocalMode();
  const [session, setSession] = useState({ desktop: false, subject: null as string | null, epoch: -1 });
  const { desktop, subject, epoch } = session;
  const enabled = desktop && !local;
  const [greeting, setGreeting] = useState<Greeting | null>(null);
  const [phase, setPhase] = useState<Phase>("tap");
  const [saveError, setSaveError] = useState("");
  const [gone, setGone] = useState(false);
  const bobRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<Audio | null>(null);
  const b64Ref = useRef<string | null>(null);
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);
  const finishRef = useRef<() => void>(() => {});
  const doneRef = useRef(false);
  const replayRef = useRef(false);
  const pendingAudio = useRef<Promise<string> | null>(null);
  const attemptRef = useRef(0);
  const activeRef = useRef(false);
  const voiceDeadline = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mascotRef = useRef<Awaited<ReturnType<typeof mountMascot>> | null>(null);

  function mascotSpeaking(speaking: boolean, level: number | null = null) {
    mascotRef.current?.setState(speaking ? "speaking" : "idle", speaking ? level : null);
    setCharlieMascot(speaking ? "speaking" : "idle", level);
  }

  useEffect(() => {
    let live = true;
    const refresh = async () => {
      if (isDesktopShell() && !verifiedSessionSubject()) await ensureSession();
      if (!live) return;
      const next = { desktop: isDesktopShell(), subject: verifiedSessionSubject(), epoch: verifiedSessionEpoch() };
      setSession(prev => prev.desktop === next.desktop && prev.subject === next.subject && prev.epoch === next.epoch ? prev : next);
    };
    refresh();
    const events = ["supymem-session-changed", "supymem-desktop-ready", "supymem-reconnect", "online", "focus"];
    events.forEach(event => window.addEventListener(event, refresh));
    return () => { live = false; events.forEach(event => window.removeEventListener(event, refresh)); };
  }, []);

  useEffect(() => {
    setGreeting(null); setGone(false); setPhase("tap"); setSaveError("");
    doneRef.current = false;
    if (!enabled) return;
    if (!subject) {
      announceUsageControl("greeting_suppressed_session");
      return;
    }
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    let stored: string | null = null;
    try { stored = window.localStorage.getItem("supymem.replayIntro"); } catch { /* storage unavailable */ }
    replayRef.current = replayRequested(window.location.search, stored);
    let fetching = false;
    const fetchGreeting = async () => {
      if (fetching || doneRef.current || !current()) return;
      fetching = true;
      const g = await loadGreeting(() => api("/api/me/greeting", { signal: controller.signal }), controller.signal, (ok, status) => {
        if (!current()) return;
        const controlId = ok ? "greeting_fetch_ok" : "greeting_fetch_failed";
        announceUsageControl(controlId);
        window.dispatchEvent(new CustomEvent(CONTROL_OUTCOME_EVENT, { detail: { controlId, ok, status, serverCode: null } }));
      });
      fetching = false;
      if (!current()) return;
      if (!g) return;
      if (shouldShowIntro(enabled, g, replayRef.current)) {
        announceUsageControl(replayRef.current ? "greeting_replayed" : "greeting_shown");
        setGreeting(prev => prev ?? g);
      } else announceUsageControl("greeting_suppressed_greeted");
    };
    void fetchGreeting();
    const refresh = () => { void fetchGreeting(); };
    const events = ["supymem-reconnect", "online", "focus"];
    events.forEach(event => window.addEventListener(event, refresh));
    let stopNative: (() => void) | undefined;
    void desktopListen("supymem-native-auth", refresh).then(stop => { if (controller.signal.aborted) stop(); else stopNative = stop; }).catch(() => {});
    return () => {
      controller.abort(); stopNative?.();
      events.forEach(event => window.removeEventListener(event, refresh));
    };
  }, [enabled, subject, epoch]);

  const reduced = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

  async function recordOutcome(outcome: GreetingOutcome): Promise<boolean> {
    if (replayRef.current) return true;
    if (subject !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch()) return false;
    try {
      const result = await api("/api/me/greeting/done", { method: "POST", body: JSON.stringify({ outcome }), headers: { "Content-Type": "application/json" }, signal: AbortSignal.timeout(15_000) });
      return result.ok && (result.data as { ok?: boolean } | null)?.ok === true;
    } catch { return false; }
  }

  async function finish(outcome: "completed" | "skipped") {
    if (doneRef.current) return;
    doneRef.current = true;
    const attempt = ++attemptRef.current;
    setSaveError(""); setPhase("saving");
    clearTimeout(voiceDeadline.current);
    if (activeRef.current) window.speechSynthesis?.cancel();
    activeRef.current = false;
    mascotSpeaking(false);
    try { sourceRef.current?.stop(); } catch { /* not started */ }
    sourceRef.current = null;
    void audioRef.current?.ctx.close().catch(() => {});
    const saved = await recordOutcome(outcome);
    if (attempt !== attemptRef.current || subject !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch()) return;
    if (!saved) {
      doneRef.current = false;
      setSaveError("Couldn’t save your introduction. Select Skip to try again.");
      setPhase("retry");
      return;
    }
    const control = outcome === "completed" ? "greeting_completed" : "greeting_skipped";
    announceUsageControl(control);
    const bob = bobRef.current;
    if (bob && !reduced()) {
      const t = cornerTransform({ width: window.innerWidth, height: window.innerHeight }, SIZE);
      bob.style.transition = `transform ${LEAVE_MS}ms cubic-bezier(.65,0,.25,1)`;
      bob.style.transform = `translate(${t.dx}px, ${t.dy}px) scale(${t.scale})`;
    }
    setPhase("leaving");
    window.setTimeout(() => { if (attempt === attemptRef.current) setGone(true); }, reduced() ? 300 : LEAVE_MS + 100);
  }
  finishRef.current = () => { void finish("completed"); };

  function audioFailed() {
    if (doneRef.current) return;
    clearTimeout(voiceDeadline.current);
    activeRef.current = false;
    mascotSpeaking(false);
    ++attemptRef.current;
    announceUsageControl("greeting_audio_failed");
    recordOutcome("failed");
    setPhase("retry");
  }

  /** Fetch the spoken greeting (base64 mp3). No AudioContext here: WKWebView keeps one made outside a gesture suspended. */
  async function fetchAudio(): Promise<string> {
    const q = replayRef.current ? "?replay=1" : "";
    const r = await api(`/api/me/greeting/audio${q}`, { method: "POST", body: "{}", headers: { "Content-Type": "application/json" } });
    const d = r.data as { audio_base64?: string } | null;
    if (!r.ok || !d?.audio_base64) throw new Error(`no audio (status ${r.status})`);
    return d.audio_base64;
  }

  function newContext(): AudioContext {
    const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    return new Ctor();
  }

  function play() {
    const a = audioRef.current;
    if (!a) return;
    const src = a.ctx.createBufferSource();
    src.buffer = a.buffer;
    src.connect(a.analyser);
    a.analyser.connect(a.ctx.destination);
    src.onended = () => { if (sourceRef.current === src && !doneRef.current) finishRef.current(); };
    sourceRef.current = src;
    src.start();
    setPhase("speaking");
    mascotSpeaking(true);
    log("audio started");
    const data = new Uint8Array(a.analyser.fftSize);
    let level = 0;
    const tick = () => {
      if (doneRef.current || sourceRef.current !== src) return;
      a.analyser.getByteTimeDomainData(data);
      level = smoothLevel(level, audioLevel(data));
      mascotSpeaking(true, level);
      const p = mascotPose(level);
      if (bobRef.current && !reduced()) bobRef.current.style.transform = `translateY(${p.bobPx}px) scale(${p.scale})`;
      requestAnimationFrame(tick);
    };
    tick();
  }

  useEffect(() => {
    if (!enabled || !greeting || gone) return; // once the intro has left, the renderer must be torn down, not left looping
    let disposed = false;
    let unmount: (() => void) | undefined;
    announceUsageControl("greeting_painted");
    setPhase("tap");
    void (async () => {
      if (hostRef.current) {
        const host = hostRef.current;
        mountMascot(host, reduced()).then(mounted => {
          if (disposed) mounted.dispose(); else {
            mascotRef.current = mounted;
            unmount = mounted.dispose;
            if (!doneRef.current && (sourceRef.current || activeRef.current)) mascotSpeaking(true);
            log("mascot rendered");
          }
        }).catch((e) => log("mascot failed", String(e)));
      }
      pendingAudio.current = fetchAudio();
      try {
        const b64 = await pendingAudio.current;
        if (!disposed) b64Ref.current = b64;
      } catch (e) {
        if (disposed) return;
        log("audio fetch failed", String(e));
        announceUsageControl("greeting_audio_failed");
        recordOutcome("failed");
        pendingAudio.current = null;
      }
    })();
    return () => {
      disposed = true; ++attemptRef.current; unmount?.();
      mascotRef.current = null;
      setCharlieMascot("idle");
      clearTimeout(voiceDeadline.current);
      sourceRef.current = null;
      void audioRef.current?.ctx.close().catch(() => {});
      audioRef.current = null; b64Ref.current = null; pendingAudio.current = null;
      if (activeRef.current) window.speechSynthesis?.cancel();
      activeRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, greeting, gone]);

  if (!enabled || !greeting || gone || subject !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch()) return null;
  const fade = phase === "leaving";
  const systemVoice = () => {
    const w = window as unknown as { speechSynthesis?: Parameters<typeof speakWithSystemVoice>[1]; SpeechSynthesisUtterance?: Parameters<typeof speakWithSystemVoice>[2] };
    const attempt = attemptRef.current;
    const current = () => attempt === attemptRef.current && !doneRef.current;
    activeRef.current = true;
    if (!speakWithSystemVoice(greeting.text, w.speechSynthesis, w.SpeechSynthesisUtterance,
      () => { if (current()) finishRef.current(); }, () => { if (current()) audioFailed(); },
      () => { if (current()) { clearTimeout(voiceDeadline.current); setPhase("speaking"); mascotSpeaking(true); } })) {
      activeRef.current = false;
      return false;
    }
    announceUsageControl("greeting_voice_fallback");
    voiceDeadline.current = setTimeout(() => {
      if (current()) { w.speechSynthesis?.cancel(); audioFailed(); }
    }, 3000);
    return true;
  };
  const tap = async () => {
    setPhase("loading");
    const attempt = ++attemptRef.current;
    const current = () => attempt === attemptRef.current && !doneRef.current;
    let ctx: AudioContext | null = null;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    try {
      ctx = newContext(); // created and resumed inside the gesture
      const resumed = ctx.resume().catch(() => {});
      const context = ctx;
      const buffer = await Promise.race([(async () => {
        const b64 = b64Ref.current ?? await (pendingAudio.current ??= fetchAudio());
        await resumed;
        const failure = playbackFailure(context.state, true);
        if (failure) throw new Error(failure);
        return decode(context, b64);
      })(), new Promise<never>((_, reject) => { deadline = setTimeout(() => reject(new Error("audio_timeout")), 3000); })]);
      if (!current()) { void ctx.close().catch(() => {}); return; }
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      audioRef.current = { ctx, analyser, buffer };
      play();
    } catch (e) {
      if (!current()) { void ctx?.close().catch(() => {}); return; }
      const reason = String(e instanceof Error ? e.message : e);
      log(`playback failed: ${reason}`, ctx?.state);
      const control = reason.includes("ctx_suspended") ? "greeting_ctx_suspended" : reason.includes("decode_failed") ? "greeting_decode_failed" : "greeting_audio_failed";
      announceUsageControl(control);
      void ctx?.close().catch(() => {});
      audioRef.current = null;
      recordOutcome("failed");
      if (!systemVoice()) audioFailed();
    } finally { clearTimeout(deadline); }
  };
  return (
    <div className={styles.root} data-leaving={fade} data-reduced={fade && reduced()} role="dialog" aria-label="Meet Charlie">
      <div className={styles.backdrop} />
      <button type="button" className={styles.skip} data-usage-control="greeting_skip" disabled={phase === "saving" || fade} onClick={() => void finish("skipped")}>Skip</button>
      <div className={styles.stage}>
        <div ref={bobRef} className={styles.mascot} style={{ width: SIZE, height: SIZE }}>
          <div ref={hostRef} className={styles.renderer} aria-hidden="true">{mascotPoster()}</div>
        </div>
        {!fade && (
          <div className={styles.caption} aria-live="polite">
            <ErrorNotice error={saveError} source="CharlieIntro.save" />
            {phase !== "tap" && phase !== "loading" && <p>{greeting.text}</p>}
            {phase === "tap" || phase === "retry"
              ? <>
                <button type="button" className={styles.tap} data-usage-control="greeting_tap" onClick={() => void tap()}>
                  {phase === "retry" ? "Tap to hear Charlie" : "Tap to meet Charlie"}
                </button>
              </>
              : phase === "loading" ? <span>Charlie is getting ready…</span> : phase === "saving" ? <span>Saving your choice…</span> : null}
          </div>
        )}
      </div>
    </div>
  );
}
