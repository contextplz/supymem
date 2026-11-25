"use client";

import { useEffect, useState } from "react";
import { getMarketingAssetBlob, type MarketingAsset } from "@/lib/opsApi";

/** Owned media uses the same authenticated transport as the rest of the company. */
export function ProtectedMarketingAsset({ asset, video = false }: { asset: MarketingAsset; video?: boolean }) {
  const [loaded, setLoaded] = useState<{ id: string; url: string } | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let disposed = false;
    let objectUrl = "";
    setLoaded(null); setError("");
    function release() {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = "";
    }
    function accountChanged() { release(); setLoaded(null); setError("Account changed. Reload the company's media."); }
    window.addEventListener("supymem-session-changed", accountChanged);
    void getMarketingAssetBlob(asset.id).then((blob) => {
      if (disposed) return;
      if (video ? !blob.type.startsWith("video/") : !blob.type.startsWith("image/")) throw new Error("The asset format does not match its preview.");
      objectUrl = URL.createObjectURL(blob);
      setLoaded({ id: asset.id, url: objectUrl });
    }).catch((reason) => { if (!disposed) setError(reason instanceof Error ? reason.message : "This asset could not be loaded."); });
    return () => { release(); window.removeEventListener("supymem-session-changed", accountChanged); };
  }, [asset.id, video, attempt]);
  if (error) return <p role="status">{error} <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry preview</button></p>;
  if (!loaded || loaded.id !== asset.id) return <p role="status">Loading preview…</p>;
  return video ? <video src={loaded.url} controls muted playsInline /> : <img src={loaded.url} alt={asset.title || asset.kind} />;
}
