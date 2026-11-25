"use client";

import { useRef, useState } from "react";
import styles from "./Outreach.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type ActionResult<T> = { ok: true; value: T } | { ok: false };

export function useOutreachAction() {
  const inFlight = useRef(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run<T>(action: () => T | Promise<T>): Promise<ActionResult<T>> {
    // Guard synchronously: a second click can arrive before React renders disabled controls.
    if (inFlight.current) return { ok: false };
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      return { ok: true, value: await action() };
    } catch (cause) {
      setError(cause instanceof Error && cause.message ? cause.message : "The action failed. Please try again.");
      return { ok: false };
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  return { pending, error, run };
}

export function ActionFeedback({ pending, error }: { pending: boolean; error: string | null }) {
  return (
    <>
      {pending && <p className={styles.muted} role="status">Working…</p>}
      <ErrorNotice error={error} source="useOutreachAction.error" className={styles.err} />
    </>
  );
}
