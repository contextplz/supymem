"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { firstlookPreviewPath, firstlookPreviewSandbox } from "@/lib/firstlookPreview";
import { isLocalConsole } from "@/lib/localMode";
import { openablePreviewUrl, previewWaitlistUrl } from "@/lib/previewNavigation";
import styles from "./LandingPreviewFrame.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export function LandingPreviewFrame({
  src,
  title = "Landing page preview",
  sandbox = "allow-scripts allow-forms allow-same-origin",
  className,
  ventureId,
}: {
  src: string;
  title?: string;
  sandbox?: string;
  className?: string;
  ventureId?: string;
}) {
  const [status, setStatus] = useState<"loading" | "ready" | "slow">("loading");
  const gen = useRef(0);
  const previewPath = firstlookPreviewPath(src, ventureId);
  const [document, setDocument] = useState<{ path: string; html: string; owner: string | null; epoch: number } | null>(null);
  const [sessionEpoch, setSessionEpoch] = useState(verifiedSessionEpoch);
  const [error, setError] = useState<string | null>(null);
  const [navigated, setNavigated] = useState(false);
  const [waitlistUrl, setWaitlistUrl] = useState<string | undefined>();
  const loaded = useRef(false);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const liveUrl = openablePreviewUrl(src);
  const linkOnly = isLocalConsole() && !previewPath;

  useEffect(() => {
    if (linkOnly) return;
    const mine = ++gen.current;
    setStatus("loading");
    setDocument(null);
    setError(null);
    setNavigated(false);
    setWaitlistUrl(undefined);
    loaded.current = false;
    let disposed = false;
    const controller = new AbortController();
    const owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    const current = () => !disposed && !controller.signal.aborted && gen.current === mine && verifiedSessionSubject() === owner && verifiedSessionEpoch() === epoch;
    if (previewPath) {
      void api(previewPath, { signal: controller.signal }).then((response) => {
        if (!current()) return;
        const payload = response.data as { html?: unknown; venture_id?: unknown } | null;
        if (!response.ok || typeof payload?.html !== "string" || (ventureId !== undefined && payload.venture_id !== ventureId)) {
          setError("Your private preview could not be loaded. Reopen your venture page and choose Open preview to try again.");
          return;
        }
        setDocument({ path: previewPath, html: payload.html, owner, epoch });
      }).catch(() => {
        if (current()) setError("Your private preview could not be loaded. Reopen your venture page and choose Open preview to try again.");
      });
    }
    const slow = window.setTimeout(() => {
      if (gen.current === mine) {
        setStatus((prev) => (prev === "ready" ? prev : "slow"));
      }
    }, 8000);
    return () => { disposed = true; controller.abort(); window.clearTimeout(slow); };
  }, [src, previewPath, sessionEpoch, linkOnly]);

  useEffect(() => {
    const changed = () => setSessionEpoch(verifiedSessionEpoch());
    window.addEventListener("supymem-session-changed", changed);
    return () => window.removeEventListener("supymem-session-changed", changed);
  }, []);

  const documentCurrent = document?.path === previewPath && document?.owner === verifiedSessionSubject() && document?.epoch === verifiedSessionEpoch();

  useEffect(() => {
    if (!navigated || !ventureId || previewPath) return;
    let disposed = false;
    void api(`/api/ventures/${encodeURIComponent(ventureId)}/waitlist`).then((response) => {
      if (!disposed && response.ok) setWaitlistUrl(previewWaitlistUrl(response.data, ventureId));
    }).catch(() => { /* The live page link remains available. */ });
    return () => { disposed = true; };
  }, [navigated, ventureId, previewPath]);

  if (linkOnly) return liveUrl ? <a href={liveUrl} target="_blank" rel="noopener noreferrer">Open live landing page in browser</a> : null;

  return (
    <div className={`${styles.wrap}${className ? ` ${className}` : ""}`}>
      {previewPath && <p>Your private preview</p>}
      <ErrorNotice error={error} source="LandingPreviewFrame.error" />
      {navigated && !previewPath && (
        <div className={styles.navigation} role="status">
          <p>This preview followed a link. Some destinations cannot appear inside an embedded preview.</p>
          <div className={styles.navigationActions}>
            {liveUrl && <a href={liveUrl} target="_blank" rel="noopener noreferrer">Open live landing page in browser</a>}
            {waitlistUrl && <a href={waitlistUrl} target="_blank" rel="noopener noreferrer">Open managed waitlist directly</a>}
            <button type="button" onClick={() => {
              setNavigated(false);
              loaded.current = false;
              if (frameRef.current) frameRef.current.src = src;
            }}>Return to preview</button>
          </div>
        </div>
      )}
      {!error && status !== "ready" ? (
        <div className={styles.overlay} aria-live="polite">
          <span className={styles.spin} aria-hidden />
          <p>
            {status === "slow" ? "Still loading the preview…" : "Loading the preview…"}
          </p>
        </div>
      ) : null}
      {(!previewPath || documentCurrent) && <iframe
        ref={frameRef}
        className={styles.frame}
        title={title}
        src={previewPath ? undefined : src}
        srcDoc={previewPath && documentCurrent ? document?.html : undefined}
        sandbox={firstlookPreviewSandbox(previewPath, sandbox)}
        onLoad={() => {
          // A blocked destination still fires load; the parent cannot inspect a cross-origin frame.
          if (!previewPath && loaded.current) setNavigated(true);
          loaded.current = true;
          const mine = gen.current;
          window.setTimeout(() => {
            if (gen.current === mine) setStatus("ready");
          }, 700);
        }}
      />}
    </div>
  );
}
