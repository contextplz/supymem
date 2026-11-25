"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChatAnchor } from "@/components/chat/ChatLink";
import {
  createPausedMarketingAd,
  generateMarketingImage,
  generateMarketingVideo,
  recordMarketingDemo,
  getContentStatus,
  getMarketingAssets,
  getMarketingMediaJobs,
  getMediaCapabilities,
  getOpsWorkspace,
  probeMarketingBrowser,
  probeMarketingDesktop,
  reviewMarketingAsset,
  uploadMarketingAsset,
  type MarketingAsset,
  type MarketingJobRow,
  type MediaCapabilities,
} from "@/lib/opsApi";
import styles from "../content/ContentStudio.module.css";
import mediaStyles from "./MediaStudio.module.css";
import { ProtectedMarketingAsset } from "./ProtectedMarketingAsset";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

function capOk(value: unknown): boolean {
  if (value === true) return true;
  if (value && typeof value === "object" && "ok" in value) {
    return Boolean((value as { ok?: boolean }).ok);
  }
  return false;
}

function capNote(value: unknown): string {
  if (value && typeof value === "object") {
    const rec = value as { note?: string; error?: string; refused?: string };
    return rec.note || rec.error || rec.refused || "";
  }
  return "";
}

export function MediaStudio() {
  const [caps, setCaps] = useState<MediaCapabilities | null>(null);
  const [assets, setAssets] = useState<MarketingAsset[]>([]);
  const [jobs, setJobs] = useState<MarketingJobRow[]>([]);
  const [headline, setHeadline] = useState("");
  const [scene, setScene] = useState("");
  const [wantPerson, setWantPerson] = useState(false);
  const [seconds, setSeconds] = useState(12);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [last, setLast] = useState<Record<string, unknown> | null>(null);
  const [productUrl, setProductUrl] = useState("");
  const [copyFromBrief, setCopyFromBrief] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploadKind, setUploadKind] = useState<"logo" | "founder" | "product">(
    "founder"
  );

  const refresh = useCallback(async () => {
    const [c, a, j, status] = await Promise.all([
      getMediaCapabilities(),
      getMarketingAssets(),
      getMarketingMediaJobs(),
      getContentStatus().catch(() => null),
    ]);
    setCaps(c);
    setAssets(a);
    setJobs([...(j.jobs || [])].reverse());
    const product = (status?.brief?.product || {}) as Record<string, unknown>;
    const oneLiner = String(product.one_liner || "").trim();
    const diffs = Array.isArray(product.differentiators)
      ? product.differentiators.map(String)
      : [];
    const category = String(product.category || "").trim();
    if (oneLiner) {
      setHeadline((prev) => (prev.trim() ? prev : oneLiner));
      setCopyFromBrief(true);
    }
    if (category || diffs[0]) {
      const nextScene = [category, diffs[0]].filter(Boolean).join(". ");
      setScene((prev) => (prev.trim() ? prev : nextScene));
    }
    const url = String(product.url || "").trim();
    if (url && !url.includes("example.com")) {
      setProductUrl(url);
    }
  }, []);

  useEffect(() => {
    void refresh().catch((err) =>
      setError(err instanceof Error ? err.message : "Media API unreachable")
    );
  }, [refresh]);

  async function run(label: string, fn: () => Promise<Record<string, unknown>>) {
    setBusy(label);
    setError("");
    setSaved("");
    try {
      const result = await fn();
      setLast(result);
      if (result.ok) {
        setSaved(`${label} finished.`);
      } else {
        setError(
          String(
            result.block_reason ||
              result.error ||
              `${label} did not complete`
          )
        );
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function onUpload(file: File) {
    setBusy("upload");
    setError("");
    try {
      await uploadMarketingAsset(file, uploadKind);
      setSaved(`Saved ${file.name} as ${uploadKind}.`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  const adsHref = `/ads?workspace=${encodeURIComponent(getOpsWorkspace())}`;
  const executionAvailable = caps?.legacy_execution_available === true;

  const images = assets.filter((a) => a.kind === "generated_image");
  const videos = assets.filter((a) => a.kind === "generated_video");
  const kit = assets.filter((a) =>
    ["logo", "founder", "product"].includes(a.kind)
  );
  const hasProduct = kit.some((a) => a.kind === "product");
  const hasFounder = kit.some((a) => a.kind === "founder");
  const kitReady = wantPerson ? hasFounder : hasProduct || hasFounder;

  return (
    <div className={styles.wrap}>
      <section className={styles.banner}>
        <span
          className={`${styles.pill} ${
            caps ? styles.pillLive : styles.pillOff
          }`}
        >
          {caps?.execution_location === "hosted_server" ? "hosted server" : caps?.execution_location === "local_backend" ? "local backend" : "loading"}
        </span>
        <span>
          Real photos in, real pixels out. We will not invent a person or
          click ChatGPT.
        </span>
      </section>

      <ErrorNotice error={error} source="MediaStudio.error" className={styles.error} />
      {saved && <p className={styles.ok}>{saved}</p>}
      {!executionAvailable && <p role="status">{caps?.unavailable_reason || "Media execution is unavailable until the backend confirms its capabilities."} <ChatAnchor href={adsHref}>Open Ads</ChatAnchor></p>}

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Media execution environment</h2>
          <p>Capabilities refer to the connected backend, not this desktop app. Local package detection does not verify an installed browser or an authorized provider account.</p>
        </div>
        <div className={styles.grid}>
          {[
            ["Compositor", capOk(caps?.image?.compositor), "Pillow + grain + logo overlay"],
            ["ffmpeg", capOk(caps?.video?.ffmpeg), capNote(caps?.video?.ffmpeg)],
            ["ComfyUI", capOk(caps?.image?.comfyui), capNote(caps?.image?.comfyui)],
            ["Playwright", capOk(caps?.browser?.playwright), "isolated browser"],
            [
              "Product demo",
              capOk(caps?.video?.product_demo),
              capNote(caps?.video?.product_demo) || "Playwright + ffmpeg",
            ],
            [
              "Recordly GUI",
              capOk(caps?.video?.recordly),
              capNote(caps?.video?.recordly) || "AGPL desktop app, not in-process",
            ],
            ["CUA / Peekaboo", capOk(caps?.desktop?.cua) || capOk(caps?.desktop?.peekaboo), capNote(caps?.desktop)],
            ["Google Ads API", capOk(caps?.ads?.google), "always PAUSED"],
            ["Meta Ads API", capOk(caps?.ads?.meta), "always PAUSED"],
            ["Resolve Studio", capOk(caps?.video?.resolve), capNote(caps?.video?.resolve)],
          ].map(([label, ok, note]) => (
            <div key={String(label)} className={styles.provider}>
              <div className={styles.providerHead}>
                <strong>{label}</strong>
                <span
                  className={`${styles.pill} ${
                    ok ? styles.pillLive : styles.pillOff
                  }`}
                >
                  {ok ? "ready" : "needs you"}
                </span>
              </div>
              {note ? <p className={styles.caveat}>{String(note)}</p> : null}
            </div>
          ))}
        </div>
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Brand kit</h2>
          <p>
            Logos and founder photos are composited, never redrawn. Upload a
            real person before asking for a human in the frame.
          </p>
        </div>
        <div className={styles.row}>
          <select
            value={uploadKind}
            onChange={(e) =>
              setUploadKind(e.target.value as "logo" | "founder" | "product")
            }
            aria-label="Asset kind"
          >
            <option value="founder">Founder photo</option>
            <option value="logo">Logo</option>
            <option value="product">Product photo</option>
          </select>
          <button
            type="button"
            className={styles.btn}
            disabled={Boolean(busy)}
            onClick={() => fileRef.current?.click()}
          >
            Upload
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/svg+xml"
            hidden
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onUpload(file);
              e.target.value = "";
            }}
          />
        </div>
        {kit.length === 0 ? (
          <p className={styles.muted} style={{ marginTop: "0.8rem" }}>
            No assets yet. A still can still render as an empty documentary
            frame.
          </p>
        ) : (
          <div className={mediaStyles.gallery}>
            {kit.map((asset) => (
              <figure key={asset.id} className={mediaStyles.thumb}>
                <ProtectedMarketingAsset asset={asset} />
                <figcaption>
                  {asset.kind} · {asset.review_status}
                </figcaption>
              </figure>
            ))}
          </div>
        )}
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Make a still or a short clip</h2>
          <p>
            Local compositor on real uploads. ffmpeg Ken Burns for video. Human
            in frame needs a real founder photo, we will not draw one.
            {copyFromBrief ? " Copy is from the content brief." : ""}
          </p>
        </div>
        <label className={mediaStyles.label}>
          Headline
          <input
            value={headline}
            onChange={(e) => setHeadline(e.target.value)}
            placeholder="From the content brief once you save one"
          />
        </label>
        <label className={mediaStyles.label}>
          Scene
          <input
            value={scene}
            onChange={(e) => setScene(e.target.value)}
            placeholder="What the still is actually of"
          />
        </label>
        <div className={styles.row} style={{ marginTop: "0.6rem" }}>
          <label className={mediaStyles.check}>
            <input
              type="checkbox"
              checked={wantPerson}
              onChange={(e) => setWantPerson(e.target.checked)}
            />
            Human in the frame (requires founder photo)
          </label>
          <label className={mediaStyles.check}>
            Seconds
            <input
              type="number"
              min={8}
              max={30}
              value={seconds}
              onChange={(e) => setSeconds(Number(e.target.value) || 12)}
              style={{ width: "4rem" }}
            />
          </label>
        </div>
        <div className={styles.row} style={{ marginTop: "0.8rem" }}>
          <button
            type="button"
            className={styles.btn}
            disabled={!executionAvailable || Boolean(busy) || !kitReady || !headline.trim()}
            onClick={() =>
              void run("image", () =>
                generateMarketingImage({
                  headline,
                  scene,
                  want_person: wantPerson,
                })
              )
            }
          >
            {busy === "image" ? "Rendering…" : "Generate still"}
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={!executionAvailable || Boolean(busy) || !kitReady || !headline.trim()}
            onClick={() =>
              void run("video", () =>
                generateMarketingVideo({
                  headline,
                  scene,
                  seconds,
                  want_person: wantPerson,
                })
              )
            }
          >
            {busy === "video" ? "Encoding…" : "Generate clip"}
          </button>
        </div>
        {!kitReady ? (
          <p className={styles.muted} style={{ marginTop: "0.7rem" }}>
            {wantPerson
              ? "Upload a real founder photograph first. Drawings are rejected."
              : "Upload a real product screenshot first. We will not invent a desk or a face."}
          </p>
        ) : null}
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Product demo</h2>
          <p>
            Local backend mode can record a URL with Playwright and ffmpeg.
            Hosted recording is unavailable pending an isolated execution service.
            Recordly opens on the backend host, not through the desktop app.
          </p>
        </div>
        <label className={mediaStyles.label}>
          URL to record
          <input
            value={productUrl}
            onChange={(e) => setProductUrl(e.target.value)}
            placeholder="https://your-product.example"
          />
        </label>
        <div className={styles.row} style={{ marginTop: "0.8rem" }}>
          <button
            type="button"
            className={styles.btn}
            disabled={!executionAvailable || Boolean(busy) || !productUrl.trim()}
            onClick={() =>
              void run("demo", () =>
                recordMarketingDemo({
                  url: productUrl,
                  headline: headline || "Product demo",
                  seconds,
                })
              )
            }
          >
            {busy === "demo" ? "Recording…" : "Record walkthrough"}
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={!executionAvailable || Boolean(busy)}
            onClick={() =>
              void run("recordly", () =>
                recordMarketingDemo({ launch_recordly: true })
              )
            }
          >
            Open Recordly
          </button>
        </div>
        <p className={styles.muted} style={{ marginTop: "0.7rem" }}>
          Brand-kit uploads accept images, not MP4 files. Hosted video import is
          unavailable. In local backend mode, use the local recording workflow
          to import a Recordly export, then review it before publishing.
        </p>
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Gallery</h2>
          <p>Approve before anything is allowed to publish.</p>
        </div>
        {images.length + videos.length === 0 ? (
          <p className={styles.muted}>Nothing generated yet.</p>
        ) : (
          <div className={mediaStyles.gallery}>
            {images.map((asset) => (
              <figure key={asset.id} className={mediaStyles.thumb}>
                <ProtectedMarketingAsset asset={asset} />
                <figcaption>
                  {asset.source} · {asset.review_status}
                </figcaption>
                {asset.review_status !== "approved" && (
                  <button
                    type="button"
                    className={`${styles.btn} ${styles.btnGhost}`}
                    onClick={() =>
                      void reviewMarketingAsset(asset.id, "approved").then(
                        refresh
                      )
                    }
                  >
                    Approve
                  </button>
                )}
              </figure>
            ))}
            {videos.map((asset) => (
              <figure key={asset.id} className={mediaStyles.thumb}>
                <ProtectedMarketingAsset asset={asset} video />
                <figcaption>
                  {asset.duration_ms
                    ? `${Math.round(asset.duration_ms / 1000)}s`
                    : "clip"}{" "}
                  · {asset.review_status}
                </figcaption>
                {asset.review_status !== "approved" && (
                  <button
                    type="button"
                    className={`${styles.btn} ${styles.btnGhost}`}
                    onClick={() =>
                      void reviewMarketingAsset(asset.id, "approved").then(
                        refresh
                      )
                    }
                  >
                    Approve
                  </button>
                )}
              </figure>
            ))}
          </div>
        )}
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Browser, desktop, ads</h2>
          <p>These legacy controls operate on the backend host. Hosted actions are unavailable. Use <ChatAnchor href={adsHref}>Ads</ChatAnchor> for campaign proposals and approval.</p>
        </div>
        <div className={styles.row}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={!executionAvailable || Boolean(busy)}
            onClick={() =>
              void run("browser", () =>
                probeMarketingBrowser({ url: productUrl })
              )
            }
          >
            Probe browser
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={!executionAvailable || Boolean(busy)}
            onClick={() =>
              void run("desktop", () => probeMarketingDesktop({ app: "desktop" }))
            }
          >
            Probe desktop
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={!executionAvailable || Boolean(busy)}
            onClick={() =>
              void run("resolve", () =>
                probeMarketingDesktop({ app: "resolve" })
              )
            }
          >
            Probe Resolve
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={!executionAvailable || Boolean(busy)}
            onClick={() =>
              void run("ads-meta", () =>
                createPausedMarketingAd({
                  platform: "meta",
                  name: "Paused test · Meta",
                })
              )
            }
          >
            Paused Meta ad
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={!executionAvailable || Boolean(busy)}
            onClick={() =>
              void run("ads-google", () =>
                createPausedMarketingAd({
                  platform: "google",
                  name: "Paused test · Google",
                })
              )
            }
          >
            Paused Google ad
          </button>
        </div>
        {last && (
          <pre className={mediaStyles.dump}>
            {JSON.stringify(
              {
                ok: last.ok,
                status: last.status,
                error: last.error,
                block_reason: last.block_reason,
                result: last.result,
              },
              null,
              2
            )}
          </pre>
        )}
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Job log</h2>
          <p>Every click above writes a WorkOrder with proof or a NeedsYou reason.</p>
        </div>
        {jobs.length === 0 ? (
          <p className={styles.muted}>No media jobs yet.</p>
        ) : (
          jobs.map((job) => (
            <div key={job.id} className={styles.eventRow}>
              <span className={styles.eventKind}>{job.kind}</span>
              <span>{job.status}</span>
              <span>{job.title}</span>
              <div style={{ flex: 1 }} />
              <span className={styles.muted}>
                {job.block_reason || job.error_message || "ok"}
              </span>
            </div>
          ))
        )}
      </section>
    </div>
  );
}
