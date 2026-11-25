"use client";

import { useRef, useState } from "react";
import { downloadFile } from "@/lib/client";
import { firstlookPreviewPath } from "@/lib/firstlookPreview";
import { LandingPreviewFrame } from "@/components/cockpit/LandingPreviewFrame";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import styles from "./PageResultLink.module.css";

export function pageResultKind(href?: string): "preview" | "export" | null {
  if (!href) return null;
  if (firstlookPreviewPath(href)?.startsWith("/api/ventures/")) return "preview";
  return /^\/api\/ventures\/[^/?#\s\\]+\/(?:pagebuilder\/jobs\/[^/?#\s\\]+\/source(?:\?variant=[0-2])?|device-work-orders\/[^/?#\s\\]+\/source-archive)$/.test(href) ? "export" : null;
}

export function PageResultLink({ href, className, "data-usage-control": usageControl }: {
  href: string; className?: string; "data-usage-control"?: string;
}) {
  const kind = pageResultKind(href);
  const dialog = useRef<HTMLDialogElement>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!kind) return null;
  return <span className={styles.result}>
    <button type="button" className={className} data-usage-control={usageControl} disabled={busy} onClick={async () => {
      if (kind === "preview") {
        setPreviewOpen(true);
        dialog.current?.showModal();
        return;
      }
      setBusy(true);
      setError(null);
      const downloaded = await downloadFile(href, href.includes("/device-work-orders/") ? "build-source.zip" : "landing-page.zip");
      setBusy(false);
      if (!downloaded) setError("Your export could not be downloaded. Choose Export to try again, or reopen your app to restore your session.");
    }}>{kind === "preview" ? "Open preview" : busy ? "Exporting…" : "Export"}</button>
    {error && <ErrorNotice as="span" error={error} source="chat.page_export" />}
    {kind === "preview" && <dialog ref={dialog} className={styles.preview} aria-label="Your private landing page" onClose={() => setPreviewOpen(false)}>
      <form method="dialog"><button type="submit">Close preview</button></form>
      {previewOpen && <LandingPreviewFrame src={href} title="Your private landing page" sandbox="" className={styles.previewFrame} />}
    </dialog>}
  </span>;
}
