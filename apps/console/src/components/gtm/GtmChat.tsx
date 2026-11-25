"use client";

import { useEffect, useRef, useState } from "react";
import Link from "@/components/chat/ChatLink";
import {
  connectAiGtmGmail,
  downloadAiGtmExport,
  getAiGtmContacts,
  getAiGtmStatus,
  getOpsWorkspace,
  runAiGtmFind,
  runAiGtmLabGenerate,
  setAiGtmRun,
  startAiGtmLinkedIn,
  startAiGtmOutreach,
  syncAiGtmGmail,
  opsApi,
  type AiGtmContact,
  type AiGtmLead,
  type AiGtmStatus,
} from "@/lib/opsApi";
import {
  contactIdsFromLeads,
  filterLeads,
  parseGtmChat,
  summarizeLeads,
  type GtmLeadFilters,
} from "@/lib/gtmChatParse";
import styles from "./GtmChat.module.css";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { operationsHref } from "@/lib/operationsScope";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

type ChatRole = "user" | "assistant";

type ChatMessage = {
  id: string;
  role: ChatRole;
  text: string;
  leads?: AiGtmLead[];
  showActions?: boolean;
  reviews?: EmailReview[];
};

type EmailReview = {
  queueId: string;
  approval: { id: string; status: string };
  review: {
    recipient: string;
    subject: string;
    body: string;
    mailbox: string;
    daily_send_limit: number;
    sent_today: number;
    credit_status: string;
    credit_estimate_micro?: number | null;
    rate_version?: string | null;
  };
  execution?: { status: "succeeded" | "failed"; detail: string };
};

type EmailReviewResponse = Omit<EmailReview, "queueId">;

function nid(): string {
  return `m_${Math.random().toString(36).slice(2, 10)}`;
}

