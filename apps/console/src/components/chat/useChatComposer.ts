"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import type { ChatMessage } from "@/lib/api-types";
import { canStartSend, clearChatDraftIfMatches, draftAfterFailedSend, draftStorage, readChatDraft, writeChatDraft } from "@/lib/chatComposer";
import { announceUsageControl } from "@/lib/usageControl";

type SendResult = void | boolean | { ok: boolean };

function sendFailed(result: SendResult): boolean {
  if (result === false) return true;
  if (result && typeof result === "object" && result.ok === false) return true;
  return false;
}

/**
 * Shared send/clear/restore/pending for every chat composer.
 * Clears optimistically; restores the failed text only if the founder has not typed something new.
 */
export function useChatComposer(onSend: (body: string) => void | Promise<SendResult>, draftKey: string | null = null, loading = false, inputRef?: RefObject<HTMLInputElement | HTMLTextAreaElement | null>, messages?: ChatMessage[]) {
  const storage = draftStorage();
  const [draft, setDraft] = useState(() => readChatDraft(storage, draftKey));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const pendingRef = useRef(false);
  const sendEpoch = useRef(0);
  const previousKeyRef = useRef(draftKey);
  const loadingRef = useRef(loading);
  const editedRef = useRef(false);
  const activeKeyRef = useRef(draftKey);
  activeKeyRef.current = draftKey;
  const restoredDraftRef = useRef<string | null>(null);

  useLayoutEffect(() => {
    const wasLoading = loadingRef.current;
    loadingRef.current = loading;
    if (previousKeyRef.current === draftKey) return;
    const previousKey = previousKeyRef.current;
    previousKeyRef.current = draftKey;
    // If identity arrives after the founder began typing, retain that text.
    // A venture/account switch instead loads only the new scope's draft.
    const keep = previousKey === null || (wasLoading && editedRef.current && !!draftKey?.startsWith(`${previousKey}:`));
    if (!keep) {
      restoredDraftRef.current = null;
      sendEpoch.current++;
      pendingRef.current = false;
      setPending(false);
      setError("");
    }
    const next = keep && draftRef.current ? draftRef.current : readChatDraft(storage, draftKey);
    editedRef.current = keep && editedRef.current;
    draftRef.current = next;
    setDraft(next);
    if (keep && next) {
      writeChatDraft(storage, draftKey, next);
      if (previousKey && readChatDraft(storage, draftKey) === next) clearChatDraftIfMatches(storage, previousKey, next);
    }
  }, [draftKey, storage, loading]);

  useLayoutEffect(() => {
    const restored = restoredDraftRef.current;
    restoredDraftRef.current = null;
    if (restored !== null && draft === restored) inputRef?.current?.select();
  }, [draft, pending, inputRef]);

  // Pages can prefill this composer (e.g. "Record result" on Experiments) without sending anything.
  useEffect(() => {
    function onPrefill(event: Event) {
      const text = String((event as CustomEvent<{ text?: string }>).detail?.text || "");
      if (!text) return;
      editedRef.current = true;
      draftRef.current = text;
      setDraft(text);
      writeChatDraft(storage, activeKeyRef.current, text);
      document.querySelector<HTMLElement>(`[aria-label="Message the team"]`)?.focus();
    }
    window.addEventListener("supymem-chat-prefill", onPrefill);
    return () => window.removeEventListener("supymem-chat-prefill", onPrefill);
  }, [storage]);

  const submit = useCallback(
    async (raw?: string) => {
      if (previousKeyRef.current !== draftKey) return false;
      const body = (raw ?? draftRef.current).trim();
      if (!canStartSend(body, pendingRef.current)) return false;
      // One accepted submit attempt for button and keyboard paths alike.
      announceUsageControl("chat_send");
      pendingRef.current = true;
      const epoch = ++sendEpoch.current;
      setPending(true);
      setError("");
      setDraft("");
      draftRef.current = "";
      // Keep the submitted text in local storage until the send is acknowledged.
      // A reload or temporary sign-in gate during the request can then restore it.
      writeChatDraft(storage, draftKey, body);
      try {
        const result = await onSend(body);
        if (epoch !== sendEpoch.current) {
          if (activeKeyRef.current !== draftKey && !sendFailed(result)) clearChatDraftIfMatches(storage, draftKey, body);
          return false;
        }
        if (sendFailed(result)) {
          if (activeKeyRef.current !== draftKey) return false;
          const restored = draftAfterFailedSend({
            failedText: body,
            currentDraft: draftRef.current,
          });
          restoredDraftRef.current = restored === body ? restored : null;
          setDraft(restored);
          draftRef.current = restored;
          writeChatDraft(storage, draftKey, restored);
          return false;
        }
        clearChatDraftIfMatches(storage, draftKey, body);
        return true;
      } catch {
        if (epoch !== sendEpoch.current) return false;
        if (activeKeyRef.current !== draftKey) return false;
        const restored = draftAfterFailedSend({
          failedText: body,
          currentDraft: draftRef.current,
        });
        restoredDraftRef.current = restored === body ? restored : null;
        setDraft(restored);
        draftRef.current = restored;
        writeChatDraft(storage, draftKey, restored);
        setError("This message could not be confirmed. Check the chat before trying again.");
        return false;
      } finally {
        if (epoch === sendEpoch.current) {
          pendingRef.current = false;
          setPending(false);
        }
      }
    },
    [onSend, draftKey, storage],
  );

  const latest = messages?.at(-1);
  const refusal = latest?.id.startsWith("local-err-") ? latest : messages?.findLast(message => message.id.startsWith("local-uncertain-"));
  return {
    draft,
    acknowledgeDraft: (sent: string) => {
      if (activeKeyRef.current !== draftKey || draftRef.current.trim() !== sent.trim()) return;
      clearChatDraftIfMatches(storage, draftKey, draftRef.current);
      draftRef.current = "";
      setDraft("");
      setError("");
    },
    setDraft: (value: string) => {
      editedRef.current = true;
      draftRef.current = value;
      setDraft(value);
      writeChatDraft(storage, draftKey, value);
    },
    pending,
    stop: () => {
      sendEpoch.current++;
      pendingRef.current = false;
      setPending(false);
      setError("");
      writeChatDraft(storage, draftKey, draftRef.current);
    },
    error,
    refusal: pending ? undefined : refusal,
    clearError: () => setError(""),
    submit,
  };
}
