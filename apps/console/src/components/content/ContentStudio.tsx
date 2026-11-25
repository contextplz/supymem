"use client";

import { plainJob } from "@/lib/jobCopy";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "@/components/chat/ChatLink";
import { useSearchParams } from "next/navigation";
import {
  getContentBriefTemplate,
  getContentEvents,
  getContentPosts,
  getContentStatus,
  revokeContentAccount,
  runContentAgentTick,
  runContentTick,
  startContentOAuth,
  syncContentAccounts,
  forgetContentAccounts,
  opsApi,
  uploadContentBrief,
  type ContentAccount,
  type ContentEvent,
  type ContentPost,
  type ContentStatus,
} from "@/lib/opsApi";
import { contentRefreshFence, refreshContentViews } from "@/lib/contentCompletion";
import styles from "./ContentStudio.module.css";
import { ContentGenerationPanel } from "./ContentGenerationPanel";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { operationsHref } from "@/lib/operationsScope";
import { api } from "@/lib/client";
import { contentBriefFromCompany } from "@/lib/contentCompanyBrief";
import { diagnosisClient, type GtmVenture } from "@/lib/gtmDiagnosisApi";
import { opsErrorMessage } from "@/lib/errorCopy";
import { useIsOperator } from "@/lib/operatorSession";
import { useFeatureVisibility } from "@/components/layout/LocalModeBoundary";
import {
  briefForm, briefFromForm, briefGaps, channelName, friendlyBriefError, postStatusLabel, publishOffer,
  type BriefForm,
} from "@/lib/contentFounder";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type ContentExternalReview = {
  approval: { id: string; status: string };
  review: {
    channel: string;
    destination: string;
    title: string;
    body: string;
    visual_path: string;
    credit_status: string;
    credit_estimate_micro?: number | null;
    rate_version?: string | null;
  };
  execution?: { status: "succeeded" | "failed"; detail: string };
};