export function GtmChat() {
  const operationsScope = useOperationsScope();
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: nid(),
      role: "assistant",
      text:
        "Ask me in plain English.\n\nExample: hey find me the leads of AI companies in San Francisco specifically with companies starting with S and ending with M\n\nThen download Excel/JSON, or say “email them” / “linkedin them”.",
    },
  ]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<AiGtmStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [mailboxEmail, setMailboxEmail] = useState<string | null>(null);
  const [leads, setLeads] = useState<AiGtmLead[]>([]);
  const [contacts, setContacts] = useState<AiGtmContact[]>([]);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement | null>(null);
  const mounted = useRef(true);
  const actionPending = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  function assertCurrentCompany() {
    if (!mounted.current || !operationsScope || getOpsWorkspace() !== operationsScope.workspaceId) {
      throw new Error("The company selection changed. Return to the intended company before continuing.");
    }
  }

  async function runAction(action: () => Promise<unknown>) {
    if (actionPending.current) return;
    actionPending.current = true;
    setBusy(true);
    setError("");
    try { assertCurrentCompany(); await action(); }
    catch (err) { if (mounted.current) setError(err instanceof Error ? err.message : "GTM operation failed."); }
    finally { actionPending.current = false; if (mounted.current) setBusy(false); }
  }

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const st = await getAiGtmStatus();
        if (!st) throw new Error("GTM status was not returned. Refresh the company workspace to retry.");
        if (current) setStatus(st);
      } catch (err) {
        if (current) {
          setStatus(null);
          setError(err instanceof Error ? err.message : "GTM status could not be loaded.");
        }
      } finally {
        if (current) setStatusLoading(false);
      }
    })();
    return () => { current = false; };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("gmail") !== "connected") return;
    void (async () => {
      try {
        await syncAiGtmGmail();
        const next = await getAiGtmStatus();
        setStatus(next);
        push({
          role: "assistant",
          text: next?.gmail?.ready
            ? "Gmail connected and synced. You can enable sending, then email outreach."
            : "Returned from Gmail OAuth, but the mailbox is not ready yet, try Sync Gmail.",
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Gmail sync after connect failed");
      } finally {
        params.delete("gmail");
        const nextQs = params.toString();
        const url = `${window.location.pathname}${nextQs ? `?${nextQs}` : ""}`;
        window.history.replaceState({}, "", url);
      }
    })();
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy]);

  function push(msg: Omit<ChatMessage, "id">) {
    setMessages((prev) => [...prev, { ...msg, id: nid() }]);
  }

  async function refreshContacts(): Promise<AiGtmContact[]> {
    try {
      assertCurrentCompany();
      const res = await getAiGtmContacts();
      assertCurrentCompany();
      const rows = res.contacts || [];
      setContacts(rows);
      return rows;
    } catch {
      return contacts;
    }
  }

  async function runFind(filters: GtmLeadFilters) {
    assertCurrentCompany();
    const result = await runAiGtmFind({
      query: filters.query,
      count: filters.count,
      industry: filters.industry || "B2B",
      region: filters.region || null,
      source: "auto",
      deep: true,
      auto_outreach: false,
      verified_only: false,
    });
    assertCurrentCompany();
    const matched = filterLeads(result.leads || [], filters);
    const nextContacts = result.contacts?.length
      ? result.contacts
      : await refreshContacts();
    setLeads(matched);
    if (result.contacts?.length) setContacts(result.contacts);
    else setContacts(nextContacts);
    return { matched, rawCount: result.leads?.length || 0, message: result.message, state: result.state };
  }

  async function doDownload(format: "json" | "xlsx" | "csv") {
    if (!leads.length) {
      push({
        role: "assistant",
        text: "No leads in memory yet. Ask me to find leads first.",
      });
      return;
    }
    await downloadAiGtmExport(leads, format);
    const label =
      format === "xlsx" ? "Excel (.xlsx)" : format === "csv" ? "CSV" : "JSON";
    push({
      role: "assistant",
      text: `Downloaded ${leads.length} leads as ${label}.`,
      leads,
      showActions: true,
    });
  }

  async function runRandomLab() {
    const result = await runAiGtmLabGenerate({
      count: 15,
      random: true,
      enable_discovery: false,
    });
    const matched = result.leads || [];
    setLeads(matched);
    if (result.contacts?.length) setContacts(result.contacts);
    else await refreshContacts();
    if (result.engine) {
      setStatus((prev) =>
        prev
          ? {
              ...prev,
              engine: result.engine as AiGtmStatus["engine"],
              funnel: (result.funnel || prev.funnel) as AiGtmStatus["funnel"],
            }
          : prev
      );
    }
    const scen = result.scenario;
    push({
      role: "assistant",
      text:
        `Random situation: ${scen?.label || scen?.id || "lab"}\n` +
        `Query: ${result.query || scen?.query || "Not recorded"}\n` +
        summarizeLeads(matched) +
        `\n\nOutreach was not started. Download CSV/JSON/Excel, or say “email them”.`,
      leads: matched,
      showActions: matched.length > 0,
    });
  }

  async function doOutreach(kind: "email" | "linkedin" | "both") {
    assertCurrentCompany();
    const rows = contacts.length ? contacts : await refreshContacts();
    assertCurrentCompany();
    const ids = contactIdsFromLeads(leads, rows);
    if (!ids.length) {
      push({
        role: "assistant",
        text:
          "The displayed leads have no matching saved contact IDs. No outreach was prepared. Refresh these leads before continuing.",
      });
      return;
    }

    const notes: string[] = [];
    let reviews: EmailReview[] = [];
    if (kind === "email" || kind === "both") {
      const email = await startAiGtmOutreach({ contact_ids: ids, auto_send: false });
      const enrolled = email.enrolled?.length || 0;
      notes.push(
        email.blocker
          ? `Email blocked: ${email.blocker}`
          : `Email: enrolled ${enrolled}. No email was sent; each queued draft needs review and founder approval.`
      );
      const queueIds = (email as typeof email & { queue_ids?: string[] }).queue_ids || [];
      if (queueIds.length) {
        const results = await Promise.all(
          queueIds.map(async (queueId) => {
            try {
              assertCurrentCompany();
              const result = await opsApi.post<EmailReviewResponse>(
                `/ai-gtm/outreach/queue/${encodeURIComponent(queueId)}/review`,
                {}
              );
              return { queueId, result };
            } catch (err) {
              notes.push(
                `Draft ${queueId} could not be reviewed: ${err instanceof Error ? err.message : "unknown error"}.`
              );
              return null;
            }
          })
        );
        reviews = results
          .filter((row): row is { queueId: string; result: EmailReviewResponse } => Boolean(row))
          .map(({ queueId, result }) => ({ ...result, queueId }));
        if (reviews.length) {
          notes.push(`Review the exact recipient, copy, mailbox limit, and credit status below.`);
        }
      }
    }
    if (kind === "linkedin" || kind === "both") {
      assertCurrentCompany();
      const li = await startAiGtmLinkedIn({ contact_ids: ids });
      const enrolled = li.enrolled?.length || 0;
      notes.push(
        li.blocker
          ? `LinkedIn blocked: ${li.blocker}`
          : `LinkedIn: queued ${enrolled} warm connect jobs${
              li.provider === "human_queue" ? " (human queue, no Unipile yet)" : ""
            }.`
      );
    }
    push({
      role: "assistant",
      text: notes.join("\n"),
      leads,
      showActions: true,
      reviews,
    });
  }

  async function decideEmailReview(
    messageId: string,
    queueId: string,
    approvalId: string,
    decision: "approved" | "rejected"
  ) {
    setBusy(true);
    setError("");
    try {
      const approval = await opsApi.post<{ id: string; status: string }>(
        `/ai-gtm/outreach/queue/${encodeURIComponent(queueId)}/approval`,
        { approval_id: approvalId, decision }
      );
      setMessages((current) =>
        current.map((message) =>
          message.id !== messageId || !message.reviews
            ? message
            : {
                ...message,
                reviews: message.reviews.map((review) =>
                  review.queueId === queueId ? { ...review, approval } : review
                ),
              }
        )
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the review");
    } finally {
      setBusy(false);
    }
  }

  async function executeEmailReview(messageId: string, review: EmailReview) {
    setBusy(true);
    setError("");
    try {
      const result = await opsApi.post<{ skipped: boolean; provider_message_id?: string; error?: string }>(
        `/ai-gtm/outreach/queue/${encodeURIComponent(review.queueId)}/execute`,
        { approval_id: review.approval.id }
      );
      const detail = result.skipped
        ? result.error || "Execution was not completed."
        : `Sent with provider receipt ${result.provider_message_id || "recorded"}.`;
      setMessages((current) => current.map((message) =>
        message.id !== messageId || !message.reviews ? message : {
          ...message,
          reviews: message.reviews.map((item) =>
            item.queueId === review.queueId
              ? { ...item, execution: { status: result.skipped ? "failed" : "succeeded", detail } }
              : item
          ),
        }
      ));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not execute this reviewed email");
    } finally {
      setBusy(false);
    }
  }

  async function onSend(text?: string) {
    const value = (text ?? draft).trim();
    if (!value || busy || actionPending.current) return;
    actionPending.current = true;
    setDraft("");
    setError("");
    push({ role: "user", text: value });
    const parsed = parseGtmChat(value);
    push({ role: "assistant", text: parsed.reply });
    setBusy(true);
    try {
      assertCurrentCompany();
      if (parsed.intent === "help" || parsed.intent === "unknown") {
        return;
      }
      if (parsed.intent === "download_json") {
        await doDownload("json");
        return;
      }
      if (parsed.intent === "download_xlsx") {
        await doDownload("xlsx");
        return;
      }
      if (parsed.intent === "download_csv") {
        await doDownload("csv");
        return;
      }
      if (parsed.intent === "outreach_email") {
        await doOutreach("email");
        return;
      }
      if (parsed.intent === "outreach_linkedin") {
        await doOutreach("linkedin");
        return;
      }
      if (parsed.intent === "outreach_both") {
        await doOutreach("both");
        return;
      }
      if (parsed.intent === "find") {
        const { matched, rawCount, message, state } = await runFind(parsed.filters);
        const summary = state === "unavailable" ? "Public discovery is unavailable." : summarizeLeads(matched);
        const note =
          matched.length === 0 && rawCount > 0
            ? `\n\nDiscovery returned ${rawCount} leads, but none matched your name/region filters.`
            : "";
        push({
          role: "assistant",
          text: `${summary}${note}${message ? `\n\n${message}` : ""}${matched.length ? "\n\nUse the buttons or say “download excel”, “download json”, “email them”, or “linkedin them”." : ""}`,
          leads: matched,
          showActions: matched.length > 0,
        });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Something went wrong";
      setError(msg);
      push({ role: "assistant", text: `That failed: ${msg}` });
    } finally {
      actionPending.current = false;
      setBusy(false);
    }
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <h1>AI GTM chat</h1>
        <p>
          Find leads in plain English, download Excel/JSON, then prepare cold email or LinkedIn
          outreach from the same thread.
        </p>
        <p className={styles.status}>
          {statusLoading ? "Loading GTM status…" : !status ? "GTM status unavailable. Refresh the company workspace to retry." : <>
          {status.dry_run === true ? "Sending disabled in this environment · " : status.dry_run === false ? "Live delivery configured · " : "Delivery status unavailable · "}
          {status?.gmail?.ready
            ? `Gmail ready${mailboxEmail ? ` (${mailboxEmail})` : ""} · `
            : "Gmail not ready · "}
          {status?.engine?.send_enabled ? "Sending on · " : "Sending off · "}
          {status?.unipile?.ready ? "LinkedIn API ready" : "LinkedIn human queue"}
          {status?.localization && !status.localization.allowed
            ? " · thesis approval required before find"
            : ""}
          </>}
        </p>
        <div className={styles.headerActions}>
          <button
            type="button"
            className={styles.actionBtn}
            disabled={busy}
            onClick={() => {
              void (async () => {
                setBusy(true);
                setError("");
                try {
                  const started = await connectAiGtmGmail();
                  if (!started.redirect_url) {
                    throw new Error(
                      "Composio did not return a Gmail connect link."
                    );
                  }
                  window.location.href = started.redirect_url;
                } catch (err) {
                  setError(
                    err instanceof Error ? err.message : "Gmail connect failed"
                  );
                  setBusy(false);
                }
              })();
            }}
          >
            Connect mail
          </button>
          <Link href={operationsHref("/connections", operationsScope)} className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}>
            All connections
          </Link>
          <button
            type="button"
            className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
            disabled={busy}
            onClick={() => {
              void (async () => {
                setBusy(true);
                setError("");
                try {
                  const synced = await syncAiGtmGmail();
                  const next = await getAiGtmStatus();
                  setStatus(next);
                  const email =
                    synced.accounts?.find((a) => a.email)?.email || null;
                  setMailboxEmail(email);
                  push({
                    role: "assistant",
                    text: `Synced ${synced.synced ?? 0} Gmail mailbox link(s)${
                      email ? ` · ${email}` : ""
                    }. ${
                      next?.gmail?.ready ? "Gmail is ready." : "Gmail still not ready."
                    }`,
                  });
                } catch (err) {
                  setError(
                    err instanceof Error ? err.message : "Gmail sync failed"
                  );
                } finally {
                  setBusy(false);
                }
              })();
            }}
          >
            Sync Gmail
          </button>
          <button
            type="button"
            className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
            disabled={busy}
            onClick={() => {
              void (async () => {
                setBusy(true);
                setError("");
                try {
                  await runRandomLab();
                } catch (err) {
                  const msg =
                    err instanceof Error ? err.message : "Random generate failed";
                  setError(msg);
                  push({ role: "assistant", text: `That failed: ${msg}` });
                } finally {
                  setBusy(false);
                }
              })();
            }}
          >
            Random scenario leads
          </button>
          <button
            type="button"
            className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
            disabled={busy || !status || Boolean(status.engine?.send_enabled)}
            onClick={() => {
              void (async () => {
                setBusy(true);
                setError("");
                try {
                  await setAiGtmRun({ send: true });
                  const next = await getAiGtmStatus();
                  setStatus(next);
                  push({
                    role: "assistant",
                    text: "GTM outreach queue enabled. Drafts remain pending until you review each recipient and exact message. Sending stays unavailable until outbound provider pricing and credit settlement are configured.",
                  });
                } catch (err) {
                  setError(
                    err instanceof Error ? err.message : "Could not enable sending"
                  );
                } finally {
                  setBusy(false);
                }
              })();
            }}
          >
            {status?.engine?.send_enabled ? "Queue enabled" : "Enable queue"}
          </button>
        </div>
      </div>

      <div className={styles.thread} aria-live="polite">
        {messages.map((m) => (
          <div
            key={m.id}
            className={`${styles.bubble} ${m.role === "user" ? styles.user : styles.assistant}`}
          >
            {m.text}
            {m.reviews?.map((review) => (
              <div key={review.queueId} className={styles.actions}>
                <strong>External email review · {review.approval.status}</strong>
                <span>To: {review.review.recipient}</span>
                <span>Mailbox: {review.review.mailbox} · {review.review.sent_today}/{review.review.daily_send_limit} today</span>
                <span>Subject: {review.review.subject}</span>
                <pre className={styles.reviewBody}>{review.review.body}</pre>
                <span>
                  Credits: {review.review.credit_status.replaceAll("_", " ")}
                  {review.review.rate_version ? ` · ${review.review.rate_version}` : ""}
                </span>
                {review.approval.status === "pending" && (
                  <>
                    <button
                      type="button"
                      className={styles.actionBtn}
                      disabled={busy}
                      onClick={() => void decideEmailReview(m.id, review.queueId, review.approval.id, "approved")}
                    >
                      Approve exact draft
                    </button>
                    <button
                      type="button"
                      className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
                      disabled={busy}
                      onClick={() => void decideEmailReview(m.id, review.queueId, review.approval.id, "rejected")}
                    >
                      Reject
                    </button>
                  </>
                )}
                {review.approval.status === "approved" && !review.execution && (
                  review.review.credit_status === "configured" ? (
                    <button
                      type="button"
                      className={styles.actionBtn}
                      disabled={busy}
                      onClick={() => void executeEmailReview(m.id, review)}
                    >
                      Send reviewed email
                    </button>
                  ) : (
                    <span>Approved, but not sent: {review.review.credit_status.replaceAll("_", " ")}.</span>
                  )
                )}
                {review.execution && <span>{review.execution.detail}</span>}
              </div>
            ))}
            {m.showActions && m.leads && m.leads.length > 0 && (
              <div className={styles.actions}>
                <button
                  type="button"
                  className={styles.actionBtn}
                  disabled={busy}
                  onClick={() => void runAction(() => doDownload("xlsx"))}
                >
                  Download Excel
                </button>
                <button
                  type="button"
                  className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
                  disabled={busy}
                  onClick={() => void runAction(() => doDownload("json"))}
                >
                  Download JSON
                </button>
                <button
                  type="button"
                  className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
                  disabled={busy}
                  onClick={() => void runAction(() => doDownload("csv"))}
                >
                  Download CSV
                </button>
                <button
                  type="button"
                  className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
                  disabled={busy}
                  onClick={() => void runAction(() => doOutreach("email"))}
                >
                  Email outreach
                </button>
                <button
                  type="button"
                  className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
                  disabled={busy}
                  onClick={() => void runAction(() => doOutreach("linkedin"))}
                >
                  LinkedIn outreach
                </button>
                <button
                  type="button"
                  className={`${styles.actionBtn} ${styles.actionBtnSecondary}`}
                  disabled={busy}
                  onClick={() => void runAction(() => doOutreach("both"))}
                >
                  Both channels
                </button>
              </div>
            )}
          </div>
        ))}
        {busy && (
          <div className={`${styles.bubble} ${styles.assistant}`}>Working…</div>
        )}
        <div ref={endRef} />
      </div>

      <ErrorNotice error={error} source="GtmChat.error" className={`${styles.status} ${styles.error}`} />

      <form
        className={styles.composer}
        onSubmit={(e) => {
          e.preventDefault();
          void onSend();
        }}
      >
        <textarea
          className={styles.input}
          rows={2}
          value={draft}
          placeholder='e.g. find AI companies in San Francisco starting with S and ending with M'
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void onSend();
            }
          }}
          disabled={busy}
        />
        <button className={styles.send} type="submit" disabled={busy || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
