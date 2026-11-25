"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession } from "@/lib/client";
import { shortTitle } from "@/lib/format";
import styles from "../plan/plan.module.css";

export default function MarketPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [title, setTitle] = useState("Go-to-market");
  const [choice, setChoice] = useState<"product_first" | "market_first" | "">("");
  const [msg, setMsg] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      await ensureSession();
      const [ven, charter, apps] = await Promise.all([
        api(`/api/ventures/${ventureId}`),
        api(`/api/ventures/${ventureId}/charter`),
        api(`/api/ventures/${ventureId}/apps`),
      ]);
      if (ven.ok) {
        const v = ven.data as { name?: string; raw_idea?: string; gtm_order?: string };
        setTitle(shortTitle(v.name, v.raw_idea));
        const charterOrder = (charter.data as { content?: { gtm_order?: string } } | null)?.content?.gtm_order;
        const products = (apps.data as { items?: unknown[] } | null)?.items;
        const saved = v.gtm_order || charterOrder || (Array.isArray(products) && products.length ? "product_first" : "");
        if (saved === "product_first" || saved === "market_first") setChoice(saved);
      }
    })();
  }, [ventureId]);

  async function pick(order: "product_first" | "market_first") {
    if (saving) return;
    setSaving(true);
    setMsg("");
    try {
      const r = await api(`/api/ventures/${encodeURIComponent(ventureId)}/gtm-order`, {
        method: "PUT",
        body: JSON.stringify({ gtm_order: order }),
      });
      const saved = (r.data as {gtm_order?: unknown} | null)?.gtm_order;
      if (!r.ok || saved !== order) { setMsg("Couldn’t save that choice. Your previous route is unchanged."); return; }
      setChoice(order);
      setMsg("Saved. Research will guide you to the matching next step. No campaign was started.");
    } catch {
      setMsg("Couldn’t save that choice. Your previous route is unchanged.");
    } finally { setSaving(false); }
  }

  return (
    <AppShell title={title} ventureId={ventureId}>
      <div className={`chat-scroll ${styles.page}`}>
        {choice ? <>
          <section className={styles.card}>
            <h1>{choice === "product_first" ? "Product-first path" : "Market-first path"}</h1>
            <p>{choice === "product_first"
              ? "Build and verify the smallest useful product, then test demand with the accepted release."
              : "Test demand with reviewable evidence before committing more product resources."}</p>
            <p>Research, feedback and outreach evidence stay separate from guesses. Supymem will not publish or contact anyone without the required approval.</p>
            <Link className={styles.nextButton} href={`/gtm?venture=${encodeURIComponent(ventureId)}`}>
              {choice === "market_first" ? "Set up and test your waitlist →" : "Open go-to-market workspace →"}
            </Link>
          </section>
          <details className={styles.card}>
            <summary>Change strategy</summary>
            <p>Changing this affects future recommendations. It does not publish, advertise or contact anyone.</p>
            <div className="options">
              <button type="button" disabled={saving} className={`option${choice === "product_first" ? " recommended" : ""}`} onClick={() => void pick("product_first")}>
                <span className="label">Product first</span><span className="hint">Build, verify, then test demand.</span>
              </button>
              <button type="button" disabled={saving} className={`option${choice === "market_first" ? " recommended" : ""}`} onClick={() => void pick("market_first")}>
                <span className="label">Market first</span><span className="hint">Test demand before more building.</span>
              </button>
            </div>
            {msg && <p>{msg}</p>}
          </details>
        </> : <section className={styles.card}>
          <h1>Choose how to test this company</h1>
          <p>Product first builds the smallest useful release before demand tests. Market first tests demand before more product investment.</p>
          <div className="options">
            <button
              type="button"
              className="option recommended"
              disabled={saving}
              onClick={() => void pick("product_first")}
            >
              <span className="label">
                <span className="badge-rec">Recommended</span>
                Product first
              </span>
              <span className="hint">Build the MVP, then launch carefully.</span>
            </button>
            <button
              type="button"
              className="option"
              disabled={saving}
              onClick={() => void pick("market_first")}
            >
              <span className="label">Market first</span>
              <span className="hint">Advertise the idea, learn from response, then build.</span>
            </button>
          </div>
          {msg && <p>{msg}</p>}
        </section>}
      </div>
    </AppShell>
  );
}
