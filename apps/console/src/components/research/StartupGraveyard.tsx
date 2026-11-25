import styles from "./PriorArt.module.css";

export type FailedStartup = {
  name: string; founded: string | null; ended: string | null; cash_burned: string | null;
  why_failed: string; suggestion: string; entry_url: string;
};

function entryUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && ["loot-drop.io", "www.loot-drop.io"].includes(url.hostname)
      && !url.username && !url.password && (!url.port || url.port === "443")
      && /^\/startup\/\d+-[a-z0-9-]+\/?$/.test(url.pathname) ? url.href : null;
  } catch { return null; }
}

export function StartupGraveyard({ startups, className }: { startups?: FailedStartup[] | null; className?: string }) {
  const matches = (Array.isArray(startups) ? startups : []).filter(startup => startup
    && typeof startup.name === "string" && startup.name.trim()
    && typeof startup.why_failed === "string" && startup.why_failed.trim()
    && typeof startup.suggestion === "string" && startup.suggestion.trim()
    && typeof startup.entry_url === "string" && entryUrl(startup.entry_url)).slice(0, 3);
  if (!matches.length) return null;
  return <section className={className} aria-labelledby="startup-graveyard-title">
    <h2 id="startup-graveyard-title">Startup graveyard</h2>
    <p className={styles.note}>Similar failed attempts from Loot Drop, an AI-assisted catalogue without cited sources. These short facts are not independently verified.</p>
    <div className={styles.companies}>{matches.map(startup => <article className={styles.company} key={startup.entry_url}>
      <h3>{startup.name}</h3>
      <dl>
        {/^\d{4}$/.test(startup.founded || "") && /^\d{4}$/.test(startup.ended || "") && <div className={styles.field}><dt>Years active</dt><dd>{startup.founded}-{startup.ended}</dd></div>}
        {typeof startup.cash_burned === "string" && startup.cash_burned.trim() && <div className={styles.field}><dt>Cash burned</dt><dd>{startup.cash_burned}</dd></div>}
        <div className={styles.field}><dt>Why it failed</dt><dd>{startup.why_failed}</dd></div>
        <div className={styles.field}><dt>What to do differently <span className={styles.note}>(Our suggestion)</span></dt><dd>{startup.suggestion}</dd></div>
      </dl>
      <p className={styles.sources}><a data-usage-control="research_graveyard_source" href={entryUrl(startup.entry_url)!} target="_blank" rel="noopener noreferrer">Source</a>{" · "}
        <a data-usage-control="research_graveyard_loot_drop" href={entryUrl(startup.entry_url)!} target="_blank" rel="noopener noreferrer">via Loot Drop</a></p>
    </article>)}</div>
  </section>;
}
