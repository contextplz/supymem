"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { NextStepButton, useNextStep } from "@/components/layout/NextStepStrip";
import { api, ensureSession } from "@/lib/client";
import { fieldLabel, shortTitle, sourceLabel, stageLabel } from "@/lib/format";
import { hasVisibleCharter } from "@/lib/charterPresentation";
import { objectiveLabel } from "@/lib/planObjective";
import styles from "./charter.module.css";

type CharterContent = {
  one_liner?: string;
  problem?: string;
  audience?: string;
  mvp_scope?: string[] | string;
  explicit_non_goals?: string[] | string;
  success_metric?: string;
  success_threshold?: number;
  gtm_order?: string;
  provenance?: Record<string, { source?: string; value?: unknown }>;
};

type ExecutionPlanSummary = {
  state?: string;
  version?: number;
  proposal?: {
    summary?: string;
    goal_first_paid_user?: string;
    assumptions?: string[];
    exclusions?: string[];
    evidence_gaps?: string[];
  };
};

function asText(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) return value.filter(Boolean).join(" · ");
  return String(value);
}

export default function CharterPage() {
  const params = useParams();
  const router = useRouter();
  const ventureId = String(params.ventureId || "");
  const nextStep = useNextStep(ventureId);
  const [ventureName, setVentureName] = useState("Plan");
  const [stage, setStage] = useState("charter_review");
  const [content, setContent] = useState<CharterContent | null>(null);
  const [executionPlan, setExecutionPlan] = useState<ExecutionPlanSummary | null>(null);
  const [guide, setGuide] = useState("");
  const [loading, setLoading] = useState(true);
  const canWritePlan = nextStep?.researchReportReady && !["working", "queued", "paused"].includes(nextStep.runState || "");

  useEffect(() => {
    (async () => {
      await ensureSession();
      const ven = await api(`/api/ventures/${ventureId}`);
      if (ven.ok) {
        const v = ven.data as { name?: string; raw_idea?: string; stage?: string };
        setVentureName(shortTitle(v.name, v.raw_idea));
        if (v.stage) setStage(v.stage);
      }
      const r = await api(`/api/ventures/${ventureId}/charter`);
      const charter = r.ok ? (r.data as { content?: CharterContent }).content : null;
      if (!hasVisibleCharter(charter)) {
        const list = await api(`/api/ventures/${ventureId}/plans`);
        const latest = (list.data as { items?: { id: string }[] } | null)?.items?.[0];
        const detail = latest ? await api(`/api/ventures/${ventureId}/plans/${latest.id}`) : null;
        if (detail?.ok && (detail.data as ExecutionPlanSummary)?.proposal) {
          setExecutionPlan(detail.data as ExecutionPlanSummary);
        } else {
          setGuide("No product definition is saved yet. Continue the idea journey so Supymem can write it with you.");
        }
        setLoading(false);
        return;
      }
      setContent(charter || null);
      setLoading(false);
    })();
  }, [ventureId]);

  function block(key: keyof CharterContent, value: unknown, extra = "") {
    const text = asText(value) + extra;
    if (!text) return null;
    const src = sourceLabel(content?.provenance?.[key]?.source);
    return (
      <article className={styles.card} key={key}>
        <h2>{fieldLabel(key)}</h2>
        <p>{text}</p>
        {src ? <span className={styles.source}>From {src}</span> : null}
      </article>
    );
  }

  const gtm =
    content?.gtm_order === "market_first"
      ? "Market first, test demand, then build"
      : content?.gtm_order === "product_first"
        ? "Product first, build, then launch"
        : content?.gtm_order;

  return (
    <AppShell title={ventureName} stage={stage} ventureId={ventureId}>
      <div className={styles.page}>
        <p className={styles.lede}>
          What we’re building and why. Nothing technical, the one-liner, who it’s for, and what
          we will not do.
        </p>

        {loading && <section className={styles.guide} role="status"><h2>Loading the product definition</h2><p>Reading the confirmed plan and founder decisions…</p></section>}

        {guide && !executionPlan && (
          <section className={styles.guide}>
            <h2>Plan isn’t written yet</h2>
            <p>{canWritePlan ? "Your research is saved. Review the planning estimate, then confirm to write your plan." : guide}</p>
            <p>
              {canWritePlan && nextStep
                ? <NextStepButton step={{ ...nextStep, key: "plan.generate", chat_action: "plan.generate", payload: null,
                    href: `/v/${ventureId}/plan`, cta_label: "Write my plan" }} className="primary" />
                : <Link href={`/v/${ventureId}/idea`}>Continue the idea journey →</Link>}
            </p>
          </section>
        )}

        {executionPlan?.proposal && (
          <>
            <section className={styles.hero}>
              <p className={styles.label}>Approved product definition</p>
              <h1>{executionPlan.proposal.summary || "Execution plan"}</h1>
              <p>Version {executionPlan.version ?? 1} · {stageLabel(executionPlan.state || "approved")}</p>
            </section>
            <div className={styles.grid}>
              {executionPlan.proposal.goal_first_paid_user && (
                <article className={styles.card}>
                  <h2>{objectiveLabel(executionPlan.proposal)}</h2><p>{executionPlan.proposal.goal_first_paid_user}</p>
                </article>
              )}
              {!!executionPlan.proposal.assumptions?.length && (
                <article className={styles.card}><h2>Assumptions to test</h2><ul>{executionPlan.proposal.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul></article>
              )}
              {!!executionPlan.proposal.exclusions?.length && (
                <article className={styles.card}><h2>Not in this version</h2><ul>{executionPlan.proposal.exclusions.map((item, index) => <li key={index}>{item}</li>)}</ul></article>
              )}
              {!!executionPlan.proposal.evidence_gaps?.length && (
                <article className={styles.card}><h2>Evidence still needed</h2><ul>{executionPlan.proposal.evidence_gaps.map((item, index) => <li key={index}>{item}</li>)}</ul></article>
              )}
            </div>
            <div className={styles.actions}>
              <button className="primary" type="button" onClick={() => router.push(`/v/${ventureId}/plan`)}>Open milestones and resources →</button>
              <button type="button" onClick={() => router.push(`/v/${ventureId}/idea`)}>Review the confirmed idea</button>
            </div>
          </>
        )}

        {content && (
          <>
            {content.one_liner && (
              <section className={styles.hero}>
                <p className={styles.label}>One-liner</p>
                <h1>{content.one_liner}</h1>
                <p>This is the sentence we use everywhere else, landing page, research, team.</p>
              </section>
            )}
            <div className={styles.grid}>
              {block("problem", content.problem)}
              {block("audience", content.audience)}
              {block("mvp_scope", content.mvp_scope)}
              {block("explicit_non_goals", content.explicit_non_goals)}
              {block(
                "success_metric",
                content.success_metric
                  ? `${content.success_metric}${
                      content.success_threshold != null ? ` ≥ ${content.success_threshold}` : ""
                    }`
                  : "",
              )}
              {gtm ? (
                <article className={styles.card}>
                  <h2>{fieldLabel("gtm_order")}</h2>
                  <p>{gtm}</p>
                </article>
              ) : null}
            </div>
            <div className={styles.actions}>
              <button className="primary" type="button" onClick={() => router.push(`/v/${ventureId}`)}>
                Back to Overview
              </button>
            </div>
          </>
        )}
      </div>
    </AppShell>
  );
}
