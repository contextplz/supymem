import { parseBrowserExecutionProfile } from "@/lib/browserExecutionProfile";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { isLocalConsole } from "../../lib/localMode";

type QualificationRule = {rule_id: string; method: "GET" | "HEAD" | "POST"; path: string; query: string;
  max_requests: number; max_commits: number; rejected_retry_limit?: number;
  fields: {name: string; value: string | number | boolean | string[]; input_value?: string | null}[]};
type QualificationReview = {trial_id: string; task_id: string; catalog_digest: string; seed_digest: string;
  review_digest: string; profile_digest: string; rules?: QualificationRule[]; allowed_operations?: string[];
  max_tabs?: number; frames?: unknown[]; downloads?: {filename: string; max_bytes: number; sha256: string}[]};

function qualificationReview(value: unknown, grant: boolean): QualificationReview | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const common = ["schema_version", "actor_id", "device_id", "workspace_id", "venture_id", "trial_id", "catalog_digest",
    "task_id", "seed_digest", "order_id", "review_digest", "profile_digest", "expires_at"];
  const required = [...common, ...(grant ? ["nonce", "origin", "rules", "grant_digest"] : ["binding_digest"])];
  const optional = grant ? ["allowed_operations", "max_tabs", "frames", "downloads"] : [];
  const hash = (x: unknown) => typeof x === "string" && /^[a-f0-9]{64}$/.test(x);
  const count = (x: unknown, min: number, max: number) => Number.isSafeInteger(x) && Number(x) >= min && Number(x) <= max;
  if (!required.every(key => Object.hasOwn(v, key)) || !Object.keys(v).every(key => [...required, ...optional].includes(key))
    || v.schema_version !== 1 || !["actor_id", "device_id", "workspace_id", "venture_id", "trial_id", "order_id", "task_id"].every(key =>
      typeof v[key] === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(v[key] as string))
    || !["catalog_digest", "seed_digest", "review_digest", "profile_digest", grant ? "grant_digest" : "binding_digest"].every(key => hash(v[key]))
    || !count(v.expires_at, 1, 32_503_680_000)) return null;
  if (grant && (v.origin !== "https://api.supymem.com" || typeof v.nonce !== "string" || !/^[A-Za-z0-9_-]{32,128}$/.test(v.nonce)
    || !Array.isArray(v.rules) || !count(v.rules.length, 1, 128) || !v.rules.every(rule => {
      if (!rule || typeof rule !== "object" || Array.isArray(rule)) return false;
      const r = rule as Record<string, unknown>;
      return typeof r.rule_id === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(r.rule_id)
        && ["GET", "HEAD", "POST"].includes(String(r.method)) && typeof r.path === "string"
        && r.path.startsWith(`/public/browser-qualification/${v.nonce}/`) && typeof r.query === "string"
        && count(r.max_requests, 1, 128) && count(r.max_commits, 0, 16) && Number(r.max_commits) <= Number(r.max_requests)
        && (r.rejected_retry_limit === undefined || count(r.rejected_retry_limit, 0, 1))
        && Array.isArray(r.fields) && r.fields.length <= 16 && r.fields.every(field => {
          if (!field || typeof field !== "object" || Array.isArray(field)) return false;
          const f = field as Record<string, unknown>;
          return typeof f.name === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(f.name)
            && (typeof f.value === "string" || typeof f.value === "boolean" || Number.isSafeInteger(f.value)
              || Array.isArray(f.value) && f.value.every(x => typeof x === "string"))
            && (f.input_value == null || typeof f.input_value === "string" && f.input_value.length <= 600);
        });
    }) || (v.allowed_operations !== undefined && (!Array.isArray(v.allowed_operations) || !v.allowed_operations.every(x =>
      typeof x === "string" && ["click", "type", "wait", "navigate", "scroll", "extract", "select", "check", "new_tab", "switch_tab", "close_tab", "download", "hover"].includes(x))))
    || (v.max_tabs !== undefined && !count(v.max_tabs, 1, 4))
    || (v.frames !== undefined && (!Array.isArray(v.frames) || v.frames.length > 2))
    || (v.downloads !== undefined && (!Array.isArray(v.downloads) || v.downloads.length > 1 || !v.downloads.every(d =>
      !!d && typeof d.filename === "string" && /^[A-Za-z0-9_-]{1,80}\.csv$/.test(d.filename)
      && count(d.max_bytes, 1, 1_048_576) && hash(d.sha256)))))) return null;
  return value as QualificationReview;
}

