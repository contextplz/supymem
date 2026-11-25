"use client";

import { useEffect } from "react";

import { isChunkLoadError, reloadOnceForChunkError } from "@/lib/chunkLoadError";
import { recordWebviewError } from "@/lib/webviewDiagnostics";

export default function VentureError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    recordWebviewError("render_error", error);
    if (isChunkLoadError(error)) reloadOnceForChunkError();
  }, [error]);

  const network =
    /failed to fetch|network|load failed|unreachable/i.test(error.message || "");

  return (
    <div style={{ padding: "2rem", maxWidth: 480 }}>
      <h2 style={{ margin: "0 0 8px", font: "var(--t-title)" }}>
        {network ? "Supymem lost its connection" : "This page did not load"}
      </h2>
      <p className="muted" style={{ marginBottom: 16 }}>
        {network
          ? "Your venture is safe. Check your internet connection, then try again."
          : "Your venture is safe. Try again. If it keeps happening, go back to your ventures and reopen this one."}
      </p>
      <button type="button" className="primary" onClick={() => reset()}>
        Try again
      </button>
      {" "}<a href="/">Back to my ventures</a>
    </div>
  );
}
