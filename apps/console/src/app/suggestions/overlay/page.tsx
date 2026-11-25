"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { desktopInvokeOrThrow, desktopListen } from "@/lib/desktop";
import { reportUserError } from "@/lib/userError";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { scopedSuggestions, snoozeSuggestions, suggestionsSnoozed, suggestionsPath, type Decision, type Suggestion, type SuggestionList } from "@/lib/suggestions";
import { SuggestionCard } from "@/components/suggestions/SuggestionCard";
import styles from "@/components/suggestions/suggestions.module.css";

type Custody = { subject: string; device_id: string; auth_epoch: number; presentation_id: string; venture_id?: string | null; viewport_width?: number };
type Reply = { status: number; data: unknown };
async function overlayInvoke<T = unknown>(command: string, args?: Record<string, unknown>): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      desktopInvokeOrThrow<T>(command, args).catch(() => { throw new Error("The Mac app could not finish this action. Retry to check saved status."); }),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          const message = "The Mac app took too long to respond. Retry to check saved status.";
          reportUserError({ source: command, message: `${message} (${command})`, serverCode: "native_timeout" });
          reject(new Error(message));
        }, 8_000);
      }),
    ]);
  } finally { clearTimeout(timer); }
}
function sameCustody(left: Custody | null, right: Custody | null) {
  return !!left && !!right && left.subject === right.subject && left.device_id === right.device_id
    && left.auth_epoch === right.auth_epoch && left.presentation_id === right.presentation_id;
}

