"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { ChatBar } from "@/components/chat/ChatBar";
import { api, ensureSession, type SessionInfo } from "@/lib/client";
import { shortTitle } from "@/lib/format";
import { isLocalConsole } from "@/lib/localMode";
import { CredentialField } from "@/components/secrets/CredentialField";
import { SocialWalkthrough, type WalkthroughStep } from "./SocialWalkthrough";
import { useVentureChat } from "@/components/chat/useVentureChat";
import styles from "./connect.module.css";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

const SLOT: Record<string, { slotId: string; envKey: string; label: string }> = {
  github: { slotId: "github.pat", envKey: "GITHUB_TOKEN", label: "GitHub token" },
  social: { slotId: "zernio.api_key", envKey: "ZERNIO_API_KEY", label: "Zernio API key" },
  stripe: { slotId: "stripe.secret_key", envKey: "STRIPE_SECRET_KEY", label: "Stripe secret key" },
};

type GuideField = {
  id: string;
  label: string;
  placeholder?: string;
  secret?: boolean;
  optional?: boolean;
};

type GuideStep = {
  text?: string;
  url?: string | null;
};

type Guide = {
  open_url?: string;
  open_label?: string;
  skip_label?: string;
  steps?: GuideStep[];
  fields?: GuideField[];
  extra_links?: Array<{ label: string; url: string }>;
  walkthrough?: WalkthroughStep[];
  chat_hints?: string[];
};

type ConnectPayload = {
  provider?: string;
  guide?: Guide;
  task?: { title?: string; why?: string; status?: string } | null;
};

const TITLES: Record<string, string> = {
  github: "Connect GitHub",
  social: "Connect posting",
  stripe: "Connect Stripe",
};

export default function ConnectPage() {
  const params = useParams();
  const ventureId = String(params.ventureId || "");
  const provider = String(params.provider || "").toLowerCase();
  if (isLocalConsole() && provider === "stripe") {
    return <AppShell title="Spending" ventureId={ventureId}>
      <p>To manage your spending, open <a href="/settings/local#spend-title">Keys and connections › Your monthly spend cap</a>.</p>
    </AppShell>;
  }
  return <ProviderConnection key={`${ventureId}:${provider}`} ventureId={ventureId} provider={provider} />;
}

