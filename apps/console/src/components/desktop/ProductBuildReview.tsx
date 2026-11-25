import { codingToolLabel, productBuildStatus, safeLoopText, type ProductBuildWatch } from "@/lib/loopProgress";
import { ChatAnchor } from "@/components/chat/ChatLink";
import { renderLightMd, splitFences } from "@/lib/chatMarkdown";

export type ProductLoopReview = { operation: string; provider: string; workspace_slug: string;
  profile: string; phase?: "plan" | "implement"; max_iterations: number; max_wall_seconds: number;
  per_call_seconds: number; max_turns?: number | null; plan_order_id?: string; plan_digest?: string };
export type ProductDeployReview = { operation: string; provider: "vercel"; target: "preview" | "production";
  workspace_slug: string; source_commit: string; file_manifest_sha256: string; source_order_id: string;
  project_link_sha256?: string };
export type ProductPlan = { spec: string; tasks: Array<{ id: string; title: string; files: string[];
  acceptance: Array<{ test_file: string; test_name: string; behaviour: string }> }>; plan_digest: string };
export type ProductScope = { in_scope: string[]; not_in_this_build: string[]; reasons?: string[] };
export type ProductDeployReceipt = { status: "deployed" | "protected" | "unverified" | "failed";
  target: "preview" | "production"; url?: string; exit_status: number; project_link_sha256?: string;
  source_commit?: string; file_manifest_sha256?: string;
  availability_status?: "available" | "protected" | "unavailable";
  functional_status?: "verified" | "failed" | "unavailable" | "not_verified_on_live"; http_journey?: unknown };
export type ProductPreviewReceipt = { status: "ready" | "unavailable" | "failed" | "expired" | "stopped";
  ready: boolean; url?: string; source_commit?: string; file_manifest_sha256?: string;
  expires_at?: number; stop_available?: boolean; transport?: "device_order_v1" };

function previewBound(preview?: ProductPreviewReceipt | null) {
  return !!preview?.source_commit && /^[a-f0-9]{40}$/.test(preview.source_commit)
    && !!preview.file_manifest_sha256 && /^[a-f0-9]{64}$/.test(preview.file_manifest_sha256);
}

export function productPreviewUrl(preview?: ProductPreviewReceipt | null) {
  const port = preview?.url && /^http:\/\/127\.0\.0\.1:([0-9]{1,5})\/?$/.exec(preview.url)?.[1];
  return preview?.status === "ready" && preview.ready && previewBound(preview)
    && port && Number(port) >= 1 && Number(port) <= 65535 && typeof preview.expires_at === "number"
    && Number.isFinite(preview.expires_at) && preview.expires_at > Date.now() / 1000
    && preview.stop_available === true ? preview.url : undefined;
}

