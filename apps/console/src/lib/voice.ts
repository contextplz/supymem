import { usageLimitMessage } from "./credits.ts";
// Pure helpers for Realtime voice chat (OpenAI WebRTC data-channel events).
// Event names: developers.openai.com/api/reference/resources/realtime

export type VoiceEvent =
  | { kind: "tool_call"; callId: string; name: string; text: string; arguments: string }
  | { kind: "caption"; text: string; final: boolean; who?: "charlie"; itemId?: string; failed?: boolean }
  | { kind: "error"; message: string; benign: boolean; code?: string; eventId?: string }
  | { kind: "response"; state: "created" | "done"; responseId?: string; cancelled?: true; failed?: true }
  | { kind: "committed"; itemId: string; previousItemId?: string | null }
  | { kind: "configured"; manual: boolean }
  | { kind: "signal"; signal: VoiceSignal; itemId?: string; responseId?: string };

export function parseRealtimeEvent(raw: string): VoiceEvent | null {
  let e: Record<string, unknown>;
  try { e = JSON.parse(raw); } catch { return null; }
  if (!e || typeof e !== "object" || Array.isArray(e)) return null;
  switch (e.type) {
    case "response.function_call_arguments.done": {
      const args = typeof e.arguments === "string" ? e.arguments : "";
      let text = "";
      try { const value = (JSON.parse(args) as { text?: unknown }).text; if (typeof value === "string") text = value; } catch { /* empty */ }
      return { kind: "tool_call", callId: String(e.call_id ?? ""), name: String(e.name ?? ""), text: text.trim(), arguments: args };
    }
    case "conversation.item.input_audio_transcription.delta":
      return { kind: "caption", text: String(e.delta ?? ""), final: false,
        ...(typeof e.item_id === "string" && e.item_id ? {itemId: e.item_id} : {}) };
    case "conversation.item.input_audio_transcription.completed":
      return { kind: "caption", text: String(e.transcript ?? ""), final: true, itemId: String(e.item_id ?? "") };
    case "conversation.item.input_audio_transcription.failed":
      return { kind: "caption", text: "", final: true, itemId: String(e.item_id ?? ""), failed: true };
    case "input_audio_buffer.speech_started": return { kind: "signal", signal: "speech_started",
      ...(typeof e.item_id === "string" && e.item_id ? {itemId: e.item_id} : {}) };
    case "input_audio_buffer.speech_stopped": return { kind: "signal", signal: "speech_stopped",
      ...(typeof e.item_id === "string" ? { itemId: e.item_id } : {}) };
    case "output_audio_buffer.started": return { kind: "signal", signal: "reply_started",
      ...(typeof e.response_id === "string" ? {responseId: e.response_id} : {}) };
    case "output_audio_buffer.stopped":
    case "output_audio_buffer.cleared": return { kind: "signal", signal: "reply_stopped" };
    case "response.output_audio_transcript.delta":
      return { kind: "caption", text: String(e.delta ?? ""), final: false, who: "charlie" };
    case "response.output_audio_transcript.done":
      return { kind: "caption", text: String(e.transcript ?? ""), final: true, who: "charlie" };
    case "session.updated": {
      const input = (e.session as { audio?: { input?: { turn_detection?: { create_response?: boolean; interrupt_response?: boolean } | null } } } | undefined)?.audio?.input;
      const td = input?.turn_detection;
      return { kind: "configured", manual: !!input && (td === null || (td?.create_response === false && td?.interrupt_response === false)) };
    }
    case "input_audio_buffer.committed": return { kind: "committed", itemId: String(e.item_id ?? ""),
      ...(typeof e.previous_item_id === "string" || e.previous_item_id === null ? {previousItemId: e.previous_item_id} : {}) };
    case "response.created":
    case "response.done": { // also fires for cancelled/failed
      const id = (e.response as { id?: unknown } | undefined)?.id;
      return { kind: "response", state: e.type === "response.created" ? "created" : "done",
        ...(typeof id === "string" ? { responseId: id } : {}),
        ...((e.response as { status?: unknown } | undefined)?.status === "cancelled" ? { cancelled: true as const } : {}),
        ...(["failed", "incomplete"].includes(String((e.response as { status?: unknown } | undefined)?.status)) ? { failed: true as const } : {}) };
    }
    case "error": {
      const err = e.error as { message?: string; code?: string; event_id?: string } | undefined;
      const message = String(err?.message ?? "Voice error");
      // Prefer the id of the response the error is about (it is in the message); else the failing client event.
      const eventId = /\b(resp_[A-Za-z0-9]+)/.exec(message)?.[1] ?? err?.event_id ?? (typeof e.event_id === "string" ? e.event_id : undefined);
      return { kind: "error", message, benign: isBenignVoiceError(err?.code, message),
        ...(err?.code ? { code: err.code } : {}), ...(eventId ? { eventId } : {}) };
    }
    default:
      return null;
  }
}

