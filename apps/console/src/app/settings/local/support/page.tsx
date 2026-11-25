"use client";

import { useState } from "react";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";

export default function LocalSupportPage() {
  const local = useLocalMode();
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  async function download() {
    setBusy(true);
    setStatus("");
    try {
      const response = await fetch("/api/local/support-bundle", { cache: "no-store" });
      if (!response.ok) throw new Error();
      const url = URL.createObjectURL(await response.blob());
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = "supymem-support.json";
        link.click();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      setStatus("Your bundle is ready. Review it before sharing it.");
    } catch {
      setStatus("Check your app data folder and downloads folder permissions, then choose Download support bundle again.");
    } finally {
      setBusy(false);
    }
  }

  if (!local) return null;
  return <main aria-labelledby="support-title">
    <h1 id="support-title">Your support bundle</h1>
    <p>You can download a summary of recent crashes. It leaves out your keys, logs, and account details. Nothing is uploaded.</p>
    <button type="button" disabled={busy} onClick={() => void download()}>Download support bundle</button>
    <p role="status">{status}</p>
    <a href="/settings/local">Back to Keys and connections</a>
  </main>;
}
