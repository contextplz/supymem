"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/client";
import styles from "./SiteAddress.module.css";

export type AddressCheck = {
  available: boolean; normalized: string; reason: string | null; current?: string | null; message?: string | null; suggestion?: string | null;
};
export type AddressStatus = { tone: "idle" | "checking" | "ok" | "bad"; text: string; suggestion?: string; taken?: boolean };

export const DEBOUNCE_MS = 400;
const PLAIN: Record<string, string> = {
  taken: "Taken",
  reserved: "That address is kept for Supymem itself",
  invalid: "Use only letters, numbers and single dashes, starting and ending with a letter or number",
  too_short: "Use at least 3 characters",
  too_long: "Use at most 48 characters",
};

/** Plain words for one server answer. */
export function describeCheck(check: AddressCheck): AddressStatus {
  if (check.available) return { tone: "ok", text: "✓ Available" };
  const reason = check.reason ?? "invalid";
  return { tone: "bad", text: PLAIN[reason] ?? PLAIN.invalid, suggestion: check.suggestion || undefined, taken: reason === "taken" };
}

type Props = {
  ventureId: string;
  /** Called with the checked address when it is free, else null. */
  onReady: (slug: string | null, current: string | null) => void;
  disabled?: boolean;
  label?: string;
  /** Which app the address belongs to: the first-look concept page (default) or the page builder's page. */
  target?: "pagebuilder";
};

/** `[ prefix ].supymem.com` with a debounced availability check as the founder types. */
export function SiteAddress({ ventureId, onReady, disabled, label = "Your landing page address", target }: Props) {
  const [value, setValue] = useState("");
  const [status, setStatus] = useState<AddressStatus>({ tone: "idle", text: "" });
  const serial = useRef(0);
  const timer = useRef<number | null>(null);
  const loaded = useRef(false);
  const base = `/api/ventures/${encodeURIComponent(ventureId)}/site-address/check`;

  async function run(text: string, prefill: boolean) {
    const mine = ++serial.current;
    setStatus({ tone: "checking", text: "Checking…" });
    onReady(null, null);
    const result = await api(`${base}?slug=${encodeURIComponent(text)}${target ? `&target=${target}` : ""}`);
    if (mine !== serial.current) return; // a newer keystroke owns the answer
    if (!result.ok) {
      setStatus({ tone: "bad", text: "Could not check this address right now. Try again in a moment." });
      return;
    }
    const check = result.data as AddressCheck;
    if (prefill) {
      const start = check.current || check.suggestion || "";
      setValue(start);
      if (start) { void run(start, false); return; }
    }
    setStatus(describeCheck(check));
    onReady(check.available ? check.normalized : null, check.current ?? null);
  }

  useEffect(() => {
    if (loaded.current) return;
    loaded.current = true;
    void run("", true);
    return () => { serial.current++; if (timer.current !== null) window.clearTimeout(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ventureId]);

  function change(text: string) {
    setValue(text);
    if (timer.current !== null) window.clearTimeout(timer.current);
    serial.current++;
    onReady(null, null);
    if (!text.trim()) { setStatus({ tone: "idle", text: "" }); return; }
    setStatus({ tone: "checking", text: "Checking…" });
    timer.current = window.setTimeout(() => { void run(text, false); }, DEBOUNCE_MS);
  }

  function useSuggestion(slug: string) {
    if (timer.current !== null) window.clearTimeout(timer.current);
    setValue(slug);
    void run(slug, false);
  }

  return <div className={styles.field}>
    <label htmlFor="site-address-input" className={styles.label}>{label}</label>
    <div className={styles.row}>
      <input id="site-address-input" className={styles.input} type="text" inputMode="url" autoComplete="off" autoCapitalize="none"
        spellCheck={false} maxLength={60} value={value} disabled={disabled} aria-describedby="site-address-status"
        aria-invalid={status.tone === "bad"} onChange={event => change(event.target.value)} />
      <span className={styles.suffix} aria-hidden="true">.supymem.com</span>
    </div>
    <p id="site-address-status" className={styles.status} data-tone={status.tone} role="status" aria-live="polite">
      {status.tone === "bad" && status.suggestion
        ? <>{status.text}{status.taken ? ", try " : ". Try "}
          <button type="button" data-usage-control="website_address_suggestion" className={styles.suggest} disabled={disabled}
            onClick={() => useSuggestion(status.suggestion!)}>{status.suggestion}</button></>
        : status.text}
    </p>
  </div>;
}
