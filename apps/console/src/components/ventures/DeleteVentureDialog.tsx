"use client";

import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { api } from "@/lib/client";
import { displayName } from "@/lib/format";
import type { HubVenture, UndoBanner } from "./VentureHub";
import styles from "./DeleteVentureDialog.module.css";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const FALLBACK_PHRASE = "DELETE";
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function phraseMatches(typed: string, expected: string): boolean {
  return typed.trim().toUpperCase() === expected.trim().toUpperCase();
}

function errorCode(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  const root = data as {
    error?: { code?: string };
    detail?: { error?: { code?: string } } | Array<{ type?: string }>;
  };
  if (root.error?.code) return root.error.code;
  if (root.detail && typeof root.detail === "object" && !Array.isArray(root.detail)) {
    return root.detail.error?.code || "";
  }
  return "";
}

function errorMessage(data: unknown, fallback: string): string {
  if (!data || typeof data !== "object") return fallback;
  const root = data as {
    error?: { message?: string } | string;
    detail?: { error?: { message?: string }; message?: string } | string;
    message?: string;
  };
  if (typeof root.error === "object" && root.error?.message) return root.error.message;
  if (typeof root.detail === "object" && !Array.isArray(root.detail)) {
    if (root.detail.error?.message) return root.detail.error.message;
    if (root.detail.message) return root.detail.message;
  }
  if (typeof root.detail === "string" && root.detail) return root.detail;
  if (root.message) return root.message;
  return fallback;
}

function teamLoss(venture: HubVenture): string {
  const hired = venture.agents?.hired;
  if (typeof hired === "number" && hired > 0) {
    return `The team of ${hired} ${hired === 1 ? "agent" : "agents"}`;
  }
  return "The team (none hired yet)";
}

