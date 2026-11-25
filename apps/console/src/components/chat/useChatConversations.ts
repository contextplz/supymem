"use client";

import {useCallback, useEffect, useRef, useState} from "react";
import {useActivityRefresh, watchActivity} from "@/lib/activityRefresh";
import {isLocalConsole} from "@/lib/localMode";
import {api, apiLive, ensureSession, verifiedSessionEpoch, verifiedSessionSubject} from "@/lib/client";
import {getOpsWorkspace} from "@/lib/opsApi";
import {isCompanyOperationsPath} from "@/lib/operationsScope";
import {chatConversationCatalog, chatConversationItem, chatRouteCurrent, conversationListPath, conversationSelectionKey, type ChatConversation, type ChatConversationCatalog, type ChatConversationPatch} from "@/lib/chatConversations";

type Owner = {actor: string | null; epoch: number; venture: string; path: string; search: string; workspace: string | null};
type Model = {owner: Owner; catalog: ChatConversationCatalog | null; listedItems?: ChatConversation[]; archivedItems?: ChatConversation[]; selected: string | null; version: number; ready: boolean; busy: boolean; error: string};

export function useChatConversations(ventureId: string) {
  const activity = useActivityRefresh(ventureId);
  const ownerRef = useRef<Owner | null>(null), mounted = useRef(false), flight = useRef<Owner | null>(null), counter = useRef(0);
  const composerVersion = useRef(0);
  const loads = useRef([0, 0]);
  const selection = useRef<{owner: Owner; id: string | null; version: number} | null>(null);
  const createAttempt = useRef<{owner: Owner; key: string} | null>(null);
  const [, rerender] = useState(0);
  const path = typeof window === "undefined" ? "" : window.location.pathname;
  const proposed = {actor: verifiedSessionSubject(), epoch: verifiedSessionEpoch(), venture: ventureId, path,
    search: typeof window === "undefined" ? "" : window.location.search,
    workspace: isCompanyOperationsPath(path) ? getOpsWorkspace() : null};
  if (!ownerRef.current || ownerRef.current.actor !== proposed.actor || ownerRef.current.epoch !== proposed.epoch || ownerRef.current.venture !== ventureId ||
      ownerRef.current.workspace !== proposed.workspace || !chatRouteCurrent(ventureId, ownerRef.current.path, proposed.path, ownerRef.current.search, proposed.search)) {
    if (ownerRef.current && !(ownerRef.current.actor === null && proposed.actor && ownerRef.current.venture === ventureId &&
        ownerRef.current.workspace === proposed.workspace && chatRouteCurrent(ventureId, ownerRef.current.path, proposed.path, ownerRef.current.search, proposed.search))) composerVersion.current++;
    ownerRef.current = proposed; selection.current = null; createAttempt.current = null;
  }
  const owner = ownerRef.current;
  const [state, setState] = useState<Model | null>(null);
  const current = useCallback(() => mounted.current && !!owner.actor && ownerRef.current === owner &&
    owner.actor === verifiedSessionSubject() && owner.epoch === verifiedSessionEpoch() && typeof window !== "undefined" &&
    chatRouteCurrent(owner.venture, owner.path, window.location.pathname, owner.search, window.location.search) &&
    (owner.workspace === null || (!!owner.workspace && owner.workspace === getOpsWorkspace())), [owner]);
  const model = state?.owner === owner ? state : null;
  const catalogRef = useRef<Model | null>(null); catalogRef.current = model;
  const selectionCurrent = useCallback((id: string | null, version: number) => current() && selection.current?.owner === owner &&
    selection.current.id === id && selection.current.version === version, [current, owner]);

  useEffect(() => {
    mounted.current = true;
    const retire = () => {if (ownerRef.current?.actor) composerVersion.current++; ownerRef.current = null; selection.current = null; createAttempt.current = null; rerender(value => value + 1);};
    window.addEventListener("supymem-session-changed", retire);
    return () => {mounted.current = false; ownerRef.current = null; selection.current = null; window.removeEventListener("supymem-session-changed", retire);};
  }, []);

  const loadConversations = useCallback(async (archived = false, signal?: AbortSignal) => {
    if (!current()) return;
    const startedSelection = selection.current;
    const request = ++loads.current[Number(archived)];
    let selected = startedSelection?.owner === owner ? startedSelection.id : null;
    if (!selected) {try {selected = window.sessionStorage.getItem(conversationSelectionKey(owner.actor!, ventureId));} catch {selected = null;}}
    try {
      const path = conversationListPath(ventureId, selected, archived);
      const result = await (isLocalConsole() ? api(path, {signal}) : apiLive(path, signal));
      if (!current() || selection.current !== startedSelection || loads.current[Number(archived)] !== request) return;
      if (!archived && !result.ok && [404, 405].includes(result.status)) {
        // An older Core keeps its existing shared chat, but cannot create new conversations.
        const version = selection.current?.owner === owner && selection.current.id === null ? selection.current.version : ++counter.current;
        if (selection.current?.owner === owner && selection.current.id !== null) composerVersion.current++;
        selection.current = {owner, id: null, version};
        setState({owner, catalog: null, selected: null, version, ready: true, busy: false, error: "New chats are unavailable until the server supports saved conversations."});
        return true;
      }
      const catalog = result.ok ? chatConversationCatalog(result.data, ventureId, owner.actor!) : null;
      if (!catalog || (owner.workspace !== null && catalog.workspace_id !== owner.workspace)) throw new Error("Could not verify saved chats. Retry the chat list.");
      if (archived) {
        setState(previous => previous?.owner === owner ? {...previous, archivedItems: catalog.items.filter(item => item.archived), error: ""} : previous);
        return true;
      }
      if (!startedSelection && !catalog.items.some(item => item.id === selected)) {
        try {selected = window.sessionStorage.getItem(conversationSelectionKey(owner.actor!, ventureId));} catch {selected = null;}
      }
      if (!catalog.items.some(item => item.id === selected) && startedSelection?.id !== selected) selected = catalog.legacy_conversation_id;
      const version = selection.current?.owner === owner && selection.current.id === selected ? selection.current.version : ++counter.current;
      if (selection.current?.owner === owner && selection.current.id !== selected) composerVersion.current++;
      selection.current = {owner, id: selected, version};
      setState(previous => {
        const retained = previous?.owner === owner ? [...(previous.archivedItems || []), ...(previous.catalog?.items || [])].find(item => item.id === selected) : null;
        return {owner, catalog: retained && !catalog.items.some(item => item.id === selected) ? {...catalog, items: [...catalog.items, retained]} : catalog,
          listedItems: catalog.items, archivedItems: previous?.owner === owner ? previous.archivedItems : undefined, selected, version, ready: true, busy: flight.current === owner, error: ""};
      });
      return true;
    } catch {
      if (current() && selection.current === startedSelection && loads.current[Number(archived)] === request) setState(previous => previous?.owner === owner ? {...previous, error: "You cannot load your saved chats right now. Try again."} :
        {owner, catalog: null, selected: null, version: 0, ready: false, busy: false, error: "You cannot load your saved chats right now. Try again."});
      return false;
    }
  }, [current, owner, ventureId]);

  useEffect(() => {
    if (!owner.actor) {void ensureSession().catch(() => undefined); return;}
    const controller = new AbortController();
    if (isLocalConsole()) {
      const stop = watchActivity(activity, () => loadConversations(false, controller.signal));
      return () => { controller.abort(); stop(); };
    }
    let disposed = false, inFlight = false;
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      if (disposed || !current() || inFlight) return;
      clearTimeout(timer);
      inFlight = true;
      try {
        const loaded = await loadConversations(false, controller.signal);
        if (!disposed && current() && !loaded) timer = setTimeout(read, 15000);
      } finally {
        inFlight = false;
      }
    };
    const wake = () => { if (!document.hidden) void read(); };
    window.addEventListener("online", wake);
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    void read();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); window.removeEventListener("online", wake); window.removeEventListener("focus", wake); document.removeEventListener("visibilitychange", wake); };
  }, [activity, owner, loadConversations]);

  const selectConversation = useCallback((id: string) => {
    const loaded = catalogRef.current;
    const item = loaded?.catalog?.items.find(item => item.id === id) || loaded?.archivedItems?.find(item => item.id === id);
    if (!current() || loaded?.owner !== owner || !loaded.catalog || !item) return;
    const version = ++counter.current;
    composerVersion.current++;
    selection.current = {owner, id, version};
    try {window.sessionStorage.setItem(conversationSelectionKey(owner.actor!, ventureId), id);} catch { /* Selection works without storage. */ }
    setState({...loaded, catalog: {...loaded.catalog, items: [...loaded.catalog.items.filter(row => row.id !== id), item]}, selected: id, version, error: ""});
  }, [current, owner, ventureId]);

  const updateConversation = useCallback(async (id: string, patch: ChatConversationPatch) => {
    const loaded = catalogRef.current;
    if (!current() || loaded?.owner !== owner || flight.current === owner ||
        ![...(loaded.catalog?.items || []), ...(loaded.archivedItems || [])].some(item => item.id === id)) return false;
    flight.current = owner;
    setState({...loaded, busy: true, error: ""});
    try {
      const result = await api(`${conversationListPath(ventureId)}/${encodeURIComponent(id)}`, {method: "PATCH", body: JSON.stringify(patch)});
      if (!current()) return false;
      if (!result.ok) throw new Error("chat_update_failed");
      if (patch.archived !== undefined || loaded.archivedItems) await loadConversations(true);
      await loadConversations();
      return current();
    } catch {
      if (current()) setState(previous => previous?.owner === owner ? {...previous, error: "Could not update this chat. Try again."} : previous);
      return false;
    } finally {
      if (flight.current === owner) flight.current = null;
      if (current()) setState(previous => previous?.owner === owner ? {...previous, busy: false} : previous);
    }
  }, [current, owner, ventureId, loadConversations]);

  const newConversation = useCallback(async () => {
    const loaded = catalogRef.current;
    if (!current() || loaded?.owner !== owner || !loaded.catalog || flight.current === owner) return false;
    const startedSelection = selection.current;
    flight.current = owner;
    if (createAttempt.current?.owner !== owner) createAttempt.current = {owner, key: crypto.randomUUID()};
    setState({...loaded, busy: true, error: ""});
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}/chat/conversations`, {method: "POST",
        body: JSON.stringify({idempotency_key: createAttempt.current.key})});
      if (!current() || selection.current !== startedSelection) return false;
      const item = result.ok ? chatConversationItem(result.data, ventureId, loaded.catalog.workspace_id) : null;
      if (!item || item.visibility !== "private") throw new Error("new_chat_unconfirmed");
      const version = ++counter.current;
      composerVersion.current++;
      selection.current = {owner, id: item.id, version}; createAttempt.current = null;
      try {window.sessionStorage.setItem(conversationSelectionKey(owner.actor!, ventureId), item.id);} catch { /* No chat content is stored here. */ }
      setState({owner, catalog: {...loaded.catalog, items: [item, ...loaded.catalog.items.filter(row => row.id !== item.id)]},
        archivedItems: loaded.archivedItems, selected: item.id, version, ready: true, busy: false, error: ""});
      await loadConversations();
      return current() && selection.current?.id === item.id;
    } catch {
      if (current() && selection.current === startedSelection) setState(previous => previous?.owner === owner ? {...previous, busy: false,
        error: "The new chat could not be confirmed. Refresh saved chats before trying again."} : previous);
      return false;
    } finally {
      if (flight.current === owner) flight.current = null;
      if (current()) setState(previous => previous?.owner === owner ? {...previous, busy: false} : previous);
    }
  }, [current, owner, ventureId, loadConversations]);

  return {contextCurrent: current, conversationId: model?.selected ?? null, conversationVersion: model?.version ?? 0,
    conversationKey: JSON.stringify([ventureId, composerVersion.current]),
    conversationReady: model?.ready === true, conversationCurrent: selectionCurrent,
    conversationItems: (model?.listedItems || model?.catalog?.items)?.filter(item => !item.archived) ?? [], archivedConversationItems: model?.archivedItems ?? [], conversationsAvailable: !!model?.catalog, conversationBusy: model?.busy === true,
    conversationError: model?.error || "", reloadConversations: loadConversations, selectConversation, newConversation, updateConversation,
    conversationDraftId: model?.catalog?.items.find(item => item.id === model.selected)?.visibility === "private" ? model.selected : null};
}
