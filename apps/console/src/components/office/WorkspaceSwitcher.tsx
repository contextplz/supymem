"use client";

import Link from "next/link";
import { NewVentureLink } from "@/components/chat/NewVentureLink";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import { api, apiSwr, verifiedSessionSubject } from "@/lib/client";
import { cacheGet } from "@/lib/apiCache";
import { displayName } from "@/lib/format";
import { renameVenture, VENTURE_NAME_MAX_CHARS } from "@/lib/ventures";
import { startVentureNameRecovery } from "@/lib/ventureNameRecovery";
import { workspaceSwitcherCopy } from "@/lib/workspaceSwitcherCopy";
import { cx } from "@/lib/cx";
import { FaceMark, tileColor } from "@/components/ventures/venturePortrait";
import classicStyles from "./office.module.css";
import lightStyles from "./WorkspaceSwitcher.module.css";

type Venture = {
  id: string;
  name: string;
  raw_idea?: string;
  stage?: string;
};

export function WorkspaceSwitcher({
  currentId,
  onRenamed,
  onResolved,
  children,
  presentation = "classic",
}: {
  currentId?: string;
  onRenamed?: (name: string) => void;
  onResolved?: (name: string) => void;
  children?: ReactNode;
  presentation?: "classic" | "portrait";
}) {
  const light = useContext(FounderUIContext) === "home" && presentation === "classic";
  const styles = light ? lightStyles : classicStyles;
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [ventures, setVentures] = useState<Venture[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [resolved, setResolved] = useState<{ id: string; name: string } | null>(() => {
    const cached = currentId ? (cacheGet(verifiedSessionSubject(), `/api/ventures/${currentId}`)?.data as Venture | undefined) : undefined;
    return cached?.id === currentId && cached?.name ? { id: cached.id, name: cached.name } : null;
  });
  const currentTitle = resolved && resolved.id === currentId ? resolved.name : "";
  const [listError, setListError] = useState("");
  const [renameError, setRenameError] = useState("");
  useEffect(() => { onResolved?.(currentTitle); }, [currentTitle, onResolved]);
  const alive = useRef(true);
  const renamePending = useRef(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    alive.current = true;
    const recovery = currentId ? startVentureNameRecovery(currentId, {
      read: apiSwr, scope: verifiedSessionSubject,
      accept: name => setResolved({ id: currentId, name }),
    }) : null;
    function handleRenamed(e: Event) {
      const detail = (e as CustomEvent<{ ventureId?: string; name?: string }>).detail;
      if (detail?.ventureId !== currentId || !detail.name?.trim() || !currentId) return;
      recovery?.stop(); // the founder's saved rename wins over an older GET
      setResolved({ id: currentId, name: detail.name.trim() });
      onRenamed?.(detail.name.trim());
    }
    const wake = () => { if (!document.hidden) void recovery?.refresh(); };
    window.addEventListener("supymem:venture-renamed", handleRenamed);
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    void recovery?.refresh();
    return () => {
      recovery?.stop(); alive.current = false;
      window.removeEventListener("supymem:venture-renamed", handleRenamed);
      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [currentId, onRenamed]);

  useEffect(() => {
    if (!open) return;
    void loadVentures();
  }, [open]);

  async function loadVentures() {
    setListError("");
    await apiSwr("/api/ventures", (r) => {
      if (!alive.current) return;
      if (r.ok) setVentures((r.data as { items?: Venture[] }).items || []);
      else setListError("Couldn’t load your ventures.");
      setLoaded(true);
    });
  }

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  useEffect(() => {
    if (!editing) setDraft(currentTitle || "");
  }, [currentTitle, editing]);

  async function saveName(e?: FormEvent) {
    e?.preventDefault();
    if (renamePending.current || !currentTitle) return;
    const next = draft.trim();
    if (!currentId || next.length < 2 || next === (currentTitle || "").trim()) {
      setEditing(false);
      setDraft(currentTitle || "");
      return;
    }
    renamePending.current = true;
    setSaving(true);
    setRenameError("");
    const r = await renameVenture(currentId, next);
    renamePending.current = false;
    if (!alive.current) return;
    setSaving(false);
    if (r.ok) {
      const saved = ((r.data as { name?: string })?.name || next).trim();
      setResolved({ id: currentId, name: saved });
      onRenamed?.(saved);
      window.dispatchEvent(new CustomEvent("supymem:venture-renamed", { detail: { ventureId: currentId, name: saved } }));
      setEditing(false);
    } else setRenameError("Couldn’t save the venture name. Try again.");
  }

  const labelCopy = workspaceSwitcherCopy(currentId, currentTitle);
  const label = labelCopy.label;

  return (
    <div ref={rootRef} className={cx(styles.switcherRoot, presentation === "portrait" && styles.portraitRoot)}>
      {editing && currentId ? (
        <form className={styles.switcher} onSubmit={(e) => void saveName(e)}>
          <span className={styles.switcherMark} aria-hidden>
            {(draft || label).slice(0, 1).toUpperCase()}
          </span>
          <span className={styles.switcherCopy}>
            <span className={styles.switcherEyebrow}>Venture name</span>
            <input
              className={styles.switcherNameInput}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => void saveName()}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();
                  setDraft(currentTitle || "");
                  setEditing(false);
                  setRenameError("");
                }
              }}
              maxLength={VENTURE_NAME_MAX_CHARS}
              autoFocus
              disabled={saving}
              aria-label="Venture name"
            />
            <ErrorNotice error={renameError} source="WorkspaceSwitcher.renameError" as="span" className={styles.switcherMeta} />
          </span>
        </form>
      ) : (
        <>
      <div className={styles.switcherRow}>
      <button
        type="button"
        className={styles.switcher}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={`Switch venture: ${label}`}
        title={`Switch venture: ${label}`}
        onClick={() => setOpen((v) => !v)}
      >
        {presentation === "portrait" ? <span className={styles.portraitTile} style={{ background: tileColor(currentId || label) }}><FaceMark seed={currentId || label} /></span> : <span className={styles.switcherMark} aria-hidden>
          {label.slice(0, 1).toUpperCase()}
        </span>}
        <span className={styles.switcherCopy}>
          <span className={styles.switcherEyebrow}>{labelCopy.eyebrow}</span>
          <span className={styles.switcherName} title={label}>{label}</span>
        </span>
        <svg className={styles.switcherChevron} width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="m4 6 4 4 4-4" />
        </svg>
      </button>
      {currentId ? (
        <button
          type="button"
          className={styles.switcherRename}
          aria-label="Rename venture"
          title="Rename venture"
          disabled={!currentTitle}
          onClick={() => {
            setOpen(false);
            setEditing(true);
          }}
        >
          {light ? "Edit" : <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="m16 3 5 5-12 12-6 1 1-6zM14 5l5 5" />
          </svg>}
        </button>
      ) : null}
      {children}
      </div>
      {open && (
        <div className={styles.switcherMenu} role="listbox" onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(false);
            rootRef.current?.querySelector<HTMLButtonElement>('button[aria-haspopup="listbox"]')?.focus();
          }
        }}>
          <NewVentureLink className={styles.switcherCreate} onClick={() => setOpen(false)}>
            + New venture
          </NewVentureLink>
          <Link href="/" className={styles.switcherItem} onClick={() => setOpen(false)}>
            All ventures
            <span className={styles.switcherMeta}>Switch venture</span>
          </Link>
          <div className={styles.switcherList}>
            {listError && <>
              <ErrorNotice error={listError} source="WorkspaceSwitcher.listError" as="span" className={styles.switcherMeta} />
              <button type="button" className={styles.switcherItem} onClick={() => void loadVentures()}>Try again</button>
            </>}
            {!loaded && ventures.length === 0 ? (
              <span className={cx(styles.switcherItem, styles.switcherMeta)} data-not-error>Loading ventures…</span>
            ) : null}
            {ventures.map((venture) => {
              const name = displayName(venture.name, venture.raw_idea);
              return (
                <Link
                  key={venture.id}
                  href={`/v/${venture.id}`}
                  className={cx(styles.switcherItem, venture.id === currentId && styles.switcherItemActive)}
                  data-usage-control="venture_switch"
                  onClick={() => setOpen(false)}
                >
                  {name}
                  <span className={styles.switcherMeta}>Open venture</span>
                </Link>
              );
            })}
          </div>
        </div>
      )}
        </>
      )}
    </div>
  );
}