export function DeleteVentureDialog({
  venture,
  retentionDays,
  onClose,
  onDeleted,
}: {
  venture: HubVenture;
  retentionDays: number;
  onClose: () => void;
  onDeleted: (info: UndoBanner) => void;
}) {
  const local = useLocalMode();
  const titleId = useId();
  const descId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const phraseRef = useRef<HTMLInputElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  const shortName = displayName(venture.name, venture.raw_idea);

  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [phraseInput, setPhraseInput] = useState("");
  const [phrase, setPhrase] = useState(FALLBACK_PHRASE);
  const [days, setDays] = useState(retentionDays);
  const [mismatch, setMismatch] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [closing, setClosing] = useState(false);
  const afterClose = useRef<(() => void) | null>(null);

  function dismiss(callback = onClose) {
    if (afterClose.current) return;
    afterClose.current = callback;
    setClosing(true);
  }

  useEffect(() => {
    if (!closing) return;
    const timer = setTimeout(() => {
      const callback = afterClose.current;
      afterClose.current = null;
      callback?.();
    }, 280);
    return () => clearTimeout(timer);
  }, [closing]);

  const phraseOk = phraseMatches(phraseInput, phrase);

  useEffect(() => {
    previousFocus.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    if (previousFocus.current && dialogRef.current) {
      const source = previousFocus.current.getBoundingClientRect();
      const dialog = dialogRef.current.getBoundingClientRect();
      dialogRef.current.style.setProperty("--dialog-source-x", `${source.left + source.width / 2 - dialog.left}px`);
      dialogRef.current.style.setProperty("--dialog-source-y", `${source.top + source.height / 2 - dialog.top}px`);
    }

    void (async () => {
      const result = await api("/api/ventures/delete-phrase");
      if (!result.ok) return;
      const data = result.data as {
        phrase?: string;
        retention_days?: number;
      };
      if (data.phrase) setPhrase(data.phrase);
      if (typeof data.retention_days === "number") setDays(data.retention_days);
    })();

    return () => {
      document.body.style.overflow = prevOverflow;
      previousFocus.current?.focus?.();
    };
  }, []);

  useEffect(() => {
    if (step === 2) phraseRef.current?.focus();
  }, [step]);

  function focusables(): HTMLElement[] {
    const root = dialogRef.current;
    if (!root) return [];
    return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (el) => !el.hasAttribute("disabled") && el.getAttribute("aria-hidden") !== "true",
    );
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      dismiss();
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = focusables();
    if (nodes.length === 0) {
      event.preventDefault();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function onBackdrop(event: MouseEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget) return;
    if (step === 1) dismiss();
  }

  async function submitDelete() {
    if (!phraseOk || submitting) return;
    setSubmitting(true);
    setMismatch("");
    const result = await api(`/api/ventures/${venture.id}/delete`, {
      method: "POST",
      body: JSON.stringify({ confirmation: phrase }),
    });
    setSubmitting(false);
    if (result.ok) {
      const receipt = {
        id: venture.id,
        name: shortName,
        retentionDays: days,
      };
      dismiss(() => onDeleted(receipt));
      return;
    }
    const code = errorCode(result.data);
    if (result.status === 422 || code === "confirmation_mismatch") {
      setMismatch(
        errorMessage(result.data, "Type DELETE to confirm."),
      );
      setStep(2);
      return;
    }
    setMismatch(errorMessage(result.data, "Couldn’t delete this venture. Try again."));
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (step === 1) {
      setStep(2);
      return;
    }
    if (step === 2) {
      if (phraseOk) setStep(3);
      return;
    }
    void submitDelete();
  }

  return (
    <div className={styles.overlay} data-closing={closing} inert={closing} onMouseDown={onBackdrop}
      onTransitionEnd={(event) => {
        if (event.target !== event.currentTarget || event.propertyName !== "opacity" || !afterClose.current) return;
        const callback = afterClose.current;
        afterClose.current = null;
        callback();
      }}>
      <div
        ref={dialogRef}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <p className={styles.step} aria-live="polite">
          Step {step} of 3
        </p>

        {step === 1 && (
          <>
            <h2 id={titleId}>Delete {shortName}?</h2>
            <p id={descId} className={styles.lead}>
              {local ? "You’ll move this venture to Trash and hide it from your list right away."
                : "You are about to remove this venture from your venture list. You will lose access to:"}
            </p>
            {!local && <ul className={styles.loss}>
              <li>{teamLoss(venture)}</li>
              <li>The research dossier</li>
              <li>The landing page and its URL</li>
              <li>Chat history</li>
              <li>Tasks</li>
              <li>The cost ledger</li>
            </ul>}
            <p className={styles.retain}>
              To undo, open All ventures and choose Restore under Recently deleted within {days} days.
              {!local && " This does not delete independent backups or take down external pages."}
            </p>
            {local && <>
              <p className={styles.retain}>
                After the restore window, cleanup removes this venture’s chat history, setup documents and saved proposals.
                Past versions of your idea and other saved records stay on this computer.
              </p>
              <p className={styles.retain}>
                To clear your local app data, open Settings &gt; Keys and connections. To keep a copy, choose Export my data under Your data and recovery.
                Find the folder path under Your data folder and backup. Quit the app, then delete that folder.
                This clears local data for all your ventures, not just this one. Exports and backups are separate copies.
                Delete any copies you don’t want to keep and take down published pages with your hosting provider.
              </p>
            </>}
          </>
        )}

        {step === 2 && (
          <>
            <h2 id={titleId}>Type DELETE</h2>
            <p id={descId} className={styles.lead}>
              Type the word below to continue. Capitalization does not matter.
            </p>
            <code className={styles.quote}>{phrase}</code>
          </>
        )}

        {step === 3 && (
          <>
            <h2 id={titleId}>Confirm deletion</h2>
            <p id={descId} className={styles.lead}>
              {shortName} will leave your list now. You can restore it for {days} days.
            </p>
          </>
        )}

        <form onSubmit={onSubmit}>
          {step === 2 && (
            <label className={styles.field}>
              <span className={styles.fieldLabel}>Confirmation</span>
              <input
                ref={phraseRef}
                value={phraseInput}
                onChange={(event) => {
                  setPhraseInput(event.target.value);
                  if (mismatch) setMismatch("");
                }}
                autoComplete="off"
                spellCheck={false}
                placeholder="DELETE"
                aria-invalid={mismatch ? true : undefined}
                aria-describedby={mismatch ? `${titleId}-mismatch` : undefined}
                aria-label="Type DELETE to continue"
              />
            </label>
          )}

          <ErrorNotice error={mismatch} id={`${titleId}-mismatch`} className={styles.mismatch} source="deleteventure.confirm" intent="venture_delete" />

          <div className={styles.actions}>
            {step === 1 ? (
              <button type="button" className="ghost" onClick={() => dismiss()}>
                Cancel
              </button>
            ) : (
              <button
                type="button"
                className="ghost"
                onClick={() => {
                  setMismatch("");
                  setStep(step === 3 ? 2 : 1);
                }}
              >
                Back
              </button>
            )}
            {step < 3 ? (
              <button
                type="submit"
                className="primary"
                disabled={step === 2 && !phraseOk}
              >
                Continue
              </button>
            ) : (
              <button
                type="submit"
                className={`danger ${styles.deleteSubmit}`}
                data-usage-control="venture_delete"
                disabled={submitting}
              >
                {submitting ? "Deleting…" : "Delete venture"}
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
