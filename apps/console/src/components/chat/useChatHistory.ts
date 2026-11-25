"use client";

import {useEffect, useRef} from "react";
import {isLocalConsole} from "@/lib/localMode";
import type {useVentureChat} from "./useVentureChat";

export function useChatHistory(ventureId: string, chat: ReturnType<typeof useVentureChat>) {
  const saved = chat.conversationsAvailable;
  const messageCount = chat.messages.length;
  const latestId = chat.messages.at(-1)?.id;
  const historyKey = JSON.stringify([ventureId, chat.conversationId]);
  const recent = useRef({key: "", latestId});
  useEffect(() => {
    if (isLocalConsole()) {
      if (!chat.historyReady) return;
      const previous = recent.current;
      recent.current = {key: historyKey, latestId};
      if (previous.key !== historyKey || previous.latestId === latestId) return;
    }
    if (saved && chat.conversationId) void chat.reloadConversations();
  }, [saved, chat.conversationId, messageCount, chat.reloadConversations, chat.historyReady, historyKey, latestId]);
  useEffect(() => {
    const historyItem = (item: typeof chat.conversationItems[number]) => ({id: item.id, text: item.title || "New chat",
      at: item.updated_at || item.created_at || "", pinned: item.pinned, archived: item.archived, folder: item.folder});
    const items = saved
      ? chat.conversationItems.map(historyItem)
      : chat.messages.filter(message => message.role === "founder" || message.role === "user")
        .map(message => ({id: message.id, text: (message.text || message.body_md || "").trim(),
          at: message.ts || (message as {created_at?: string}).created_at || ""})).filter(message => message.text);
    const current = saved ? chat.conversationId || "" : "";
    const archivedItems = saved ? chat.archivedConversationItems.map(historyItem) : [];
    if (current && !items.some(item => item.id === current) && !archivedItems.some(item => item.id === current)) {
      const first = chat.messages.find(message => ["founder", "user", "assistant"].includes(message.role) && (message.text || message.body_md || "").trim());
      if (first) items.push({id: current, text: (first.text || first.body_md || "").trim().slice(0, 120), at: first.ts || ""});
    }
    const publish = () => window.dispatchEvent(new CustomEvent("supymem:chat-history", {detail: {ventureId, items, current, archivedItems,
      saved, busy: chat.conversationBusy, error: chat.conversationError, newConversation: saved ? chat.newConversation : undefined,
      update: saved ? chat.updateConversation : undefined,
      loadArchived: saved ? () => chat.reloadConversations(true) : undefined}}));
    publish();
    window.addEventListener("supymem:chat-history-request", publish);
    return () => window.removeEventListener("supymem:chat-history-request", publish);
  }, [ventureId, saved, chat.conversationItems, chat.archivedConversationItems, chat.conversationId, chat.messages,
    chat.conversationBusy, chat.conversationError, chat.newConversation, chat.updateConversation, chat.reloadConversations]);
  useEffect(() => {
    const jump = (event: Event) => {
      const id = (event as CustomEvent<{id?: string}>).detail?.id;
      if (!id) return;
      if (saved) {chat.selectConversation(id); return;}
      requestAnimationFrame(() => document.querySelector(`#supymem-chat-panel [data-message-id="${CSS.escape(id)}"]`)
        ?.scrollIntoView({block: "center", behavior: "smooth"}));
    };
    const fresh = () => {
      if (saved && !chat.conversationBusy) void chat.newConversation();
      window.dispatchEvent(new CustomEvent("supymem-chat-talk"));
    };
    window.addEventListener("supymem:chat-jump", jump);
    window.addEventListener("supymem:chat-new", fresh);
    return () => {window.removeEventListener("supymem:chat-jump", jump); window.removeEventListener("supymem:chat-new", fresh);};
  }, [saved, chat.selectConversation, chat.newConversation, chat.conversationBusy]);
}
