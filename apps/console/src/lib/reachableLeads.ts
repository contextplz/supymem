import { safeEvidenceUrl, type ReachableLead, type ReachRoute } from "./gtmJourneyApi.ts";

export function guessedEmailWarning(route: ReachRoute | undefined): string | null {
  return route && ["guess", "pattern"].includes(route.route_type || "")
    ? `We guessed this address (${Math.max(0, Math.min(100, Math.round(route.confidence || 0)))}% confidence). It may bounce.` : null;
}

export function reachAction(lead: Pick<ReachableLead, "name" | "company" | "draft" | "connection_note" | "x_note">, route: ReachRoute): { url: string; text: string; label: string } | null {
  if (route.kind === "email") {
    if (!route.email || !/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(route.email)) return null;
    if (/^(careers|jobs|job|recruitment|recruit|vacancies|hr|feedback|complaints|privacy|dpo|gdpr|data|accounts|billing|invoices|finance|payroll|noreply|no-reply|donotreply|abuse|webmaster|postmaster|press|media|marketing-unsubscribe)$/i.test(route.email.split("@")[0])) return null;
    return { url: `mailto:${encodeURIComponent(route.email)}?subject=${encodeURIComponent(`A question for ${lead.company || lead.name}`)}&body=${encodeURIComponent(lead.draft)}`, text: lead.draft, label: "Copy email + open mail" };
  }
  if (route.kind === "phone") {
    const phone = (route.value || "").replace(/[ ().-]/g, "");
    return /^\+?\d{8,15}$/.test(phone) ? { url: `tel:${phone}`, text: lead.draft, label: "Open phone" } : null;
  }
  const url = safeEvidenceUrl(route.url);
  if (!url) return null;
  if (route.kind === "booking") {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || !parsed.pathname.replace(/\//g, "") || !(["calendly.com", "cal.com", "calendar.app.google", "meetings.hubspot.com"].includes(parsed.hostname)
      || parsed.hostname === "calendar.google.com" && parsed.pathname.startsWith("/calendar/appointments/"))) return null;
    return { url, text: lead.draft, label: "Open booking page" };
  }
  if (route.kind === "linkedin") {
    const parsed = new URL(url);
    if (!/(^|\.)linkedin\.com$/.test(parsed.hostname) || !/^\/in\/[^/]+\/?$/.test(parsed.pathname)) return null;
    return { url, text: lead.connection_note.slice(0, 300), label: "Open LinkedIn" };
  }
  if (route.kind === "contact_form") return { url, text: lead.draft, label: "Open contact form" };
  if (route.kind === "website") return { url, text: lead.draft, label: "Open their website" };
  if (route.kind === "x") {
    const parsed = new URL(url);
    if (!/(^|\.)(x|twitter)\.com$/.test(parsed.hostname) || !/^\/[A-Za-z0-9_]{1,15}\/?$/.test(parsed.pathname) || /^\/(home|search|explore|i|intent|share|settings|messages|notifications|hashtag|lists|login|signup|compose|help|about|privacy|tos|download)\/?$/i.test(parsed.pathname)) return null;
    return { url, text: (lead.x_note || lead.connection_note).slice(0, 280), label: "Open X" };
  }
  if (route.kind === "forum") return { url, text: lead.draft, label: route.profile ? "Open author profile" : /(^|\.)reddit\.com$/.test(new URL(url).hostname) ? "Reply on Reddit" : "Reply in forum" };
  return null;
}