/** Client events that hand the tool result back and ask the model to speak it. */
export type SpokenReply = { body_md?: string; actions?: { label?: string; action?: string; text?: string; href?: string; payload?: unknown }[];
  citations?: { label?: string; href?: string }[]; partial?: boolean };

export function toolResultEvents(callId: string, reply: SpokenReply | null) {
  const output = (reply?.body_md || "I could not answer yet. Say so briefly and do not guess.")
    + (reply?.citations?.length ? `\n\nSources: ${JSON.stringify(reply.citations)}` : "")
    + (reply?.actions?.length ? `\n\nNext steps (explain the available choices; requested work runs directly, while suggestions wait for your request): ${JSON.stringify(reply.actions.map(({ label, action, text, href }) => ({ label, action, text, href })))}` : "");
  return [
    { type: "conversation.item.create", item: { type: "function_call_output", call_id: callId, output } },
    { type: "response.create" },
  ];
}

export const SHARED_VOICE_TOOLS = new Set(["web_search", "read_url", "venture_context", "suggest_next_step"]);
export const VOICE_TOOL_STEPS = 4;
export const VOICE_TOOL_WALL_MS = 20_000;
export const VOICE_CHAT_WAIT_MS = 12_000;
export const VOICE_CHAT_WAIT_NOTE = "I'm still working on it. I'll speak when the result arrives.";
export const VOICE_CHAT_WAIT_RESPONSE = { tools: [], tool_choice: "none",
  instructions: `Say only "${VOICE_CHAT_WAIT_NOTE}". Do not repeat the request or call tools.` };
export const VOICE_TOOL_PARTIAL = "I could not finish that check this turn. Answer briefly from what you already found, say what is missing, and do not guess or call more tools.";
/** An isolated, tools-free filler cannot repeat a pending call or pollute the venture conversation. */
export const VOICE_TOOL_FILLER = { conversation: "none", input: [], tools: [], tool_choice: "none",
  instructions: "Say only 'Let me check that' in the founder's language. Do not answer the question yet.", max_output_tokens: 64 };
export const VOICE_TOOL_ANSWER = { tools: [], tool_choice: "none",
  instructions: VOICE_TOOL_PARTIAL };

export const PUSH_TO_TALK_RELEASE = [{ type: "input_audio_buffer.commit" }];

// Turn detection. Fields per developers.openai.com/api/docs/guides/realtime-vad. Must match
// TURN_DETECTION in services/company_os/app/api/voice_api.py. The server never interrupts Charlie
// or answers on its own: any cough, door or keyboard click used to cancel him mid-sentence and
// start a fresh answer. The client decides per turn with founderTurn() once the words are known.
export const VAD_ON = { type: "semantic_vad", eagerness: "medium", create_response: false, interrupt_response: false };
/** Browser-side cleanup before audio reaches the model; Charlie's own voice must not come back as "speech". */
export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
};
/** Charlie's volume while the founder might be talking over him, until founderTurn() decides. */
export const DUCK_VOLUME = 0.35;