function ProviderConnection({ ventureId, provider }: { ventureId: string; provider: string }) {
  const router = useRouter();
  const [title, setTitle] = useState(TITLES[provider] || "Connect");
  const [data, setData] = useState<ConnectPayload | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [sessionSubject, setSessionSubject] = useState<string | undefined>();
  const chat = useVentureChat(ventureId);

  useEffect(() => {
    (async () => {
      const session = await ensureSession();
      setSessionSubject(session.ok ? (session.data as SessionInfo | null)?.subject : undefined);
      const ven = await api(`/api/ventures/${ventureId}`);
      if (ven.ok) {
        const v = ven.data as { name?: string; raw_idea?: string };
        setTitle(shortTitle(v.name, v.raw_idea));
      }
      const r = await api(`/api/ventures/${ventureId}/connect/${provider}`);
      if (!r.ok) {
        setError("This connect flow is not available.");
        return;
      }
      const payload = r.data as ConnectPayload;
      setData(payload);
    })();
  }, [ventureId, provider]);

  const guide = data?.guide;
  const heading = data?.task?.title || TITLES[provider] || "What we need from you";
  const why =
    provider === "social"
      ? "Follow the four steps. Click Explain in detail on any step if you want the exact screen, cursor, and button."
      : data?.task?.why || "The team is parked until this is in.";
  const isSocial = provider === "social";
  const isStripe = provider === "stripe";
  const walkthrough = isSocial ? guide?.walkthrough || [] : [];

  const slot = SLOT[provider];
  const publicFields = (guide?.fields || []).filter((field) => !field.secret);
  const canSubmit = useMemo(() => {
    if (provider === "social") return true;
    if (!publicFields.length && slot) return true;
    return publicFields.every((field) => field.optional || (fields[field.id] || "").trim());
  }, [fields, provider, publicFields, slot]);

  async function submit(skipped: boolean) {
    setBusy(true);
    setError("");
    const r = await api(`/api/ventures/${ventureId}/connect/${provider}`, {
      method: "POST",
      body: JSON.stringify({ fields, skipped }),
    });
    setBusy(false);
    if (!r.ok) {
      const detail =
        typeof r.data === "object" && r.data && "detail" in r.data
          ? String((r.data as { detail?: string }).detail || "")
          : "";
      setError(
        detail === "need_handles"
          ? "Paste the Zernio API key (it starts with sk_), or add at least one public handle."
          : detail === "need_token"
            ? "Paste the token, or skip if you want to keep going locally."
            : "Couldn’t save that. Try again.",
      );
      return;
    }
    setDone(true);
    window.setTimeout(() => router.push(`/v/${ventureId}`), 900);
  }

  return (
    <AppShell
      title={title}
      ventureId={ventureId}
      bottomBar={
        <ChatBar
          key={chat.conversationKey}
          conversationId={chat.conversationDraftId}
          ventureId={ventureId}
          subject={sessionSubject}
          messages={chat.messages}
          streaming={chat.streaming}
          onSend={(body) => chat.sendChat(body)}
          onAction={(action, payload) => chat.confirmChatAction(action, payload)}
        />
      }
    >
      <div className={isSocial ? styles.guidePage : styles.page}>
        <a href={`/v/${ventureId}`} className="ghost sm">
          Back to cockpit
        </a>
        <h1 className={styles.question}>{heading}</h1>
        <p className={styles.lede}>{why}</p>
        {isSocial ? (
          <p className={styles.callout}>
            You are <strong>not</strong> creating an X developer app, and you will{" "}
            <strong>not</strong> paste an X password or X API secret here. Zernio already
            has X&apos;s permission. Enter the Zernio key in the secure form, never in chat.
          </p>
        ) : null}
        {isStripe ? (
          <p className={styles.callout}>
            Payments are not part of this rollout. You do not need a payment account.
            Skip this page and keep working.
          </p>
        ) : null}
        <ErrorNotice error={error} source="connect.provider.error" className={styles.error} />
        {isStripe ? (
          <p>
            <a href={`/v/${ventureId}`} className="primary">
              Back to the venture
            </a>
          </p>
        ) : done ? (
          <p className={styles.ok}>Saved. Parked work can continue.</p>
        ) : (
          <>
            {walkthrough.length > 0 ? (
              <SocialWalkthrough steps={walkthrough} />
            ) : (
              <ol className={styles.steps}>
                {(guide?.steps || []).map((step, index) => (
                  <li key={`${step.text}-${index}`}>
                    <span>{step.text}</span>
                    {step.url ? (
                      <a href={step.url} target="_blank" rel="noreferrer" className="primary sm">
                        Open
                      </a>
                    ) : null}
                  </li>
                ))}
              </ol>
            )}
            <div className={styles.links}>
              {guide?.open_url ? (
                <a href={guide.open_url} target="_blank" rel="noreferrer" className="primary">
                  {guide.open_label || "Open the site"}
                </a>
              ) : null}
              {(guide?.extra_links || []).map((link) => (
                <a key={link.url} href={link.url} target="_blank" rel="noreferrer" className="ghost">
                  {link.label}
                </a>
              ))}
            </div>
            {isSocial && (guide?.chat_hints || []).length > 0 ? (
              <div className={styles.hints}>
                <p className={styles.hintLabel}>Stuck? Ask the team in the chat bar below.</p>
                <div className={styles.hintRow}>
                  {(guide?.chat_hints || []).map((hint) => (
                    <button
                      key={hint}
                      type="button"
                      className="ghost sm"
                      onClick={() => void chat.sendChat(hint)}
                    >
                      {hint}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
            <form
              className={styles.form}
              onSubmit={(event) => {
                event.preventDefault();
                void submit(false);
              }}
            >
              {slot ? (
                <CredentialField
                  slotId={slot.slotId}
                  fields={[{ env_key: slot.envKey, label: slot.label, input: "password" }]}
                />
              ) : null}
              {publicFields.map((field) => (
                <label key={field.id} className={styles.field}>
                  {field.label}
                  <input
                    type="text"
                    autoComplete="username"
                    placeholder={field.placeholder}
                    value={fields[field.id] || ""}
                    onChange={(event) =>
                      setFields((prev) => ({ ...prev, [field.id]: event.target.value }))
                    }
                  />
                </label>
              ))}
              <div className={styles.actions}>
                <button className="primary" type="submit" disabled={busy || !canSubmit}>
                  {busy ? "Saving…" : "Save and continue"}
                </button>
                {guide?.skip_label ? (
                  <button
                    className="ghost"
                    type="button"
                    disabled={busy}
                    onClick={() => void submit(true)}
                  >
                    {guide.skip_label}
                  </button>
                ) : null}
              </div>
            </form>
          </>
        )}
      </div>
    </AppShell>
  );
}
