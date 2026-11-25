"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/client";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type MemoryRow = {
  id: string;
  kind: string;
  subject: string;
  statement: string;
  valid_from?: string | null;
  source?: { type?: string; ref?: string; quote?: string };
  sources?: { type?: string; ref?: string }[];
};

export function VentureMemoryPanel({ ventureId, compact = false }: { ventureId: string; compact?: boolean }) {
  const [items, setItems] = useState<MemoryRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!ventureId) return;
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const res = await api(
        `/api/ventures/${encodeURIComponent(ventureId)}/memories?limit=50`,
      );
      if (!res.ok) {
        setError("Could not load saved memory.");
        setItems([]);
        return;
      }
      const data = res.data as { items?: MemoryRow[]; availability?: string; conflict_count?: number };
      setItems(Array.isArray(data.items) ? data.items : []);
      setNotice(data.availability === "conflict"
        ? "Some latest statements conflict. Tell Charlie which statement to use. Earlier sources are kept."
        : data.availability === "unavailable" || data.availability === "partial"
          ? "Some saved sources could not be loaded. This list may be incomplete. Please try again later."
          : "");
    } catch {
      setError("Could not load saved memory. Try again.");
      setItems([]);
    } finally { setLoading(false); }
  }, [ventureId]);

  useEffect(() => {
    void load();
  }, [load]);

  const forget = async (memId: string) => {
    if (busyId) return;
    setBusyId(memId);
    try {
      const res = await api(`/api/ventures/${encodeURIComponent(ventureId)}/memories/${encodeURIComponent(memId)}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        setError("Could not forget that memory.");
        return;
      }
      setError("");
      setItems((prev) => prev.filter((row) => row.id !== memId));
    } catch { setError("Could not forget that memory. Try again."); }
    finally { setBusyId(null); }
  };

  return (
    <div id="memory" className="panel-card" style={{ marginTop: compact ? 0 : 24 }}>
      <h3>Saved memory</h3>
      {compact ? <details><summary>About saved memory</summary><p className="muted">Saved statements and preferences are context, not approvals or verified results. To correct something, tell Charlie the current statement. Ask what you said before to review earlier sources.</p></details> :
      <p className="muted">
        Saved statements and preferences for this venture, with their sources. These are context,
        not approvals or verified results. To correct something, tell Charlie the current statement.
        To review earlier statements, ask what you said before about the topic.
      </p>}
      {loading ? <p className="muted" style={{ marginTop: 12 }}>Loading…</p> : null}
      <ErrorNotice error={error} source="VentureMemoryPanel.error" style={{ marginTop: 12, color: "var(--danger)" }} />
      {(error || notice) && <button type="button" disabled={loading} onClick={() => void load()}>Try again</button>}
      {notice ? <p className="muted" role="status" style={{ marginTop: 12 }}>{notice}</p> : null}
      {!loading && !error && !notice && items.length === 0 ? (
        <p className="muted" style={{ marginTop: 12 }}>
          No current saved memory is listed. In chat, say &quot;Remember that I prefer short answers.&quot;
        </p>
      ) : null}
      <ul style={{ listStyle: "none", padding: 0, margin: "12px 0 0" }}>
        {items.map((row) => (
          <li
            key={row.id}
            style={{
              display: "flex",
              gap: 12,
              alignItems: "flex-start",
              justifyContent: "space-between",
              padding: "10px 0",
              borderTop: "1px solid var(--border, #ddd)",
            }}
          >
            <div>
              <div className="muted" style={{ fontSize: 12 }}>
                {row.kind.replace(/_/g, " ")}
                {row.valid_from
                  ? ` · ${new Date(row.valid_from).toLocaleDateString()}`
                  : ""}
              </div>
              <div style={{ marginTop: 4 }}>
                {row.source?.type === "import" ? "An import source was recorded."
                  : row.source?.type === "task" ? "A task receipt was recorded."
                  : row.statement}
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                Source: {row.source?.type === "research" ? "a saved research excerpt"
                  : row.source?.type === "import" ? "an import receipt"
                  : row.source?.type === "task" ? "a task receipt"
                  : row.source?.type === "chat" || row.source?.ref?.startsWith("chat:")
                    ? "your saved chat statement" : "a saved source record"}
              </div>
            </div>
            <button
              type="button"
              data-usage-control="settings_memory_delete"
              disabled={Boolean(busyId)}
              onClick={() => void forget(row.id)}
            >
              {busyId === row.id ? "…" : "Forget"}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
