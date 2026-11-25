"use client";

import { useContext, useEffect, useState } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import { CharlieMascot } from "./CharlieDock";
import { ChatDock } from "./ChatDock";
import { useVentureChat } from "./useVentureChat";
import { useChatHistory } from "./useChatHistory";
import type { DockSize } from "@/lib/chatComposer";
import type { ChatMessage } from "@/lib/api-types";
import { PREFILL_ACTION } from "@/lib/chatPrefill";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "./VentureChatLauncher.module.css";

export type PendingAsk = { action: string; payload?: unknown };

export function VentureChatLauncher({ ventureId, subject, size, onSizeChange, onMinimize, closable = true, pendingAsk, onAskSent, open = true, primary = false }: {
  ventureId: string;
  subject?: string;
  size: DockSize;
  onSizeChange: (size: DockSize) => void;
  onMinimize?: () => void;
  closable?: boolean;
  pendingAsk?: PendingAsk | null;
  onAskSent?: () => void;
  open?: boolean;
  primary?: boolean;
}) {
  const [handledAsk] = useState<{ current: PendingAsk | null }>({ current: null });
  const home = useContext(FounderUIContext) === "home";
  const chat = useVentureChat(ventureId, { navigateOnResponse: !home });
  // PR #1248: the header shows the open sidebar view's title (the company by default).
  const [title, setTitle] = useState(chat.managerName);
  useEffect(() => { setTitle(chat.managerName); }, [chat.managerName]);
  useEffect(() => {
    const onTitle = (event: Event) => {
      const next = (event as CustomEvent<{ title?: string }>).detail?.title;
      if (next) setTitle(next);
    };
    window.addEventListener("supymem:conversation", onTitle);
    return () => window.removeEventListener("supymem:conversation", onTitle);
  }, []);
  useChatHistory(ventureId, chat);
  useEffect(() => {
    if (!pendingAsk) { handledAsk.current = null; return; }
    if (handledAsk.current === pendingAsk) return;
    if (!chat.historyReady && pendingAsk.action !== PREFILL_ACTION) return;
    handledAsk.current = pendingAsk;
    onAskSent?.();
    void chat.confirmChatAction(pendingAsk.action, pendingAsk.payload);
  }, [pendingAsk, chat.historyReady]); // eslint-disable-line react-hooks/exhaustive-deps

  const ChatTitle = primary ? "h1" : "div";
  return (
    <section id="supymem-chat-panel" className={`${styles.panel}${home ? ` ${styles.homePanel}` : ""}`} hidden={!open} inert={!open}
      role={primary ? "main" : "complementary"} tabIndex={primary ? -1 : undefined} aria-label="Company conversation">
      <div className={styles.header}>
        {home ? <>
          <div className={styles.who}>
            <CharlieMascot small active={false} />
            <ChatTitle className={styles.titleHeading} aria-label={primary ? title : undefined}>
              <button type="button" className={styles.titleBtn} aria-label={`Open settings for ${title}`} onClick={() => window.dispatchEvent(new CustomEvent("supymem:open-settings"))}>
                <strong>{title}</strong>
                <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
              </button>
            </ChatTitle>
          </div>
          {closable && onMinimize && <button type="button" className={styles.minimize} onClick={onMinimize} aria-label={`Dock ${chat.managerName}`} aria-expanded={true} aria-controls="supymem-chat-panel">×</button>}
        </> : <>
        <strong>{chat.managerName} <span className={styles.role}>· Manager</span></strong>
        <div className={styles.controls}>
          <button type="button" className={styles.control} disabled={!chat.conversationsAvailable || chat.conversationBusy}
            onClick={() => void chat.newConversation()}>New chat</button>
          <button type="button" className={styles.control} onClick={onMinimize} aria-label="Minimize chat" aria-expanded={true} aria-controls="supymem-chat-panel">
            Minimize
          </button>
        </div>
        </>}
      </div>
      {home && chat.conversationError && <div className={styles.intro}>
        <ErrorNotice error={chat.conversationError} source="chat.conversations" />
        <button type="button" className="ghost sm" onClick={() => void chat.reloadConversations()}>Try again</button>
      </div>}
      {!home && <div className={styles.intro}>
        {chat.conversationsAvailable && <details className={styles.history}><summary>Previous chats</summary><label><select aria-label="Choose a saved chat" value={chat.conversationId || ""}
          disabled={chat.conversationBusy} onChange={event => chat.selectConversation(event.target.value)}>
          {chat.conversationItems.map(item => <option key={item.id} value={item.id}>{item.visibility === "legacy_shared" ? "Shared saved history" : item.title || "New chat"}</option>)}
        </select></label><p>New chat keeps saved venture context, tasks and approvals.</p></details>}
        <ErrorNotice error={chat.conversationError} source="chat.conversations" />
        {chat.conversationError && <button type="button" className="ghost sm" onClick={() => void chat.reloadConversations()}>Try again</button>}
      </div>}
      <div className={styles.content} data-webview-main={open ? "chat" : undefined}>
          <ChatDock
            key={chat.conversationKey}
            ventureId={ventureId}
            subject={subject}
            conversationId={chat.conversationDraftId}
            voiceConversationId={chat.conversationId}
            messages={chat.messages}
            onLoadEarlier={chat.loadEarlier}
            hasEarlier={chat.hasEarlier}
            historyLoading={chat.historyLoading}
            historyReady={chat.historyReady}
            historyError={chat.historyError}
            onReloadHistory={chat.reloadHistory}
            streaming={chat.streaming}
            onSend={(body) => chat.sendChat(body)}
            onVoiceSend={(text, options) => new Promise((resolve) => {
              let reply: ChatMessage | null = null;
              void chat.sendChat(text, { source: "voice", idempotencyKey: options?.idempotencyKey, onReply: (m) => { reply = m; } }).then(() => resolve(reply));
            })}
            onAction={(action, payload) => chat.confirmChatAction(action, payload)}
            variant="panel"
            visible={open}
            autoFocus={!primary}
            managerName={chat.managerName}
            resizable
            resizeHeight={false}
            size={size}
            onSizeChange={onSizeChange}
          />
      </div>
    </section>
  );
}
