import { noDash, plainText, productDescription } from "@/lib/researchBrief";
import { displayResearchText } from "./ResearchBrief";
import styles from "./PriorArt.module.css";

export type PriorArtFinding = {
  id: string; title: string; url: string | null; summary?: string;
  additional_excerpts?: { text?: string }[];
  founder_flag?: "pin" | "exclude" | null; evidence_kind?: string; category?: string;
};
type Citation = { finding_id: string; quote?: string; quote_index?: number; description?: string };
type Company = {
  key: string; name: string; status: "live" | "inactive" | "acquired" | "unknown";
  kind: "direct" | "adjacent" | "oss" | "dead"; finding_ids: string[]; source_types?: string[];
  what_they_built?: Citation | null; pricing_now?: Citation[]; complaints?: Citation[]; why_failed?: Citation | null;
  positioning?: Citation | null;
  first_seen?: string | null; last_seen?: string | null;
  timeline?: { finding_id: string; date: string; event: string }[];
  status_evidence?: { finding_id: string; basis: string }[];
  traction_signals?: { finding_id: string; type: string; value: number }[];
};
export type PriorArtSummary = {
  companies: Company[];
  lessons?: { text: string; kind: "pattern" | "anecdote"; citations: Citation[]; company_keys: string[] }[];
  people?: { name: string; public_url: string; relation: string; role_claim_finding_id: string }[];
  freshness?: { newest_source?: string | null; oldest_used?: string | null; stale_companies?: string[] };
  coverage_gaps?: string[];
};

function publicUrl(value?: string | null): string | undefined {
  try {
    const url = new URL(value || ""), host = url.hostname.toLowerCase().replace(/\.$/, "");
    if (!/^https?:$/.test(url.protocol) || url.username || url.password || !host.includes(".")
      || host.split(".").some(label => ["local", "localhost", "internal", "metadata", "instance-data"].includes(label))
      || host.includes(":") || /^[\d.]+$/.test(host) || (url.port && !["80", "443"].includes(url.port))) return undefined;
    if ([...url.searchParams.keys()].some(key => /(?:auth|token|secret|password|credential|email|phone|session|signature|api.?key)/i.test(key))
      || /[\w.+-]+@[\w.-]+\.[a-z]{2,}/i.test(decodeURIComponent(url.href))) return undefined;
    return url.href;
  } catch { return undefined; }
}