// Transcripts of background noise: silence, fillers and the classic transcription hallucinations.
const NOISE = new Set(["uh", "um", "umm", "hmm", "hm", "mm", "mmm", "mhm", "ah", "oh", "eh", "er", "you", "thanks for watching"]);
const QUIET_ACKNOWLEDGEMENTS = new Set(["thank you", "thanks", "bye", "ok bye"]);
/** True when a transcript holds words the founder meant, not pure filler. */
export function isRealSpeech(text: string, charlieAudible = false): boolean {
  const s = text.toLowerCase().replace(/[^\p{L}\p{N}']+/gu, " ").trim();
  return s.replace(/[\s']/g, "").length >= 2 && !NOISE.has(s)
    && !(charlieAudible && QUIET_ACKNOWLEDGEMENTS.has(s)) && !s.split(/\s+/).every(word => NOISE.has(word));
}
// ponytail: transcript word heuristic, use acoustic confidence if sustained noise produces real words.
export const shouldInterruptPlayback = (text: string, speechMs: number) =>
  isRealSpeech(text, true) && (text.trim().split(/\s+/).filter(word => isRealSpeech(word)).length >= 2 || speechMs >= 600);
/** A founder turn finished transcribing (transcript null = transcription failed). Real words: stop
 *  Charlie if he is talking (WebRTC truncates his item to what was played) and answer. Noise: drop the
 *  turn so he keeps talking and never sees it. Failed transcription never authorizes a turn. */
export function founderTurn(t: Track, itemId: string, transcript: string | null): { respond: boolean; events: object[] } {
  const audible = t.audioPlaying || t.audioPending || t.responseActive;
  const real = transcript !== null && isRealSpeech(transcript, audible);
  if (!real) return { respond: false, events: itemId ? [{ type: "conversation.item.delete", item_id: itemId }] : [] };
  return { respond: true, events: audible ? bargeInEvents(t.responseActive) : [] };
}
export const vadUpdate = (td: object | null) =>
  ({ type: "session.update", session: { type: "realtime", audio: { input: { turn_detection: td } } } });
/** response.cancel with nothing running (the server already interrupted, or the response just ended). */
export const isBenignVoiceError = (code?: string, message = "") =>
  code === "response_cancel_not_active" || code === "conversation_already_has_active_response"
  || /no active response|already has an active response/i.test(message);
/** Every Realtime error is reported, even a silently recovered one, except a cancel with nothing to cancel (pure noise). */
export const isReportableVoiceError = (code?: string, message = "") =>
  !(code === "response_cancel_not_active" || /no active response/i.test(message));
/** Our response.create lost a race with a response the server started; it is active, so re-queue ours. */
export const requeueOnConflict = (t: Track, message: string): Track =>
  /already has an active response/i.test(message) ? { ...t, responseActive: true, createQueued: true } : t;
export const VOICE_ERROR_NOTE = "Voice hit a problem. Retry or type your answer instead.";

/** Barge-in fallback: only cancel a response that is actually active; always flush audio. */
export const bargeInEvents = (responseActive: boolean) =>
  responseActive ? [{ type: "response.cancel" }, { type: "output_audio_buffer.clear" }] : [{ type: "output_audio_buffer.clear" }];

export type Track = { createQueued: boolean; responseActive: boolean; audioPlaying: boolean; audioPending: boolean; toolPending: boolean; founderSpeaking: boolean; hadReply: boolean };
export const NEW_TRACK: Track = { createQueued: false, responseActive: false, audioPlaying: false, audioPending: false, toolPending: false, founderSpeaking: false, hadReply: false };
export type TrackKey = "response_created" | "response_done" | "audio_started" | "audio_stopped"
  | "stream_started" | "stream_stopped" | "speech_started" | "speech_stopped" | "tool_start" | "tool_end";
/** What the Realtime session is doing right now, so cancel and auto-end only act on real state. */
export function track(t: Track, k: TrackKey): Track {
  switch (k) {
    case "response_created": return { ...t, responseActive: true };
    case "response_done": return { ...t, responseActive: false, hadReply: true };
    case "audio_started": return { ...t, audioPlaying: true };
    case "audio_stopped": return { ...t, audioPlaying: false };
    case "stream_started": return { ...t, audioPending: true };
    case "stream_stopped": return { ...t, audioPending: false };
    case "speech_started": return { ...t, founderSpeaking: true };
    case "speech_stopped": return { ...t, founderSpeaking: false };
    case "tool_start": return { ...t, toolPending: true };
    case "tool_end": return { ...t, toolPending: false };
  }
}
/** Only one response may be active. Ask for one: send now if none is active, else queue until it ends. */
export function requestResponse(t: Track): { t: Track; send: boolean } {
  return t.responseActive ? { t: { ...t, createQueued: true }, send: false } : { t: { ...t, responseActive: true }, send: true };
}
/** response.done: release a queued response.create, if any. */
export function responseEnded(t: Track, succeeded = true): { t: Track; send: boolean } {
  const done = { ...track(t, "response_done"), hadReply: t.hadReply || succeeded };
  return done.createQueued ? { t: { ...done, createQueued: false, responseActive: true }, send: true } : { t: done, send: false };
}
/** Charlie answered and nothing is in flight: the quiet period before auto-end can start. */
export const isIdleAfterReply = (t: Track) =>
  t.hadReply && !t.responseActive && !t.audioPlaying && !t.audioPending && !t.toolPending && !t.founderSpeaking;
export const AUTO_END_MS = 30_000;
export const AUTO_END_WARNING_MS = 20_000;
export const AUTO_END_WARNING = "I'll end the call in ten seconds unless you keep talking. You can resume anytime.";
export const AUTO_END_WARNING_RESPONSE = { conversation: "none", input: [], tools: [], tool_choice: "none",
  instructions: `Say only "${AUTO_END_WARNING}".`, max_output_tokens: 96 };
export const autoEndDue = (idleSince: number | null, now: number, limit = AUTO_END_MS) =>
  idleSince !== null && now - idleSince >= limit;
export const AUTO_END_NOTE = "Voice ended. Tap the mic to talk again.";

export const LONG_PRESS_MS = 400;
export const SILENCE_MS = 60_000;
export const MAX_SESSION_MS = 10 * 60_000;
export const RENEW_SESSION_MS = 8 * 60_000;
export const MAX_CALL_MS = 60 * 60_000;

export type VoicePhase = "idle" | "connecting" | "listening" | "thinking" | "speaking";
export type VoiceSignal = "speech_started" | "speech_stopped" | "reply_started" | "reply_stopped"
  | "tool_start" | "noise" | "connect" | "connected" | "end";

/** idle -> connecting -> listening -> thinking -> speaking -> listening ... -> idle. */
export function nextPhase(p: VoicePhase, s: VoiceSignal): VoicePhase {
  if (s === "end") return "idle";
  if (s === "connect") return p === "idle" ? "connecting" : p;
  if (p === "idle") return p;
  if (s === "connected") return p === "connecting" ? "listening" : p;
  if (p === "connecting") return p;
  switch (s) {
    case "speech_started": return "listening";
    case "speech_stopped": return p === "listening" ? "thinking" : p;
    case "tool_start": return "thinking";
    case "reply_started": return "speaking";
    case "reply_stopped": return "listening";
    case "noise": return p === "thinking" ? "listening" : p;
    default: return p;
  }
}

/** A press held past LONG_PRESS_MS is push-to-talk; anything shorter is a tap. */
export const isLongPress = (heldMs: number) => heldMs > LONG_PRESS_MS;
export const isSilent = (now: number, lastActivity: number, limit = SILENCE_MS) => now - lastActivity >= limit;
export const isOverCap = (now: number, startedAt: number, cap = MAX_SESSION_MS) => now - startedAt >= cap;

export const PHASE_LABEL: Record<VoicePhase, string> = {
  idle: "", connecting: "Connecting…", listening: "Listening…", thinking: "Thinking…", speaking: "Charlie is speaking",
};

/** 0..1 loudness from AnalyserNode.getByteTimeDomainData samples (128 = silence). */
export function levelFromSamples(samples: ArrayLike<number>): number {
  if (!samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) { const v = (samples[i] - 128) / 128; sum += v * v; }
  return Math.min(1, Math.sqrt(sum / samples.length) * 3);
}

export function voiceSessionError(status: number, code?: string, message?: string): string {
  if (message && ["voice_unavailable", "voice_provider_error", "voice_session_not_saved", "voice_session_unavailable", "local_spend_limit"].includes(code || "")) return message;
  if (status === 402 || code === "insufficient_credits") return usageLimitMessage();
  if (code === "voice_unavailable") return "Voice needs an OpenAI key. Open Settings > Keys and connections > Your AI provider, choose OpenAI and save your key, then try voice again. You can keep typing.";
  if (status === 404) return "Your voice session could not be opened. Open your conversation and try again.";
  return "Voice could not connect. Check your connection, restart your app and try voice again. You can keep typing.";
}

export function micErrorMessage(err: unknown): string {
  const name = (err as { name?: string } | null)?.name;
  return name === "NotAllowedError" || name === "SecurityError"
    ? "Microphone access was denied. Allow it in System Settings, then try again."
    : "Could not start the microphone.";
}


export type VoiceMetric = "barge_in_accepted" | "barge_in_ignored" | "response_cancelled"
  | "duplicate_response_blocked" | "turn_latency" | "transcription_failed"
  | "mic_permission_ms" | "mint_ms" | "sdp_ms" | "first_remote_audio_ms" | "local_playing_ms"
  | "playback_blocked" | "transcription_ms" | "response_ms";

export function observeVoicePlayback(element: HTMLAudioElement,
  changed: (state: "playing" | "waiting" | "blocked") => void) {
  let disposed = false;
  let attempts = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let playingConfirmed = false;
  const blocked = () => { if (!disposed) changed("blocked"); };
  const retry = () => {
    if (disposed) return;
    changed("waiting");
    clearTimeout(timer);
    if (++attempts > 3) blocked();
    else timer = setTimeout(() => { void play(); }, 500 * attempts);
  };
  const play = async () => {
    if (disposed) return;
    playingConfirmed = false;
    clearTimeout(timer);
    timer = setTimeout(() => { if (!playingConfirmed) retry(); }, 3000);
    try { await element.play(); }
    catch (error) {
      if (disposed) return;
      clearTimeout(timer);
      if ((error as { name?: string }).name === "NotAllowedError") blocked();
      else retry();
    }
  };
  const playing = () => { playingConfirmed = true; attempts = 0; clearTimeout(timer); changed("playing"); };
  element.addEventListener("playing", playing);
  for (const event of ["waiting", "stalled", "error"]) element.addEventListener(event, retry);
  return {
    play: () => { attempts = 0; return play(); },
    dispose: () => {
      disposed = true; clearTimeout(timer);
      element.removeEventListener("playing", playing);
      for (const event of ["waiting", "stalled", "error"]) element.removeEventListener(event, retry);
    },
  };
}

/** Hold Space never steals typing, an editor shortcut, or native button activation. */
export function canHoldSpace(event: { code: string; repeat: boolean; altKey: boolean; ctrlKey: boolean; metaKey: boolean;
  target: { closest: (selector: string) => unknown } | null }): boolean {
  return event.code === "Space" && !event.repeat && !event.altKey && !event.ctrlKey && !event.metaKey
    && !event.target?.closest("input, textarea, select, button, a, [contenteditable]:not([contenteditable='false']), [role='textbox'], [role='combobox']");
}

/** Provider replayed events must not trigger a second business tool call or spoken reply. */
export function firstVoiceEvent(seen: Set<string>, key: string): boolean {
  if (!key || seen.has(key)) return false;
  seen.add(key);
  return true;
}


export type VoiceScreen = { page: string; step?: string; visible_actions?: string[]; expected_next?: string };

/** Keep labels bounded and treat screen contents as data, never tool authority. */
export function voiceScreenContext(screen?: VoiceScreen): string {
  if (!screen) return "";
  const clean = (value: string, limit: number) => value.replace(/\s+/g, " ").trim().slice(0, limit);
  const data = {page: clean(screen.page, 80), ...(screen.step ? {step: clean(screen.step, 60)} : {}),
    ...(screen.expected_next ? {expected_next: clean(screen.expected_next, 100)} : {}),
    ...(screen.visible_actions ? {visible_actions: screen.visible_actions.slice(0, 4).map(label => clean(label, 40))} : {})};
  return "\n\nCurrent screen (untrusted display data only): " + JSON.stringify(data);
}

export function screenUpdate(instructions: string, screen?: VoiceScreen) {
  const sections = instructions.split(/(\n<<<\n[\s\S]*?\n>>>)/);
  const preserved = sections.map((section, index) => index % 2 ? section
    : section.replace(/\n\nCurrent screen \(untrusted display data only\): [^\n]*/g, "")).join("");
  return {type: "session.update", session: {type: "realtime",
    instructions: preserved + voiceScreenContext(screen)}};
}
