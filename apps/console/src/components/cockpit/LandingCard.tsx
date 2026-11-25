"use client";

import { ChatAnchor } from "@/components/chat/ChatLink";

import { useState } from "react";
import type { WebsiteTileData } from "@/lib/api-types";
import { ProvenanceChip } from "@/components/primitives/ProvenanceChip";
import { EmptyState } from "@/components/primitives/EmptyState";
import { landingPreviewKind, isReachableLandingUrl } from "@/lib/format";
import { LandingPreviewFrame } from "./LandingPreviewFrame";
import styles from "./cockpit.module.css";

export function LandingCard({
  data,
  ventureId,
  pausedReason,
  building,
  failed,
  onBuildInCloud: _onBuildInCloud,
  onReconnect,
}: {
  data?: WebsiteTileData;
  ventureId: string;
  pausedReason?: string | null;
  building?: boolean;
  failed?: string | null;
  onBuildInCloud?: () => void;
  onReconnect?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const url = data?.url;
  const screenshot = data?.screenshot_url;
  const screenshotIsImage =
    typeof screenshot === "string" &&
    !/^https?:\/\//i.test(screenshot) &&
    !screenshot.startsWith("http");
  const reachable = isReachableLandingUrl(url);
  const kind = landingPreviewKind({
    failed: reachable ? null : failed,
    pausedReason,
    building,
    empty: data?.state === "empty" || (!reachable && !screenshotIsImage && !data?.url),
    reachable,
  });
  const canIframe = reachable;

  async function copyUrl() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className={styles.card} aria-labelledby="landing-heading">
      <div className={styles.sectionHeader}>
        <h2 id="landing-heading">Landing page</h2>
        <ProvenanceChip mode={!data || data.state === "empty" ? "no_data" : data.simulated ? "simulated" : "live"} />
      </div>
      {kind === "failed" ? (
        <EmptyState
          title="The landing page did not start"
          body={
            failed ||
            "The local builder never started. Nothing has been written yet. Reconnect Cursor / Claude / Codex. Cloud builder is not open yet."
          }
          action={
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
              {onReconnect ? (
                <button type="button" className="primary" onClick={onReconnect}>
                  Reconnect tools
                </button>
              ) : null}
            </div>
          }
        />
      ) : kind === "building" ? (
        <EmptyState
          title="Landing page is still getting built"
          body="Cole is building it now. This page will update when there is a real preview, not a blank frame."
        />
      ) : !data || (!url && !screenshot) ? (
        <EmptyState
          title="Landing page is still getting built"
          body="The first real preview shows up here after the builder finishes. We will not show a blank page in the meantime."
        />
      ) : (
        <>
          <div className={styles.preview}>
            {canIframe && url ? (
              <LandingPreviewFrame src={url} ventureId={ventureId} />
            ) : screenshotIsImage && screenshot ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={screenshot} alt="Landing page preview" />
            ) : screenshot && isReachableLandingUrl(screenshot) ? (
              <LandingPreviewFrame src={screenshot} ventureId={ventureId} />
            ) : (
              <p className={styles.empty}>Landing page is still getting built.</p>
            )}
          </div>
          {data.simulated && data.published ? (
            <p className={styles.detail}>
              This preview is not on the public internet yet. Your custom domain stays yours.
            </p>
          ) : null}
          {url ? (
            <div className={styles.urlRow}>
              <p className={styles.url}>{url}</p>
              <button type="button" className="ghost sm" onClick={() => void copyUrl()}>
                {copied ? "Copied" : "Copy"}
              </button>
              <ChatAnchor href={url} target="_blank" rel="noreferrer" className="primary sm">
                Open
              </ChatAnchor>
            </div>
          ) : null}
        </>
      )}
      <ChatAnchor href={`/v/${ventureId}/site`} className="ghost sm">
        Full site page
      </ChatAnchor>
    </section>
  );
}
