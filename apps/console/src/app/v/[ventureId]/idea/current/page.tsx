"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { shortTitle } from "@/lib/format";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { RelativeTime } from "@/components/primitives/RelativeTime";
import { prefillChat } from "@/lib/chatPrefill";
import { ChatAnchor } from "@/components/chat/ChatLink";

type Experiment = { name: string; source: string; status: string; result: string };
type IdeaPage = {
  summary?: string; problem?: string; goal?: string; offer?: string; confirmed?: boolean;
  customers?: { current?: string[]; alternatives?: string[] };
  open_questions?: string[]; experiments?: Experiment[]; what_changed?: string[];
  what_we_know?: { text: string; href: string }[];
};
type IdeaRevision = { summary?: string; created_at?: string; structured?: Record<string, unknown> };

async function readConfirmedIdea(path: string, ventureId: string): Promise<IdeaRevision | null> {
  const listed = await api(`${path}/idea/sessions`);
  const items = (listed.data as { items?: { id: string; state: string }[] } | null)?.items;
  if (!listed.ok || !Array.isArray(items)) throw new Error("Couldn’t load your saved idea.");
  const latest = items.find(item => item && item.state !== "abandoned");
  if (latest?.state !== "confirmed") return null;
  if (typeof latest.id !== "string" || !latest.id) throw new Error("Couldn’t load your saved idea.");
  const detail = await api(`${path}/idea/sessions/${encodeURIComponent(latest.id)}`);
  const idea = detail.data as { id?: string; venture_id?: string; state?: string; current_revision?: IdeaRevision } | null;
  if (!detail.ok || idea?.id !== latest.id || idea.venture_id !== ventureId || idea.state !== "confirmed" || !idea.current_revision) {
    throw new Error("Couldn’t load your saved idea.");
  }
  return idea.current_revision;
}

function savedText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() && !/^(unknown|none|n\/a|null)$/i.test(value.trim()) ? value : undefined;
}

function Section({ title, text, items, empty = "Not set yet", ask }: { title: string; text?: string; items?: string[]; empty?: string; ask?: { label: string; text: string } }) {
  const list = items || [];
  const placeholder = <p className="muted">{ask ? "Not set yet. " : empty}{ask && <button type="button" data-usage-control="chat_followup_action" onClick={() => prefillChat(ask.text)}>{ask.label}</button>}</p>;
  return (
    <div className="panel-card">
      <h3>{title}</h3>
      {items ? (list.length ? <ul>{list.map((item) => <li key={item}>{item}</li>)}</ul> : placeholder)
        : savedText(text) ? <p>{text}</p> : placeholder}
    </div>
  );
}

export default function CurrentIdeaPage() {
  const ventureId = String(useParams().ventureId || "");
  const [title, setTitle] = useState("Idea");
  const [page, setPage] = useState<IdeaPage | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let live = true;
    (async () => {
      setLoading(true); setError("");
      try {
        await ensureSession();
        const path = `/api/ventures/${encodeURIComponent(ventureId)}`;
        const [ven, r, revision] = await Promise.all([
          api(path).catch(() => null),
          api(`${path}/idea-page`).catch(() => null),
          readConfirmedIdea(path, ventureId).catch(() => undefined),
        ]);
        if (!live) return;
        if (ven?.ok) {
          const v = ven.data as { name?: string; raw_idea?: string };
          setTitle(shortTitle(v.name, v.raw_idea));
        }
        const d = r?.ok ? r.data as { page?: IdeaPage | null; updated_at?: string | null } : null;
        if (revision) {
          const fields = revision.structured || {};
          const audience = savedText(fields.customer) || savedText(fields.audience);
          setPage({ ...d?.page, confirmed: true, summary: savedText(revision.summary),
            problem: savedText(fields.problem),
            goal: savedText(fields.goal) || savedText(fields.desired_outcome),
            offer: savedText(fields.offer),
            customers: { ...d?.page?.customers, current: audience ? [audience] : [] } });
          setUpdatedAt(revision.created_at || d?.updated_at || null);
          return;
        }
        if (!d || (!d.page && revision === undefined)) {
          setError(r && !r.ok ? apiFailure(r, "loading your idea") : "Couldn’t load your idea. Try again.");
          return;
        }
        setPage(d.page || null);
        setUpdatedAt(d.updated_at || null);
      } catch {
        if (live) setError("Couldn’t load your idea. Try again.");
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => { live = false; };
  }, [ventureId, refresh]);

  const customers = page?.customers || {};
  return (
    <AppShell title={title} ventureId={ventureId}>
      <div className="chat-scroll">
        <div className="chat-inner">
          <div className="panel-card">
            <h3>Idea</h3>
            {updatedAt && <p className="muted">Updated <RelativeTime iso={updatedAt} /></p>}
            {page && <p>{page.summary || "Not set yet"}</p>}
            {page?.confirmed && <><p>Status: Confirmed</p><button type="button" onClick={() => prefillChat("Correction: ")}>Edit</button></>}
          </div>
          {loading && <div className="panel-card muted" role="status">Loading your idea…</div>}
          <ErrorNotice error={error} source="idea_page.error" as="div" className="panel-card" />
          {error && <button type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>Try again</button>}
          {!loading && !error && !page && <div className="panel-card muted">You have no saved idea summary yet. <a data-chat-route-native href={`/v/${encodeURIComponent(ventureId)}/idea`}>Open Idea and describe what you want to build.</a></div>}
          {page && (
            <>
              <Section title={page.confirmed ? "Who it’s for" : "Target customers"} items={customers.current} ask={{label: "Tell Charlie the customers", text: "Help me describe who my idea is for. Ask me what is missing, then update my idea."}} />
              {!!customers.alternatives?.length && <Section title="Other customers considered" items={customers.alternatives} />}
              <Section title="Problem" text={page.problem} ask={{ label: "Tell Charlie the problem", text: "Help me describe the problem my idea solves. Ask me what is missing, then update my idea." }} />
              {page.confirmed && <Section title="Goal" text={page.goal} />}
              <Section title="Current offer" text={page.offer} ask={{ label: "Tell Charlie the offer", text: "Help me describe my current offer. Ask me what is missing, then update my idea." }} />
              <Section title="Open questions" items={page.open_questions} empty="None saved." />
              <div className="panel-card">
                <h3>What we know</h3>
                {(page.what_we_know || []).length === 0 && <p className="muted">No experiment results yet.</p>}
                <ul>{(page.what_we_know || []).map((result) => (
                  <li key={result.href}><ChatAnchor href={result.href}>{result.text}</ChatAnchor></li>
                ))}</ul>
              </div>
              <div className="panel-card">
                <h3>Experiments</h3>
                {(page.experiments || []).length === 0 && <p className="muted">None yet.</p>}
                {(page.experiments || []).map((e) => (
                  <div key={`${e.source}:${e.name}`} style={{ marginBottom: 12 }}>
                    <div className="kv-label">{e.status} · {e.source}</div>
                    <div>{e.name}</div>
                    {e.result && e.result !== "unknown" && <p className="muted">Result: {e.result}</p>}
                  </div>
                ))}
              </div>
              <Section title="What changed recently" items={page.what_changed} empty="Nothing new since the last update." />
            </>
          )}
        </div>
      </div>
    </AppShell>
  );
}
