"use client";

import { useCallback, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { VoiceOrb } from "./VoiceOrb";
import { LONG_PRESS_MS, PHASE_LABEL, voiceSessionError, type VoiceScreen } from "@/lib/voice";
import { useVoiceKeys, useVoiceChat, voiceCallActive, type VoiceSend } from "./useVoiceChat";
import styles from "./VoiceMic.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

/** Live audio-level bars: mic input while listening, Charlie's output while speaking. */
function Bars() {
  return <span className={styles.bars} aria-hidden="true"><i /><i /><i /><i /><i /></span>;
}

/** Tap-to-talk mic for the composer row (long press = push-to-talk). Renders nothing unless voice is enabled. */
export function VoiceMic({ ventureId, onVoiceSend, disabled, conversationId, onToolsComplete, screen, onSummary, targetVentureId, active, onTypeInstead, compact }: {
  ventureId: string | null;
  onVoiceSend: VoiceSend;
  disabled?: boolean;
  active?: boolean;
  onTypeInstead?: () => void;
  compact?: boolean;
  conversationId?: string | null;
  targetVentureId?: string | null;
  onToolsComplete?: () => void;
  screen?: VoiceScreen;
  onSummary?: (markdown: string, noteId?: string) => void;
}) {
  const v = useVoiceChat(ventureId, onVoiceSend, { conversationId, onToolsComplete, screen, onSummary, targetVentureId, active });
  const live = v.phase !== "idle";
  const connecting = v.phase === "connecting";
  const down = useRef<{ timer: ReturnType<typeof setTimeout>; long: boolean } | null>(null);
  const endVoice = useCallback(() => {
    if (down.current) clearTimeout(down.current.timer);
    down.current = null;
    v.typeInstead();
    onTypeInstead?.();
  }, [v.typeInstead, onTypeInstead]);
  const releaseHold = useCallback(() => {
    if (down.current) clearTimeout(down.current.timer);
    down.current = null;
    v.pttEnd();
  }, [v.pttEnd]);
  useVoiceKeys((v.enabled || live) && active !== false && !disabled && (!v.summaryPending || live), v.pttStart, releaseHold, endVoice);
  useEffect(() => () => { if (down.current) clearTimeout(down.current.timer); }, []);
  useEffect(() => {
    if ((!v.enabled && !live) || active === false) return;
    // One handler per click: the panel that owns a live call ends it; with no call, the first eligible panel starts one.
    const talk = (event: Event) => {
      if (!live && voiceCallActive()) return;
      if (!live && (disabled || v.summaryPending)) return;
      event.stopImmediatePropagation();
      void v.toggle();
    };
    window.addEventListener("supymem-chat-voice", talk);
    return () => window.removeEventListener("supymem-chat-voice", talk);
  }, [v.enabled, active, disabled, v.summaryPending, live, v.toggle]);
  if (!v.enabled && !live) return v.note ? <details className={styles.helpWrap}>
    <summary className={styles.mic} aria-label="Talk live" title="Voice setup">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8" /></svg>
    </summary>
    <small className={styles.help} role="status" data-voice-note>{v.note === "Voice unavailable, type instead" ? voiceSessionError(503, "voice_unavailable") : v.note}</small>
  </details> : null;

  const begin = () => {
    if (down.current) clearTimeout(down.current.timer);
    const press = { long: false, timer: setTimeout(() => {
      if (down.current !== press) return;
      press.long = true; void v.pttStart();
    }, LONG_PRESS_MS) };
    down.current = press;
  };
  const end = () => {
    const press = down.current;
    down.current = null;
    if (!press) return;
    clearTimeout(press.timer);
    if (press.long) v.pttEnd(); else void v.toggle();
  };
  const label = live ? "End voice" : "Talk live";
  return (
    <span className={styles.wrap} data-console-reload-blocked={live || v.summaryPending}>
      <button
        type="button"
        className={`${styles.mic} ${connecting ? styles.connecting : live ? styles.listening : ""}`}
        disabled={!live && (disabled || active === false || v.summaryPending)}
        aria-label={label}
        title={connecting ? "Connecting…" : live ? label : "Talk live or hold Space."}
        aria-pressed={live}
        aria-busy={connecting}
        data-usage-control="voice_mic_press"
        onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); begin(); }}
        onPointerUp={end}
        onClick={(e) => { if (e.detail === 0) void v.toggle(); }}
        onPointerCancel={releaseHold}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); if (!e.repeat) void v.toggle(); }
          if (e.code === "Space") { e.preventDefault(); if (!e.repeat) void v.pttStart(); } }}
        onKeyUp={(e) => { if (e.code === "Space") { e.preventDefault(); v.pttEnd(); } }}
        onBlur={releaseHold}
      >
        {connecting ? <span className={styles.spinner} aria-hidden="true" /> : <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 12v-2a9 9 0 0 1 18 0v2M3 12v5h4v-5H3ZM17 12v5h4v-5h-4Z" />
        </svg>}
        <span className={styles.micLabel}>{live ? "Type instead" : label}</span>
      </button>
      {!compact && !live && v.voiceFirst && <><small className={styles.pttHint}>Hold Space to talk</small>
        <button type="button" data-voice-type-instead onClick={endVoice}>Type instead</button></>}
      {(live || v.error || (v.note && v.note !== "Voice unavailable, type instead") || v.summaryPending) && typeof document !== "undefined" && createPortal(
        <VoiceOrb phase={v.phase} getLevel={v.getLevel}><span className={styles.status} data-voice-status>
          {live && v.live.length > 0 && (
            <span className={styles.transcript}>
              {v.live.map((t) => (
                <span key={t.id} className={t.role === "founder" ? styles.you : styles.charlie}>
                  <b>{t.role === "founder" ? "You" : "Charlie"}</b> {t.text}
                </span>
              ))}
            </span>
          )}
          {live && <span className={styles.pttHint}>
            {v.pushToTalk ? "Mic opens only while you hold Space or the mic." : <button type="button" className="ghost sm" onClick={v.usePushToTalk}>Noisy room? Use hold Space</button>}
          </span>}
          <span className={styles.bar}>
            {live && <Bars />}
            {live && <span aria-live="polite" className={styles.label}>{PHASE_LABEL[v.phase]}</span>}
            {v.playbackBlocked && <button type="button" className="ghost sm" data-usage-control="voice_enable_sound" onClick={() => void v.enableSound()}>Enable sound</button>}
            {live && <button type="button" className="ghost sm" data-usage-control="voice_end" onClick={endVoice} aria-label="End voice">Type instead</button>}
            {v.recovery && v.recovery !== "reconnecting" && <button type="button" className="ghost sm" disabled={v.savingSummary || v.summaryPending || disabled || active === false} onClick={() => void v.resume()}>Resume</button>}
            {v.error && !v.recovery && !v.summaryPending && <button type="button" className="ghost sm" disabled={disabled || active === false} onClick={() => void v.retry()}>Retry</button>}
            {!live && (v.error || v.recovery) && !v.voiceFirst && <button type="button" className="ghost sm" onClick={endVoice}>Type instead</button>}
            {!live && v.savingSummary && <span className={styles.label} role="status">Saving your voice notes</span>}
            {v.summaryPending && v.error && <button type="button" className="ghost sm" data-usage-control="voice_save_notes" disabled={v.savingSummary} onClick={() => void v.retrySummary()}>Save voice notes</button>}
            <ErrorNotice error={v.error} source="VoiceMic.v_error" as="span" className={styles.error} />
          </span>
          {v.note && <span className={styles.label} role="status">{v.note}</span>}
        </span></VoiceOrb>, document.body
      )}
    </span>
  );
}
