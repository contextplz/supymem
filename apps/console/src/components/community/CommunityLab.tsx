"use client";

import Link from "@/components/chat/ChatLink";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  communityDiscordAction,
  connectCommunityDiscord,
  getCommunityDiscordStatus,
  getCommunityStatus,
  postCommunityLabLog,
  postCommunityPlan,
  postCommunityThesis,
  provisionCommunityDiscord,
  type CommunityStatus,
  type CommunityThesis,
} from "@/lib/opsApi";
import styles from "@/components/marketing/MarketingLab.module.css";

type UiLog = {
  id: string;
  ts: string;
  level: string;
  action: string;
  detail?: string;
  json?: unknown;
};

const SAMPLE_BUILD = {
  product_name: "ForecastForge",
  one_liner: "RevOps forecast hygiene for B2B SaaS",
  category: "b2b_saas",
  paying_customers: 80,
  monthly_active_users: 200,
  content_audience: 1500,
  founder_hours_per_week: 8,
  pmf_signal: "early",
  features: ["pipeline review", "forecast snapshot", "tool sprawl map"],
  icp_segments: [
    {
      name: "RevOps leads",
      role: "RevOps lead",
      industry: "B2B SaaS",
      company_size: "21-200",
      technical_depth: "med",
      self_serve: true,
      pains: [
        "weekly forecast miss after Monday pipeline review",
        "daily tool sprawl across CRM and sheets",
        "nobody to ask for peer playbooks on forecast hygiene",
      ],
      triggers: ["quarter close"],
      objections: ["too hard to migrate"],
      watering_holes: ["r/salesops", "RevOps LinkedIn"],
    },
  ],
  personas: [{ name: "Maya", identity_labels: ["RevOps", "sales ops"] }],
  competitors: [
    {
      name: "Clari",
      strengths: ["enterprise forecasting"],
      weaknesses: ["expensive"],
    },
  ],
};

const SAMPLE_DONT = {
  product_name: "AI Widget",
  one_liner: "Generic AI tool",
  paying_customers: 8,
  founder_hours_per_week: 2,
  pmf_signal: "none",
  core_job_completions: 2,
  icp_segments: [
    {
      name: "buyers",
      role: "decision makers at mid-market companies",
      pains: ["buy software once"],
      watering_holes: [],
      buying_committee: true,
    },
  ],
};

function formatLabError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const m = msg.match(/^API\s+(\d+):\s*([\s\S]+)$/);
  if (!m) return msg;
  const status = m[1];
  const raw = m[2].trim();
  try {
    type LabErrorFields = {
      block_reason?: string;
      needs_you_reason?: string;
      message?: string;
      error?: string;
      status?: string;
    };
    type LabErrorPayload = LabErrorFields & {
      detail?: string | LabErrorFields;
    };
    const parsed = JSON.parse(raw) as LabErrorPayload;
    const detail: string | LabErrorFields = parsed.detail ?? parsed;
    if (typeof detail === "string") {
      return `NeedsYou (${status}): ${detail}`;
    }
    if (detail && typeof detail === "object") {
      const reason =
        detail.block_reason ||
        detail.needs_you_reason ||
        detail.message ||
        detail.error;
      if (reason) return `NeedsYou (${status}): ${String(reason)}`;
    }
  } catch {
    /* keep raw */
  }
  return msg.length > 280 ? `${msg.slice(0, 280)}…` : msg;
}

function founderDiscordMessage(value: unknown): string {
  const message = String(value || "").trim();
  if (!message) return "Connect Discord before provisioning your community workspace.";
  if (/COMPOSIO_API_KEY|DISCORDBOT auth config/i.test(message)) {
    return "Discord connections are not enabled for this Supymem environment yet. Ask the workspace administrator to enable the Discord connection service.";
  }
  return message
    .replaceAll("DISCORDBOT", "Discord")
    .replaceAll("guild_id", "Server ID");
}

function JsonBlock({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: "0.5rem" }}>
      <button
        type="button"
        className={`${styles.btn} ${styles.btnGhost}`}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Hide technical details" : "Show technical details"}
      </button>
      {open ? (
        <pre className={styles.pre}>{JSON.stringify(value, null, 2)}</pre>
      ) : null}
    </div>
  );
}

