"use client";

import Link from "@/components/chat/ChatLink";
import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { AppShell } from "@/components/layout/AppShell";
import { NAV_ICONS, type NavIconId } from "@/components/layout/NavIcons";
import { GUIDE_TOPICS, GUIDE_STAGES, guideContent, guideBlockedMessage, filterGuideCapabilities, guideDestinationHref, type GuideCapability, type GuidePrompt, type GuideStage, type GuideTopic } from "@/lib/capabilityGuide";
import { prefillChat } from "@/lib/chatPrefill";
import { useFeatureVisibility, useLocalMode } from "@/components/layout/LocalModeBoundary";
import { api } from "@/lib/client";
import { capabilityView, type Capability } from "@/app/settings/local/capabilities/capabilities";
import styles from "./CapabilityGuide.module.css";

type CapabilityRead = { state: "loading" | "failed" } | { state: "ready"; items: Capability[] };

const TOPIC_ICONS: Record<GuideTopic, NavIconId> = {
  "All topics": "ventures", "Start & shape": "idea", Research: "research", Website: "website",
  Customers: "market", "Work & memory": "work", "Account & tools": "connections", "Coming next": "now",
};

const STAGE_HINTS: Record<GuideStage, { title: string; text: string }> = {
  "Any stage": { title: "Start where you are.", text: "You do not need a perfect brief. Tell Charlie what you have, what you want and what feels uncertain." },
  Exploring: { title: "An interest is a good beginning.", text: "Describe a problem you care about. Ask for help shaping it, then choose an idea to explore before committing." },
  "An idea": { title: "Make the idea concrete.", text: "Clarify the problem and audience, review the research, then choose a landing page or customer conversations." },
  "A prototype": { title: "Bring the progress you have made.", text: "Describe what works, what is unfinished and what you want to learn. Add your existing resources when reviewing a plan." },
  "A live landing page": { title: "Give the website a purpose.", text: "Share its address in the conversation. Focus on the offer, customer drafts and a small experiment. Ask for a new landing page only if you need one." },
  Customers: { title: "Use what your customers are telling you.", text: "Bring feedback, replies and experiment results into the venture. Ask Charlie to help choose one useful change to test next." },
};

const IMAGE_ALT: Record<NonNullable<GuideCapability["image"]>, string> = {
  idea: "A hand sketching an early idea on paper, with a lightbulb forming",
  research: "Open books, charts and a magnifying glass for researching an idea",
  page: "A simple web page taking shape on a laptop screen",
  customers: "Friendly conversations between a founder and potential customers",
  mac: "A Mac laptop carrying out a short browser task",
  credits: "Coins and an account card representing your account",
};

function PromptAction({ item, ventureId, children, className }: { item: GuidePrompt; ventureId?: string; children?: ReactNode; className?: string }) {
  return ventureId
    ? <button type="button" className={className || styles.promptAction} onClick={() => prefillChat(item.prompt)} data-usage-control="guide_start" data-guide-prompt={item.id}>{children || item.prompt}</button>
    : <Link className={className || styles.promptAction} href={`${guideDestinationHref("new")}?guide_prompt=${encodeURIComponent(item.id)}`} data-usage-control="guide_start" data-guide-prompt={item.id}>{children || item.prompt}</Link>;
}

