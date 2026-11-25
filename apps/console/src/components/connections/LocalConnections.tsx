"use client";

import { useEffect, useState } from "react";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import Link from "@/components/chat/ChatLink";
import { api } from "@/lib/client";
import { capabilityView, STATE_TITLES } from "@/app/settings/local/capabilities/capabilities";
import { CodingAgentTiles } from "./CodingAgentTiles";
import styles from "@/components/marketing/MarketingLab.module.css";

const SECTIONS = [
  ["send_email", "Mail", "smtp"], ["research", "Search", "search"],
  ["publish_pages", "Publishing", "publishing"], ["connected_apps", "Connected apps", "composio"],
] as const;
type Capability = NonNullable<ReturnType<typeof capabilityView>>;
type Card = Capability & { section: string };

export function LocalConnections() {
  const [providerState, setProviderState] = useState<"ready" | "saved" | "missing" | null>(null);
  const [cards, setCards] = useState<Card[]>([]);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setProviderState(null); setCards([]); setError("");
    const changed = () => { live = false; setProviderState(null); setCards([]); setAttempt(value => value + 1); };
    window.addEventListener("supymem-session-changed", changed);
    window.addEventListener("supymem-local-settings-changed", changed);
    void (async () => {
      try {
        const [provider, features] = await Promise.all([api("/api/local/providers"), api("/api/local/capabilities")]);
        if (!live) return;
        const model = provider.data as { configured?: unknown; verified?: unknown; sample_selected?: unknown } | null;
        if (!provider.ok || typeof model?.configured !== "boolean") throw new Error();
        setProviderState(model.configured && model.sample_selected !== true ? model.verified === true ? "ready" : "saved" : "missing");
        const raw = (features.data as { capabilities?: unknown } | null)?.capabilities;
        if (!features.ok || !Array.isArray(raw)) throw new Error();
        const capabilities = raw.map(capabilityView);
        setCards(SECTIONS.map(([name, title, section]) => {
          const matching = capabilities.filter(item => item?.name === name);
          if (matching.length !== 1 || !matching[0]) throw new Error();
          return { ...matching[0], title, section };
        }));
      } catch {
        if (live) setError("Your saved connections could not be loaded. Open Keys and connections to check your settings, then reload connections here.");
      }
    })();
    return () => { live = false; window.removeEventListener("supymem-session-changed", changed); window.removeEventListener("supymem-local-settings-changed", changed); };
  }, [attempt]);

  return <section aria-label="Your local connections">
    <h2>Your keys and connections</h2>
    {error ? <ErrorNotice error={error} source="connections.local.settings">{error} <button type="button" onClick={() => setAttempt(value => value + 1)}>Reload connections</button></ErrorNotice>
      : null}
    <div className={styles.connectionsGrid}><article className={styles.connector}>
      <h3>Your AI provider</h3><span className={`${styles.pill} ${providerState === "ready" ? styles.pillLive : providerState === "saved" ? "" : styles.pillWarn}`} role="status">{providerState === "ready" ? "Ready" : providerState === "saved" ? "Saved" : providerState === "missing" ? "Needs setup" : error ? "Could not check" : "Checking..."}</span>
      <p>{providerState === "ready" ? "Your AI connection is ready. You can update it in Keys and connections."
        : providerState === "saved" ? "You can use your saved provider. To check it, open Your AI provider in Keys and connections and choose Test your connection."
        : providerState === "missing" ? "Choose your AI service and model in Keys and connections, then save and test your connection." : null}</p>
      <Link href="/settings/local#providers-title">Keys and connections</Link>
    </article></div>
    <CodingAgentTiles />
    {cards.length > 0 && <div className={styles.connectionsGrid}>{cards.map(card => <article key={card.name} className={styles.connector}>
        <h3>{card.title}</h3><span className={`${styles.pill} ${card.state === "ready" ? styles.pillLive : styles.pillWarn}`}>{STATE_TITLES[card.state]}</span>
        <p>{card.state === "ready" && card.name === "connected_apps" ? "Choose an app below to connect your account." : card.next_step}</p>
        {card.state === "off" ? <Link href={`/settings/local/capabilities#${card.name}-title`}>Capabilities</Link>
          : <Link href={`/settings/local#${card.section}-title`}>Keys and connections</Link>}
      </article>)}</div>}
    {error && <Link href="/settings/local">Keys and connections</Link>}
  </section>;
}
