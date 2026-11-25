"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { api, ensureSession } from "@/lib/client";
import { apiFailure } from "@/lib/apiFailure";
import { shortTitle } from "@/lib/format";

export default function FeedbackPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const [title, setTitle] = useState("Feedback");
  const [text, setText] = useState("");
  const [source, setSource] = useState<"customer" | "founder">("customer");
  const [items, setItems] = useState<{ summary?: string; trust?: string }[]>([]);
  const [loadingItems, setLoadingItems] = useState(true);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    (async () => {
      setLoadingItems(true);
      try {
        await ensureSession();
        const ven = await api(`/api/ventures/${ventureId}`);
        if (ven.ok) {
          const v = ven.data as { name?: string; raw_idea?: string };
          setTitle(shortTitle(v.name, v.raw_idea));
        }
        const r = await api(`/api/ventures/${ventureId}/feedback`);
        if (r.ok) setItems(((r.data as { items?: { summary?: string; trust?: string }[] }).items) || []);
      } finally {
        setLoadingItems(false);
      }
    })();
  }, [ventureId]);

  async function submit() {
    if (!text.trim() || busy) return;
    setBusy(true); setMsg("");
    const r = await api(`/api/ventures/${ventureId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ text: text.trim(), source }),
    });
    setMsg(r.ok ? "Saved. Customer and public feedback remains untrusted evidence and never acts alone." : apiFailure(r, "saving this feedback"));
    if (r.ok) {
      setText("");
      const list = await api(`/api/ventures/${ventureId}/feedback`);
      if (list.ok) setItems(((list.data as { items?: { summary?: string; trust?: string }[] }).items) || []);
    }
    setBusy(false);
  }

  return (
    <AppShell title={title} ventureId={ventureId}>
      <div className="chat-scroll">
        <div className="chat-inner">
          <div className="panel-card">
            <h3>What are customers saying?</h3>
            <label>Source <select value={source} disabled={busy} onChange={event => setSource(event.target.value as "customer" | "founder")}>
              <option value="customer">Customer or public feedback</option><option value="founder">My own founder note</option>
            </select></label>
            <label>Feedback<textarea value={text} maxLength={4000} disabled={busy} onChange={(e) => setText(e.target.value)} placeholder="Paste a comment, review, or your own note…" rows={4} style={{ width: "100%", marginTop: 10 }} /></label>
            <button className="primary" type="button" disabled={busy || !text.trim()} onClick={() => void submit()} style={{ marginTop: 10 }}>
              {busy ? "Saving…" : "Add feedback"}
            </button>
            {msg && <p role={msg.startsWith("Saved") ? "status" : "alert"} className="muted" style={{ marginTop: 8 }}>{msg}</p>}
          </div>
          {loadingItems && <div className="panel-card muted" role="status">Loading saved feedback…</div>}
          {items.slice(0, 10).map((it, i) => (
            <div className="panel-card" key={i}>
              <div className="kv-label">{it.trust === "untrusted" ? "External" : "Founder"}</div>
              <div>{it.summary || "Feedback event"}</div>
            </div>
          ))}
        </div>
      </div>
    </AppShell>
  );
}
