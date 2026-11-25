"use client";

import { useCallback, useEffect, useState } from "react";
import { DailyQueue, type QueueItem } from "@/components/outreach/DailyQueue";
import { DraftReview } from "@/components/outreach/DraftReview";
import { EvidenceConnect } from "@/components/outreach/EvidenceConnect";
import { IngestPaste } from "@/components/outreach/IngestPaste";
import { InterviewCapture } from "@/components/outreach/InterviewCapture";
import { OnboardingIcp } from "@/components/outreach/OnboardingIcp";
import { PilotTracker } from "@/components/outreach/PilotTracker";
import styles from "@/components/outreach/Outreach.module.css";
import {
  captureOutreachInterview,
  copyOutreachDraft,
  createOutreachDraft,
  createOutreachPilot,
  getOutreachFunnel,
  getOutreachOnboarding,
  getOutreachQueue,
  getOutreachStatus,
  ingestOutreachPaste,
  listOutreachPilots,
  markOutreachDraftSent,
  patchOutreachContact,
  patchOutreachOnboarding,
  patchOutreachPilot,
  recordOutreachEvidence,
} from "@/lib/opsApi";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { api } from "@/lib/client";
import { diagnosisClient, type GtmVenture } from "@/lib/gtmDiagnosisApi";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type OnboardingFlags = {
  icp: boolean;
  excludes: boolean;
  sender_geo: boolean;
  send_path: boolean;
  voice: boolean;
  evidence: boolean;
  ready: boolean;
  outbound_mode?: string;
  sender_geo_code?: string;
  sender_postal_address?: string;
  segment?: { who?: string };
};

type DraftState = {
  id: string;
  contact_id: string;
  body: string | null;
  blocked?: boolean;
  reason?: string | null;
  personalize?: string;
  explain?: string | null;
  band?: string | null;
  sent_at?: string | null;
};

type FunnelStage = { n: number; d: number; d_meaning: string; rate: number | null };

type EvidenceRow = {
  provider: string;
  status: string;
  connected_account_id?: string | null;
  fake?: boolean;
};

type Pilot = {
  id: string;
  use_case: string;
  success_criterion: string;
  status: string;
  paid_decision_at?: string | null;
  permission_to_demo_at?: string | null;
};

