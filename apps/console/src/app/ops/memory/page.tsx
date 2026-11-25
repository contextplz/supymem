"use client";

import { useState } from "react";
import { opsApi, type MemoryItem } from "@/lib/opsApi";
import styles from "@/components/ops/ops.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export default function OpsMemoryPage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const search = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    setSearched(true);
    try {
      const data = await opsApi.get<MemoryItem[]>(
        `/memory?q=${encodeURIComponent(query)}&limit=20`,
      );
      setResults(Array.isArray(data) ? data : []);
    } catch (e) {
      setResults([]);
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>Memory</h1>
      <div className={styles.searchRow}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void search()}
          placeholder="Search company memory…"
        />
        <button
          type="button"
          className="primary"
          onClick={() => void search()}
          disabled={loading}
        >
          {loading ? "Searching…" : "Search"}
        </button>
      </div>

      {error && (
        <ErrorNotice error={error} source="ops.memory.search" className={styles.empty}>
          Search failed, ops API may be offline. {error}
        </ErrorNotice>
      )}

      {!error && searched && results.length === 0 && (
        <p className={styles.empty}>No memory entries matched.</p>
      )}

      <div className={styles.stack}>
        {results.map((m) => (
          <div key={m.id} className={styles.rowCard}>
            <div className={styles.rowBody}>
              <div style={{ display: "flex", gap: "var(--s-2)", alignItems: "center", marginBottom: 4 }}>
                <span className={styles.chip}>{m.category}</span>
                <span className={styles.rowMeta}>
                  {new Date(m.created_at).toLocaleDateString()}
                </span>
              </div>
              <p className={styles.rowTitle}>{m.title}</p>
              <p className={styles.rowMeta}>{m.content}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