export function CommunityLab() {
  const [status, setStatus] = useState<CommunityStatus | null>(null);
  const [thesis, setThesis] = useState<CommunityThesis | null>(null);
  const [researchJson, setResearchJson] = useState("{\n  \n}");
  const [guildId, setGuildId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [logs, setLogs] = useState<UiLog[]>([]);
  const [lastAction, setLastAction] = useState<unknown>(null);
  const [routeMissing, setRouteMissing] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const pushLog = useCallback(
    (level: string, action: string, detail?: string, json?: unknown) => {
      const entry: UiLog = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        ts: new Date().toISOString(),
        level,
        action,
        detail,
        json,
      };
      setLogs((prev) => [entry, ...prev].slice(0, 80));
      void postCommunityLabLog({
        level,
        action,
        detail,
        ok: level !== "error",
      }).catch(() => undefined);
    },
    []
  );

  const refresh = useCallback(async () => {
    try {
      const st = await getCommunityStatus();
      setStatus(st);
      setRouteMissing(false);
      if (st.plan?.thesis) setThesis(st.plan.thesis);
      return st;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("404") || msg.toLowerCase().includes("not found")) {
        setRouteMissing(true);
      }
      throw err;
    }
  }, []);

  useEffect(() => {
    void refresh().catch((err) => {
      pushLog(
        "error",
        "GET /community/status",
        err instanceof Error ? err.message : String(err)
      );
    });
  }, [refresh, pushLog]);

  const runFn = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    try {
      const result = await fn();
      setLastAction(result);
      pushLog("success", label, undefined, result);
      await refresh().catch(() => undefined);
      return result;
    } catch (err) {
      const msg = formatLabError(err);
      pushLog("error", label, msg);
      setLastAction({ error: msg });
      // Do not rethrow — callers use void runFn(...); rethrow became an uncaught
      // pageerror for expected NeedsYou / 409 lab outcomes.
      return { error: msg };
    } finally {
      setBusy(null);
    }
  };

  const discord = status?.discord || {};
  const connected = Boolean(discord.connected);
  const needsYou = Boolean(discord.needs_you) || !connected;
  const blockers = (discord.blockers as { code?: string; message?: string }[]) || [];
  const funnel = status?.funnel?.stages || {};
  const communityStageLabels: Record<string, string> = {
    visitor: "Visitors",
    member: "Members",
    activated: "Active members",
    product_user: "Product users",
    pqcl: "Qualified customer leads",
    customer: "Customers",
    referral: "Referrals",
  };
  const identity = status?.plan?.identity as Record<string, unknown> | null | undefined;
  const program = status?.plan?.program_30d as
    | Record<string, unknown>
    | null
    | undefined;
  const buildOwned = thesis?.verdict === "build_owned";
  const canProvision = connected && !Boolean(discord.needs_you);
  const canConnect = Boolean(discord.composio_configured);
  const connectBusy = busy === "Connect Discord";

  const handleConnectDiscord = useCallback(async () => {
    if (!canConnect) {
      const reason = founderDiscordMessage(discord.needs_you_reason);
      setConnectError(reason);
      pushLog("needs_you", "Connect Discord", reason);
      return { error: reason };
    }
    setConnectError(null);
    setBusy("Connect Discord");
    try {
      const started = await connectCommunityDiscord();
      const url = started.redirect_url;
      if (!url) {
        const reason = founderDiscordMessage(
          started.block_reason ||
          started.error ||
          "Discord connections are not enabled for this Supymem environment yet."
        );
        setConnectError(reason);
        pushLog("error", "Connect Discord", reason, started);
        setLastAction({ error: reason, ...started });
        return started;
      }
      const opened = window.open(url, "_blank", "noopener,noreferrer");
      if (!opened) {
        // Popup blocked — fall back to same-tab navigation so founder can still connect.
        window.location.href = url;
      }
      pushLog("success", "Connect Discord", "Opened Composio OAuth in a new tab", {
        has_redirect: true,
        connection_id: started.connection_id,
      });
      setLastAction(started);
      await refresh().catch(() => undefined);
      return started;
    } catch (err) {
      const msg = formatLabError(err);
      setConnectError(msg);
      pushLog("error", "Connect Discord", msg);
      setLastAction({ error: msg });
      throw err;
    } finally {
      setBusy(null);
    }
  }, [canConnect, discord.needs_you_reason, pushLog, refresh]);

  const phasePills = useMemo(() => {
    const p = status?.phases || {};
    return [
      ["Fit", p["0_thesis"]],
      ["Plan", p["1_identity"]],
      ["Connect", p["2_connect"]],
      ["Set up", p["3_provision"]],
      ["Operate", p["4_operate"]],
      ["Grow", p["5_gtm_handoff"]],
    ] as const;
  }, [status]);

  return (
    <div className={styles.wrap}>
      <div className={styles.links}>
        <Link href="/connections">Connections hub →</Link>
        <Link href="/marketing">Marketing workspace →</Link>
        <Link href="/gtm">Go-to-market →</Link>
      </div>

      {routeMissing ? (
        <div className={styles.banner}>
          <span className={`${styles.pill} ${styles.pillErr}`}>NeedsYou</span>
          <span>
            `/community/*` routes missing, restart ops (`company_os`) so the
            Community Department API loads.
          </span>
        </div>
      ) : null}

      <div className={styles.banner}>
        <span className={`${styles.pill} ${styles.pillLive}`}>
          Community setup
        </span>
        {phasePills.map(([label, on]) => (
          <span
            key={label}
            className={`${styles.pill} ${on ? styles.pillLive : styles.pillOff}`}
          >
            {label}
          </span>
        ))}
        <button
          type="button"
          className={`${styles.btn} ${styles.btnGhost}`}
          disabled={Boolean(busy)}
          onClick={() => void runFn("Refresh status", () => refresh())}
        >
          Refresh
        </button>
      </div>

      {/* Sticky Connect CTA — always visible when Discord is not connected (no plan/thesis gate). */}
      {!connected ? (
        <div
          className={styles.banner}
          style={{
            position: "sticky",
            top: 0,
            zIndex: 20,
            background: "var(--bg-1, #fff)",
            borderColor: "#c47b00",
            boxShadow: "0 4px 14px rgba(0,0,0,0.06)",
          }}
        >
          <span className={`${styles.pill} ${styles.pillNeed}`}>NeedsYou</span>
          <span style={{ flex: "1 1 12rem" }}>
            {canConnect
              ? "Connect Discord to continue. Create the server yourself, then let Supymem open the secure authorization flow."
              : founderDiscordMessage(discord.needs_you_reason)}
          </span>
          <button
            type="button"
            className={styles.btn}
            disabled={Boolean(busy) || !canConnect}
            title={canConnect ? "Connect Discord" : "Discord connections are not enabled yet"}
            onClick={() => void handleConnectDiscord().catch(() => undefined)}
            style={{ fontWeight: 700 }}
          >
            {connectBusy ? "Connecting…" : "Connect Discord"}
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={Boolean(busy)}
            onClick={() =>
              void runFn("Discord status", () => getCommunityDiscordStatus())
            }
          >
            Refresh Discord status
          </button>
          <a
            href="#community-connect-discord"
            className={styles.muted}
            style={{ fontSize: "0.78rem" }}
          >
            View connection steps →
          </a>
        </div>
      ) : null}

      {/* Sticky guild_id CTA — bot connected but Server ID still missing. */}
      {connected && Boolean(discord.needs_guild_id) ? (
        <div
          className={styles.banner}
          style={{
            position: "sticky",
            top: 0,
            zIndex: 21,
            background: "#fff8e8",
            borderColor: "#c47b00",
            boxShadow: "0 4px 14px rgba(0,0,0,0.08)",
            flexWrap: "wrap",
            gap: "0.65rem",
          }}
          data-testid="needs-guild-id-banner"
        >
          <span className={`${styles.pill} ${styles.pillNeed}`}>NeedsYou</span>
          <span style={{ flex: "1 1 16rem", fontWeight: 600 }}>
            Discord is connected, paste the Discord Server ID to
            provision channels and create a tracked invite. Composio redacts the
            id from the connection payload.
          </span>
          <label
            className={styles.muted}
            style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: "16rem" }}
          >
            <span style={{ fontWeight: 700, color: "#7a3e00" }}>
              Discord Server ID, required
            </span>
            <input
              className={styles.input}
              value={guildId}
              onChange={(e) => setGuildId(e.target.value.trim())}
              placeholder="18-digit Server ID"
              style={{
                fontSize: "1.05rem",
                fontWeight: 700,
                letterSpacing: "0.02em",
                borderColor: "#c47b00",
                minHeight: "2.5rem",
              }}
              data-testid="guild-id-input-sticky"
            />
          </label>
          <button
            type="button"
            className={styles.btn}
            disabled={Boolean(busy) || !guildId || !canProvision}
            style={{ fontWeight: 700 }}
            onClick={() =>
              void runFn("Provision", () =>
                provisionCommunityDiscord({
                  guild_id: guildId || undefined,
                })
              )
            }
          >
            Provision skeleton
          </button>
          <a href="#community-provision" className={styles.muted} style={{ fontSize: "0.78rem" }}>
            Jump to section D →
          </a>
          {Array.isArray(discord.founder_steps) && discord.founder_steps.length ? (
            <ol
              style={{
                flex: "1 1 100%",
                margin: "0.15rem 0 0",
                paddingLeft: "1.25rem",
                fontSize: "0.85rem",
              }}
            >
              {discord.founder_steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}

      {/* A. Thesis */}
      <section className={styles.card}>
        <h2 className={styles.h2}>Decide whether community is worth building</h2>
        <p className={styles.muted}>
          Use real company evidence to decide whether an owned community will help customers.
          The examples are clearly marked and never become your company data unless you submit them.
        </p>
        <textarea
          className={styles.textarea}
          rows={12}
          value={researchJson}
          onChange={(e) => setResearchJson(e.target.value)}
          aria-label="Structured company evidence"
          placeholder='{"product_name":"Your product","one_liner":"What it does"}'
          spellCheck={false}
        />
        <div className={styles.row} style={{ marginTop: "0.65rem" }}>
          <button
            type="button"
            className={styles.btn}
            disabled={Boolean(busy)}
            onClick={() =>
              void runFn("Evaluate thesis", async () => {
                const body = JSON.parse(researchJson) as Record<string, unknown>;
                const res = await postCommunityThesis(body);
                setThesis(res.thesis);
                return res;
              })
            }
          >
            Evaluate thesis
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            onClick={() => setResearchJson(JSON.stringify(SAMPLE_BUILD, null, 2))}
          >
            Load example: community may fit
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            onClick={() => setResearchJson(JSON.stringify(SAMPLE_DONT, null, 2))}
          >
            Load example: community may not fit
          </button>
        </div>
        {thesis ? (
          <div style={{ marginTop: "0.85rem" }}>
            <div className={styles.row}>
              <span
                className={`${styles.pill} ${
                  buildOwned ? styles.pillLive : styles.pillNeed
                }`}
              >
                {thesis.verdict} · score {thesis.thesis_score}
              </span>
              <span className={styles.pill}>motion: {thesis.motion}</span>
            </div>
            <p style={{ marginTop: "0.5rem" }}>{thesis.because}</p>
            <div className={styles.row} style={{ marginTop: "0.5rem" }}>
              {(thesis.gates || []).map((g) => (
                <span
                  key={g.id}
                  className={`${styles.pill} ${
                    g.passed ? styles.pillLive : styles.pillErr
                  }`}
                  title={g.evidence}
                >
                  {g.id} {g.passed ? "pass" : "fail"}
                </span>
              ))}
            </div>
            <div className={styles.row} style={{ marginTop: "0.45rem" }}>
              {(thesis.dimensions || []).map((d) => (
                <span key={d.id} className={styles.pill}>
                  {d.id} {d.score}/5
                </span>
              ))}
            </div>
            <JsonBlock value={thesis} />
          </div>
        ) : (
          <p className={styles.muted}>No thesis yet.</p>
        )}
      </section>

      {/* B. Identity */}
      <section className={styles.card}>
        <h2 className={styles.h2}>Community identity and 30-day program</h2>
        <p className={styles.muted}>
          Generated only when the evidence supports building an owned community.
        </p>
        <button
          type="button"
          className={styles.btn}
          disabled={Boolean(busy)}
          onClick={() =>
            void runFn("Create plan", async () => {
              const body = JSON.parse(researchJson) as Record<string, unknown>;
              if (thesis) body.thesis = thesis;
              return postCommunityPlan(body);
            })
          }
        >
          Create / refresh plan
        </button>
        {identity ? (
          <div style={{ marginTop: "0.75rem" }}>
            <strong>{String(identity.name || "")}</strong>
            <p className={styles.muted}>{String(identity.positioning || "")}</p>
            <JsonBlock value={{ identity, program, objectives: status?.plan?.objectives }} />
          </div>
        ) : (
          <p className={styles.muted} style={{ marginTop: "0.5rem" }}>
            {buildOwned
              ? "Create the community plan and its first 30-day program."
              : thesis
                ? `Not build_owned, follow pre-community motion “${thesis.motion || "see thesis"}” instead of provisioning Discord.`
                : "Evaluate thesis first. If not build_owned, follow the pre-community motion instead of provisioning Discord."}
          </p>
        )}
      </section>

      {/* C. Connect — always available; not gated on thesis/plan/build_owned */}
      <section className={styles.card} id="community-connect-discord">
        <h2 className={styles.h2}>Connect Discord</h2>
        <p className={styles.muted}>
          Create the server in Discord yourself (bots cannot create guilds), then
          authorize it through Supymem. You can connect before a thesis or plan exists.
        </p>
        <div className={styles.row}>
          <span
            className={`${styles.pill} ${
              connected ? styles.pillLive : styles.pillNeed
            }`}
          >
            {connected ? "Discord connected" : "Not connected"}
          </span>
          <span
            className={`${styles.pill} ${
              discord.composio_configured ? styles.pillLive : styles.pillErr
            }`}
          >
            Connection service {discord.composio_configured ? "available" : "not enabled"}
          </span>
          <span
            className={`${styles.pill} ${
              discord.auth_config_env_set ? styles.pillLive : styles.pillWarn
            }`}
          >
            Secure authorization {discord.auth_config_env_set ? "ready" : "will be prepared during connection"}
          </span>
        </div>
        {needsYou ? (
          <div className={styles.banner} style={{ marginTop: "0.65rem" }}>
            <span className={`${styles.pill} ${styles.pillNeed}`}>NeedsYou</span>
            <span>
              {String(
                founderDiscordMessage(discord.needs_you_reason)
              )}
            </span>
          </div>
        ) : null}
        {connectError ? (
          <div className={styles.banner} style={{ marginTop: "0.65rem" }}>
            <span className={`${styles.pill} ${styles.pillErr}`}>Error</span>
            <span>{connectError}</span>
          </div>
        ) : null}
        {blockers.length ? (
          <ul className={styles.muted}>
            {blockers.map((b) => (
              <li key={b.code || b.message}>{founderDiscordMessage(b.message)}</li>
            ))}
          </ul>
        ) : null}
        <div className={styles.row} style={{ marginTop: "0.85rem", gap: "0.65rem" }}>
          <button
            type="button"
            className={styles.btn}
            disabled={Boolean(busy) || !canConnect}
            title={canConnect ? "Connect Discord" : "Discord connections are not enabled yet"}
            onClick={() => void handleConnectDiscord().catch(() => undefined)}
            style={{
              fontWeight: 700,
              fontSize: "0.95rem",
              padding: "0.65rem 1.15rem",
            }}
            data-testid="connect-discord"
          >
            {connectBusy ? "Connecting…" : "Connect Discord"}
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={Boolean(busy)}
            onClick={() =>
              void runFn("Discord status", () => getCommunityDiscordStatus())
            }
            data-testid="refresh-discord-status"
          >
            Refresh Discord status
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={Boolean(busy)}
            onClick={() =>
              void runFn("Check Discord connection", () =>
                communityDiscordAction({ action: "list_tools" })
              )
            }
          >
            Check connection
          </button>
        </div>
        <JsonBlock value={discord} />
      </section>

      {/* D. Provision / actions */}
      <section className={styles.card} id="community-provision">
        <h2 className={styles.h2}>Set up the community space</h2>
        <p className={styles.muted}>
          Create the recommended channels and roles after Discord is connected. Supymem checks the server before making changes.
        </p>
        {!canProvision ? (
          <div className={styles.banner} style={{ marginTop: "0.35rem" }}>
            <span className={`${styles.pill} ${styles.pillNeed}`}>NeedsYou</span>
            <span>
              Provision is locked until Discord is <strong>connected</strong>.
              Complete <strong>Connect Discord</strong> above first
              {discord.needs_you_reason
                ? ` Not recorded ${founderDiscordMessage(discord.needs_you_reason)}`
                : "."}
            </span>
          </div>
        ) : null}
        {canProvision && Boolean(discord.needs_guild_id) ? (
          <div className={styles.banner} style={{ marginTop: "0.35rem" }}>
            <span className={`${styles.pill} ${styles.pillNeed}`}>NeedsYou</span>
            <span>
              {String(
                discord.guild_needs_you_reason ||
                  "Paste the Discord Server ID below, then Provision. Enable Developer Mode → right-click server → Copy Server ID."
              )}
            </span>
          </div>
        ) : null}
        <div className={styles.row}>
          <label className={styles.muted} style={{ flex: "1 1 18rem" }}>
            <span style={{ fontWeight: 700 }}>
              Discord Server ID, required to go live
            </span>
            <input
              className={styles.input}
              value={guildId}
              onChange={(e) => setGuildId(e.target.value.trim())}
              placeholder="18-digit snowflake from Discord → Copy Server ID"
              style={{
                display: "block",
                width: "100%",
                marginTop: 4,
                fontSize: "1.05rem",
                fontWeight: 700,
                borderColor: guildId ? undefined : "#c47b00",
                minHeight: "2.5rem",
              }}
              data-testid="guild-id-input"
            />
          </label>
          <label className={styles.muted}>
            Discord channel ID{" "}
            <input
              className={styles.input}
              value={channelId}
              onChange={(e) => setChannelId(e.target.value)}
              placeholder="for invite / message"
            />
          </label>
        </div>
        <div className={styles.row} style={{ marginTop: "0.65rem" }}>
          <button
            type="button"
            className={styles.btn}
            disabled={Boolean(busy) || !canProvision}
            title={
              canProvision
                ? "Create channel/role skeleton"
                : "Connect Discord first"
            }
            onClick={() =>
              void runFn("Provision", () =>
                provisionCommunityDiscord({
                  guild_id: guildId || undefined,
                  plan_id: status?.plan?.id,
                })
              )
            }
          >
            Create recommended channels
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={Boolean(busy) || !guildId || !canProvision}
            onClick={() =>
              void runFn("List channels", () =>
                communityDiscordAction({
                  action: "list_channels",
                  params: { guild_id: guildId },
                })
              )
            }
          >
            List channels
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={Boolean(busy) || !channelId || !canProvision}
            onClick={() =>
              void runFn("Create invite", () =>
                communityDiscordAction({
                  action: "create_invite",
                  params: { channel_id: channelId },
                })
              )
            }
          >
            Create invite
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnGhost}`}
            disabled={Boolean(busy) || !channelId || !canProvision}
            onClick={() =>
              void runFn("Send test message", () =>
                communityDiscordAction({
                  action: "send_message",
                  params: {
                    channel_id: channelId,
                    content:
                      "Company OS Community Lab test, safe to delete.",
                    confirm: true,
                  },
                })
              )
            }
          >
            Send test message
          </button>
        </div>
        <JsonBlock value={lastAction} />
      </section>

      {/* E. Funnel */}
      <section className={styles.card}>
        <h2 className={styles.h2}>Community progress</h2>
        <p className={styles.muted}>
          These counts stay at zero until Discord is connected and real member events are received.
        </p>
        <div className={styles.row}>
          {Object.entries(funnel).map(([k, v]) => (
            <span key={k} className={styles.pill}>
              {communityStageLabels[k] || k.replaceAll("_", " ")}: {String(v)}
            </span>
          ))}
        </div>
        <p className={styles.muted} style={{ marginTop: "0.5rem" }}>
          People who engage can move into the same founder-reviewed outreach workflow.{" "}
          <Link href="/gtm">Open go-to-market →</Link>
        </p>
      </section>

      {/* F. Problem log */}
      <section className={styles.card} style={{ position: "sticky", bottom: 0 }}>
        <h2 className={styles.h2}>Recent activity</h2>
        <div className={styles.logList}>
          {logs.length === 0 ? (
            <p className={styles.muted}>No community actions yet.</p>
          ) : (
            logs.map((l) => (
              <div key={l.id} className={styles.logRow}>
                <span
                  className={`${styles.pill} ${
                    l.level === "error"
                      ? styles.pillErr
                      : l.level === "success"
                        ? styles.pillLive
                        : styles.pillOff
                  }`}
                >
                  {l.level}
                </span>
                <span className={styles.muted}>{l.ts}</span>
                <strong>{l.action}</strong>
                {l.detail ? <span>{l.detail}</span> : null}
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}