export default function OutreachPage() {
  const operationsScope = useOperationsScope();
  const [flags, setFlags] = useState<OnboardingFlags | null>(null);
  const [items, setItems] = useState<QueueItem[]>([]);
  const [ready, setReady] = useState(false);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [contactId, setContactId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [funnel, setFunnel] = useState<Record<string, FunnelStage>>({});
  const [sources, setSources] = useState<EvidenceRow[]>([]);
  const [pilots, setPilots] = useState<Pilot[]>([]);
  const [remaining, setRemaining] = useState<{ connect?: number; dm?: number; email?: number } | null>(
    null,
  );
  const [outreachWeek, setOutreachWeek] = useState<number | null>(null);
  const [suggestedWho, setSuggestedWho] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [ob, q, fn, st, pl] = await Promise.all([
        getOutreachOnboarding(),
        getOutreachQueue(),
        getOutreachFunnel(),
        getOutreachStatus(),
        listOutreachPilots(),
      ]);
      setErr(null);
      setFlags({
        outbound_mode: ob.outbound_mode as string | undefined,
        sender_geo_code: ob.sender_geo_code as string | undefined,
        sender_postal_address: ob.sender_postal_address as string | undefined,
        segment: ob.segment as { who?: string } | undefined,
        icp: Boolean(ob.icp),
        excludes: Boolean(ob.excludes),
        sender_geo: Boolean(ob.sender_geo),
        send_path: Boolean(ob.send_path),
        voice: Boolean(ob.voice),
        evidence: Boolean(ob.evidence),
        ready: Boolean(ob.ready),
      });
      setSources((ob.evidence_sources as EvidenceRow[]) || []);
      setItems(q.items || []);
      setReady(Boolean(q.ready));
      setFunnel(fn.stages || {});
      setRemaining(st?.remaining || null);
      setOutreachWeek(st?.outreach_week ?? null);
      setPilots((pl.items || []) as Pilot[]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load outreach");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!operationsScope?.actorId || !operationsScope.workspaceId || !operationsScope.ventureId) return;
    const owner = {
      actorId: operationsScope.actorId,
      workspaceId: operationsScope.workspaceId,
      ventureId: operationsScope.ventureId,
    };
    let active = true;
    (async () => {
      try {
        const result = await api(`/api/ventures/${encodeURIComponent(owner.ventureId)}`);
        if (!result.ok) return;
        const venture = result.data as GtmVenture;
        const context = await diagnosisClient.context(owner, venture);
        if (active) setSuggestedWho(context.who);
      } catch {
        // The founder can still enter an audience manually when saved context is unavailable.
      }
    })();
    return () => { active = false; };
  }, [operationsScope?.actorId, operationsScope?.ventureId, operationsScope?.workspaceId]);

  return (
    <section aria-label="Founder outreach">
      <div className={styles.wrap}>
        <aside className={styles.notice} aria-label="LinkedIn send policy">
          <strong>We never send on LinkedIn.</strong>
          <p>
            Copy the draft, paste it in LinkedIn yourself, then mark it sent here. LinkedIn forbids
            bots, crawlers, and scraping, we will not log in, fetch profiles, or send for you.
          </p>
        </aside>
        <ErrorNotice error={err} source="ops.outreach.err" className={styles.err} />
        {Object.keys(funnel).length > 0 && (
          <div className={styles.banner}>
            {(["sent", "accepted", "replied"] as const).map((key) => {
              const stage = funnel[key];
              if (!stage) return null;
              return (
                <span key={key} className={styles.pill}>
                  {key} {stage.n}/{stage.d} {stage.d_meaning}
                </span>
              );
            })}
          </div>
        )}
        <OnboardingIcp
          flags={flags}
          suggestedWho={suggestedWho}
          onSave={async (body) => {
            setErr(null);
            await patchOutreachOnboarding(body);
            await refresh();
          }}
        />
        <EvidenceConnect
          sources={sources}
          onAction={async (provider, action) => {
            setErr(null);
            await recordOutreachEvidence(provider, action);
            await refresh();
          }}
        />
        <div className={`${styles.grid} ${styles.grid2}`}>
          <IngestPaste
            onIngest={async (raw, source) => {
              setErr(null);
              const out = await ingestOutreachPaste({ source, raw_text: raw });
              const cid = out.contact_id;
              if (!cid) {
                setDraft({
                  id: out.signal_id,
                  contact_id: "",
                  body: null,
                  blocked: true,
                  reason: "layer_a_only",
                  personalize: "content_play",
                  explain: "Layer A signal saved. Promote a named person to draft.",
                });
                await refresh();
                return;
              }
              setContactId(cid);
              setDraft(null);
              const d = await createOutreachDraft(cid, "connect");
              setDraft({ ...d, contact_id: cid });
              await refresh();
            }}
          />
          <DailyQueue
            items={items}
            ready={ready}
            remaining={remaining}
            outreachWeek={outreachWeek}
            onPick={async (id) => {
              const d = await createOutreachDraft(id, "connect");
              setContactId(id);
              setDraft({ ...d, contact_id: id });
            }}
          />
        </div>
        <DraftReview
          draft={draft}
          onWhy={async (why) => {
            if (!contactId) return;
            await patchOutreachContact(contactId, { why_this_week: why });
            const d = await createOutreachDraft(contactId, "connect");
            setDraft({ ...d, contact_id: contactId });
          }}
          onCopy={async () => {
            if (!draft?.id || !draft.body) return;
            await navigator.clipboard.writeText(draft.body);
            await copyOutreachDraft(draft.id);
            await refresh();
          }}
          onSent={async () => {
            if (!draft?.id) return;
            const sent = await markOutreachDraftSent(draft.id);
            setDraft({ ...draft, sent_at: sent.sent_at });
            await refresh();
          }}
        />
        <InterviewCapture
          contactId={contactId}
          onSave={async (notes, momTestOk) => {
            if (!contactId) return {};
            return captureOutreachInterview({
              contact_id: contactId,
              notes_md: notes,
              mom_test_ok: momTestOk,
            });
          }}
        />
        <PilotTracker
          contactId={contactId}
          items={pilots}
          onCreate={async (body) => {
            if (!contactId) return;
            await createOutreachPilot({ contact_id: contactId, ...body });
            await refresh();
          }}
          onPatch={async (id, body) => {
            await patchOutreachPilot(id, body);
            await refresh();
          }}
        />
      </div>
    </section>
  );
}