function dated(value?: string | null): string {
  if (!value || !/^\d{4}(?:-\d{2}(?:-\d{2}(?:T.*)?)?)?$/.test(value)) return "Unknown";
  const date = new Date(value.length === 4 ? `${value}-01-01` : value);
  if (!Number.isFinite(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat("en", { year: "numeric", ...(value.length > 4 ? { month: "short" as const } : {}), timeZone: "UTC" }).format(date);
}

const statusLabels = { live: "Live", inactive: "Inactive", acquired: "Acquired", unknown: "" };
const tractionLabels: Record<string, string> = { hn_points: "Hacker News points", hn_comments: "Hacker News comments", github_stars: "GitHub stars", app_rating_count: "app ratings", team_size: "team members" };
const normalized = (text: string) => text.replace(/\s+/g, " ").trim();
const rows = <T,>(value?: T[] | null): T[] => Array.isArray(value) ? value.filter(row => row && typeof row === "object") : [];

export function PriorArt({ priorArt, findings, className }: {
  priorArt?: PriorArtSummary | null; findings: PriorArtFinding[]; className?: string;
}) {
  if (!priorArt || typeof priorArt !== "object") return null;
  const sources = new Map(findings.filter(finding => finding.founder_flag !== "exclude"
    && !["name_source", "search_lead_unverified"].includes(finding.evidence_kind || "") && finding.category !== "review_lead")
    .map(finding => [finding.id, finding]));
  const companies = rows(priorArt.companies).filter(company => typeof company.name === "string"
    && typeof company.key === "string" && Array.isArray(company.finding_ids) && company.finding_ids.some(id => sources.has(id))
    && [company.what_they_built, company.positioning, company.why_failed, ...rows(company.pricing_now), ...rows(company.complaints)]
      .some(citation => Boolean(plainText(resolvedQuote(citation) || ""))));
  const order = (company: Company) => company.kind === "oss" ? 2 : company.status === "live" ? 0 : 1;
  const sorted = [...companies].sort((a, b) => order(a) - order(b)).filter((company, index, all) =>
    all.findIndex(other => other.key === company.key || other.name.toLowerCase() === company.name.toLowerCase()) === index).slice(0, 5);
  function sourceLink(id: string) {
    const source = sources.get(id), url = publicUrl(source?.url);
    return source ? url ? <a data-usage-control="research_prior_source" href={url} target="_blank" rel="noopener noreferrer">{noDash(source.title || "Source")}</a>
      : <span>Source link unavailable</span> : null;
  }
  function resolvedQuote(citation?: Citation | null) {
    const finding = citation && sources.get(citation.finding_id), quote = typeof citation?.quote === "string" ? citation.quote.trim() : null;
    if (!finding || !quote || !displayResearchText(quote)) return null;
    return [finding.summary || "", ...(finding.additional_excerpts || []).map(excerpt => excerpt.text || "")]
      .some(excerpt => normalized(excerpt).includes(normalized(quote))
        || (plainText(quote) && plainText(excerpt).includes(plainText(quote)))) ? quote : null;
  }
  function quoteField(label: string, citation?: Citation | null) {
    const savedQuote = resolvedQuote(citation);
    let quote = savedQuote && plainText(savedQuote);
    if (label === "What they built" && savedQuote) {
      quote = productDescription(savedQuote);
      const preferred = productDescription(sources.get(citation!.finding_id)?.summary || "");
      if (citation?.description && [quote, preferred].includes(citation.description)) quote = productDescription(citation.description);
    }
    return quote ? <div className={styles.field}><dt>{label}</dt><dd><q>{noDash(quote)}</q> <span className={styles.source}>{sourceLink(citation!.finding_id)}</span></dd></div> : null;
  }
  const lessons = rows(priorArt.lessons).filter(lesson => typeof lesson.text === "string" && Array.isArray(lesson.citations) && lesson.citations.length
    && lesson.citations.every(citation => citation && typeof citation.finding_id === "string" && sources.has(citation.finding_id)));
  const people = rows(priorArt.people).filter(person => {
    if (typeof person.name !== "string" || typeof person.relation !== "string" || typeof person.public_url !== "string") return false;
    const url = publicUrl(person.public_url);
    if (!sources.has(person.role_claim_finding_id) || !url || /\S+@\S+|(?:\+?\d[\s().-]*){7,}/.test(`${person.name} ${person.relation}`)) return false;
    const host = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
    return ["news.ycombinator.com", "github.com", "ycombinator.com", "www.ycombinator.com"].includes(host)
      || companies.some(company => company.finding_ids.includes(person.role_claim_finding_id) && !company.key.includes("/")
        && (host === company.key || host.endsWith(`.${company.key}`)));
  });
  const stale = (Array.isArray(priorArt.freshness?.stale_companies) ? priorArt.freshness.stale_companies : []).filter(key => companies.some(company => company.key === key));
  if (!sorted.length && !lessons.length && !people.length) return null;
  return <section className={className} aria-labelledby="prior-art-title">
    <h2 id="prior-art-title">Who tried this before</h2>
    {sorted.length ? <div className={styles.companies}>{sorted.map(company => {
      const statusEvidence = rows(company.status_evidence).filter(citation => sources.has(citation.finding_id));
      const status = statusEvidence.length ? company.status : "unknown";
      const statusLabel = status === "live" && statusEvidence.every(citation => citation.basis === "page_available")
        ? "Public page available" : statusLabels[status] || statusLabels.unknown;
      const pricing = rows(company.pricing_now).find(citation => resolvedQuote(citation));
      const complaint = rows(company.complaints).find(citation => resolvedQuote(citation));
      const timeline = rows(company.timeline).filter(event => typeof event.date === "string" && typeof event.event === "string" && sources.has(event.finding_id) && dated(event.date) !== "Unknown");
      return <article key={company.key} className={styles.company}>
        <h3>{noDash(company.name)} {statusLabel && <span className={styles.status}>{statusLabel}</span>}</h3>
        <p className={styles.note}>{company.kind === "oss" ? "Open-source project" : company.kind === "dead" ? "Earlier attempt" : "Related product"}
          {dated(company.first_seen) !== "Unknown" && ` · First seen: ${dated(company.first_seen)}`}{dated(company.last_seen) !== "Unknown" && ` · Last seen: ${dated(company.last_seen)}`}</p>
        <dl>
          {quoteField("What they built", company.what_they_built)}
          {quoteField("Public pricing", pricing)}
          {quoteField("User complaint", complaint)}
          {resolvedQuote(company.positioning) && quoteField("Positioning", company.positioning)}
          {resolvedQuote(company.why_failed) && quoteField("Why it struggled", company.why_failed)}
        </dl>
        {timeline.length > 0 && <details className={styles.timeline}><summary data-usage-control="research_prior_timeline">What happened</summary>
          <ol>{[...timeline].sort((a, b) => a.date.localeCompare(b.date)).map((event, index) => <li key={index}>
            <time dateTime={event.date}>{dated(event.date)}</time> · {noDash(event.event)} · {sourceLink(event.finding_id)}
          </li>)}</ol>
        </details>}
        {rows(company.traction_signals).some(signal => sources.has(signal.finding_id) && tractionLabels[signal.type] && Number.isFinite(signal.value)) && <p className={styles.note}>
          Public adoption signals: {rows(company.traction_signals).filter(signal => sources.has(signal.finding_id) && tractionLabels[signal.type] && Number.isFinite(signal.value)).map((signal, index) =>
            <span key={index}>{index > 0 && "; "}{signal.value.toLocaleString()} {tractionLabels[signal.type]} ({sourceLink(signal.finding_id)})</span>)}.
        </p>}
        <p className={styles.sources}>Public sources: {company.finding_ids.filter(id => sources.has(id)).map((id, index) => <span key={id}>{index > 0 && " · "}{sourceLink(id)}</span>)}</p>
      </article>;
    })}</div> : null}
    {lessons.length > 0 && <><h3>Lessons to test</h3>
    <ul className={styles.lessons}>{lessons.map((lesson, index) => {
      const citedCompanies = companies.filter(company => Array.isArray(lesson.company_keys) && lesson.company_keys.includes(company.key)
        && lesson.citations.some(citation => company.finding_ids.includes(citation.finding_id)));
      const pattern = lesson.kind === "pattern" && new Set(citedCompanies.map(company => company.key)).size >= 2;
      return <li key={index}><strong>{pattern ? "Pattern" : "Anecdote"} · Inference</strong><p>{noDash(plainText(lesson.text))}</p>
        <p className={styles.sources}>Sources: {lesson.citations.map((citation, sourceIndex) => <span key={sourceIndex}>{sourceIndex > 0 && " · "}{sourceLink(citation.finding_id)}</span>)}</p></li>;
    })}</ul></>}
    {people.length > 0 && <><h3>People to learn from</h3>
    <ul className={styles.people}>{people.map((person, index) => <li key={index}>
      <a data-usage-control="research_prior_profile" href={publicUrl(person.public_url)} target="_blank" rel="noopener noreferrer">{noDash(person.name)}</a>
      <p>{noDash(person.relation)} · {sourceLink(person.role_claim_finding_id)}</p>
    </li>)}</ul>
    <p className={styles.note}>Public profiles only. If you reach out, introduce yourself, explain why you are asking and respect their choice to decline.</p></>}
    {(dated(priorArt.freshness?.newest_source) !== "Unknown" || dated(priorArt.freshness?.oldest_used) !== "Unknown") && <p className={styles.note}>
      {dated(priorArt.freshness?.newest_source) !== "Unknown" && `Newest source: ${dated(priorArt.freshness?.newest_source)}. `}
      {dated(priorArt.freshness?.oldest_used) !== "Unknown" && `Oldest source used: ${dated(priorArt.freshness?.oldest_used)}.`}
      {stale.length > 0 && ` ${stale.length} ${stale.length === 1 ? "company has" : "companies have"} no source from the past two years.`}</p>}
  </section>;
}
