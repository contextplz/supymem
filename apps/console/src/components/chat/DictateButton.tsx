"use client";

import { useEffect, useRef, useState } from "react";
import { desktopInvokeOrThrow, desktopInvokeQuiet, desktopListenOrThrow, isDesktopShell } from "@/lib/desktop";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { setCharlieMascot } from "@/lib/charlieMascot";
import styles from "./VoiceMic.module.css";

type Recognition = {
  continuous: boolean; interimResults: boolean; lang: string;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: (() => void) | null; onend: (() => void) | null;
  start: () => void; stop: () => void; abort: () => void;
};
function browserRecognition() {
  if (typeof window === "undefined") return undefined;
  const speech = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  return speech.SpeechRecognition || speech.webkitSpeechRecognition;
}
const dictationRetry = (cause: unknown) => {
  const message = typeof cause === "string" ? cause : cause instanceof Error ? cause.message : "";
  return message === "Allow Speech Recognition in System Settings, or type your answer."
    ? message : "Dictation stopped. Check microphone access, retry Dictate, or type your answer.";
};

/** Dictation only changes the draft. Sending always requires the composer. */
export function DictateButton({ value, onChange, disabled, scope }: { value: string; onChange: (value: string) => void; disabled?: boolean; scope?: string | null }) {
  const [native, setNative] = useState(false);
  const [available, setAvailable] = useState(false);
  const [listening, setListening] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<{ id: string; base: string; last: string; scope?: string | null; started?: boolean; speech?: Recognition; unlisten?: () => void } | null>(null);
  const latest = useRef({ value, onChange, scope }); latest.current = { value, onChange, scope };
  useEffect(() => {
    let alive = true;
    setListening(false); setError("");
    setAvailable(!isDesktopShell() && Boolean(browserRecognition()));
    if (isDesktopShell()) void desktopInvokeQuiet<boolean>("dictation_available", { locale: navigator.language }).then((ok) => {
      if (alive && ok === true) { setNative(true); setAvailable(true); }
    });
    return () => {
      alive = false;
      const session = active.current; active.current = null;
      session?.speech?.abort(); session?.unlisten?.();
      if (session && !session.speech) void desktopInvokeOrThrow("dictation_stop", { sessionId: session.id }, { quiet: true }).catch(() => undefined);
    };
  }, [scope]);
  useEffect(() => {
    if (!listening) return;
    setCharlieMascot("listening");
    return () => setCharlieMascot("idle");
  }, [listening]);
  const finish = () => {
    const session = active.current; active.current = null;
    session?.speech?.abort(); session?.unlisten?.(); setListening(false);
  };
  const update = (text: string) => {
    const session = active.current;
    if (!session || latest.current.scope !== session.scope) return;
    // Preserve a manual edit rather than replacing it with a late partial result.
    if (latest.current.value !== session.last) return;
    session.last = [session.base, text.trim()].filter(Boolean).join(" ");
    latest.current.onChange(session.last);
    latest.current.value = session.last;
  };
  async function toggle() {
    const session = active.current;
    if (session) {
      if (!session.started) { finish(); return; }
      try {
        if (session.speech) session.speech.stop();
        else await desktopInvokeOrThrow("dictation_stop", { sessionId: session.id });
      } catch { if (active.current === session) setError("Could not stop dictation. Try Stop again."); }
      return;
    }
    setError("");
    const next = { id: crypto.randomUUID(), base: value.trimEnd(), last: value, scope } as NonNullable<typeof active.current>;
    active.current = next; setListening(true);
    try {
      if (native) {
        next.unlisten = await desktopListenOrThrow("dictation-transcript", (payload: unknown) => {
          const event = payload as { sessionId?: string; text?: string; isFinal?: boolean; error?: string };
          if (active.current !== next || event.sessionId !== next.id) return;
          if (typeof event.text === "string") update(event.text);
          if (event.error) setError(dictationRetry(event.error));
          if (event.isFinal || event.error) finish();
        });
        if (active.current !== next) { next.unlisten(); return; }
        next.started = true;
        await desktopInvokeOrThrow("dictation_start", { sessionId: next.id, locale: navigator.language }, { quiet: true });
      } else {
        const Speech = browserRecognition();
        if (!Speech) { finish(); return; }
        const speech = new Speech(); next.speech = speech;
        speech.continuous = true; speech.interimResults = true; speech.lang = navigator.language;
        speech.onresult = (event) => { if (active.current === next) update(Array.from(event.results, (result) => result[0]?.transcript || "").join(" ")); };
        speech.onerror = () => { if (active.current !== next) return; setError("Dictation stopped. Check microphone access or type your answer."); finish(); };
        speech.onend = () => { if (active.current === next) finish(); }; next.started = true; speech.start();
      }
    } catch (cause) {
      if (active.current !== next) return;
      setError(dictationRetry(cause));
      finish();
    }
  }
  if (!available) return null;
  return <span className={styles.wrap} data-console-reload-blocked={listening}>
    <button type="button" className={styles.mic} disabled={disabled && !listening} aria-pressed={listening}
      data-usage-control="dictation_toggle" aria-label={listening ? "Stop dictation" : "Dictate"}
      title="Dictate into your draft. Review it before sending." onClick={() => void toggle()}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M7 7h10M7 12h10M7 17h6" /></svg>
      <span>{listening ? "Stop" : "Dictate"}</span>
    </button>
    {error && <ErrorNotice error={error} source="dictation.error" as="span" className={styles.error} />}
  </span>;
}