export function CapabilityGuide({ ventureId }: { ventureId?: string }) {
  const local = useLocalMode();
  const visible = useFeatureVisibility();
  const content = guideContent(local);
  const [capabilities, setCapabilities] = useState<CapabilityRead>({ state: "loading" });
  const [capabilityAttempt, setCapabilityAttempt] = useState(0);
  const guideRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState<GuideTopic>("All topics");
  const [stage, setStage] = useState<GuideStage>("Any stage");
  const [copyStatus, setCopyStatus] = useState<{ id: string; message: string } | null>(null);
  const matches = filterGuideCapabilities(query, topic, stage, local).filter(card => visible("guide", card.id));
  const current = matches.filter(card => !card.comingNext);
  const upcoming = matches.filter(card => card.comingNext);
  const hint = STAGE_HINTS[stage];

  useEffect(() => {
    if (!local) return;
    let cancelled = false;
    void api("/api/local/capabilities").then(result => {
      const data = result.data as { capabilities?: unknown[] } | undefined;
      const items = result.ok && Array.isArray(data?.capabilities) ? data.capabilities.map(capabilityView) : [];
      if (!cancelled) setCapabilities(items.length && items.every((item): item is Capability => item !== null)
        ? { state: "ready", items } : { state: "failed" });
    }).catch(() => { if (!cancelled) setCapabilities({ state: "failed" }); });
    return () => { cancelled = true; };
  }, [local, capabilityAttempt]);

  useEffect(() => {
    if (!guideRef.current || typeof IntersectionObserver === "undefined" || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) {
        (entry.target as HTMLElement).dataset.guideReveal = "visible";
        observer.unobserve(entry.target);
      }
    }, { threshold: 0.08 });
    const elements = guideRef.current.querySelectorAll<HTMLElement>("[data-guide-reveal]");
    for (const element of elements) if (element.dataset.guideReveal !== "visible") {
      element.dataset.guideReveal = "pending";
      observer.observe(element);
    }
    return () => {
      observer.disconnect();
      for (const element of elements) element.dataset.guideReveal = "visible";
    };
  }, [query, topic, stage]);

  async function copyExample(card: GuideCapability) {
    if (!card.prompt || card.comingNext) return;
    try {
      await navigator.clipboard.writeText(card.prompt);
      setCopyStatus({ id: card.id, message: "Copied. Adapt it in your conversation." });
    } catch {
      setCopyStatus({ id: card.id, message: "Select the example text and copy it manually." });
    }
  }

  function resetFilters() { setQuery(""); setTopic("All topics"); setStage("Any stage"); }

  function jumpToSection(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault();
    const container = guideRef.current?.closest<HTMLElement>(".workspace-body");
    const section = guideRef.current?.querySelector<HTMLElement>(event.currentTarget.hash);
    if (!container || !section) return;
    const margin = Number.parseFloat(window.getComputedStyle(section).scrollMarginTop) || 0;
    container.scrollTop += section.getBoundingClientRect().top - container.getBoundingClientRect().top - container.clientTop - margin;
    section.focus({ preventScroll: true });
  }

  return <div className={styles.guide} ref={guideRef}>
    <a className={styles.skipLink} href="#guide-explore" onClick={jumpToSection}>Skip to capabilities</a>
    <header className={styles.hero}>
      <div className={styles.heroCopy}>
        <p className={styles.eyebrow}>YOUR SUPYMEM FIELD GUIDE</p>
        <h1>How to use Supymem</h1>
        <p className={styles.heroLead}>Your idea. Your first customers.<br />One conversation with Charlie.</p>
        <p className={styles.heroText}>{local ? "You learn what customers need and choose your next step with Charlie. Your own connected coding tool builds your pages, product, fixes and small tools on your laptop. Charlie writes the brief and checks the result. Nothing starts or is published unless you ask." : "Supymem helps you learn what customers need, make a landing page and prepare outreach. Tell Charlie what you have. Review the work in chat, approve what should happen next and learn from the results."}</p>
        <div className={styles.heroActions}>
          <Link className={styles.primary} href={ventureId ? guideDestinationHref("overview", ventureId) : "/new"} data-usage-control="guide_start">{ventureId ? "Return to your venture" : "Start with an idea"}<span aria-hidden="true">↗</span></Link>
          <a className={styles.textLink} href="#guide-begin" onClick={jumpToSection}>Start in three steps <span aria-hidden="true">↓</span></a>
        </div>
      </div>
      <div className={styles.heroArt}><img className={styles.heroImg} src="/guide/hero.webp" width={1200} height={800} alt="A founder and a helper sketching an idea together at a table" loading="eager" fetchPriority="high" decoding="async" onError={event => { event.currentTarget.style.display = "none"; }} /><div className={styles.charlieIntro}><img src="/charlie/charlie-mark.png" width={64} height={64} alt="Charlie, your Supymem companion" onError={event => { event.currentTarget.style.display = "none"; }} /><p><strong>Meet Charlie</strong><br />You bring the context. Charlie helps with the next move.</p></div></div>
    </header>

    <nav className={styles.sectionNav} aria-label="Guide sections">
      <a href="#guide-begin" onClick={jumpToSection}>Start here</a>{!local && <a href="#guide-use-cases" onClick={jumpToSection}>Use cases</a>}<a href="#guide-explore" onClick={jumpToSection}>{local ? "Explore your next step" : "Everything Supymem can do"}</a>{!local && <a href="#guide-tips" onClick={jumpToSection}>Power user tips</a>}<a href="#guide-faq" onClick={jumpToSection}>FAQs</a>
    </nav>

    <section id="guide-begin" className={styles.lessonSection} aria-labelledby="guide-begin-title" tabIndex={-1}>
      <div className={styles.sectionHead}><div><p className={styles.eyebrow}>NO PERFECT BRIEF REQUIRED</p><h2 id="guide-begin-title">Start here</h2></div><p>Three small steps. You stay in control.</p></div>
      <ol className={styles.quickStart}>{content.quickStart.map((step, index) => <li key={step.id} className={styles.lessonCard} data-guide-reveal="ready">
        {step.image && <img src={`/guide/${step.image}.webp`} width={1200} height={800} alt={IMAGE_ALT[step.image]} loading="lazy" decoding="async" onError={event => { event.currentTarget.style.display = "none"; }} />}
        <div className={styles.lessonBody}><p className={styles.eyebrow}>STEP {index + 1}</p><h3>{step.title}</h3>{"text" in step ? <><p>{step.text}</p><PromptAction item={step} ventureId={ventureId} className={styles.stepAction}>{step.action}<span aria-hidden="true">↗</span></PromptAction></> : <><p>{step.description}</p>{step.destination && <Link className={styles.stepAction} href={guideDestinationHref(step.destination, ventureId, local)}>{!ventureId && step.destination === "research" ? "Choose a venture to research" : step.action}<span aria-hidden="true">↗</span></Link>}</>}</div>
      </li>)}</ol><p className={styles.sectionNote}>{local ? "Try an example in your venture chat. It opens a draft you can edit; nothing starts until you send it." : "Example prompts open a chat draft, not an email or a task. Make the words yours, then send them to Charlie. Without a venture, you start in a new conversation."}</p>
    </section>

    {!local && <section id="guide-use-cases" className={styles.lessonSection} aria-labelledby="guide-use-cases-title" tabIndex={-1}>
      <div className={styles.sectionHead}><div><p className={styles.eyebrow}>CHOOSE AN OUTCOME</p><h2 id="guide-use-cases-title">Use cases</h2></div><p>Start with what you want to learn or do.<br />Times are rough estimates, not deadlines.</p></div>
      <div className={styles.storyGrid}>{content.useCases.map(story => <article key={story.id} className={styles.lessonCard} data-guide-reveal="ready" aria-labelledby={`guide-story-${story.id}`}>
        <img src={`/guide/${story.image}.webp`} width={1200} height={800} alt={IMAGE_ALT[story.image]} loading="lazy" decoding="async" onError={event => { event.currentTarget.style.display = "none"; }} />
        <div className={styles.lessonBody}><h3 id={`guide-story-${story.id}`}>{story.title}</h3><p>{story.who}</p><p className={styles.eyebrow}>SAY THIS TO CHARLIE</p><PromptAction item={story} ventureId={ventureId} /><p><strong>What you get:</strong> {story.result}</p><p className={styles.time}>{story.time}</p></div>
      </article>)}</div>
      <p className={styles.availabilityNote}>Coming soon: let Charlie research within a weekly experiment budget you approve, then help you read the results and choose the next test. For a goal such as booking three customer calls this week, a visible checklist would show progress and your required approvals. After calls, coaching would help you ask about real experiences rather than invite compliments. These are not available from this guide yet; no release date or outcome is promised.</p>
    </section>}

    <section id="guide-explore" className={styles.explore} aria-labelledby="guide-explore-title" tabIndex={-1}>
      <div className={styles.sectionHead}><div><p className={styles.eyebrow}>FIND YOUR NEXT MOVE</p><h2 id="guide-explore-title">{local ? "Explore your next step" : "Everything Supymem can do"}</h2></div><p>Examples to adapt, paths to explore<br className={styles.desktopBreak} /> and a clear idea of what happens next.</p></div>
      <div className={styles.finder}>
        <label className={styles.search}><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6.5" /><path d="m15 15 5 5" /></svg><span className={styles.srOnly}>Search capabilities and examples</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder={local ? "Try research, outreach or backups…" : "Try research, waitlist or memory…"} data-usage-control="guide_search" /></label>
        <label className={styles.stageSelect}><span>I am at</span><select value={stage} onChange={event => setStage(event.target.value as GuideStage)} data-usage-control="guide_stage">{GUIDE_STAGES.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      </div>
      <div className={styles.stageHint}><span className={styles.hintGlyph} aria-hidden="true">✳</span><div><strong>{hint.title}</strong><p>{hint.text}</p></div></div>
      <div className={styles.topics} role="group" aria-label="Filter by topic">{GUIDE_TOPICS.filter(value => !local || value !== "Coming next").map(value => <button key={value} type="button" aria-pressed={topic === value} className={topic === value ? styles.topicActive : styles.topic} onClick={() => setTopic(value)} data-usage-control="guide_topic">{value}</button>)}</div>
      <div className={styles.resultsMeta}><p role="status" aria-live="polite">{matches.length} {matches.length === 1 ? "capability" : "capabilities"}{query ? " match your search" : " to explore"}</p>{(query || topic !== "All topics" || stage !== "Any stage") && <button type="button" onClick={resetFilters} className={styles.reset} data-usage-control="guide_reset">Clear filters</button>}</div>
      {local && capabilities.state !== "ready" && <div>
        <p role="status">{capabilities.state === "loading" ? "Loading your feature status…" : "Couldn’t load your feature status. You can still read the guide. Select Try again to check your features."}</p>
        {capabilities.state === "failed" && <button type="button" className={styles.reset} onClick={() => {
          setCapabilities({ state: "loading" });
          setCapabilityAttempt(attempt => attempt + 1);
        }}>Try again</button>}
      </div>}
      {current.length > 0 && <div className={styles.cards}>{current.map((card, index) => {
        const blocked = local && capabilities.state === "ready" ? guideBlockedMessage(card, capabilities.items) : undefined;
        const needsSetup = blocked || (local && capabilities.state !== "ready" && card.requires?.length);
        const destination = card.destination && guideDestinationHref(card.destination, ventureId, local);
        const prompt = card.prompt;
        return <article key={card.id} className={styles.card} data-guide-reveal="ready" style={{ animationDelay: `${Math.min(index, 8) * 60}ms` }} aria-labelledby={`guide-${card.id}`}>
        {card.image ? <img className={styles.cardImg} src={`/guide/${card.image}.webp`} width={1200} height={800} loading="lazy" decoding="async" alt={IMAGE_ALT[card.image]} onError={event => { event.currentTarget.style.display = "none"; }} /> : <div className={styles.band} data-topic={card.topic} aria-hidden="true" />}
        <div className={styles.cardBody}>
        <div className={styles.cardTop}><span className={styles.cardIcon} data-topic={card.topic} aria-hidden="true">{NAV_ICONS[TOPIC_ICONS[card.topic]]({})}</span><span className={styles.cardTopic}>{card.topic}</span></div>
        <h3 id={`guide-${card.id}`}>{card.title}</h3><p className={styles.description}>{card.description}</p>
        {card.prompt && <div className={styles.example}><p>{card.prompt}</p><button type="button" onClick={() => void copyExample(card)} aria-label={`Copy example for ${card.title}`} data-usage-control="guide_copy"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" /><path d="M12 4V3H3v9h1" /></svg>{copyStatus?.id === card.id ? "Copy again" : "Copy"}</button></div>}
        {copyStatus?.id === card.id && <p className={styles.cardCopyStatus}>{copyStatus.message}</p>}
        <details className={styles.cardDetails} data-usage-control="guide_details"><summary>What happens next <span aria-hidden="true">+</span></summary><p>{card.result}</p>{card.note && <p className={styles.note}>{card.note}</p>}</details>
        {card.settingsLink && <p><Link href={card.settingsLink.href}>{card.settingsLink.title}</Link></p>}
        {needsSetup ? <>{blocked && <p role="status">{blocked}</p>}<Link className={styles.cardLink} href="/settings/local/capabilities" data-usage-control="guide_open">Open your feature setup<span aria-hidden="true">↗</span></Link></>
          : <>{local && prompt && ventureId && <button type="button" className={styles.promptAction} aria-label={`Try this in chat: ${card.title}`} onClick={() => prefillChat(prompt)} data-usage-control="guide_start" data-guide-prompt={card.id}>Try this in chat</button>}
            {destination && <Link className={styles.cardLink} href={destination} data-usage-control="guide_open">{!ventureId && destination === "/" && card.destination !== "ventures" ? "Choose a venture to explore" : card.action}<span aria-hidden="true">↗</span></Link>}</>}
        </div>
      </article>;
      })}</div>}
      {upcoming.length > 0 && <details className={styles.upcoming} open={topic === "Coming next" || Boolean(query)} data-usage-control="guide_upcoming"><summary><span className={styles.upcomingGlyph} aria-hidden="true">◌</span><span><strong>Coming soon: a look ahead</strong><small>These capabilities are not available generally yet. No release date is promised.</small></span><span className={styles.upcomingCount}>{upcoming.length}<span aria-hidden="true"> +</span></span></summary><div className={styles.upcomingGrid}>{upcoming.map(card => <article key={card.id}><p className={styles.eyebrow}>COMING NEXT</p><h3>{card.title}</h3><p>{card.description}</p><p>{card.result}</p><p className={styles.note}>{card.note}</p></article>)}</div></details>}
      {matches.length === 0 && <div className={styles.empty}><h3>No matches yet</h3><p>Try a broader word, another stage or all topics.</p><button type="button" onClick={resetFilters} data-usage-control="guide_reset">Clear filters</button></div>}
      <p className={styles.availabilityNote}>{local ? "You control feature switches in Settings > Capabilities and save keys in Settings > Keys and connections. Each blocked feature shows your next setup step. Copying an example does not start work. Sending keeps your requested scope, source disclosure, opt-out, suppression checks and send caps." : "The actual screen shows what your workspace can use. Some actions need a connected account, review or preview access. Copying an example does not start work."}</p>
      <p className={styles.copyStatus} role="status" aria-live="polite">{copyStatus?.message || ""}</p>
    </section>

    {!local && <section id="guide-tips" className={styles.lessonSection} aria-labelledby="guide-tips-title" tabIndex={-1}><div className={styles.sectionHead}><div><p className={styles.eyebrow}>SMALL HABITS, USEFUL RESULTS</p><h2 id="guide-tips-title">Power user tips</h2></div><p>Try a prompt. Review before you send.</p></div><div className={styles.tipGrid}>{content.tips.map(tip => <article key={tip.id} className={styles.tipCard} data-guide-reveal="ready" aria-labelledby={`guide-${tip.id}`}><h3 id={`guide-${tip.id}`}>{tip.title}</h3><p>{tip.text}</p><PromptAction item={tip} ventureId={ventureId} /></article>)}</div></section>}
    <section id="guide-faq" className={styles.faq} aria-labelledby="guide-faq-title" tabIndex={-1}><div><p className={styles.eyebrow}>A FEW REASSURING ANSWERS</p><h2 id="guide-faq-title">FAQs</h2><p>You stay in control.<br />The review screen is the place to decide.</p></div><div>{content.faqs.map(faq => <details key={faq.question} data-usage-control="guide_faq"><summary>{faq.question}<span aria-hidden="true">+</span></summary><p>{faq.answer}</p>{faq.settingsLink && <p><Link href={faq.settingsLink.href}>{faq.settingsLink.title}</Link></p>}</details>)}</div></section>
    <footer className={styles.footer}><img src="/charlie/charlie-mark.png" width={40} height={40} alt="Charlie" loading="lazy" onError={event => { event.currentTarget.style.display = "none"; }} /><p>You do not need to do everything.<br /><strong>Just choose the next useful step.</strong></p><a className={styles.textLink} href="#guide-begin" onClick={jumpToSection}>Back to start ↑</a></footer>
  </div>;
}

export function GuidePage({ ventureId }: { ventureId?: string }) {
  return <AppShell ventureId={ventureId} title="How to use Supymem"><CapabilityGuide ventureId={ventureId} /></AppShell>;
}
