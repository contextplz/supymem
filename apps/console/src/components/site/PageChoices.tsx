"use client";

import Link from "@/components/chat/ChatLink";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";
import styles from "./PageChoices.module.css";

/** Both routes remain available; the recommendation is only a small text label. */
export function PageChoices({ ventureId, onBuild, disabled = false }: { ventureId: string; onBuild?: () => void; disabled?: boolean }) {
  const pathname = usePathname(), owner = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
  const sourceHref = typeof window === "undefined" ? pathname : window.location.href;
  const scope = useRef({ ventureId, owner, epoch, pathname, sourceHref, active: true });
  if (scope.current.ventureId !== ventureId || scope.current.owner !== owner || scope.current.epoch !== epoch || scope.current.pathname !== pathname || scope.current.sourceHref !== sourceHref)
    scope.current = { ventureId, owner, epoch, pathname, sourceHref, active: true };
  const renderScope = scope.current;
  const unavailable = useRef(disabled); unavailable.current = disabled;
  const current = () => renderScope.active && scope.current === renderScope && owner === verifiedSessionSubject()
    && epoch === verifiedSessionEpoch() && pathname === window.location.pathname && sourceHref === window.location.href;
  useEffect(() => {
    renderScope.active = true;
    return () => { renderScope.active = false; };
  }, [renderScope]);
  function skip() {
    if (!current() || unavailable.current || !owner) return;
    window.dispatchEvent(new CustomEvent("supymem:ask", { detail: { action: "journey.skip_step",
      payload: { step: "landing_page", why: "I want to find customers before building a landing page." } } }));
  }
  return <div className={styles.choices} aria-label="Landing page or customers">
    <div className={styles.option}>
      <span className={styles.recommendation}>Recommended</span>
      {onBuild ? <button type="button" className="primary" disabled={disabled} onClick={() => {
        if (current() && !unavailable.current) onBuild();
      }}
        data-usage-control="page_choice_build">Build the landing page and explore customers</button>
        : <Link href={`/v/${encodeURIComponent(ventureId)}/site?build=1`} className="primary"
          aria-disabled={disabled} onClick={event => { if (!current() || unavailable.current) event.preventDefault(); }}
          data-usage-control="page_choice_build">Build the landing page and explore customers</Link>}
    </div>
    <div className={styles.option}>
      <span className={styles.recommendation} aria-hidden="true">&nbsp;</span>
      <button type="button" className="ghost sm" disabled={disabled} onClick={skip}
        data-usage-control="page_choice_skip">Skip this step</button>
      <p className="muted">Find customers with saved context, without a new page. Confirm in chat.</p>
    </div>
  </div>;
}