function ProductSpec({ spec }: { spec: string }) {
  const prose = spec.replace(/^(?:#{1,6}[ \t]+[^\r\n]+\r?\n(?:[ \t]*\r?\n)*)? {0,3}```([^\n`]*)\n[\s\S]*?(?:^ {0,3}```[ \t]*\r?$|(?![\s\S]))/gm,
    (block, language: string) => /^(?:http-journey|json)$/i.test(language.trim()) ? "" : block);
  return <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
    {splitFences(prose).map((segment, index) => segment.code ? <pre key={index}><code>{segment.text}</code></pre>
      : <div key={index}>{segment.text.split(/^(#{1,6})[ \t]+(.+)$/m).map((part, section, parts) => {
        if (section % 3 === 1 || !part.trim()) return null;
        if (section % 3 === 0) return <div key={section} dangerouslySetInnerHTML={{ __html: renderLightMd(part.trim()) }} />;
        const Heading = (["h3", "h4", "h5", "h6", "h6", "h6"] as const)[parts[section - 1].length - 1];
        return <Heading key={section} dangerouslySetInnerHTML={{ __html: renderLightMd(part.trim()) }} />;
      })}</div>)}
  </div>;
}

export function ProductBuildReview({ loop, deploy, scope, plan, receipt, preview, onStopPreview, onRetryPreview,
  local = false, watch, busy = false, onStopBuild, onRetryBuild, onStartBuild, onRevealFolder,
  inlineStage, previewHref, exportHref, reviewHref, browserPreview = false, checksPassed }: {
  loop?: ProductLoopReview; deploy?: ProductDeployReview; scope?: ProductScope;
  plan?: ProductPlan | null; receipt?: ProductDeployReceipt | null;
  preview?: ProductPreviewReceipt | null; onStopPreview?: () => void; onRetryPreview?: () => void;
  local?: boolean; watch?: ProductBuildWatch; busy?: boolean; onStopBuild?: () => void;
  onRetryBuild?: () => void; onStartBuild?: () => void; onRevealFolder?: () => void;
  inlineStage?: string; previewHref?: string; exportHref?: string; reviewHref?: string;
  browserPreview?: boolean; checksPassed?: number;
}) {
  const product = !!loop && (local || loop.profile === "web_app_vercel_v1");
  const utility = loop?.profile === "workspace_utility_v1";
  if (!product && !deploy) return null;
  const watchedStatus = productBuildStatus(watch);
  const status = ["ready", "running"].includes(watchedStatus)
    ? inlineStage === "Needs you" ? "failed" : inlineStage === "Stopped" ? "stopped"
      : inlineStage === "Ready" ? "finished" : watchedStatus : watchedStatus;
  const remaining = status === "running" && typeof watch?.build_deadline_at === "number" && Number.isFinite(watch.build_deadline_at)
    ? Math.max(0, Math.ceil(watch.build_deadline_at - Date.now() / 1000)) : null;
  const previewUrl = productPreviewUrl(preview);
  const showPreview = loop?.profile === "web_app_vercel_v1" && loop.phase === "implement";
  const statusCopy = {
    ready: onStartBuild ? "Your build is ready to start. Choose Start build to run your requested work on this laptop."
      : "This saved build has not started. Open your venture chat and ask Charlie to build your app with your connected coding tool.",
    running: "Your coding tool is running. Watch the activity below, or choose Stop build.",
    stopping: "You asked to stop. Wait here while your tool stops; your saved files will stay on this laptop.",
    stop_unconfirmed: "Your tool has not confirmed Stop. Choose Check Stop before retrying or starting another build.",
    stopped: "Your coding tool stopped. Open your result folder to check the saved work, or choose Retry build.",
    cancelled: "Your build request was cancelled. Choose Retry build when you want to start a new build.",
    failed: "Your build needs another try. Open your result folder to check the saved work, then choose Retry build.",
    finished: loop?.phase === "plan" ? "Your build plan is finished. Open your venture chat and ask Charlie to run it with your coding tool."
      : utility ? "Your files passed the workspace checks. Open your result folder to review them and your tool's test notes."
      : "Your build is finished. Nothing is published.",
    missing: "Your coding tool is missing. In Settings > Keys and connections > Coding tools, follow its installation steps, then check the connection and retry your build.",
    signed_out: "Your coding tool is signed out. In Settings > Keys and connections > Coding tools, sign in with your own login, then check the connection and retry your build.",
    disconnected: "Your laptop is not connected. Open Settings > Keys and connections > Coding tools, reconnect your tool, then retry your build.",
  };
  const phase = deploy ? 3 : loop?.phase === "implement" ? 2 : 1;
  const safeUrl = receipt?.url && /^https:\/\/[^\s/@]+(?:\/[^\s]*)?$/.test(receipt.url)
    ? receipt.url : null;
  const previewExpired = preview?.expires_at != null && preview.expires_at <= Date.now() / 1000;
  if (local && inlineStage) {
    const safeResultHref = (href?: string) => href && /^\/api\/ventures\/[^/]+\/(?:pagebuilder\/jobs\/[^/]+\/(?:preview|source)(?:\?variant=[0-2])?|device-work-orders\/[^/]+\/source-archive)$/.test(href) ? href : undefined;
    const pagePreview = safeResultHref(previewHref), source = safeResultHref(exportHref);
    const retryStep = loop?.profile === "static_landing_v1" ? "Ask Charlie “Show my landing page”, then choose Retry for your stopped build."
      : reviewHref ? "Choose Watch your tool in your desktop app, then Retry build."
        : utility ? "Open your venture chat and resend your code request."
        : "In your venture chat, ask Charlie “Build my app with my coding tool” to start a new build.";
    const inlineCopy = status === "failed" ? `Your build needs another try. ${retryStep}`
      : status === "stopped" ? `Your coding tool stopped. ${retryStep}`
        : status === "cancelled" ? `Your build request was cancelled. ${retryStep}` : statusCopy[status];
    return <section aria-label="Connected tool build" aria-busy={busy}>
      <h3>{codingToolLabel(loop?.provider || "")}</h3>
      <p role="status" aria-live="polite"><strong>{utility && inlineStage === "Checking the page" ? "Checking your files" : inlineStage}</strong></p>
      <p>{status === "ready" ? "Your requested build is getting ready. Keep this card open to watch your tool." : inlineCopy}</p>
      {remaining != null && inlineStage !== "Checking the page" && <p role="timer">Time left for your tool: {Math.floor(remaining / 60)}m {remaining % 60}s.</p>}
      {["missing", "signed_out", "disconnected"].includes(status) && <a href="/settings/local">Open Keys and connections</a>}
      {onStopBuild && !["finished", "stopped", "cancelled", "failed"].includes(status) && <button type="button"
        disabled={busy || status === "stopping"} onClick={onStopBuild}>
        {status === "stop_unconfirmed" ? "Check Stop" : status === "stopping" ? "Stopping…" : "Stop"}</button>}
      <div style={{display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8}}>
      {(pagePreview || previewUrl) && <ChatAnchor href={pagePreview || previewUrl} target="_blank" rel="noopener noreferrer">Open preview</ChatAnchor>}
      {onRevealFolder && <button type="button" disabled={busy} onClick={onRevealFolder}>Open folder</button>}
      {source && <ChatAnchor href={source} download>Export</ChatAnchor>}
      </div>
    </section>;
  }
  const previewStatus = preview?.status === "ready" && previewExpired ? "expired" : preview?.status === "ready" && !previewUrl
    ? "unavailable" : preview?.status || "unavailable";
  const spec = plan && <><ProductSpec spec={plan.spec} />
    <h3>Tasks and acceptance checks</h3><ol>{plan.tasks.map(task => <li key={task.id}>
      <strong>{safeLoopText(task.title)}</strong>
      <ul>{task.acceptance.map((check, index) => <li key={index}>{safeLoopText(check.behaviour, 500)}</li>)}</ul>
    </li>)}</ol></>;
  const finishedBuild = local && status === "finished" && loop?.phase !== "plan";
  return <section aria-label={utility ? "Your code changes" : "Build your app"}>
    {local && <>
      <h2>{codingToolLabel(loop?.provider || "")} on your laptop</h2>
      <p role="status" aria-live="polite">{statusCopy[status]}</p>
      {!finishedBuild && <p>You use your own coding-tool login. {utility ? "Your work stays in this venture's folder."
        : "Your work stays in this venture's folder until you choose to publish it."}</p>}
      {["missing", "signed_out", "disconnected"].includes(status) && <a href="/settings/local">Open Keys and connections</a>}
      {status === "ready" && !onStartBuild && <a href="/settings/local">Check your coding-tool connection</a>}
      {status === "ready" && onStartBuild && <button type="button" disabled={busy} onClick={onStartBuild}>Start build</button>}
      {["running", "stopping", "stop_unconfirmed"].includes(status) && <button type="button" disabled={busy && status !== "running" || status === "stopping" || !onStopBuild}
        onClick={onStopBuild}>{status === "stopping" ? "Stopping build…" : status === "stop_unconfirmed" ? "Check Stop" : "Stop build"}</button>}
      {["failed", "stopped", "cancelled", "missing", "signed_out", "disconnected"].includes(status) && onRetryBuild && <button type="button" disabled={busy} onClick={onRetryBuild}>Retry build</button>}
      <div style={{display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8}}>
        {showPreview && (previewUrl ? <a href={previewUrl}>Open preview</a>
          : preview?.stop_available !== true && onRetryPreview && <button type="button" disabled={busy} onClick={onRetryPreview}>Open preview</button>)}
        {onRevealFolder && <button type="button" disabled={busy} onClick={onRevealFolder}>Open result folder</button>}
        {showPreview && preview?.stop_available === true && <button type="button" disabled={busy || !onStopPreview} onClick={onStopPreview}>Stop preview</button>}
      </div>
      {showPreview && browserPreview && <p>In Terminal, open your result folder and run <code>PORT=4173 HOST=127.0.0.1 node dev-server.mjs</code>.
        Then open <a href="http://127.0.0.1:4173" target="_blank" rel="noopener noreferrer">http://127.0.0.1:4173</a>. Press Ctrl+C in Terminal to stop.</p>}
      {showPreview && previewStatus === "failed" && preview?.stop_available === true && <p>Your preview has not stopped yet. Choose Stop preview again before reopening it.</p>}
    </>}
    {!local && <><ol aria-label="Separate build approvals">
      {["Build plan", "Run the coding loop", "Deploy to your account"].map((label, index) =>
        <li key={label} aria-current={phase === index + 1 ? "step" : undefined}>{label}</li>)}
    </ol>
    <h2>{deploy ? `Review ${deploy.target === "production" ? "production" : "preview"} deployment`
      : loop?.phase === "implement" ? "Review the coding run" : "Review the build plan task"}</h2>
    {loop && <>
      <p>Uses your {codingToolLabel(loop.provider)} account on this Mac.
        Supymem makes no hosted model calls for this task. Only your venture folder can be changed.</p>
      <p>{loop.phase === "plan" ? "Writes a product spec and task list for you to review. It does not start coding the app."
        : `Up to ${loop.max_iterations} tries, at most ${Math.ceil(loop.max_wall_seconds / 60)} minutes.
          Model-run commands have no internet access. Local tests may use only this Mac's loopback connection.
          You can stop at any time.`}</p>
      <p>Each coding call is limited to {Math.ceil(loop.per_call_seconds / 60)} minutes.
        {loop.max_turns != null ? ` Claude Code is limited to ${loop.max_turns} turns per call.`
          : " Codex is bounded by the time limits; it has no reviewed turn limit."}</p>
    </>}</>}
    {finishedBuild && <section aria-label="Build summary">
      <h3>What you built</h3>
      <p>{(scope?.in_scope.length ? scope.in_scope : plan?.tasks.map(task => task.title))?.slice(0, 3)
        .map(text => safeLoopText(text)).join(" · ") || "Your coding tool saved the finished work in your result folder."}</p>
      <p>{checksPassed ? `Tests passed: ${checksPassed}/${checksPassed}` : "Your build passed its checks."}</p>
    </section>}
    {!finishedBuild && scope && <>
      <h3>In this build</h3><ul>{scope.in_scope.map((item, index) => <li key={index}>{safeLoopText(item)}</li>)}</ul>
      {scope.not_in_this_build.length > 0 && <><h3>Not in this build</h3>
        <ul>{scope.not_in_this_build.map((item, index) => <li key={index}>{safeLoopText(item)}</li>)}</ul></>}
    </>}
    {spec && (local ? <details><summary>Your product spec</summary>{spec}</details> : <section aria-label="Proposed product plan">
      <h3>Your product spec</h3>{spec}
      <p>Approving this plan opens a separate coding review. Coding starts only after you approve and start that run.</p>
    </section>)}
    {!local && product && showPreview && <section aria-label="Your local app preview">
      <h3>Try your app on this Mac</h3>
      <p role="status">{previewUrl ? "Your saved app is ready to try on this Mac. This does not deploy it."
        : previewStatus === "failed" && preview?.stop_available === true ? "Your preview has not stopped cleanly. Try stopping it again before starting another preview."
        : previewStatus === "failed" ? "Your preview could not start. Your saved app has not been deployed."
        : previewStatus === "expired" ? "Your preview has expired or your saved app changed. Start a new preview before trying it."
        : previewStatus === "stopped" ? "You stopped your preview. Your saved app has not been deployed."
        : "Your local preview is not available in this review. You can still review a Vercel preview separately."}</p>
      {previewBound(preview) && <p>Your saved version: <code aria-label="Saved version identifier">{preview?.file_manifest_sha256}</code></p>}
      {previewUrl && <><a href={previewUrl} target="_blank" rel="noopener noreferrer">Open your local app</a>{" "}</>}
      {preview?.stop_available === true && <button type="button" onClick={onStopPreview} disabled={!onStopPreview}>Stop preview</button>}
      {!previewUrl && preview?.stop_available !== true && onRetryPreview && <button type="button" onClick={onRetryPreview}>Try your preview again</button>}
      <p>You still review any Vercel deployment separately.</p>
    </section>}
    {deploy && <>
      <p>Send the checked, saved version to your Vercel account as a {deploy.target}.
        {deploy.project_link_sha256 ? " Uses the same linked Vercel project as your approved preview."
          : ` This may create a project called ${deploy.workspace_slug} in your account.`}
        Supymem does not create an account or pay Vercel. Your Vercel plan and settings apply.</p>
      <p>Only the approved app files are uploaded. Review notes, tests, Git files and environment files are excluded.
        A preview does not grant permission to deploy production.</p>
    </>}
    {receipt && <p role="status">{receipt.status === "protected"
      ? "Deployed, protected by your Vercel settings."
      : receipt.status === "deployed" && receipt.functional_status === "verified" ? "Your deployed app returned the results in your approved plan."
      : receipt.status === "deployed" ? "The deployed URL answered the availability check."
      : "Deployment was not verified. Check your Vercel account before retrying."}
      {receipt.status === "protected" ? " Your Vercel settings block app checks. This does not confirm your app works."
        : receipt.status === "deployed" && receipt.functional_status === "not_verified_on_live" ? " Your app journey was not verified on the live deployment. Its checks stay in your local preview."
        : receipt.status === "deployed" && receipt.functional_status === "failed" ? " Your app did not return the results in your approved plan."
        : receipt.status === "deployed" && receipt.functional_status !== "verified" ? " Your app behavior has not been checked." : null}
      {safeUrl && <> <a href={safeUrl} target="_blank" rel="noopener noreferrer">Open deployed app</a></>}</p>}
    {!local && <details><summary>Deploy it yourself with Vercel</summary><ol>
      <li>Install the Vercel CLI if needed.</li><li>In Terminal, run <code>vercel login</code>.</li>
      <li>In your project folder, run <code>vercel deploy --target=preview</code>.</li><li>Check the link it prints.</li>
    </ol><p>This checklist does not run a command or grant permission.</p></details>}
  </section>;
}
