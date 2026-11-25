"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { useEffect, useRef, useState } from "react";
import { OneCard } from "@/components/primitives/OneCard";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { founderAssetsClient, safeSiteUrl, type ImportedSite, type SiteImport } from "@/lib/founderAssetsApi";
import type { GtmOwner } from "@/lib/gtmDiagnosisIntent";
import { verifiedSessionEpoch, verifiedSessionSubject } from "@/lib/client";

export function ExistingSiteCard({ owner, site, knownUrl, current, saved }: { owner: GtmOwner; site: ImportedSite | null; knownUrl?: string; current: () => boolean; saved: (value: SiteImport) => void }) {
  const [url, setUrl] = useState(site?.url || knownUrl || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false), mounted = useRef(true);
  useEffect(() => { setUrl(site?.url || knownUrl || ""); }, [site?.url, knownUrl]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const checked = site?.status === "verified";
  const content = <>
    {site && <>
      <p>{checked ? "Your existing website was checked." : "Your website is saved, but Supymem could not verify it yet."} {safeSiteUrl(site.url) && <ChatAnchor href={safeSiteUrl(site.url)} target="_blank" rel="noopener noreferrer" data-usage-control="website_existing_open">Open your website</ChatAnchor>}</p>
      {!checked && site.note && <p>{site.note}</p>}
      {checked && site.audit && <>
        {site.audit.headline && <p><strong>Headline:</strong> {site.audit.headline}</p>}
        <ul>{site.audit.recommendations.map((item, index) => <li key={index}>{item}</li>)}</ul>
        <p>Speed and mobile layout still need a browser check. These recommendations come from the website content.</p>
      </>}
      <p>Continue with research and customers. You can request a new landing page later.</p>
      <ChatAnchor href={`/v/${encodeURIComponent(owner.ventureId)}/research`} data-usage-control="website_existing_research">Continue research</ChatAnchor>
    </>}
    <form onSubmit={event => {
      event.preventDefault();
      if (pending.current || !mounted.current || !current() || !safeSiteUrl(url)) return;
      const epoch = verifiedSessionEpoch(), actor = verifiedSessionSubject();
      const active = () => mounted.current && current() && epoch === verifiedSessionEpoch() && actor === verifiedSessionSubject();
      pending.current = true; setBusy(true); setError("");
      void founderAssetsClient.importSite(owner, url.trim()).then(value => { if (active()) saved(value); })
        .catch(() => { if (active()) setError("The website could not be checked. Try again with a public website URL."); })
        .finally(() => { pending.current = false; if (active()) setBusy(false); });
    }}>
      <label>Website URL <input type="url" required maxLength={2000} value={url} disabled={busy} data-usage-control="website_existing_url" placeholder="https://your-company.com" onChange={event => setUrl(event.target.value)} /></label>
      <button type="submit" className="ghost" data-usage-control="website_existing_import" disabled={busy || !safeSiteUrl(url)}>{busy ? "Checking website…" : site ? "Check this site again" : "Check existing website"}</button>
    </form>
    <ErrorNotice error={error} source="website.existing_site" />
  </>;
  return site || knownUrl ? <OneCard id="existing-site-audit" title="Improve your existing website" status={checked ? "done" : "needs_you"}>{content}</OneCard>
    : <details><summary>I already have a website</summary>{content}</details>;
}