/** The Mac app's current-venture offer, collapsed until the founder opens it. */
export default function SuggestionsOverlayPage() {
  const [items, setItems] = useState<Suggestion[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadedOwner, setLoadedOwner] = useState<Custody | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pendingDecision, setPendingDecision] = useState<Record<string, Decision>>({});
  const [confirmed, setConfirmed] = useState<Record<string, Decision>>({});
  const [loadError, setLoadError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [snoozed, setSnoozed] = useState(false);
  const shown = useRef(new Set<string>());
  const root = useRef<HTMLDivElement>(null);
  const presentation = useRef<string | null>(null);
  const owner = useRef<Custody | null>(null);
  const generation = useRef(0);
  const loads = useRef(0);
  const active = useRef(false);
  const decisions = useRef(new Set<string>());
  const uncertain = useRef(new Set<string>());
  const completed = useRef(new Set<string>());
  const venture = useRef<string | null>(null);
  const visible = expanded && !snoozed ? items[0] : undefined;

  const collapse = useCallback(() => {
    setExpanded(false);
    root.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, []);

  const retire = useCallback(() => {
    generation.current++; loads.current++;
    owner.current = null; shown.current.clear(); decisions.current.clear();
    uncertain.current.clear(); completed.current.clear();
    setPendingDecision({}); setConfirmed({}); setLoadError("");
    setItems([]); setLoaded(false); setLoadedOwner(null); setBusy({}); setErrors({});
    setExpanded(false); setSnoozed(false); venture.current = null;
  }, []);

  const custody = useCallback(async (): Promise<Custody | null> => {
    if (!presentation.current) return null;
    const value = await overlayInvoke<Custody>("suggestions_overlay_custody", { presentationId: presentation.current });
    return value && typeof value.subject === "string" && value.subject.startsWith("usr_")
      && typeof value.device_id === "string" && !!value.device_id
      && Number.isSafeInteger(value.auth_epoch) && value.auth_epoch >= 0
      && value.presentation_id === presentation.current ? value : null;
  }, []);

  const request = useCallback(async (bound: Custody, method: "GET" | "POST", path: string, body?: unknown) => {
    const reply = await overlayInvoke<Reply>("suggestions_overlay_request", {
      presentationId: bound.presentation_id, subject: bound.subject, deviceId: bound.device_id,
      authEpoch: bound.auth_epoch, method, path, ...(body === undefined ? {} : { body }),
    });
    return { ok: !!reply && reply.status >= 200 && reply.status < 300, status: reply?.status ?? 0, data: reply?.data };
  }, []);

  const load = useCallback(async (): Promise<void> => {
    const before = generation.current, sequence = ++loads.current;
    try {
      const bound = await custody();
      if (!active.current || before !== generation.current || sequence !== loads.current) return;
      if (!bound) { retire(); return; }
      if (owner.current && !sameCustody(owner.current, bound)) { retire(); return; }
      owner.current = bound;
      if (!bound.venture_id) {
        retire(); void overlayInvoke("suggestions_overlay_close", { presentationId: bound.presentation_id }).catch(() => {}); return;
      }
      if (venture.current !== bound.venture_id) {
        venture.current = bound.venture_id; setItems([]); setExpanded(false); setLoaded(false);
      }
      const current = () => active.current && before === generation.current && sequence === loads.current && sameCustody(owner.current, bound);
      const r = await request(bound, "GET", suggestionsPath(bound.venture_id || "", "venture"));
      if (!current()) return;
      const after = await custody();
      if (!current()) return;
      if (!sameCustody(bound, after) || bound.venture_id !== after?.venture_id) { retire(); void load(); return; }
      if (!r.ok || !Array.isArray((r.data as SuggestionList | undefined)?.items)) throw new Error("Suggestions could not be loaded. Retry.");
      const fresh = scopedSuggestions((r.data as SuggestionList).items, bound.venture_id || "", "venture").filter((i) => i.status === "new" && !completed.current.has(i.id));
      setSnoozed(suggestionsSnoozed(bound.subject));
      setItems(fresh); setLoadedOwner(bound); setLoaded(true);
      setLoadError("");
    } catch (error) {
      if (active.current && before === generation.current && sequence === loads.current) {
        setLoadError(error instanceof Error ? error.message : "Suggestions could not be loaded. Retry.");
      }
    }
  }, [custody, request, retire]);

  const accountChanged = useCallback(async () => {
    const before = generation.current, bound = owner.current;
    let current: Custody | null;
    try { current = await custody(); } catch { return; }
    if (!active.current || before !== generation.current) return;
    // A completed same-account request can emit a connected status after
    // reporting busy. Only exact native custody retirement clears this page.
    // The native host already hides retired presentations immediately.
    if (bound && sameCustody(bound, current)) return;
    retire(); void load();
  }, [custody, load, retire]);

  useEffect(() => {
    document.documentElement.style.background = "transparent";
    document.body.style.background = "transparent";
    // This document owns only its initial native-issued presentation. Never
    // adapt a retired document to another account's later URL or token.
    presentation.current ??= new URLSearchParams(window.location.search).get("presentation") || "";
    active.current = true;
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    let closed = false;
    const stops: (() => void)[] = [];
    for (const [event, listener] of [
      ["suggestions-refresh", () => void load()],
      ["supymem-native-auth", () => void accountChanged()],
      ["suggestions-collapse", collapse],
    ] as const) {
      void desktopListen(event, listener).then((stop) => { if (closed) stop(); else stops.push(stop); });
    }
    return () => { closed = true; active.current = false; generation.current++; loads.current++;
      window.clearInterval(timer); stops.forEach((stop) => stop()); };
  }, [load, accountChanged, collapse]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") collapse(); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [collapse]);

  useEffect(() => {
    if (!root.current || !loaded || !loadedOwner) return;
    const bound = loadedOwner, observed = generation.current;
    let closed = false;
    const current = () => !closed && active.current && observed === generation.current && sameCustody(owner.current, bound);
    if (items.length === 0 || snoozed || (bound.viewport_width !== undefined && bound.viewport_width <= 600)) {
      void overlayInvoke("suggestions_overlay_close", { presentationId: bound.presentation_id }).catch(() => {}); return;
    }
    const height = () => Math.ceil(root.current?.getBoundingClientRect().height || 0);
    const width = () => visible ? 400 : Math.ceil(root.current?.getBoundingClientRect().width || 0);
    // A fresh list must commit to the DOM before native presentation. The
    // shell checks the nonce/owner again at its actual no-activation show.
    void (async () => {
      if (!current() || !sameCustody(bound, await custody()) || !current()) return;
      const ready = await overlayInvoke<boolean>("suggestions_overlay_ready", {
        presentationId: bound.presentation_id, subject: bound.subject, deviceId: bound.device_id,
        authEpoch: bound.auth_epoch, height: height(), width: width(),
      });
      if (ready !== true || !current()) return;
      const unseen = visible && !shown.current.has(visible.id) ? [visible.id] : [];
      if (unseen.length) {
        unseen.forEach((id) => shown.current.add(id));
        await request(bound, "POST", "/api/suggestions/shown", { ids: unseen });
      }
    })().catch((error) => { if (current()) setLoadError(error instanceof Error ? error.message : "Suggestions could not be shown. Retry."); });
    const report = () => { if (current()) void overlayInvoke("suggestions_overlay_resize", {
      presentationId: bound.presentation_id, height: height(), width: width(),
    }).catch(() => {}); };
    const obs = new ResizeObserver(report);
    obs.observe(root.current);
    return () => { closed = true; obs.disconnect(); };
  }, [items, loaded, loadedOwner, custody, request, visible, snoozed]);

  async function decide(id: string, decision: Decision) {
    const bound = loadedOwner, observed = generation.current;
    if (!bound || !sameCustody(owner.current, bound) || !items.some((item) => item.id === id) || decisions.current.has(id) || completed.current.has(id)) return;
    // ponytail: old IPC cannot be cancelled, reconcile by read only until the presentation is retired.
    if (uncertain.current.has(id)) {
      await load();
      if (active.current && observed === generation.current && sameCustody(owner.current, bound)) {
        setErrors((value) => ({ ...value, [id]: "Saved status checked. If the card remains, Retry again or choose Later; approval may still be finishing." }));
      }
      return;
    }
    const current = () => active.current && observed === generation.current && sameCustody(owner.current, bound);
    decisions.current.add(id);
    setBusy((b) => ({ ...b, [id]: true }));
    setPendingDecision((value) => ({ ...value, [id]: decision }));
    setErrors((e) => ({ ...e, [id]: "" }));
    try {
      const now = await custody();
      if (!sameCustody(bound, now) || now?.venture_id !== bound.venture_id || !current()) { if (current()) retire(); return; }
      uncertain.current.add(id);
      const r = await request(bound, "POST", `/api/suggestions/${encodeURIComponent(id)}/decision`, { decision });
      if (!current()) return;
      const data = r.data as { error?: { code?: string; request_sent?: boolean; automatic_retry_allowed?: boolean }; detail?: { error?: { code?: string } } } | undefined;
      const alreadyDecided = r.status === 409 && (data?.error?.code === "already_decided" || data?.detail?.error?.code === "already_decided");
      const notSent = r.status === 503 && data?.error?.code === "suggestions_send_busy"
        && data.error.request_sent === false && data.error.automatic_retry_allowed === true;
      if (notSent) {
        uncertain.current.delete(id);
        setErrors((value) => ({ ...value, [id]: "The Mac app is busy. Nothing was sent. Retry your decision." }));
      }
      else if (r.ok || alreadyDecided) {
        completed.current.add(id); uncertain.current.delete(id);
        if (!alreadyDecided) {
          setConfirmed((value) => ({ ...value, [id]: decision }));
          await new Promise<void>((resolve) => setTimeout(resolve, 500));
        }
        if (current()) setItems((list) => list.filter((i) => i.id !== id));
      }
      else setErrors((e) => ({ ...e, [id]: "The outcome could not be confirmed. Refresh saved status before trying again." }));
    } catch (error) {
      if (current()) setErrors((e) => ({ ...e, [id]: error instanceof Error ? error.message : "The request stopped. Retry to check saved status." }));
    } finally {
      if (current()) {
        decisions.current.delete(id);
        setBusy((b) => { const next = { ...b }; delete next[id]; return next; });
      }
    }
  }

  async function details(item: Suggestion) {
    const bound = loadedOwner, observed = generation.current;
    const current = () => active.current && observed === generation.current && sameCustody(owner.current, bound);
    try {
      if (!bound || !current() || !items.some((card) => card.id === item.id) || !sameCustody(bound, await custody()) || !current()) return;
      const opened = await overlayInvoke("suggestions_open_venture", { ventureId: item.venture_id, presentationId: bound.presentation_id });
      if (opened === null) throw new Error("Details could not be opened. Try See details again.");
      if (current()) void overlayInvoke("suggestions_overlay_close", { presentationId: bound.presentation_id }).catch(() => {});
    } catch (error) {
      if (current()) setErrors((value) => ({ ...value, [item.id]: error instanceof Error ? error.message : "Try See details again." }));
    }
  }

  async function later() {
    const bound = loadedOwner;
    if (!bound || !sameCustody(owner.current, bound) || !active.current) return;
    snoozeSuggestions(bound.subject);
    setSnoozed(true); setExpanded(false);
    void overlayInvoke("suggestions_overlay_close", { presentationId: bound.presentation_id }).catch(() => {});
  }

  if (snoozed || (loadedOwner?.viewport_width !== undefined && loadedOwner.viewport_width <= 600)) return <div ref={root} />;
  if (items.length === 0) return <div ref={root}><ErrorNotice error={loadError} source="suggestions.overlay.load" />
    {loadError ? <button type="button" onClick={() => void load()}>Retry</button> : null}</div>;
  return (
    <div ref={root} className={styles.overlay}>
      <ErrorNotice error={loadError} source="suggestions.overlay.load" />
      {loadError ? <button type="button" onClick={() => void load()}>Retry</button> : null}
      <button type="button" className={styles.pill} aria-expanded={expanded} aria-controls="suggestion-offer"
        data-usage-control="suggestions_expand" onClick={() => setExpanded((value) => !value)}>
        {items.length} suggestion{items.length === 1 ? "" : "s"}
      </button>
      {visible ? <section id="suggestion-offer" aria-label="Supymem suggestion" className={styles.offer}>
        <div className={styles.overlayHead}>
          <button type="button" data-usage-control="suggestions_pause_today" onClick={() => void overlayInvoke("suggestions_pause_today").catch(() => setLoadError("Pause could not be confirmed. Try Pause for today again or choose Later."))}>Pause for today</button>
          <button type="button" data-usage-control="suggestions_later" onClick={() => void later()}>Later</button>
          <button type="button" aria-label="Collapse suggestions" data-usage-control="suggestions_collapse" onClick={collapse}>Close</button>
        </div>
        <SuggestionCard key={visible.id} item={visible} busy={busy[visible.id]} error={errors[visible.id]}
          pendingDecision={pendingDecision[visible.id]} confirmedDecision={confirmed[visible.id]}
          onRetry={() => { if (pendingDecision[visible.id]) void decide(visible.id, pendingDecision[visible.id]); else void details(visible); }}
          onDecide={(id, decision) => void decide(id, decision)} onDetails={(item) => void details(item)} showVenture />
      </section> : null}
    </div>
  );
}
