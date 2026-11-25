"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import styles from "../plan/plan.module.css";

type Overview = {
  goal?: string;
  status?: string;
  achieved_on?: string | null;
  today?: { visits?: number; signups?: number; revenue_minor?: number | null };
  days?: { day?: string; visits?: number; signups?: number; revenue_minor?: number | null }[];
};
type WaitlistStatus = { total: number; enabled: boolean; email_verified: false; note: string };

const funnelLabel: Record<string, string> = {
  landing_view: "Landing-page visits",
  cta_click: "Calls to action clicked",
  signup: "Sign-ups reported by the page",
  first_action: "First actions reported by the product",
  payment: "Payments reported by the page (not confirmed)",
};

export default function AnalyticsPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [funnel, setFunnel] = useState<{ steps?: string[]; counts?: Record<string, number> } | null>(null);
  const [waitlist, setWaitlist] = useState<WaitlistStatus | null>(null);
  const [waitlistError, setWaitlistError] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    setOverview(null); setFunnel(null); setWaitlist(null); setWaitlistError(""); setError("");
    (async () => {
      await ensureSession();
      const [apps, waitlistStatus] = await Promise.all([
        api(`/api/ventures/${ventureId}/apps`),
        api(`/api/ventures/${ventureId}/waitlist`),
      ]);
      if (!current) return;
      if (waitlistStatus.ok) setWaitlist(waitlistStatus.data as WaitlistStatus);
      else setWaitlistError(apiFailure(waitlistStatus, "loading managed waitlist count"));
      if (!apps.ok) { setError(apiFailure(apps, "loading venture apps")); return; }
      const items = ((apps.data as { items?: { id: string }[] })?.items || []) as { id: string }[];
      if (!items[0]) {
        setError("No app yet, approve a plan and build first.");
        return;
      }
      const id = items[0].id;
      const o = await api(`/api/apps/${id}/analytics/overview`);
      if (!current) return;
      if (!o.ok) { setError(apiFailure(o, "loading analytics")); return; }
      setOverview(o.data as Overview);
      const f = await api(`/api/apps/${id}/analytics/funnel`);
      if (!current) return;
      if (!f.ok) { setError(apiFailure(f, "loading event counts")); return; }
      setFunnel(f.data as { steps?: string[]; counts?: Record<string, number> });
    })();
    return () => { current = false; };
  }, [ventureId]);

  const counts = funnel?.counts || {};
  const hasMeasurements = Boolean(overview?.days?.some((day) =>
    [day.visits, day.signups, day.revenue_minor].some((value) => Number.isFinite(value) && Number(value) > 0))) ||
    Object.values(counts).some((value) => Number.isFinite(value) && value > 0);

  return (
    <AppShell title="Analytics" ventureId={ventureId}>
      <div className={`chat-scroll ${styles.page}`}>
        {waitlistError ? <section className={styles.card}><h2>Waitlist sign-ups couldn’t load</h2><p>{waitlistError}</p></section> : null}
        {waitlist ? <section className={styles.card}>
          <h2>Waitlist sign-ups</h2>
          <p>{waitlist.total} saved sign-up{waitlist.total === 1 ? "" : "s"}. These people agreed to join your waitlist. We haven’t checked their email addresses or whether they use or pay for your product. Test sign-ups are not counted.</p>
          <span className={styles.meta}>These are saved waitlist entries. The page’s sign-up count below is tracked separately and may differ.</span>
        </section> : null}
        {error ? <section className={styles.card}><h2>Analytics need attention</h2><p>{error}</p></section> : null}
        {!error && (!overview || !funnel) ? <section className={styles.card}><h2>Loading analytics</h2><p>Checking visits and sign-ups…</p></section> : null}
        {!error && overview && funnel && !hasMeasurements ? <>
          <section className={styles.card}>
            <h2>No visits recorded yet</h2>
            <p>We haven’t received any visit or sign-up data yet. Check that your page is live and tracking is set up. An empty dashboard doesn’t necessarily mean nobody has visited.</p>
            <span className={styles.meta}>Only recorded counts are shown. We don’t fill in missing data.</span>
          </section>
          <section className={styles.card}>
            <h2>What you can track here</h2>
            <ol className={styles.milestones}>
              {(funnel.steps || []).map((step) => <li key={step}>{funnelLabel[step] || step.replace(/_/g, " ")}</li>)}
            </ol>
          </section>
        </> : null}
        {!error && overview && funnel && hasMeasurements ? <>
          <section className={styles.card}>
            <h2>Payments aren’t confirmed here yet</h2>
            <p>This dashboard doesn’t receive receipts from your payment provider. A payment reported by the page alone doesn’t prove money was received or that someone became a paying customer.</p>
            <p>Today: {overview.today?.visits ?? 0} page views · {overview.today?.signups ?? 0} sign-ups reported by the page</p>
          </section>
          <section className={styles.card}>
            <h2>Activity reported by your page</h2>
            <p>These are separate counts, not the journey of one person through your product. Repeat visits and test activity may be included. They don’t tell us how many different people use your product, come back, or pay.</p>
            <ol className={styles.milestones}>
              {(funnel.steps || []).map((step) => <li key={step}>{funnelLabel[step] || step.replace(/_/g, " ")}: {counts[step] ?? 0}</li>)}
            </ol>
          </section>
        </> : null}
      </div>
    </AppShell>
  );
}