function when(iso: string | null): string {
  if (!iso) return "Not recorded";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function lintReason(reason: string): string {
  if (reason.startsWith("too_many_links")) return "Too many links for this channel.";
  if (reason === "unapproved_conversion_link") return "Use only the exact HTTPS website URL saved in the brief for a promotional LinkedIn link.";
  if (reason.startsWith("unregistered_claim")) return "A number in this draft is not supported by the brief.";
  if (reason.startsWith("too_long")) return "This draft is too long for the selected channel.";
  if (reason.startsWith("too_short")) return "This draft is too short for the selected channel.";
  return reason.replaceAll("_", " ");
}

function accountFor(
  accounts: ContentAccount[],
  platform: string
): ContentAccount | undefined {
  return accounts.find((a) => a.platform === platform && a.status !== "revoked");
}

export function ContentStudio() {
  const operationsScope = useOperationsScope();
  const operator = useIsOperator();
  const featureVisible = useFeatureVisibility();
  const posting = featureVisible("settings", "social");
  const [form, setForm] = useState<BriefForm | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [copied, setCopied] = useState("");
  const params = useSearchParams();
  const [generationIntent, setGenerationIntent] = useState<{ kind: "topics" | "draft"; platform?: string; nonce: number } | null>(null);
  const [status, setStatus] = useState<ContentStatus | null>(null);
  const [posts, setPosts] = useState<ContentPost[]>([]);
  const [events, setEvents] = useState<ContentEvent[]>([]);
  const [draft, setDraft] = useState("");
  const [briefErrors, setBriefErrors] = useState<string[]>([]);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [externalReviews, setExternalReviews] = useState<Record<string, ContentExternalReview>>({});
  const fileRef = useRef<HTMLInputElement | null>(null);
  const companyPlanAttempt = useRef("");
  const [editingPost, setEditingPost] = useState<{ id: string; body: string; title: string; digest: string } | null>(null);

  const refreshFence = useRef(contentRefreshFence());
  const refreshOwner = JSON.stringify([operationsScope?.actorId, operationsScope?.workspaceId, operationsScope?.ventureId]);
  useEffect(() => {
    const changed = () => refreshFence.current.invalidate();
    window.addEventListener("supymem-session-changed", changed);
    return () => { changed(); window.removeEventListener("supymem-session-changed", changed); };
  }, [refreshOwner]);

  const refresh = useCallback(async (opts?: { sync?: boolean; requireGenerated?: boolean }) => {
    const current = refreshFence.current.next();
    const failure = await refreshContentViews({
      status: getContentStatus({ sync: opts?.sync, ventureId: operationsScope?.ventureId }),
      posts: getContentPosts(20, operationsScope?.ventureId),
      events: getContentEvents(25),
    }, { status: setStatus, posts: setPosts, events: setEvents }, current, opts?.requireGenerated);
    if (current()) setError(failure ? (opsErrorMessage(failure, "Some content data could not be refreshed.")) : "");
  }, [refreshOwner, operationsScope?.ventureId]);
  const refreshGenerated = useCallback(() => refresh({ requireGenerated: true }), [refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Composio (and native OAuth) bounce back here. Sync links into this workspace.
  useEffect(() => {
    const connect = params.get("connect");
    const statusFlag = params.get("status");
    const returning =
      Boolean(connect) ||
      statusFlag === "success" ||
      Boolean(params.get("connectedAccountId")) ||
      Boolean(params.get("connected_account_id"));
    if (!returning) return;

    let cancelled = false;
    (async () => {
      try {
        const synced = await syncContentAccounts();
        if (cancelled) return;
        const handles = (synced.synced || [])
          .map((row) => String(row.handle || row.platform || ""))
          .filter(Boolean);
        if (params.get("error")) {
          setError(
            `${connect || "channel"} did not connect: ${params.get("error")}`
          );
        } else {
          setSaved(
            handles.length
              ? `Connected to this workspace: ${handles.join(", ")}.`
              : "Authorization finished. Linked channels appear once Composio marks them active."
          );
        }
        await refresh({ sync: true });
      } catch (err) {
        if (cancelled) return;
        setError(
          opsErrorMessage(err, "Connected in Composio, but binding to this workspace failed.")
        );
        await refresh();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [params, refresh]);

  const briefPayload = useCallback((brief: ContentStatus["brief"]) => brief ? {
    product: brief.product, icp: brief.icp, competitors: brief.competitors,
    voice: brief.voice, claims: brief.claims, cadence: brief.cadence,
  } : null, []);

  // Advanced JSON mirrors the saved brief until the operator starts typing.
  useEffect(() => {
    if (!status?.brief || draft) return;
    setDraft(JSON.stringify(briefPayload(status.brief), null, 2));
  }, [status, draft, briefPayload]);

  async function loadTemplate() {
    setError("");
    try {
      const template = await getContentBriefTemplate();
      setDraft(JSON.stringify(template, null, 2));
      setBriefErrors([]);
      setSaved("Template loaded. Replace the values with yours, then save.");
    } catch (err) {
      setError(opsErrorMessage(err, "Could not load the template"));
    }
  }

  const loadCompanyPlan = useCallback(async (opts?: { save?: boolean }) => {
    const ventureId = operationsScope?.ventureId;
    if (!ventureId) return;
    setError("");
    try {
      const [template, ventureResult, charterResult] = await Promise.all([
        getContentBriefTemplate(),
        api(`/api/ventures/${ventureId}`),
        api(`/api/ventures/${ventureId}/charter`),
      ]);
      if (!ventureResult.ok) throw new Error("Could not load this company’s saved idea.");
      const venture = ventureResult.data as GtmVenture;
      const charter = charterResult.ok
        ? ((charterResult.data as { content?: Record<string, unknown> })?.content || {})
        : {};
      const context = operationsScope?.actorId && operationsScope.workspaceId
        ? await diagnosisClient.context({
            actorId: operationsScope.actorId,
            workspaceId: operationsScope.workspaceId,
            ventureId,
          }, venture)
        : { problem: "", who: "" };
      const confirmed = {
        one_liner: typeof charter.one_liner === "string" ? charter.one_liner : "",
        mvp_scope: Array.isArray(charter.mvp_scope) || typeof charter.mvp_scope === "string" ? charter.mvp_scope : [],
        problem: typeof charter.problem === "string" && charter.problem.trim() ? charter.problem : context.problem,
        audience: typeof charter.audience === "string" && charter.audience.trim() ? charter.audience : context.who,
      };
      const built = contentBriefFromCompany(template, venture, confirmed);
      setDraft(JSON.stringify(built, null, 2));
      setBriefErrors([]);
      const next = briefForm(built);
      const gaps = briefGaps(next);
      if (opts?.save && gaps.length === 0) {
        const result = await uploadContentBrief(built, "company plan", ventureId);
        setSaved(`Brief built from your idea and plan (version ${result.version}). Check it below and edit anything that is off.`);
        await refresh();
      } else {
        setForm(next);
        setSaved(gaps.length
          ? `Your plan is missing ${gaps.join(", ")}. Fill that in and save.`
          : "Filled in from your plan. Review it and save to use it.");
      }
    } catch (err) {
      setError(opsErrorMessage(err, "Could not load the company plan"));
    }
  }, [operationsScope?.actorId, operationsScope?.ventureId, operationsScope?.workspaceId, refresh]);

  useEffect(() => {
    const ventureId = operationsScope?.ventureId;
    if (!ventureId || status === null || status.has_brief || draft || companyPlanAttempt.current === ventureId) return;
    companyPlanAttempt.current = ventureId;
    void loadCompanyPlan({ save: true });
  }, [draft, loadCompanyPlan, operationsScope?.ventureId, status]);

  async function onFile(file: File) {
    const text = await file.text();
    setDraft(text);
    setBriefErrors([]);
    setSaved(`Loaded ${file.name}. Review it, then save.`);
  }

  async function saveBrief() {
    setBusy(true);
    setError("");
    setSaved("");
    setBriefErrors([]);
    try {
      const parsed = JSON.parse(draft);
      if (!operationsScope?.ventureId) throw new Error("Choose a company before saving its content brief.");
      const result = await uploadContentBrief(parsed, "console", operationsScope.ventureId);
      setSaved(`Saved as version ${result.version}.`);
      await refresh();
    } catch (err) {
      if (err instanceof SyntaxError) {
        setBriefErrors([`That is not valid JSON: ${err.message}`]);
      } else {
        const message = err instanceof Error ? err.message : String(err);
        // The API returns a 422 with the full list of what is wrong.
        const match = message.match(/\{[\s\S]*\}/);
        if (match) {
          try {
            const detail = JSON.parse(match[0]);
            const list = detail?.detail?.errors || detail?.errors;
            if (Array.isArray(list)) {
              setBriefErrors(list.map(String));
              return;
            }
          } catch {
            /* fall through to the plain message */
          }
        }
        setBriefErrors([message]);
      }
    } finally {
      setBusy(false);
    }
  }

  function startEdit() {
    setForm(briefForm(status?.brief as unknown as Record<string, unknown> | null));
    setSaved("");
    setBriefErrors([]);
  }

  async function saveForm() {
    if (!form || !operationsScope?.ventureId) return;
    const gaps = briefGaps(form);
    if (gaps.length) { setBriefErrors([`Add ${gaps.join(", ")} first.`]); return; }
    setBusy(true); setError(""); setSaved(""); setBriefErrors([]);
    try {
      const base = briefPayload(status?.brief ?? null) ?? (draft ? JSON.parse(draft) : {});
      const result = await uploadContentBrief(briefFromForm(base, form), "console", operationsScope.ventureId);
      setSaved(`Brief saved (version ${result.version}). New drafts will use it.`);
      setDraft(""); setForm(null);
      await refresh();
    } catch (err) {
      setBriefErrors([friendlyBriefError(err instanceof Error ? err.message : String(err))]);
    } finally { setBusy(false); }
  }

  async function copyPost(id: string, body: string, title?: string | null) {
    try {
      await navigator.clipboard.writeText(title ? `${title}\n\n${body}` : body);
      setCopied(id);
      window.setTimeout(() => setCopied((c) => (c === id ? "" : c)), 2500);
    } catch {
      setError("Could not copy automatically. Select the text and copy it yourself.");
    }
  }

  function downloadPost(post: ContentPost) {
    let url = "";
    try {
      const markdown = post.handoff?.markdown ?? (post.title ? `# ${post.title}\n\n${post.body}\n` : `${post.body}\n`);
      url = URL.createObjectURL(new Blob([markdown], { type: "text/markdown;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = post.handoff?.filename ?? `${post.id}.md`;
      document.body.appendChild(anchor);
      try { anchor.click(); } finally { anchor.remove(); }
    } catch {
      setError("Your draft could not download. Use Copy draft in Content and save the text in your editor instead.");
    } finally {
      if (url) window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  async function connect(platform: string) {
    setError("");
    setBusy(true);
    try {
      const started = await startContentOAuth(platform);
      window.location.href = started.authorize_url;
    } catch (err) {
      setError(opsErrorMessage(err, `Could not start ${platform} OAuth`));
      setBusy(false);
    }
  }

  async function disconnect(account: ContentAccount) {
    setBusy(true);
    try {
      await revokeContentAccount(account.id);
      await refresh();
      setSaved(`Disconnected ${account.platform}.`);
    } catch (err) {
      setError(opsErrorMessage(err, "Could not disconnect"));
    } finally {
      setBusy(false);
    }
  }

  async function forgetAll() {
    setBusy(true);
    setError("");
    try {
      const result = await forgetContentAccounts();
      await refresh();
      setSaved(
        `Fresh start: removed ${result.revoked_accounts?.length ?? 0} channel(s) and ${result.composio_removed?.length ?? 0} Composio link(s).`
      );
    } catch (err) {
      setError(opsErrorMessage(err, "Could not reset channels"));
    } finally {
      setBusy(false);
    }
  }

  async function run(action: "tick" | "refill" | "agent", platform?: string) {
    if (action === "refill" || platform) {
      setGenerationIntent({ kind: platform ? "draft" : "topics", platform, nonce: Date.now() });
      return;
    }
    setBusy(true);
    setError("");
    setSaved("");
    try {
      if (action === "agent") {
        const res = await runContentAgentTick();
        const signals = Number(res.signals ?? 0);
        const actions = Array.isArray(res.actions) ? res.actions.length : 0;
        const ok = Array.isArray(res.results)
          ? res.results.filter((r) => (r as { ok?: boolean }).ok).length
          : 0;
        const reconnect = (
          (res.keepalive as { needs_reconnect?: unknown[] } | undefined)
            ?.needs_reconnect || []
        ) as unknown[];
        if (res.reason === "no_healthy_channels") {
          setSaved(
            reconnect.length
              ? `Agent paused, reconnect ${reconnect
                  .map((r) =>
                    typeof r === "object" && r && "platform" in r
                      ? String((r as { platform: string }).platform)
                      : "channel"
                  )
                  .join(", ")}.`
              : "Agent paused, no healthy channels. Connect LinkedIn/Reddit first."
          );
        } else {
          setSaved(
            `Agent cycle: ${signals} signal(s), ${actions} action(s), ${ok} ok` +
              (res.dry_run ? " (dry run)." : ".")
          );
        }
      } else {
        const res = await runContentTick();
        const drafted = Array.isArray(res.drafted) ? res.drafted.length : 0;
        const published = Array.isArray(res.published) ? res.published.length : 0;
        setSaved(`Tick done: ${drafted} drafted, ${published} publish attempt(s).`);
      }
      await refresh();
    } catch (err) {
      setError(opsErrorMessage(err, "That run failed"));
    } finally {
      setBusy(false);
    }
  }

  async function savePostEdit() {
    if (!editingPost || !operationsScope?.ventureId) return;
    setBusy(true); setError("");
    try {
      await opsApi.put(`/content/posts/${encodeURIComponent(editingPost.id)}/draft`, {
        venture_id: operationsScope.ventureId, body: editingPost.body, title: editingPost.title, expected_digest: editingPost.digest,
      });
      setEditingPost(null); setSaved("Draft saved. Its previous model review no longer applies; publication remains separate.");
      await refresh();
    } catch (err) { setError(opsErrorMessage(err, "Could not save the draft.")); }
    finally { setBusy(false); }
  }

  async function reviewPost(postId: string) {
    setBusy(true);
    setError("");
    try {
      const result = await opsApi.post<ContentExternalReview>(
        `/content/posts/${encodeURIComponent(postId)}/review`,
        {}
      );
      setExternalReviews((current) => ({ ...current, [postId]: result }));
      setSaved("Review the exact destination and content below before approving it.");
    } catch (err) {
      setError(opsErrorMessage(err, "Could not prepare the publication review"));
    } finally {
      setBusy(false);
    }
  }

  async function decidePostReview(postId: string, decision: "approved" | "rejected") {
    const current = externalReviews[postId];
    if (!current) return;
    setBusy(true);
    setError("");
    try {
      const approval = await opsApi.post<{ id: string; status: string }>(
        `/content/posts/${encodeURIComponent(postId)}/approval`,
        { approval_id: current.approval.id, decision }
      );
      setExternalReviews((reviews) => ({
        ...reviews,
        [postId]: { ...current, approval },
      }));
      if (decision === "approved") await executePostReview(postId, { ...current, approval });
    } catch (err) {
      setError(opsErrorMessage(err, "Could not record the publication decision"));
    } finally {
      setBusy(false);
    }
  }

  async function executePostReview(postId: string, current = externalReviews[postId]) {
    if (!current) return;
    setBusy(true);
    setError("");
    try {
      const result = await opsApi.post<{ ok: boolean; external_url?: string; reason?: string }>(
        `/content/posts/${encodeURIComponent(postId)}/execute`,
        { approval_id: current.approval.id }
      );
      const detail = result.ok
        ? `Published${result.external_url ? `: ${result.external_url}` : "."}`
        : result.reason || "Publication was not completed.";
      setExternalReviews((reviews) => ({
        ...reviews,
        [postId]: {
          ...current,
          execution: { status: result.ok ? "succeeded" : "failed", detail },
        },
      }));
    } catch (err) {
      setError(opsErrorMessage(err, "Could not execute this reviewed post"));
    } finally {
      setBusy(false);
    }
  }

  const providers = status?.providers || [];
  const accounts = status?.accounts || [];
  const agentOn = Boolean(status?.agent?.enabled);
  const replyPolicy = status?.agent?.policies;
  function replyState(platform: "linkedin" | "reddit"): string {
    if (!status) return "status unavailable";
    if (!agentOn) return "paused";
    if (status.dry_run) return "dry run only";
    if (!providers.find(provider => provider.platform === platform)?.configured) return "connection unavailable";
    if (accountFor(accounts, platform)?.status !== "active") return "account not connected";
    return replyPolicy?.[platform]?.can_reply === true ? "requires draft approval" : "unavailable";
  }

  return (
    <div className={styles.wrap}>
      <p className={styles.intro}>
        Turn your idea into post drafts. Nothing is posted anywhere until you approve it, and you can always copy a draft and post it yourself.
      </p>
      <ErrorNotice error={error} source="ContentStudio.error" className={styles.error} />
      {saved && <p className={styles.ok}>{saved}</p>}

      <section className={styles.card} aria-label="Content brief">
        <div className={styles.cardHead}>
          <h2>1. Your brief</h2>
          <p>Built from your confirmed idea and plan. Every draft is written from this, so check it says what you mean.</p>
        </div>
        {status === null ? (
          <p className={styles.muted}>Loading your brief…</p>
        ) : !status.brief && !form ? (
          <p className={styles.muted}>
            {operationsScope?.ventureId
              ? "Building your brief from your idea and plan…"
              : "Open Content from one of your companies and the brief will be built from its idea and plan."}
          </p>
        ) : null}
        {status?.brief && !form && (() => {
          const v = briefForm(status.brief as unknown as Record<string, unknown>);
          const rows: Array<[string, string]> = [
            ["Product", v.name], ["In one line", v.oneLiner], ["Who it is for", v.audience],
            ["Problems you solve", v.problems.replaceAll("\n", "; ")],
            ["What makes it different", v.differentiators.replaceAll("\n", "; ")],
            ["Tone of voice", v.tone],
          ];
          return (
            <dl className={styles.summary}>
              {rows.map(([label, value]) => (
                <div key={label}><dt>{label}</dt><dd>{value || <span className={styles.muted}>Not set</span>}</dd></div>
              ))}
            </dl>
          );
        })()}
        {form && (
          <div className={styles.form}>
            {([
              ["name", "Product name", "input"], ["oneLiner", "What it does, in one line", "input"],
              ["audience", "Who it is for", "input"], ["problems", "Problems they have (one per line)", "area"],
              ["differentiators", "What makes it different (one per line)", "area"], ["tone", "Tone of voice", "input"],
            ] as const).map(([key, label, kind]) => (
              <label key={key}>
                <span>{label}</span>
                {kind === "input"
                  ? <input value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
                  : <textarea rows={3} value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />}
              </label>
            ))}
            <div className={styles.row}>
              <button type="button" className={styles.btn} disabled={busy} onClick={() => void saveForm()}>Save brief</button>
              {status?.brief && (
                <button type="button" className={`${styles.btn} ${styles.btnGhost}`} disabled={busy}
                  onClick={() => { setForm(null); setBriefErrors([]); }}>Cancel</button>
              )}
            </div>
          </div>
        )}
        {briefErrors.length > 0 && (
          <ul className={styles.errors}>{briefErrors.map((message) => <li key={message}>{message}</li>)}</ul>
        )}
        {!form && (
          <div className={styles.row} style={{ marginTop: "0.7rem" }}>
            {status?.brief && <button type="button" className={styles.btn} disabled={busy} onClick={startEdit}>Edit brief</button>}
            <button type="button" className={`${styles.btn} ${styles.btnGhost}`}
              disabled={busy || !operationsScope?.ventureId}
              onClick={() => void loadCompanyPlan({ save: !status?.brief })}>
              Rebuild from my idea and plan
            </button>
            {!operationsScope?.ventureId && (
              <span className={styles.muted}>Open Content from one of your companies to use its plan.</span>
            )}
          </div>
        )}
        <details className={styles.advanced}>
          <summary>Advanced: brief as JSON</summary>
          <p className={styles.muted}>For pasting or exporting the whole brief, including claims and posting cadence.</p>
          <textarea className={styles.editor} value={draft} spellCheck={false} onChange={(e) => setDraft(e.target.value)} />
          <div className={styles.row} style={{ marginTop: "0.7rem" }}>
            <button type="button" className={styles.btn} disabled={busy || !draft.trim()} onClick={() => void saveBrief()}>Save JSON brief</button>
            <button type="button" className={`${styles.btn} ${styles.btnGhost}`} disabled={busy} onClick={() => fileRef.current?.click()}>Upload .json</button>
            <input ref={fileRef} type="file" accept="application/json,.json" hidden onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onFile(file);
              e.target.value = "";
            }} />
          </div>
        </details>
      </section>

      <ContentGenerationPanel
        founder
        briefId={status?.brief?.id}
        hasTopics={(status?.queued_topics ?? 0) > 0}
        intent={generationIntent}
        onComplete={refreshGenerated}
      />

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>2. Your drafts</h2>
          <p>Drafts written for your company. Copy one to post it yourself.</p>
        </div>
        {posts.length === 0 ? (
          <p className={styles.muted}>
            No drafts yet. Choose “Draft a post” above.
          </p>
        ) : (
          <div className={styles.list}>
            {posts.map((post) => (
              <div key={post.id} className={styles.post}>
                <div className={styles.postHead}>
                  <span
                    className={`${styles.pill} ${
                      post.state === "published"
                        ? styles.pillLive
                        : post.state === "refused" || post.state === "failed"
                          ? styles.pillWarn
                          : styles.pillOff
                    }`}
                  >
                    {postStatusLabel(post.state)}
                  </span>
                  <strong>{channelName(post.platform)}</strong>
                  {post.target && <span>r/{post.target}</span>}
                  <span>
                    {post.published_at
                      ? `published ${when(post.published_at)}`
                      : post.scheduled_for
                        ? `scheduled ${when(post.scheduled_for)}`
                        : when(post.created_at)}
                  </span>
                  {operator && post.dry_run && <span>dry run</span>}
                  {post.external_url && !post.dry_run && (
                    <a href={post.external_url} target="_blank" rel="noreferrer">
                      view
                    </a>
                  )}
                </div>
                {post.title && <strong>{post.title}</strong>}
                <p className={styles.postBody}>{post.body}</p>
                <div className={styles.row}>
                  <button type="button" data-usage-control="content_draft_copy" className={styles.btn}
                    onClick={() => void copyPost(post.id, post.body, post.title)}>
                    {copied === post.id ? "Copied" : "Copy draft"}
                  </button>
                  <button type="button" data-usage-control="content_draft_download" className={styles.btn}
                    onClick={() => downloadPost(post)}>
                    Download Markdown
                  </button>
                  {posting && !publishOffer(post.platform, accounts).canPublish && (
                    <span className={styles.muted}>{publishOffer(post.platform, accounts).hint}</span>
                  )}
                </div>
                {post.provenance === "legacy_unassigned" && <p className={styles.muted}>Legacy or manual workspace content · company attribution unassigned</p>}
                {post.handoff && <p className={styles.muted}>{post.handoff.next_step} {post.handoff.image_next_step}</p>}
                {post.generation_run_id && post.edit_digest && ["draft", "refused"].includes(post.state) && (
                  <button data-usage-control="content_draft_edit" className={`${styles.btn} ${styles.btnGhost}`} disabled={busy} onClick={() => setEditingPost({ id: post.id, body: post.body, title: post.title || "", digest: post.edit_digest! })}>Edit draft</button>
                )}
                {editingPost?.id === post.id && <div>
                  <input aria-label="Draft title" value={editingPost.title} maxLength={200} onChange={e => setEditingPost({ ...editingPost, title: e.target.value })} />
                  <textarea aria-label="Draft body" className={styles.editor} value={editingPost.body} maxLength={8000} onChange={e => setEditingPost({ ...editingPost, body: e.target.value })} />
                  <button data-usage-control="content_draft_save" className={styles.btn} disabled={busy} onClick={() => void savePostEdit()}>Save draft</button>
                  <button data-usage-control="content_draft_cancel" className={`${styles.btn} ${styles.btnGhost}`} onClick={() => setEditingPost(null)}>Cancel edit</button>
                </div>}
                {post.refusal_reason && (
                  <p className={styles.refusal}>This draft was not accepted: {post.refusal_reason.replaceAll("_", " ")}. Edit it or draft again.</p>
                )}
                {post.lint?.reasons?.length ? (
                  <p className={styles.refusal}>Checks to fix: {post.lint.reasons.map(lintReason).join(" ")}</p>
                ) : null}
                {operator && typeof post.critic?.score === "number" && (
                  <p className={styles.muted}>
                    critic {post.critic.score.toFixed(2)}
                    {post.critic.problems?.length
                      ? ` · ${post.critic.problems.join("; ")}`
                      : ""}
                  </p>
                )}
                {post.critic?.claim_checks && <details>
                  <summary>Model claim review</summary>
                  <p className={styles.muted}>These are model assessments against the supplied brief, not independent verification. Confirm product claims and limitations before publication.</p>
                  {post.critic.claim_checks.map((check, index) => <div key={index}>
                    <p><strong>{check.status}:</strong> {check.draft_quote}</p>
                    <p>{check.reason}</p>
                    {check.evidence.map((source, sourceIndex) => <p className={styles.muted} key={sourceIndex}>{source.source_id}: {source.quote}</p>)}
                  </div>)}
                </details>}
                {posting && (post.state === "scheduled" || post.state === "approved") && publishOffer(post.platform, accounts).canPublish && (
                  <div className={styles.row}>
                    <button
                      type="button"
                      className={`${styles.btn} ${styles.btnGhost}`}
                      disabled={busy}
                      onClick={() => void reviewPost(post.id)}
                    >
                      Review for publish
                    </button>
                  </div>
                )}
                {posting && externalReviews[post.id] && (
                  <div className={styles.externalReview}>
                    <strong>External publication review · {externalReviews[post.id].approval.status}</strong>
                    <span>Destination: {externalReviews[post.id].review.destination}</span>
                    {externalReviews[post.id].review.title && (
                      <span>Title: {externalReviews[post.id].review.title}</span>
                    )}
                    <pre className={styles.reviewBody}>{externalReviews[post.id].review.body}</pre>
                    {externalReviews[post.id].review.visual_path && (
                      <span>Visual: {externalReviews[post.id].review.visual_path}</span>
                    )}
                    {externalReviews[post.id].approval.status === "pending" && (
                      <div className={styles.row}>
                        <button
                          type="button"
                          className={styles.btn}
                          disabled={busy}
                          onClick={() => void decidePostReview(post.id, "approved")}
                        >
                          Publish this post
                        </button>
                        <button
                          type="button"
                          className={`${styles.btn} ${styles.btnDanger}`}
                          disabled={busy}
                          onClick={() => void decidePostReview(post.id, "rejected")}
                        >
                          Reject
                        </button>
                      </div>
                    )}
                    {externalReviews[post.id].approval.status === "approved" && !externalReviews[post.id].execution && (
                      externalReviews[post.id].review.credit_status === "configured" ? (
                        <button
                          type="button"
                          className={styles.btn}
                          disabled={busy}
                          onClick={() => void executePostReview(post.id)}
                        >
                          Publish reviewed post
                        </button>
                      ) : (
                        <span>Your publication connection needs attention. Open Settings, then retry publishing.</span>
                      )
                    )}
                    {externalReviews[post.id]?.execution && <span>{externalReviews[post.id]?.execution?.detail}</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {posting && <section className={styles.card} aria-label="Posting channels">
        <div className={styles.cardHead}>
          <h2>3. Posting channels</h2>
          <p>Optional. Without a connected channel you copy each draft and post it yourself.</p>
        </div>
        <div className={styles.grid}>
          {providers.filter((p) => ["linkedin", "x", "reddit"].includes(p.platform)).map((provider) => {
            const account = accountFor(accounts, provider.platform);
            const connected = account?.status === "active";
            return (
              <div key={provider.platform} className={styles.provider}>
                <div className={styles.providerHead}>
                  <strong>{provider.label}</strong>
                  <span className={`${styles.pill} ${connected ? styles.pillLive : styles.pillOff}`}>
                    {connected ? `Connected${account.handle ? ` as ${account.handle}` : ""}` : "Not connected"}
                  </span>
                </div>
                {!connected && !provider.configured && (
                  <p className={styles.muted}>Direct posting to {provider.label} is not available yet. Copy the draft and post it yourself.</p>
                )}
                {!connected && provider.configured && (
                  <p className={styles.muted}>Connect {provider.label} to post directly, or copy the draft.</p>
                )}
                {account?.last_error && <p data-dead-end="reconnect_shown" className={styles.refusal}>Reconnect {provider.label}: it stopped responding.</p>}
                <div className={styles.row}>
                  {provider.configured && (
                    <button type="button" className={styles.btn} data-usage-control="content_channel_connect"
                      disabled={busy} onClick={() => void connect(provider.platform)}>
                      {connected ? "Reconnect" : `Connect ${provider.label}`}
                    </button>
                  )}
                  {account && (
                    <button type="button" className={`${styles.btn} ${styles.btnDanger}`} data-usage-control="content_channel_disconnect"
                      disabled={busy} onClick={() => void disconnect(account)}>Disconnect</button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>}

      {operator && featureVisible("settings", "admin") && (
        <details className={styles.card}>
          <summary>Operator tools (not shown to founders)</summary>
      <div className={styles.banner}>
        <span
          className={`${styles.pill} ${agentOn ? styles.pillLive : styles.pillOff}`}
        >
          {agentOn ? "Social agent on" : "Social agent off"}
        </span>
        <Link href={operationsHref("/media", operationsScope)} className={styles.pill}>
          Media studio
        </Link>
        <span
          className={`${styles.pill} ${status?.dry_run ? styles.pillWarn : styles.pillLive}`}
        >
          {status?.dry_run ? "Dry run, no external action" : "Approval-gated outbound"}
        </span>
        <span
          className={`${styles.pill} ${
            status?.composio_configured ? styles.pillLive : styles.pillOff
          }`}
        >
          {status?.composio_configured ? "Composio ready" : "Composio off"}
        </span>
        <span className={styles.pill}>{status?.queued_topics ?? 0} angles queued</span>
        <span className={styles.pill}>
          Next slot {when(status?.next_scheduled ?? null)}
        </span>
        <div style={{ flex: 1 }} />
        <button
          type="button"
          data-usage-control="content_agent_cycle"
          className={styles.btn}
          disabled={busy || !agentOn}
          onClick={() => void run("agent")}
        >
          Run agent cycle
        </button>
      </div>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Content agent</h2>
          <p>
            {agentOn ? "The enabled agent checks connected channels and prepares content for review."
              : "The background content agent is off. Your brief and saved drafts remain available."}
          </p>
        </div>
        <p className={styles.caveat}>
          LinkedIn replies: {replyState("linkedin")} · Reddit replies: {replyState("reddit")}
        </p>
        <p className={styles.muted}>
          Activity records completed checks and errors. Reconnect expired channels before requesting more work. External posts require draft approval and configured delivery.
        </p>
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Channels</h2>
          <p>
            Connect the social accounts this company will use. Review drafts and
            their destinations before approving publication.
          </p>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnDanger}`}
            disabled={busy || accounts.length === 0}
            onClick={() => void forgetAll()}
            style={{ marginLeft: "auto" }}
          >
            Forget all connections
          </button>
        </div>
        <div className={styles.grid}>
          {providers.map((provider) => {
            const account = accountFor(accounts, provider.platform);
            const via =
              account?.auth_via ||
              provider.auth_via_default ||
              (provider.composio_managed ? "composio" : "native");
            return (
              <div key={provider.platform} className={styles.provider}>
                <div className={styles.providerHead}>
                  <strong>{provider.label}</strong>
                  <span
                    className={`${styles.pill} ${
                      account?.status === "active"
                        ? styles.pillLive
                        : provider.configured
                          ? styles.pillOff
                          : styles.pillWarn
                    }`}
                  >
                    {account?.status === "active"
                      ? account.handle || "connected"
                      : provider.configured
                        ? "not connected"
                        : "unavailable"}
                  </span>
                </div>

                {provider.caveat && <p className={styles.caveat}>{provider.caveat}</p>}

                {!provider.configured && (
                  <p className={styles.envHint}>
                    This channel connection is not available in this environment. You can still prepare and edit drafts.
                  </p>
                )}

                {account?.status === "active" && (
                  <p className={styles.caveat}>
                    via {via} · {account.posts_today}/{account.daily_cap} today ·
                    last posted {when(account.last_post_at)}
                  </p>
                )}
                {account?.last_error && (
                  <p className={styles.refusal}>{plainJob(account).body}</p>
                )}

                <div className={styles.row}>
                  <button
                    type="button"
                    className={styles.btn}
                    data-usage-control="content_channel_connect"
                    disabled={busy || !provider.configured}
                    onClick={() => void connect(provider.platform)}
                  >
                    {account?.status === "active" ? "Reconnect" : "Connect"}
                  </button>
                  {account && (
                    <button
                      type="button"
                      className={`${styles.btn} ${styles.btnDanger}`}
                      data-usage-control="content_channel_disconnect"
                      disabled={busy}
                      onClick={() => void disconnect(account)}
                    >
                      Disconnect
                    </button>
                  )}
                  {status?.has_brief && (
                    <button
                      type="button"
                      data-usage-control="content_draft_entry"
                      className={`${styles.btn} ${styles.btnGhost}`}
                      disabled={busy}
                      onClick={() => void run("tick", provider.platform)}
                    >
                      Draft one
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2>Activity</h2>
          <p>Recorded agent decisions and errors. Review the exact draft before approving an external post.</p>
        </div>
        {events.length === 0 ? (
          <p className={styles.muted}>No activity yet.</p>
        ) : (
          events.map((event) => (
            <div key={event.id} className={styles.eventRow}>
              <span className={styles.eventKind}>{event.kind}</span>
              <span>{event.platform || "Not recorded"}</span>
              <div style={{ flex: 1 }} />
              <span className={styles.muted}>{when(event.created_at)}</span>
            </div>
          ))
        )}
      </section>
        </details>
      )}
    </div>
  );
}
