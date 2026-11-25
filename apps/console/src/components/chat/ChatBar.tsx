"use client";

import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "@/lib/api-types";
import { parseEmployeeStamp } from "@/components/office/avatars";
import { useOfficeOptional } from "@/components/office/OfficeProvider";
import { stripEmDash } from "@/lib/copy";
import { ChatDock } from "./ChatDock";
import { MentionField } from "./MentionField";
import { useChatComposer } from "./useChatComposer";
import { chatDraftKey } from "@/lib/chatComposer";
import styles from "./ChatBar.module.css";

export function ChatBar({
  messages,
  streaming,
  onSend,
  onAction,
  disabled,
  ventureId,
  subject,
  conversationId,
}: {
  messages: ChatMessage[];
  streaming?: { messageId: string; text: string; speaker?: string };
  onSend: (body: string) => void | Promise<void | boolean | { ok: boolean }>;
  onAction?: (action: string, payload?: unknown) => void | boolean | Promise<void | boolean>;
  disabled?: { reason: string };
  ventureId: string;
  subject?: string;
  conversationId?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const office = useOfficeOptional();
  const agents = office?.org?.agents || [];
  const isMac =
    typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || "");
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const composer = useChatComposer(onSend, chatDraftKey(subject, ventureId, conversationId), false, inputRef, messages);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        const el = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(
          `[aria-label="Message the team"]`,
        );
        el?.focus();
        return;
      }
      if (event.key === "Escape" && expanded) {
        event.preventDefault();
        setExpanded(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  const blocked = !!disabled || composer.pending;
  const sendNotice = composer.refusal ? parseEmployeeStamp(composer.refusal.body_md || "").text : composer.error;

  return (
    <div className={styles.bar}>
      {expanded ? (
        <div className={styles.thread} role="dialog" aria-label="Conversation">
          <ChatDock
            ventureId={ventureId}
            subject={subject}
            conversationId={conversationId}
            messages={messages}
            streaming={streaming}
            onSend={onSend}
            onAction={onAction}
            onRecoverDraft={text => {
              if (text) composer.acknowledgeDraft(text);
              inputRef.current?.focus({preventScroll: true});
            }}
            disabled={disabled}
            variant="panel"
            hideComposer
          />
        </div>
      ) : null}
      <div className={styles.row}>
        <MentionField
          agents={agents}
          multiline
          className={styles.input}
          placeholder="Ask the team, or @Maya / @Research…"
          disabled={blocked}
          ariaLabel="Message the team"
          inputRef={inputRef}
          value={composer.draft}
          onChange={(next) => {
            composer.clearError();
            composer.setDraft(next);
          }}
          onSubmit={(body) => {
            void composer.submit(body).then((ok) => {
              if (ok) setExpanded(true);
            });
          }}
        />
        <span className={styles.hint} aria-hidden>
          {isMac ? "⌘K" : "Ctrl+K"}
        </span>
        <button
          type="button"
          className="ghost sm"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? "Hide" : sendNotice ? "Show conversation" : "Show"}
        </button>
        <button
          type="button"
          className="primary sm"
          disabled={blocked || !composer.draft.trim()}
          onClick={() => {
            void composer.submit().then((ok) => {
              if (ok) setExpanded(true);
            });
          }}
        >
          {composer.pending ? "Sending…" : "Send"}
        </button>
      </div>
      {(disabled || sendNotice) ? (
        <p className={styles.preview} role={sendNotice ? "alert" : undefined}>
          {stripEmDash(disabled?.reason || sendNotice)}
        </p>
      ) : null}
    </div>
  );
}