export function BrowserExecutionProfileReview({ value, className, qualificationGrant, trialBinding }: {
  value: unknown; className?: string; qualificationGrant?: unknown; trialBinding?: unknown;
}) {
  const local = isLocalConsole();
  if (value === undefined || value === null) return <p>This saved browser task uses the legacy decision profile. It does not select the new OpenAI browser profile.</p>;
  const profile = parseBrowserExecutionProfile(value);
  if (!profile) return <ErrorNotice error="This saved browser profile is unsupported or incomplete. Approval and execution are unavailable; prepare a new review." source="device-work-order.browser-profile" />;
  const qualifier = qualificationGrant != null ? qualificationReview(qualificationGrant, true)
    : trialBinding != null ? qualificationReview(trialBinding, false) : undefined;
  return <section aria-label="Saved browser AI profile">
    <h2>Browser model and data review</h2>
    <dl className={className}>
      <dt>Processing service</dt><dd>OpenAI text responses</dd>
      {(["plan", "actor", "verify"] as const).map(key => {
        const entry = profile.roles[key];
        return <div key={key} style={{ display: "contents" }}>
          <dt>{key === "plan" ? "Planning (Planner)" : key === "actor" ? "Taking actions (Actor)" : "Checking results (Verifier)"}</dt>
          <dd>OpenAI · {entry.model} · {entry.service_tier} tier · reasoning {entry.reasoning} · up to {entry.max_output_tokens} output tokens per call</dd>
        </div>;
      })}
      <dt>Provider call limit</dt><dd>{profile.max_provider_calls}{local ? " attempts across planning, actions and verification." : " attempts across planning, actions and verification. Failed or uncertain attempts may consume the limit and reserved credits."}</dd>
      <dt>Request size limit</dt><dd>{profile.max_request_bytes} bytes per provider request</dd>
      <dt>Page data sent</dt><dd>Bounded, redacted current page and retained history text within the reviewed website scope is sent to the saved OpenAI roles. No image or tool inputs are included. Page text may still contain private information; review the task and websites before approving.</dd>
      <dt>Completion review</dt><dd>Planning and checking responses propose a result. They do not verify the whole task or give permission to accept results, publish, send or pay. Review the recorded outcome.</dd>
    </dl>
    {qualifier === null && <ErrorNotice error="This saved qualification scope is incomplete. Do not approve or start it." source="device-work-order.qualification" />}
    {qualifier && <section aria-label="Saved browser qualification scope">
      <h3>Qualification task and effects</h3>
      <p>Trial <code>{qualifier.trial_id}</code>; task <code>{qualifier.task_id}</code>{local ? ". Review the permitted effects below before continuing." : ". The shared trial ceiling is $10 across all its tasks, including known and uncertain paid attempts. This review retains the task credit ceiling above."}</p>
      <p>Catalog: <code>{qualifier.catalog_digest}</code>; seed: <code>{qualifier.seed_digest}</code>. Saved rules do not prove the task succeeded.</p>
      {qualifier.rules ? <>
        <p>Only the exact saved controller fixture rules below are permitted. Additional websites, credentials, payments and filesystem writes are unavailable.</p>
        <p>Operations: {qualifier.allowed_operations?.join(", ") || "none"}; up to {qualifier.max_tabs ?? 1} owned tabs; {qualifier.frames?.length ?? 0} reviewed frames.</p>
        <ul>{qualifier.rules.map(rule => <li key={rule.rule_id}>
          <code>{rule.method} {rule.path}{rule.query ? `?${rule.query}` : ""}</code>: up to {rule.max_requests} requests and {rule.max_commits} mutations.
          {rule.fields.length > 0 && <ul>{rule.fields.map(field => <li key={field.name}>
            {field.name}: exact request value <code>{JSON.stringify(field.value)}</code>; {field.input_value == null ? "typing unavailable" : <>exact input <code>{field.input_value}</code></>}.
          </li>)}</ul>}
        </li>)}</ul>
        {qualifier.downloads?.map(download => <p key={download.filename}>Reviewed CSV {download.filename}, at most {download.max_bytes} bytes, digest <code>{download.sha256}</code>.</p>)}
      </> : <p>This is trial budget membership only. It grants no fixture, form input, download, tab or network exception; the saved read-only website scope still applies.</p>}
      <p>Approve only after reviewing these exact effects. Uncertain requests stay protected and cannot be replayed.</p>
    </section>}
    <details><summary>{local ? "Saved profile identity" : "Saved profile and pricing identity"}</summary>
      <p>Profile: {profile.version}; policy {profile.policy_version}; completion: {profile.completion_policy}.</p>
      {!local && <p>Pricing table digest: <code style={{ overflowWrap: "anywhere" }}>{profile.pricing_digest}</code>. A changed model or pricing table requires a matching new review; no model fallback is granted.</p>}
    </details>
  </section>;
}
