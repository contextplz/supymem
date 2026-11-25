"use client";

import { chatNavigation } from "@/lib/chatRouteAction";

import Link from "@/components/chat/ChatLink";
import { useEffect, useRef, useState, type CSSProperties, type ReactNode, type RefObject, type PointerEvent as ReactPointerEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { founderRouteLinks } from "@/lib/founderRoutes";
import { useFeatureVisibility, useLocalMode } from "@/components/layout/LocalModeBoundary";
import { chatFirstHidden } from "@/lib/chatVisibility";
import { useCompanyHomeData } from "@/lib/companyHome";
import { prefillChat } from "@/lib/chatPrefill";
import { appRouteVentureId, resumeAppRoute } from "@/lib/appResume";
import { historyGroups, type ChatHistoryItem, type ChatConversationPatch } from "@/lib/chatConversations";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "./CompanyOffice.module.css";
import { CompanyHome, CompanyHomeContext, HOME_TABS, type HomeTab } from "./CompanyHome";

const DEFAULT_SIDEBAR_WIDTH = 420;
const SIDEBAR_WIDTH_KEY = "supymem.sidebarWidth";
const clampSidebarWidth = (width: number) => Number.isFinite(width) ? Math.min(600, Math.max(220, width)) : DEFAULT_SIDEBAR_WIDTH;

const TAB_ICONS = { home: "M2.5 7.2 8 2.8l5.5 4.4V13a1 1 0 0 1-1 1h-3.2V9.6H6.7V14H3.5a1 1 0 0 1-1-1Z", archives: "M2.5 4.2h4l1.2 1.3h5.8v7.2H2.5Z", toolkit: "M6.2 9.8 9.8 6.2M5.2 7.4 3.6 9a2.2 2.2 0 0 0 3.1 3.1l1.6-1.6M10.8 8.6l1.6-1.6a2.2 2.2 0 0 0-3.1-3.1L7.7 5.5", departments: "M3 4.2h10M3 8h10M3 11.8h10" };
const COMMAND_ICONS = {
  add: "M8 3.2v9.6M3.2 8h9.6", home: TAB_ICONS.home,
  inbox: "M2.4 8.2 4 3.6h8l1.6 4.6v4.2H2.4Zm0 0h3.1a2.5 2.5 0 0 0 5 0h3.1",
  team: "M8 8.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4ZM3.2 13.2c.6-2 2.4-3 4.8-3s4.2 1 4.8 3",
  today: "M3.2 4.2h9.6v8.6H3.2ZM3.2 6.6h9.6M5.4 2.8v2.2M10.6 2.8v2.2",
  upcoming: "M3.2 4.2h9.6v8.6H3.2ZM3.2 6.6h9.6M5.4 2.8v2.2M10.6 2.8v2.2M6 9.2h4",
  labels: "M2.6 8.2 8 2.8h5.2V8L7.4 13.6Z",
};

// PR #1248's plain sidebar, verbatim.
const SIDEBAR_CSS = `
.cos-rail {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  padding: 40px 10px 12px;
  background: #f7f7f8;
  color: #1c1c1e;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
}
.cos-nav, .cos-history { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.cos-history { flex: 1 1 auto; min-height: 0; overflow: auto; }
.local-office .cos-rail { overflow-y: auto; }
.local-office .cos-history { min-height: 68px; }
.local-office :is(.cos-nav, .cos-rail > .cos-row) { flex: none; }
.cos-rail :is(button, a).cos-row, .cos-rail button.cos-chat {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 34px;
  margin: 0;
  padding: 0 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  box-shadow: none;
  color: #1c1c1e;
  font: 500 14px/1 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  text-align: left;
  cursor: pointer;
}
.cos-rail button.cos-chat {
  display: block;
  padding: 0 10px 0 16px;
  overflow: hidden;
  color: #3a3a3c;
  font-weight: 400;
  line-height: 34px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cos-rail :is(button, a).cos-row:hover, .cos-rail button.cos-chat:hover { background: #ececee; }
.cos-rail :is(button, a).cos-row[aria-current], .cos-rail button.cos-chat[aria-current] {
  background: #e6e6ea;
  color: #111;
}
.cos-plus {
  width: 16px;
  flex: none;
  color: #1c1c1e;
  font-size: 18px;
  font-weight: 400;
  line-height: 1;
  text-align: center;
}
.cos-rule { flex: none; height: 1px; margin: 10px 8px; border: 0; background: #e3e3e6; }
.cos-day {
  margin: 12px 10px 4px;
  color: #8e8e93;
  font: 600 11px/1.2 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.cos-history section:first-child .cos-day { margin-top: 2px; }
.cos-empty { margin: 6px 12px; color: #8e8e93; font: 400 13px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif; }
.cos-history-line { position: relative; display: flex; align-items: center; min-width: 0; }
.cos-history-line > .cos-chat, .cos-history-line > form { flex: 1; min-width: 0; }
.cos-history summary.cos-chat, .cos-history input.cos-chat {
  display: block; box-sizing: border-box; height: 34px; margin: 0; padding: 0 10px 0 16px;
  border: 0; border-radius: 8px; background: transparent; color: #3a3a3c;
  font: 400 14px/34px -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  cursor: pointer; list-style: none;
}
.cos-history input.cos-chat { width: 100%; cursor: text; background: #ececee; }
.cos-history summary.cos-chat::-webkit-details-marker { display: none; }
.cos-history summary.cos-chat:hover { background: #ececee; }
.cos-history-actions { flex: none; }
.cos-history-actions > summary.cos-chat { width: 28px; padding: 0; text-align: center; opacity: 0; }
.cos-history-line:hover > .cos-history-actions > summary,
.cos-history-line:focus-within > .cos-history-actions > summary,
.cos-history-actions[open] > summary.cos-chat { opacity: 1; }
.cos-history-menu { position: absolute; top: 34px; right: 0; z-index: 1; width: 100%; background: #f7f7f8; border: 1px solid #e3e3e6; border-radius: 8px; }
.cos-history summary.cos-day { cursor: pointer; }
.cos-history button.cos-archived { flex: none; margin-top: auto; color: #8e8e93; font-size: 13px; font-weight: 400; }
.cos-history :where(button, input, summary):focus-visible { outline: 2px solid #8e8e93; outline-offset: -2px; }
`;

type History = {ventureId?: string; items: ChatHistoryItem[]; current: string; archivedItems?: ChatHistoryItem[]; saved?: boolean; busy?: boolean; error?: string;
  newConversation?: () => Promise<boolean>; update?: (id: string, patch: ChatConversationPatch) => Promise<boolean>; loadArchived?: () => Promise<void>};

// PR #1248's sidebar entries. "Idea" and "LinkedIn" are named slots: the living idea page (/v/[id]/idea/current) and LinkedIn people (/v/[id]/linkedin).
const SIDE_TABS = [
  { id: "archives", label: "Tuesday Archives", hint: "Files and folders" },
] as const;
const SIDE_SLOTS = [
  { id: "idea", label: "Idea", path: "idea/current" },
  { id: "leads", label: "Leads", path: "leads" },
  { id: "connections", label: "Connections", path: "connections" },
  { id: "linkedin", label: "LinkedIn", path: "linkedin" },
] as const;

const chatTitle = (text: string) => text.replace(/\s+/g, " ").trim();

export function CompanyOffice({ ventureId, pathname, standalone = false, onSettings, chatAvailable, children, title, nextStep, controls, scrollRef, notices }: {
  ventureId: string; pathname: string; onSettings: () => void; chatAvailable?: boolean; children: ReactNode;
  standalone?: boolean; title?: string; nextStep?: ReactNode; controls?: ReactNode; notices?: ReactNode; scrollRef?: RefObject<HTMLDivElement | null>;
}) {
  const local = useLocalMode();
  const featureVisible = useFeatureVisibility();
  const localSettings = local && /^\/settings\/local(?:\/|$)/.test(pathname);
  const showPageHead = !localSettings || pathname.replace(/\/$/, "") === "/settings/local";
  const [query, setQuery] = useState("");
  const [palette, setPalette] = useState(false);
  const [active, setActive] = useState(0);
  const [dropOver, setDropOver] = useState(false);
  const [dragGhost, setDragGhost] = useState<{ name: string; x: number; y: number; intent: "add" | "remove" } | null>(null);
  const dragging = useRef(false);
  const sequence = useRef(0);
  const dragCleanup = useRef<(() => void) | null>(null);
  const nativeRouter = useRouter();
  const router = chatNavigation(nativeRouter);
  const search = useSearchParams();
  const requestedTab = search.get("office") || "home";
  const tab: HomeTab = Object.hasOwn(HOME_TABS, requestedTab) ? requestedTab as HomeTab : "home";
  const state = useCompanyHomeData(ventureId, !local || tab !== "home");
  const paletteInput = useRef<HTMLInputElement>(null);
  const paletteRoot = useRef<HTMLDivElement>(null);
  const paletteInvoker = useRef<HTMLElement | null>(null);
  const [history, setHistory] = useState<History>({ items: [], current: "" });
  const [archived, setArchived] = useState(false);
  const [historyEdit, setHistoryEdit] = useState<{id: string; field: "title" | "folder"; value: string} | null>(null);
  const historyItems = archived ? history.archivedItems || [] : history.items;
  const folders = [...new Set([...history.items, ...(history.archivedItems || [])].map(item => item.folder).filter((folder): folder is string => !!folder))].sort((first, second) => first.localeCompare(second));
  const historyRowId = (id: string) => `chat-history-${ventureId}-${id}`;
  function closeHistoryMenu(target: HTMLElement) {
    const menu = target.closest(".cos-history-actions") as HTMLDetailsElement | null;
    if (menu) {menu.open = false; (menu.querySelector("summary") as HTMLElement | null)?.focus();}
  }
  async function updateHistory(item: ChatHistoryItem, patch: ChatConversationPatch, target?: HTMLElement) {
    if (target) closeHistoryMenu(target);
    const saved = await history.update?.(item.id, patch);
    if (saved) requestAnimationFrame(() => {
      const row = document.getElementById(historyRowId(item.id));
      const folder = row?.closest("details");
      if (folder) folder.open = true;
      (row || document.getElementById(`chat-history-toggle-${ventureId}`))?.focus();
    });
    return saved;
  }
  function historyRow(item: ChatHistoryItem) {
    return <div key={item.id} className="cos-history-line">
      {historyEdit?.id === item.id ? <form onSubmit={async event => {
        event.preventDefault();
        const value = historyEdit.field === "title" ? historyEdit.value.replace(/\s+/g, " ").trim() : historyEdit.value.trim();
        if ((historyEdit.field === "folder" || value) && await updateHistory(item, {[historyEdit.field]: value})) setHistoryEdit(null);
      }}><input className="cos-chat" aria-label={historyEdit.field === "title" ? "Chat name" : "Folder name"} autoFocus
        required={historyEdit.field === "title"} maxLength={historyEdit.field === "title" ? 120 : undefined}
        placeholder={historyEdit.field === "folder" ? "Folder name (blank removes folder)" : undefined} value={historyEdit.value} disabled={history.busy}
        onChange={event => setHistoryEdit({...historyEdit, value: event.target.value})} onKeyDown={event => {
          if (event.key === "Escape") {event.preventDefault(); setHistoryEdit(null); requestAnimationFrame(() => document.getElementById(historyRowId(item.id))?.focus());}
        }} /></form> : <button id={historyRowId(item.id)} type="button" className="cos-chat" title={chatTitle(item.text)}
        aria-current={chatFirst && (history.current ? history.current === item.id : selected === item.id) ? "true" : undefined}
        onClick={() => openHistory(item.id)}>{chatTitle(item.text)}</button>}
      {history.saved && <details className="cos-history-actions" onKeyDown={event => {
        if (event.key === "Escape") {event.preventDefault(); event.stopPropagation(); closeHistoryMenu(event.currentTarget);}
      }}><summary className="cos-chat" aria-label={`Actions for ${chatTitle(item.text)}`}>...</summary>
        <div className="cos-history-menu" role="group" aria-label="Chat actions">
          <button type="button" className="cos-chat" disabled={history.busy} onClick={event => {
            closeHistoryMenu(event.currentTarget); setHistoryEdit({id: item.id, field: "title", value: chatTitle(item.text)});
          }}>Rename</button>
          <button type="button" className="cos-chat" disabled={history.busy} onClick={event => void updateHistory(item, {pinned: !item.pinned}, event.currentTarget)}>{item.pinned ? "Unpin" : "Pin"}</button>
          <details><summary className="cos-chat">Move to folder</summary>
            {folders.map(folder => <button key={folder} type="button" className="cos-chat" disabled={history.busy} onClick={event => void updateHistory(item, {folder}, event.currentTarget)}>{folder}</button>)}
            <button type="button" className="cos-chat" disabled={history.busy} onClick={event => {
              closeHistoryMenu(event.currentTarget); setHistoryEdit({id: item.id, field: "folder", value: item.folder || ""});
            }}>New folder...</button>
          </details>
          {item.folder && <button type="button" className="cos-chat" disabled={history.busy} onClick={event => void updateHistory(item, {folder: ""}, event.currentTarget)}>Remove from folder</button>}
          <button type="button" className="cos-chat" disabled={history.busy} onClick={event => void updateHistory(item, {archived: !item.archived}, event.currentTarget)}>{item.archived ? "Unarchive" : "Archive"}</button>
        </div>
      </details>}
    </div>;
  }
  const [selected, setSelected] = useState("");
  const [sidebarWidth, setSidebarWidth] = useState(DEFAULT_SIDEBAR_WIDTH);
  const [localSidebarOpen, setLocalSidebarOpen] = useState(false);
  const localSidebarShown = localSidebarOpen || palette;
  const localMenuRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const sidebarDrag = useRef<{ pointerId: number; startX: number; width: number } | null>(null);
  function resizeSidebar(width: number) {
    const next = clampSidebarWidth(width);
    setSidebarWidth(next);
    try { window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(next)); } catch {}
  }
  function finishSidebarResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (sidebarDrag.current?.pointerId !== event.pointerId) return;
    sidebarDrag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(SIDEBAR_WIDTH_KEY);
      if (saved?.trim()) setSidebarWidth(clampSidebarWidth(Number(saved)));
    } catch {}
  }, []);
  useEffect(() => {
    const shell = sidebarRef.current?.closest<HTMLElement>(".app-shell");
    if (!shell) return;
    shell.style.setProperty("--office-rail", `${sidebarWidth}px`);
    return () => { shell.style.removeProperty("--office-rail"); };
  }, [sidebarWidth]);
  const [returnVenture, setReturnVenture] = useState<{ subject: string | null; id: string | null } | null>(null);
  useEffect(() => {
    if (!local || !pathname.startsWith("/settings/local")) return;
    const subject = state.owner.subject;
    try {
      const saved = resumeAppRoute(window.localStorage, subject, "/", true, "home");
      setReturnVenture({ subject, id: saved ? appRouteVentureId(saved) : null });
    } catch {
      setReturnVenture({ subject, id: null });
    }
  }, [local, pathname, state.owner.subject]);
  const homeHref = ventureId ? `/v/${encodeURIComponent(ventureId)}` : local
    ? pathname.startsWith("/settings/local") && returnVenture?.subject === state.owner.subject && returnVenture?.id ? `/v/${encodeURIComponent(returnVenture.id)}` : "/"
    : "/new";
  const onHome = pathname === "/new" || pathname.replace(/\/$/, "") === homeHref;
  const chatFirst = onHome && tab === "home" && chatAvailable;
  useEffect(() => { if (local) setLocalSidebarOpen(false); }, [local, pathname, tab, history.current]);
  const company = state.data.name || title || "Company";
  const { pin, pinBusy } = state;
  const commands = [
    { id: "add", label: "Quick Add", keys: ["⌥", "Q"], run: () => prefillChat(query.trim()) },
    ...founderRouteLinks(ventureId, local).filter(route => featureVisible("paths", route.href) && !chatFirstHidden(route.href) && (ventureId || ["/", "/new", "/account", ...(local ? ["/settings/local"] : [])].includes(route.href))).map(route => {
      const screen = route.href.split("?")[0].split("/").at(-1);
      const icon = ({research: "inbox", people: "team", now: "today", tasks: "upcoming", org: "labels"})[screen || ""] || "home";
      const keys = route.href === homeHref ? ["⌥", "H"] : ({research: ["⌥", "G", "then", "⌥", "I"], people: ["⌥", "G", "then", "⌘", "I"], now: ["⌥", "G", "then", "⌥", "T"], tasks: ["⌥", "G", "then", "⌥", "U"], org: ["⌥", "G", "then", "⌥", "L"]})[screen || ""] || [];
      return {id: route.href, icon, label: `Go to ${route.label}`, keys, run: () => (route.href === homeHref ? nativeRouter : router).push(route.href)};
    }),
    { id: "settings", label: "Open settings", keys: [], run: onSettings },
    ...(query.trim() && chatAvailable ? [{ id: "chat", label: `Ask Charlie: ${query.trim()}`, keys: [], run: () => prefillChat(query.trim()) }] : []),
  ].filter(command => command.id !== "add" || chatAvailable);
  const shown = commands.filter(command => command.id === "chat" || command.id === "add" || command.label.toLowerCase().includes(query.toLowerCase()));
  function closePalette(restoreFocus = true) {
    setPalette(false);
    if (restoreFocus) paletteInvoker.current?.focus({ preventScroll: true });
  }
  function runCommand(index: number) { closePalette(); shown[index]?.run(); }
  function go(next: HomeTab) { setSelected(next); router.push(`${homeHref}${next === "home" ? "" : `?office=${next}`}`); }
  // The chat stays mounted under the company tabs (as in #1248); from other pages these only open it.
  async function newChat() {
    if (!ventureId) { document.getElementById("entry-message")?.focus(); return; }
    if (history.busy || !await history.newConversation?.()) return;
    setSelected("");
    setArchived(false);
    if (!chatFirst) nativeRouter.push(homeHref);
    window.dispatchEvent(new CustomEvent("supymem-chat-talk"));
  }
  function openHistory(id: string) {
    setSelected(id);
    if (!chatFirst) nativeRouter.push(homeHref);
    if (onHome) window.dispatchEvent(new CustomEvent("supymem:chat-jump", { detail: { id } }));
  }
  function openDepartment(name: string) { router.push(`${homeHref}?office=departments&department=${encodeURIComponent(name)}`); }
  function grabDepartment(name: string, event: ReactPointerEvent<HTMLElement>, intent: "add" | "remove") {
    if (event.button !== 0 || pinBusy || state.data.pins === null) return;
    if (intent === "add" && (event.target as HTMLElement).closest("button")) return;
    event.preventDefault();
    dragCleanup.current?.();
    const startX = event.clientX, startY = event.clientY;
    let moved = false;
    const blockSelect = (event: Event) => event.preventDefault();
    const clearSelection = () => window.getSelection()?.removeAllRanges();
    const overSidebar = (event: PointerEvent) => {
      const box = document.querySelector("aside[aria-label='Sidebar']")?.getBoundingClientRect();
      return !!box && event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
    };
    const move = (event: PointerEvent) => {
      event.preventDefault(); clearSelection();
      if (Math.hypot(event.clientX - startX, event.clientY - startY) < 6) return;
      moved = true; dragging.current = true;
      const over = overSidebar(event);
      setDropOver(intent === "add" ? over : !over);
      setDragGhost({ name, x: event.clientX, y: event.clientY, intent });
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("pointerup", up, true);
      window.removeEventListener("pointercancel", cancel, true);
      document.removeEventListener("selectstart", blockSelect, true);
      document.removeEventListener("dragstart", blockSelect, true);
      document.documentElement.classList.remove("dept-dragging");
      dragCleanup.current = null;
    };
    const cancel = () => { cleanup(); dragging.current = false; setDropOver(false); setDragGhost(null); };
    const up = (event: PointerEvent) => {
      cleanup(); clearSelection();
      if (moved && intent === "add" && overSidebar(event)) void pin(name, true);
      else if (moved && intent === "remove" && !overSidebar(event)) void pin(name, false);
      else if (!moved) openDepartment(name);
      setDropOver(false); setDragGhost(null);
      window.setTimeout(() => { dragging.current = false; }, 0);
    };
    dragCleanup.current = cleanup;
    document.addEventListener("selectstart", blockSelect, true);
    document.addEventListener("dragstart", blockSelect, true);
    document.documentElement.classList.add("dept-dragging");
    clearSelection();
    window.addEventListener("pointermove", move, true);
    window.addEventListener("pointerup", up, true);
    window.addEventListener("pointercancel", cancel, true);
  }
  useEffect(() => {
    setPalette(false); setQuery(""); setActive(0); setDropOver(false); setDragGhost(null); dragging.current = false;
    return () => dragCleanup.current?.();
  }, [state.owner]);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      const editing = event.target instanceof HTMLElement && (event.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName) || Boolean(event.target.closest("form, [role='form']")) || Boolean((event.target.closest("section, article, [class*='card'], [class*='Card']") || event.target.parentElement)?.querySelector("input, textarea, select, [contenteditable='true']")));
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (palette) closePalette();
        else { paletteInvoker.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setPalette(true); }
        setQuery(""); setActive(0);
      }
      else if (event.key === "Escape" && palette) closePalette();
      else if (!palette && !editing && !event.repeat && (event.metaKey || event.ctrlKey || event.altKey)) {
        const key = event.altKey && event.code?.startsWith("Key") ? event.code.slice(3).toLowerCase() : event.key.toLowerCase();
        if ((key === "/" || key === "q") && chatAvailable) { event.preventDefault(); prefillChat(""); }
        else if (key === "h") { event.preventDefault(); nativeRouter.push(homeHref); }
        else if (key === "g") sequence.current = Date.now();
        else if (sequence.current && Date.now() - sequence.current < 1000) {
          const screen = ({i: event.metaKey || event.ctrlKey ? "people" : "research", t: "now", u: "tasks", l: "org"})[key];
          sequence.current = 0;
          if (screen) { event.preventDefault(); router.push(`${homeHref}/${screen}`); }
        }
      }
    };
    const outside = (event: PointerEvent) => { if (event.target instanceof Node && !paletteRoot.current?.contains(event.target)) closePalette(false); };
    window.addEventListener("keydown", shortcut); window.addEventListener("pointerdown", outside);
    return () => { window.removeEventListener("keydown", shortcut); window.removeEventListener("pointerdown", outside); };
  }, [chatAvailable, palette, homeHref]);
  useEffect(() => {
    const root = document.documentElement;
    if (onHome) root.dataset.officeTab = tab; else delete root.dataset.officeTab;
    return () => { delete root.dataset.officeTab; };
  }, [onHome, tab]);
  useEffect(() => {
    window.addEventListener("supymem:open-settings", onSettings);
    return () => window.removeEventListener("supymem:open-settings", onSettings);
  }, [onSettings]);
  useEffect(() => {
    window.dispatchEvent(new CustomEvent("supymem:conversation", { detail: { title: tab === "home" || tab === "departments" ? company : SIDE_TABS.find(item => item.id === tab)?.label } }));
  }, [company, tab]);
  useEffect(() => {
    setHistory({items: [], current: ""}); setArchived(false); setHistoryEdit(null);
    const onHistory = (event: Event) => {
      const detail = (event as CustomEvent<History>).detail;
      if (detail?.ventureId === ventureId && Array.isArray(detail.items)) setHistory(detail);
    };
    window.addEventListener("supymem:chat-history", onHistory);
    window.dispatchEvent(new CustomEvent("supymem:chat-history-request"));
    return () => window.removeEventListener("supymem:chat-history", onHistory);
  }, [ventureId, state.owner.epoch, state.owner.subject]);
  useEffect(() => { if (palette) paletteInput.current?.focus({ preventScroll: true }); }, [palette]);
  return <CompanyHomeContext.Provider key={`${ventureId}:${state.owner.epoch}:${state.owner.subject}`} value={state}>
    <div className={`${styles.home}${chatFirst ? ` ${styles.homeChatFirst} office-chat-first` : ""}${localSettings ? " office-settings" : ""}${standalone ? " office-standalone" : ""}${local ? ` local-office${localSidebarShown ? " local-sidebar-open" : ""}` : ""}`} style={{ "--sidebar-width": `${sidebarWidth}px` } as CSSProperties}>
      {local && (chatFirst || standalone) && <button ref={localMenuRef} type="button" className="local-office-menu" aria-controls="local-office-sidebar" aria-expanded={localSidebarShown}
        onClick={() => { setLocalSidebarOpen(!localSidebarShown); if (palette) closePalette(false); }}
        onKeyDown={event => {
          if (event.key !== "Escape" || !localSidebarShown) return;
          event.preventDefault();
          setLocalSidebarOpen(false);
          if (palette) closePalette(false);
        }}>{localSidebarShown ? "Hide sidebar" : "Show sidebar"}</button>}
      <aside ref={sidebarRef} id={local ? "local-office-sidebar" : undefined} className={`${styles.rail} ${styles.railIm} cos-rail`} aria-label="Your sidebar"
        onKeyDown={local ? event => {
          if (event.key !== "Escape" || event.defaultPrevented || !localSidebarOpen || (event.target as Element | null)?.closest("details[open]")) return;
          event.preventDefault();
          setLocalSidebarOpen(false);
          localMenuRef.current?.focus();
        } : undefined}>
        <div className={styles.sidebarResize} role="separator" aria-label="Sidebar width" aria-orientation="vertical" aria-valuemin={220} aria-valuemax={600} aria-valuenow={sidebarWidth} tabIndex={0}
          onPointerDown={event => {
            if (event.button !== 0 || event.isPrimary === false || sidebarDrag.current) return;
            event.preventDefault();
            event.currentTarget.focus();
            event.currentTarget.setPointerCapture(event.pointerId);
            sidebarDrag.current = { pointerId: event.pointerId, startX: event.clientX, width: sidebarWidth };
          }}
          onPointerMove={event => {
            const drag = sidebarDrag.current;
            if (drag?.pointerId === event.pointerId) resizeSidebar(drag.width + event.clientX - drag.startX);
          }}
          onPointerUp={finishSidebarResize} onPointerCancel={finishSidebarResize} onLostPointerCapture={() => { sidebarDrag.current = null; }}
          onKeyDown={event => {
            if (event.key === "Home") {
              event.preventDefault();
              resizeSidebar(DEFAULT_SIDEBAR_WIDTH);
              return;
            }
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            resizeSidebar(sidebarWidth + (event.key === "ArrowLeft" ? -16 : 16));
          }}
          onDoubleClick={() => resizeSidebar(DEFAULT_SIDEBAR_WIDTH)} />
        <style>{SIDEBAR_CSS}</style>
        {chatFirst && notices}
        <div id="company-office-notice" />
        <nav className="cos-nav" aria-label="Main" onClick={local ? () => { setLocalSidebarOpen(false); localMenuRef.current?.focus(); } : undefined}>
          <button type="button" className="cos-row" disabled={!!ventureId && (!history.newConversation || history.busy)} onClick={newChat}>
            <span className="cos-plus" aria-hidden="true">+</span>
            <span>New chat</span>
          </button>
          {ventureId && !local && SIDE_TABS.map(item => <button key={item.id} type="button" className="cos-row" aria-current={onHome && tab === item.id ? "page" : undefined} onClick={() => go(item.id)}>{item.label}</button>)}
          {ventureId && SIDE_SLOTS.map(item => {
            if (item.id === "connections") return <button key={item.id} type="button" className="cos-row" data-sidebar-slot={item.id}
              aria-current={pathname === "/connections" ? "page" : undefined}
              onClick={() => nativeRouter.push(`/connections?venture=${encodeURIComponent(ventureId)}`)}>{item.label}</button>;
            const href = `${homeHref}/${item.path}`;
            return <button key={item.id} type="button" className="cos-row" data-sidebar-slot={item.id} aria-current={pathname.replace(/\/$/, "") === href ? "page" : undefined} onClick={() => router.push(href)}>{item.label}</button>;
          })}
        </nav>
        <Link className="cos-row" href={ventureId ? `${homeHref}/guide` : "/guide"} aria-label="How to use Supymem" aria-current={/\/guide\/?$/.test(pathname) ? "page" : undefined} data-usage-control="guide_nav">How to use Supymem</Link>
        <hr className="cos-rule" />
        <nav className="cos-history" aria-label="Chat history">
          {historyItems.length === 0 ? <p className="cos-empty">{archived ? "No archived chats." : "Your chats with Charlie show up here."}</p> : historyGroups(historyItems).map(group => <section key={group.key} aria-label={group.folder ? undefined : group.label}>
            {group.folder ? <details open><summary className="cos-day">{group.label}</summary>{group.items.map(historyRow)}</details> : <>
              <p className="cos-day">{group.label}</p>{group.items.map(historyRow)}
            </>}
          </section>)}
          <ErrorNotice error={history.error} source="chat.conversations" className="cos-empty" />
          {history.saved && <button id={`chat-history-toggle-${ventureId}`} type="button" className="cos-row cos-archived" aria-pressed={archived} onClick={() => {
            setArchived(!archived); setHistoryEdit(null); if (!archived) void history.loadArchived?.();
          }}>Archived</button>}
        </nav>
        {chatFirst && ventureId && <CompanyHome tab={tab}>{children}</CompanyHome>}
        <hr className="cos-rule" />
        {/* Not in PR #1248: without it a founder with several ventures cannot leave this one. Same row style. */}
        <button type="button" className="cos-row" onClick={() => router.push("/")}>All ventures</button>
        <button type="button" className="cos-row" onClick={onSettings}>Settings</button>
        {palette && <div className={styles.topRow} ref={paletteRoot}>
          <div id="command-palette" className={styles.palette}>
            <div className={styles.paletteSearch}><input ref={paletteInput} role="combobox" aria-expanded={palette} aria-autocomplete="list" value={query} onChange={event => { setQuery(event.target.value); setActive(0); }} onKeyDown={event => {
              if (event.nativeEvent.isComposing || event.keyCode === 229) return;
              if (event.key === "ArrowDown") { event.preventDefault(); setActive(index => Math.min(index + 1, Math.max(shown.length - 1, 0))); }
              if (event.key === "ArrowUp") { event.preventDefault(); setActive(index => Math.max(index - 1, 0)); }
              if (event.key === "Enter") { event.preventDefault(); runCommand(active); }
            }} placeholder="What would you like to do?" aria-label="What would you like to do?" autoComplete="off" aria-controls="office-command-list" aria-activedescendant={shown[active] ? `company-command-${active}` : undefined} /><span className={styles.kbd}><kbd>⌘</kbd><kbd>K</kbd></span></div>
            <ul id="office-command-list" role="listbox" aria-label="Your commands">{shown.map((command, index) => <li key={command.id}><button id={`company-command-${index}`} type="button" role="option" aria-selected={index === active} className={index === active ? styles.paletteOn : undefined} onMouseEnter={() => setActive(index)} onMouseDown={event => event.preventDefault()} onClick={() => runCommand(index)}><svg viewBox="0 0 16 16" data-kind={command.id === "add" ? "add" : command.id === "chat" ? "inbox" : "icon" in command ? command.icon : "home"} aria-hidden="true"><path d={COMMAND_ICONS[(command.id === "add" ? "add" : command.id === "chat" ? "inbox" : "icon" in command ? command.icon : "home") as keyof typeof COMMAND_ICONS]} /></svg><span>{command.label}</span><span className={styles.keys}>{command.keys.map(key => key === "then" ? <em key={key}>then</em> : <kbd key={key}>{key}</kbd>)}</span></button></li>)}</ul>
          </div>
        </div>}
      </aside>
      {dragGhost && <div className={styles.dragGhost} style={{ left: dragGhost.x, top: dragGhost.y }}>{dragGhost.name}</div>}
      {!chatFirst && <div className={dropOver && dragGhost?.intent === "remove" ? `${styles.main} ${styles.mainDrop}` : styles.main}
        role={standalone ? "main" : undefined} tabIndex={standalone ? -1 : undefined} aria-label={standalone ? title : undefined}>
        {onHome ? <CompanyHome key={tab} tab={tab} department={search.get("department") || undefined} scrollRef={scrollRef} notices={notices} grabDepartment={grabDepartment} dragging={dragging}>{children}</CompanyHome> : <>
          {showPageHead && <div className={styles.pageHead}><div><Link className={styles.back} href={homeHref} data-chat-route-native={true}>{standalone || localSettings || pathname === "/connections" ? "Back to chat" : "Back to company"}</Link>{!localSettings && !/\/guide\/?$/.test(pathname) && <h1>{title || "Company"}</h1>}{nextStep && <div className={styles.nextStep}>{nextStep}</div>}</div>{controls && <div className={styles.pageControls}>{controls}</div>}</div>}
          <div className={`${styles.canvas} workspace-body`} ref={scrollRef}>{notices}{children}</div>
        </>}
      </div>}
    </div>
  </CompanyHomeContext.Provider>;
}
