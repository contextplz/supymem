"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "@/components/chat/ChatLink";
import { AppShell } from "@/components/layout/AppShell";
import { api } from "@/lib/client";
import { getOpsWorkspace } from "@/lib/opsApi";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type Company = { id: string; name?: string; workspace_id?: string };

export function LocalCompanyPage({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  const params = useSearchParams();
  const router = useRouter();
  const venture = params.get("venture");
  const workspace = params.get("workspace");
  const query = params.toString();
  const [companies, setCompanies] = useState<Company[] | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (venture) return;
    let live = true;
    setCompanies(null); setError("");
    const changed = () => { live = false; setCompanies(null); setAttempt(value => value + 1); };
    window.addEventListener("supymem-session-changed", changed);
    void (async () => {
      try {
        const result = await api("/api/ventures?summary=true");
        if (!live) return;
        const items = (result.data as { items?: Company[] } | null)?.items;
        if (!result.ok || !Array.isArray(items) || items.some(item => !item || typeof item.id !== "string" || !item.id
          || (item.name != null && typeof item.name !== "string") || (item.workspace_id != null && typeof item.workspace_id !== "string"))) {
          throw new Error();
        }
        const currentWorkspace = workspace || getOpsWorkspace();
        const current = items.filter(item => item.workspace_id === currentWorkspace);
        const selected = current.length === 1 ? current[0] : !workspace && items.length === 1 ? items[0] : null;
        setCompanies(items);
        if (selected) {
          const next = new URLSearchParams(query);
          next.set("venture", selected.id);
          router.replace(`${window.location.pathname}?${next}`);
        }
      } catch {
        if (live) setError("Your ventures could not be loaded. Reload your ventures to choose a company, or open Keys and connections to check your local setup.");
      }
    })();
    return () => { live = false; window.removeEventListener("supymem-session-changed", changed); };
  }, [venture, workspace, attempt, router, query]);

  if (venture) return <AppShell title={title} subtitle={subtitle}>{children}</AppShell>;
  return <main className="content-wrap">
    <h1>{title}</h1>
    {error ? <ErrorNotice error={error} source="connections.local.ventures">{error} <button type="button" onClick={() => setAttempt(value => value + 1)}>Reload your ventures</button></ErrorNotice>
      : companies === null ? <p role="status">Loading your ventures…</p>
      : companies.length ? <><label htmlFor="local-company">Choose your venture</label><select id="local-company" value="" onChange={event => {
        if (!companies.some(company => company.id === event.target.value)) return;
        const next = new URLSearchParams(params.toString());
        next.delete("workspace"); next.set("venture", event.target.value);
        router.push(`${window.location.pathname}?${next}`);
      }}><option value="">Choose a venture</option>{companies.map(company => <option key={company.id} value={company.id}>{company.name || "Your venture"}</option>)}</select></>
      : <p><Link href="/new">Create your venture</Link> to start working on your company.</p>}
    <p><Link href="/settings/local">Keys and connections</Link></p>
  </main>;
}
