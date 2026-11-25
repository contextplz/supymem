"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { NewVentureLink } from "@/components/chat/NewVentureLink";
import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import { renameVenture, VENTURE_NAME_MAX_CHARS } from "@/lib/ventures";
import { CARD_ART, cardImage, choiceFromVenture, saveCardChoice, type CardChoice } from "./ventureCards";
import homeStyles from "./VentureHubHome.module.css";
import { api, apiSwr, ensureSession, verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import { loadGreeting } from "@/lib/charlieGreeting";
import { isDesktopShell } from "@/lib/desktop";
import { USAGE_CONTROL_EVENT } from "@/lib/usageControl";
import { splitEntryHistory, unsentEntryDraftHref, type EntryHistoryItem } from "@/lib/chatEntry";
import { draftStorage } from "@/lib/chatComposer";
import { displayName } from "@/lib/format";

import { DeleteVentureDialog } from "./DeleteVentureDialog";
import { VentureCard } from "./VentureCard";
import classicStyles from "./VentureHub.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { FIRST_RUN_CTA, FIRST_RUN_VALUE_PROP } from "@/lib/betaCopy";

export type VentureStatus = {
  code?: string;
  label?: string;
  detail?: string;
};

export type VentureAgents = {
  hired?: number;
  working?: number;
  watching?: number;
};

export type VentureStopped = {
  onboarding?: boolean;
  agents?: number;
  tasks?: number;
};

export type HubVenture = {
  id: string;
  name: string;
  raw_idea?: string;
  stage: string;
  status?: VentureStatus;
  created_at?: string;
  updated_at?: string;
  agents?: VentureAgents;
  paused?: boolean;
  paused_reason?: string | null;
  paused_at?: string | null;
  stopped?: VentureStopped;
  card_type?: string | null;
  card_color?: string | null;
  card_art?: string | null;
  days_left?: number;
};

type PauseResponse = {
  id?: string;
  name?: string;
  paused?: boolean;
  paused_at?: string | null;
  paused_reason?: string | null;
  status?: VentureStatus;
  stopped?: VentureStopped;
};

type ResumeResponse = {
  id?: string;
  name?: string;
  paused?: boolean;
  resumed_at?: string;
  status?: VentureStatus;
  resumed?: { onboarding?: boolean };
};

export type UndoBanner = {
  id: string;
  name: string;
  retentionDays: number;
};

function asItems(data: unknown): HubVenture[] {
  if (!data || typeof data !== "object") return [];
  const items = (data as { items?: unknown }).items;
  return Array.isArray(items) ? (items as HubVenture[]) : [];
}

function errorMessage(_data: unknown, fallback: string): string {
  return fallback;
}

function daysLeftLabel(daysLeft: number | undefined): string {
  if (typeof daysLeft !== "number" || Number.isNaN(daysLeft)) return "recoverable";
  if (daysLeft === 1) return "1 day left to restore";
  return `${daysLeft} days left to restore`;
}

export function VentureHub({ local = false }: { local?: boolean } = {}) {
  const home = useContext(FounderUIContext) === "home";
  const styles = home ? homeStyles : classicStyles;
  const [managing, setManaging] = useState(false);
  const router = useRouter();
  const [ventures, setVentures] = useState<HubVenture[]>([]);
  const [deleted, setDeleted] = useState<HubVenture[]>([]);
  const [retentionDays, setRetentionDays] = useState(7);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [cardErrors, setCardErrors] = useState<Record<string, string>>({});
  const [deleting, setDeleting] = useState<HubVenture | null>(null);
  const [undo, setUndo] = useState<UndoBanner | null>(null);
  const [undoError, setUndoError] = useState("");
  const [undoBusy, setUndoBusy] = useState(false);
  const [restoreBusy, setRestoreBusy] = useState<string | null>(null);
  const [trashError, setTrashError] = useState("");
  const [entryDraftHref, setEntryDraftHref] = useState<string | null>(null);
  const [manageTarget, setManageTarget] = useState<HubVenture | null>(null);
  const manageDialog = useRef<HTMLDialogElement>(null);
  const resetScroll = useCallback((page: HTMLElement | null) => {
    if (page) page.scrollTop = 0;
  }, []);
  const managedVenture = ventures.find((venture) => venture.id === manageTarget?.id) ?? manageTarget;
  const managedPaused = managedVenture?.paused ?? (managedVenture?.status?.code === "paused" || managedVenture?.stage === "paused");
  const [cards, setCards] = useState<Record<string, CardChoice>>({});
  const [draftName, setDraftName] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const cardFlight = useRef(false);
  const [listsReady, setListsReady] = useState(false);

  const load = useCallback(async (quiet = false) => {
    setListsReady(false);
    if (!quiet) { setError(""); setTrashError(""); }
    let activeReady = false, deletedReady = false;
    try {
      await ensureSession();
      // Cached lists paint at once (no loader) and refresh in place; both requests run in parallel.
      await Promise.all([
        apiSwr("/api/ventures?summary=true", (active) => {
          activeReady = active.ok;
          if (!active.ok) {
            setError("Couldn’t load your ventures.");
          } else {
            setError("");
            setVentures(asItems(active.data));
          }
          setLoading(false);
        }),
        apiSwr("/api/ventures/deleted?summary=true", (trash) => {
          deletedReady = trash.ok;
          if (!trash.ok) {
            setTrashError("Couldn’t load recently deleted ventures. Try again.");
            return;
          }
          setTrashError("");
          const payload = trash.data as { items?: HubVenture[]; retention_days?: number };
          setDeleted(asItems(payload));
          if (typeof payload.retention_days === "number") {
            setRetentionDays(payload.retention_days);
          }
        }),
      ]);
      setListsReady(activeReady && deletedReady);
    } catch {
      setError("Couldn’t load your ventures. Try again.");
    } finally {
      setLoading(false);
    }
  }, []);



  async function changeCard(venture: HubVenture, choice: CardChoice) {
    if (cardFlight.current) return;
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    cardFlight.current = true;
    setBusy(venture.id, true);
    setCardErrors(value => ({ ...value, [venture.id]: "" }));
    try {
      const result = await api(`/api/ventures/${encodeURIComponent(venture.id)}/card`, {
        method: "PATCH", body: JSON.stringify({ card_type: choice.type, card_color: choice.color, card_art: choice.art }),
      });
      if (!current()) return;
      if (!result.ok) throw new Error();
      saveCardChoice(venture.id, choice);
      setCards(value => ({ ...value, [venture.id]: choice }));
      setVentures(value => value.map(item => item.id === venture.id ? { ...item, card_type: choice.type, card_color: choice.color, card_art: choice.art } : item));
    } catch {
      if (current()) setCardErrors(value => ({ ...value, [venture.id]: "Couldn’t save this card. Your previous card is unchanged. Try again." }));
    } finally {
      cardFlight.current = false;
      if (current()) setBusy(venture.id, false);
    }
  }

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (loading || !listsReady || error || ventures.length || deleted.length) return;
    const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const href = unsentEntryDraftHref(draftStorage(), subject) || "/new";
    if (!isDesktopShell()) { router.replace(href); return; }
    if (!subject) return;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
    let redirected = false;
    const redirect = () => { if (current() && !redirected) { redirected = true; router.replace(href); } };
    const dismissed = (event: Event) => {
      const control = (event as CustomEvent<{ controlId?: string }>).detail?.controlId;
      if (control === "greeting_completed" || control === "greeting_skipped") redirect();
    };
    window.addEventListener(USAGE_CONTROL_EVENT, dismissed);
    void loadGreeting(() => api("/api/me/greeting", { signal: controller.signal }), controller.signal, () => {}).then(greeting => {
      // A greeting that failed to load must not leave a first-time founder on an empty home.
      if (!greeting || greeting.greetedAt) redirect();
    });
    return () => { controller.abort(); window.removeEventListener(USAGE_CONTROL_EVENT, dismissed); };
  }, [loading, listsReady, error, ventures.length, deleted.length, router]);

  useEffect(() => {
    let live = true;
    void (async () => {
      await ensureSession();
      const subject = verifiedSessionSubject();
      if (!live || !subject) return;
      const unsent = unsentEntryDraftHref(draftStorage(), subject);
      if (unsent) { setEntryDraftHref(unsent); return; }
      const result = await api("/api/entry/chat/history");
      if (!live || verifiedSessionSubject() !== subject || !result.ok) return;
      const items = (result.data as {items?: EntryHistoryItem[]} | null)?.items;
      if (Array.isArray(items) && splitEntryHistory(items).fresh.some(item =>
        item.proposal.state === "pending" || item.proposal.state === "clarify")) setEntryDraftHref("/new");
    })().catch(() => { /* Venture lists remain usable if entry history is offline. */ });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!manageTarget) return;
    const dialog = manageDialog.current;
    if (!dialog) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialog.showModal();
    return () => {
      dialog.close();
      previousFocus?.focus();
    };
  }, [manageTarget]);

  useEffect(() => {
    if (ventures.length === 0) return;
    const next: Record<string, CardChoice> = {};
    for (const venture of ventures) {
      next[venture.id] = choiceFromVenture(
        venture.id,
        displayName(venture.name, venture.raw_idea),
        venture.raw_idea,
        venture.card_type,
        venture.card_color,
        venture.card_art,
      );
    }
    setCards(next);
  }, [ventures]);

  function setBusy(id: string, on: boolean) {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function pauseVenture(venture: HubVenture) {
    const snapshot = venture;
    setCardErrors((prev) => {
      const next = { ...prev };
      delete next[venture.id];
      return next;
    });
    setVentures((list) =>
      list.map((item) =>
        item.id === venture.id ? { ...item, paused: true } : item,
      ),
    );
    setBusy(venture.id, true);
    const result = await api(`/api/ventures/${venture.id}/pause`, {
      method: "POST",
      body: JSON.stringify({ reason: null }),
    });
    setBusy(venture.id, false);
    if (result.ok) {
      const data = (result.data || {}) as PauseResponse;
      setVentures((list) =>
        list.map((item) =>
          item.id === venture.id
            ? {
                ...item,
                paused: data.paused ?? true,
                paused_at: data.paused_at ?? item.paused_at,
                paused_reason: data.paused_reason ?? null,
                status: data.status ?? item.status,
                stopped: data.stopped,
                name: data.name || item.name,
              }
            : item,
        ),
      );
      return true;
    }
    if (result.status === 409) {
      setVentures((list) =>
        list.map((item) =>
          item.id === venture.id ? { ...item, paused: true } : item,
        ),
      );
      return true;
    }
    setVentures((list) =>
      list.map((item) => (item.id === venture.id ? snapshot : item)),
    );
    const message =
      result.status === 404
        ? "Pausing is not available right now. Try again later."
        : errorMessage(result.data, "Couldn’t pause this venture.");
    setCardErrors((prev) => ({ ...prev, [venture.id]: message }));
    return false;
  }

  async function resumeVenture(venture: HubVenture) {
    const snapshot = venture;
    setCardErrors((prev) => {
      const next = { ...prev };
      delete next[venture.id];
      return next;
    });
    setVentures((list) =>
      list.map((item) =>
        item.id === venture.id
          ? {
              ...item,
              paused: false,
              paused_reason: null,
              paused_at: null,
              stopped: undefined,
            }
          : item,
      ),
    );
    setBusy(venture.id, true);
    const result = await api(`/api/ventures/${venture.id}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    setBusy(venture.id, false);
    if (result.ok) {
      const data = (result.data || {}) as ResumeResponse;
      setVentures((list) =>
        list.map((item) =>
          item.id === venture.id
            ? {
                ...item,
                paused: data.paused ?? false,
                paused_at: null,
                paused_reason: null,
                stopped: undefined,
                status: data.status ?? item.status,
                name: data.name || item.name,
              }
            : item,
        ),
      );
      return true;
    }
    if (result.status === 409) {
      setVentures((list) =>
        list.map((item) =>
          item.id === venture.id
            ? { ...item, paused: false, paused_reason: null, stopped: undefined }
            : item,
        ),
      );
      return true;
    }
    setVentures((list) =>
      list.map((item) => (item.id === venture.id ? snapshot : item)),
    );
    const message =
      result.status === 404
        ? "You couldn’t resume this venture. Refresh All ventures to check whether it’s still available."
        : errorMessage(result.data, "You couldn’t resume this venture. Refresh All ventures and try again.");
    setCardErrors((prev) => ({ ...prev, [venture.id]: message }));
    return false;
  }

  async function restoreVenture(id: string, fromUndo = false) {
    if (fromUndo) {
      setUndoBusy(true);
      setUndoError("");
    } else {
      setRestoreBusy(id);
      setTrashError("");
    }
    const result = await api(`/api/ventures/${id}/restore`, { method: "POST" });
    if (fromUndo) setUndoBusy(false);
    else setRestoreBusy(null);
    if (!result.ok) {
      const message = errorMessage(result.data, "Couldn’t restore this venture.");
      if (fromUndo) setUndoError(message);
      else setTrashError(message);
      return;
    }
    setUndo(null);
    setUndoError("");
    await load(true);
  }

  return (
    <main className={styles.page} ref={home ? resetScroll : undefined} tabIndex={-1} aria-labelledby={home ? "venture-picker-title" : undefined}>
      {home && <div className={styles.dragRegion} data-tauri-drag-region aria-hidden="true" />}
      {home && <h1 id="venture-picker-title" className={styles.title}>{ventures.length ? "Which venture?" : "Start with your idea"}</h1>}
      {undo && (
        <div className={styles.banner} role="status" data-not-error>
          <div>
            <p>
              Deleted {undo.name}. Recoverable for {undo.retentionDays} days.
            </p>
            <ErrorNotice error={undoError} source="VentureHub.undoError" className={styles.bannerError} />
          </div>
          <div className={styles.bannerActions}>
            <button
              type="button"
              className={home ? undefined : "primary sm"}
              data-usage-control="venture_restore"
              disabled={undoBusy}
              onClick={() => void restoreVenture(undo.id, true)}
            >
              {undoBusy ? "Restoring…" : "Undo"}
            </button>
            <button
              type="button"
              className={home ? undefined : "ghost sm"}
              onClick={() => {
                setUndo(null);
                setUndoError("");
              }}
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      {loading && (home ? <p className={styles.state} data-not-error>Loading your ventures…</p> : <div className={styles.state} data-not-error>Loading your ventures…</div>)}
      <ErrorNotice error={error} source="VentureHub.error" as={home ? "p" : "div"} className={styles.error} />
      {error && <p><button type="button" className={home ? styles.manage : "primary sm"} onClick={() => void load()}>Try again</button></p>}
      <ErrorNotice error={trashError} source="VentureHub.trashError" className={styles.trashError} />
      {trashError && <button type="button" onClick={() => void load()}>Refresh recently deleted ventures</button>}

      {!loading && !error && ventures.length === 0 && (
        <section className={styles.empty}>
          <p>You have no ventures yet. A venture is the workspace for your idea, customers and progress.</p>
          <p>{FIRST_RUN_VALUE_PROP}</p>
          <NewVentureLink className={local ? classicStyles.newButton : styles.newButton} data-usage-control="onboarding_start">
            {FIRST_RUN_CTA}
            <span aria-hidden>→</span>
          </NewVentureLink>
        </section>
      )}

      {home && !loading && !error && ventures.length > 0 && (
        <ul className={styles.profiles} aria-label="Ventures">
          <li>
            <NewVentureLink className={`${styles.profile} ${styles.add}`} aria-label="Add venture">
              <span className={styles.addCircle}><span className={styles.addGlyph} aria-hidden="true">+</span></span>
              <span className={styles.name}>Add Venture</span>
            </NewVentureLink>
          </li>
          {ventures.map((venture) => {
            const name = displayName(venture.name, venture.raw_idea);
            const choice = cards[venture.id] || choiceFromVenture(venture.id, name, venture.raw_idea, venture.card_type, venture.card_color, venture.card_art);
            const portrait = (
              <span className={styles.avatar}>
                <img className={styles.cardArt} src={cardImage(choice)} alt="" />
                {managing && <span className={styles.editBadge} aria-hidden="true"><span className={styles.editIcon}>✎</span></span>}
              </span>
            );
            return (
              <li key={venture.id}>
                {managing ? (
                  <button type="button" className={`${styles.profile} ${styles.managing}`} aria-label={`Manage ${name}`} onClick={() => { setManageTarget(venture); setDraftName(name); }}>
                    {portrait}<span className={styles.name}>{name}</span>
                  </button>
                ) : (
                  <Link href={`/v/${encodeURIComponent(venture.id)}`} className={styles.profile} aria-label={`Open ${name}`}>
                    {portrait}<span className={styles.name}>{name}</span>
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {home && !loading && !error && ventures.length > 0 && (
        <button type="button" className={`${styles.manage} ${managing ? styles.manageActive : ""}`} aria-expanded={managing} onClick={() => {
          setManaging((value) => !value);
          setManageTarget(null);
        }}>
          {managing ? "Done" : "Manage Ventures"}
        </button>
      )}
      {home && managedVenture && (
        <dialog ref={manageDialog} className={styles.managePanel} aria-modal="true" aria-label={`Manage ${displayName(managedVenture.name, managedVenture.raw_idea)}`} onCancel={() => setManageTarget(null)} onClick={(event) => {
          if (event.target === event.currentTarget) setManageTarget(null);
        }}>
          <div className={styles.manageCard}>
            <h2>{displayName(managedVenture.name, managedVenture.raw_idea)}</h2>
            <p>Pause, resume, open, or delete this venture.</p>
            <label className={styles.field}>Name<input minLength={2} maxLength={VENTURE_NAME_MAX_CHARS} value={draftName} onChange={event => setDraftName(event.target.value)} /></label>
            <button type="button" disabled={renameBusy || draftName.trim().length < 2} onClick={async () => {
              if (renameBusy) return;
              const subject = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
              const current = () => subject === verifiedSessionSubject() && epoch === verifiedSessionEpoch();
              const nextName = draftName.trim();
              setRenameBusy(true);
              try {
                const result = await renameVenture(managedVenture.id, nextName);
                if (!current()) return;
                if (!result.ok) throw new Error();
                const saved = (result.data as {name?: string} | null)?.name;
                if (!saved) throw new Error();
                setDraftName(saved);
                setVentures(value => value.map(item => item.id === managedVenture.id ? { ...item, name: saved } : item));
                setCardErrors(value => ({ ...value, [managedVenture.id]: "" }));
              } catch { if (current()) setCardErrors(value => ({ ...value, [managedVenture.id]: "Couldn’t rename this venture. Try again." })); }
              finally { if (current()) setRenameBusy(false); }
            }}>{renameBusy ? "Saving…" : "Save name"}</button>
            <div className={styles.artPick}>{CARD_ART.map(art => {
              const choice = cards[managedVenture.id] || choiceFromVenture(managedVenture.id, managedVenture.name, managedVenture.raw_idea, managedVenture.card_type, managedVenture.card_color, managedVenture.card_art);
              return <button key={art} type="button" className={styles.art} aria-label={`Card ${art}`} aria-pressed={choice.art === art} disabled={busyIds.has(managedVenture.id)} onClick={() => void changeCard(managedVenture, { ...choice, art })}><img src={cardImage({ ...choice, art })} alt="" /></button>;
            })}</div>
            <div className={styles.manageActions}>
              <Link href={`/v/${encodeURIComponent(managedVenture.id)}`} onClick={() => setManageTarget(null)}>Open</Link>
              <button type="button" disabled={busyIds.has(managedVenture.id)} data-usage-control={managedPaused ? "venture_resume" : "venture_pause"} onClick={async () => {
                const succeeded = await (managedPaused ? resumeVenture(managedVenture) : pauseVenture(managedVenture));
                if (succeeded) setManageTarget(null);
              }}>
                {busyIds.has(managedVenture.id) ? "Working…" : managedPaused ? "Resume" : "Pause"}
              </button>
              <button type="button" className={styles.danger} disabled={busyIds.has(managedVenture.id)} onClick={() => {
                setDeleting(managedVenture);
                setManageTarget(null);
              }}>Delete</button>
              <button type="button" onClick={() => setManageTarget(null)}>Cancel</button>
            </div>
            <ErrorNotice error={cardErrors[managedVenture.id]} source="VentureHub.cardErrors" className={styles.error} style={{ marginTop: 14 }} />
          </div>
        </dialog>
      )}
      {ventures.length > 0 && !home && (
        <section aria-label={`${ventures.length} ${ventures.length === 1 ? "venture" : "ventures"}`}>
          <div className={styles.grid}>
            {ventures.map((venture) => (
              <VentureCard
                key={venture.id}
                venture={venture}
                busy={busyIds.has(venture.id)}
                error={cardErrors[venture.id] || ""}
                onPause={() => void pauseVenture(venture)}
                onResume={() => void resumeVenture(venture)}
                onDelete={() => setDeleting(venture)}
              />
            ))}
            <NewVentureLink className={styles.createCard}>
              <span className={styles.createIcon} aria-hidden>
                ＋
              </span>
              <strong>New venture</strong>
              <small>Start another idea</small>
            </NewVentureLink>
          </div>
        </section>
      )}

      {/* The draft check finishes after the tiles render: the slot is always there, so its arrival moves nothing a founder may be clicking. */}
      <section className={home ? styles.draftBanner : styles.banner} aria-label="Continue where you left off" style={{ visibility: entryDraftHref ? "visible" : "hidden" }} data-not-error>
        <p>Continue where you left off</p>
        <Link href={entryDraftHref || "/new"} className={styles.newButton}>Continue your draft<span aria-hidden>→</span></Link>
      </section>

      {deleted.length > 0 && (
        <details className={styles.trash}>
          <summary>Recently deleted ({deleted.length})</summary>
          <div className={styles.trashList}>
            {deleted.map((venture) => (
              <div className={styles.trashRow} key={venture.id}>
                <div>
                  <strong>{displayName(venture.name, venture.raw_idea)}</strong>
                  <span>{daysLeftLabel(venture.days_left)}</span>
                </div>
                <button
                  type="button"
                  data-usage-control="venture_restore"
                  disabled={restoreBusy === venture.id}
                  onClick={() => void restoreVenture(venture.id)}
                >
                  {restoreBusy === venture.id ? "Restoring…" : "Restore"}
                </button>
              </div>
            ))}
          </div>
        </details>
      )}

      {deleting && (
        <DeleteVentureDialog
          venture={deleting}
          retentionDays={retentionDays}
          onClose={() => setDeleting(null)}
          onDeleted={(info) => {
            setDeleting(null);
            setUndo(info);
            void load(true);
          }}
        />
      )}
    </main>
  );
}
