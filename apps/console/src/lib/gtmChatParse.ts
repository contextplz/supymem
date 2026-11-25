import type { AiGtmContact, AiGtmLead } from "./opsApi.ts";

export type GtmChatIntent =
  | "find"
  | "download_json"
  | "download_xlsx"
  | "download_csv"
  | "outreach_email"
  | "outreach_linkedin"
  | "outreach_both"
  | "help"
  | "unknown";

export type GtmLeadFilters = {
  nameStartsWith?: string;
  nameEndsWith?: string;
  region?: string;
  industry?: string;
  count: number;
  query: string;
};

export type ParsedGtmChat = {
  intent: GtmChatIntent;
  filters: GtmLeadFilters;
  reply: string;
};

const HELP_TEXT =
  "Try something like: find AI companies in San Francisco starting with S and ending with M. " +
  "After results, say download excel, download csv, download json, email them, or linkedin them.";

function companyName(lead: AiGtmLead | AiGtmContact): string {
  if ("organization" in lead && lead.organization) return String(lead.organization).trim();
  if ("company" in lead && typeof (lead as { company?: string }).company === "string") {
    return String((lead as { company?: string }).company).trim();
  }
  return "";
}

export function parseGtmChat(raw: string): ParsedGtmChat {
  const text = raw.trim();
  const lower = text.toLowerCase();
  const filters: GtmLeadFilters = {
    count: 25,
    query: text,
    industry: "B2B",
  };

  const countMatch =
    lower.match(/\b(?:find|get|show|top|first)\s+(\d{1,3})\b/) ||
    lower.match(/\b(\d{1,3})\s+(?:leads?|companies|contacts)\b/);
  if (countMatch) {
    filters.count = Math.min(100, Math.max(1, Number(countMatch[1])));
  }

  const startMatch = lower.match(/start(?:ing)?\s+with\s+['"]?([a-z0-9])/i);
  if (startMatch) filters.nameStartsWith = startMatch[1].toLowerCase();

  const endMatch = lower.match(/end(?:ing)?\s+with\s+['"]?([a-z0-9])/i);
  if (endMatch) filters.nameEndsWith = endMatch[1].toLowerCase();

  const regionMatch =
    lower.match(
      /\bin\s+([a-z][a-z\s.-]{1,40}?)(?:\s+(?:specifically|with|that|starting|ending|and|,|\.|$))/i
    ) ||
    lower.match(
      /\bin\s+(san\s+francisco|sf|new\s+york|nyc|bangalore|london|berlin|austin|toronto)\b/i
    );
  if (regionMatch) {
    let region = regionMatch[1].trim().replace(/\s+/g, " ");
    if (region === "sf" || region.replace(/\s+/g, "") === "sanfrancisco") {
      region = "San Francisco";
    }
    if (region === "nyc") region = "New York";
    filters.region = region.replace(/\b\w/g, (c) => c.toUpperCase());
  }

  if (/\bai\b/.test(lower) || /artificial intelligence/.test(lower)) {
    filters.industry = "AI";
  } else if (/saas|b2b/.test(lower)) {
    filters.industry = "B2B SaaS";
  } else if (/fintech/.test(lower)) {
    filters.industry = "Fintech";
  } else if (/legal/.test(lower)) {
    filters.industry = "Legal";
  }

  // Preserve the founder's actual sector and constraints. A handful of keyword
  // shortcuts must not rewrite rehearsal studios into generic B2B founders.

  if (/^(help|how|what can you|commands?)\b/.test(lower) || lower === "?") {
    return { intent: "help", filters, reply: HELP_TEXT };
  }

  if (
    /download\s+(excel|xlsx|sheet|spreadsheet)/.test(lower) ||
    /excel\s+(please|sheet|file)/.test(lower) ||
    lower === "excel"
  ) {
    return {
      intent: "download_xlsx",
      filters,
      reply: "Preparing an Excel download of the latest lead set.",
    };
  }

  if (/download\s+json/.test(lower) || lower === "json" || /export\s+json/.test(lower)) {
    return {
      intent: "download_json",
      filters,
      reply: "Preparing a JSON download of the latest lead set.",
    };
  }

  if (
    /download\s+csv/.test(lower) ||
    lower === "csv" ||
    /export\s+csv/.test(lower)
  ) {
    return {
      intent: "download_csv",
      filters,
      reply: "Preparing a CSV download of the latest lead set.",
    };
  }

  if (
    /^(?:(?:please|hey)\s+)?(?:find|search|get|show|look\s+up|discover)\b/.test(lower)
  ) {
    return { intent: "find", filters, reply: `Searching for “${filters.query}” (up to ${filters.count} results). No outreach will be started.` };
  }

  const negatedAction = /\b(do not|don't|never|without|no)\b/.test(lower);
  if (!negatedAction && (
    /(?:outreach|push|convert).*(?:both|email.*(?:and|&).*linkedin|linkedin.*(?:and|&).*email)/.test(lower)
    || /(?:send|prepare|draft)\s+(?:them\s+)?(?:both|everywhere)/.test(lower)
    || /^(?:email.*(?:and|&).*linkedin|linkedin.*(?:and|&).*email)\s+them[.!]?$/.test(lower)
  )) {
    return { intent: "outreach_both", filters, reply: "Preparing email and LinkedIn drafts for the displayed leads. External delivery requires review." };
  }

  if (!negatedAction && (
    /\b(linkedin|li)\b/.test(lower) &&
    (/(send|outreach|push|message|connect|queue|draft|prepare)/.test(lower) || /^linkedin\s+them[.!]?$/.test(lower))
  )) {
    return {
      intent: "outreach_linkedin",
      filters,
      reply: "Preparing LinkedIn outreach for the displayed leads. Review is required before delivery.",
    };
  }

  if (!negatedAction && (
    /\b(email|gmail|mail)\b/.test(lower) &&
    (/(send|outreach|push|message|queue|cold|draft|prepare)/.test(lower) || /^email\s+them[.!]?$/.test(lower))
  )) {
    return {
      intent: "outreach_email",
      filters,
      reply: "Preparing email drafts for the displayed leads. Each exact message requires review before sending.",
    };
  }

  if (
    /(find|search|get|show|look\s+up|discover|leads?|companies)/.test(lower) ||
    filters.nameStartsWith ||
    filters.nameEndsWith ||
    filters.region
  ) {
    const bits = [
      `Searching for ${filters.query}`,
      filters.nameStartsWith
        ? `names starting with “${filters.nameStartsWith.toUpperCase()}”`
        : null,
      filters.nameEndsWith
        ? `ending with “${filters.nameEndsWith.toUpperCase()}”`
        : null,
      `(up to ${filters.count})`,
    ].filter(Boolean);
    return {
      intent: "find",
      filters,
      reply: `${bits.join(", ")}. No outreach will be started.`,
    };
  }

  return {
    intent: "unknown",
    filters,
    reply: `I didn’t catch that. ${HELP_TEXT}`,
  };
}

export function filterLeads(leads: AiGtmLead[], filters: GtmLeadFilters): AiGtmLead[] {
  return leads.filter((lead) => {
    const name = companyName(lead).toLowerCase();
    if (!name && (filters.nameStartsWith || filters.nameEndsWith)) return false;
    if (filters.nameStartsWith && !name.startsWith(filters.nameStartsWith)) {
      return false;
    }
    if (filters.nameEndsWith && !name.endsWith(filters.nameEndsWith)) {
      return false;
    }
    if (filters.region) {
      const needle = filters.region.toLowerCase();
      const hay = `${lead.city || ""} ${lead.country || ""}`.toLowerCase();
      return hay.includes(needle);
    }
    return true;
  }).slice(0, filters.count);
}

export function contactIdsFromLeads(
  leads: AiGtmLead[],
  contacts: AiGtmContact[]
): string[] {
  const byEmail = new Map(
    contacts
      .filter((c) => c.email)
      .map((c) => [c.email.toLowerCase(), c.id] as const)
  );
  const byLinkedIn = new Map(
    contacts
      .filter((c) => c.linkedin_url)
      .map((c) => [c.linkedin_url!.toLowerCase(), c.id] as const)
  );
  const ids: string[] = [];
  for (const lead of leads) {
    const email = (lead.email || "").toLowerCase();
    const li = (lead.linkedin_url || "").toLowerCase();
    const id = (email && byEmail.get(email)) || (li && byLinkedIn.get(li));
    if (id && !ids.includes(id)) ids.push(id);
  }
  // Never substitute unrelated workspace contacts when displayed leads have no
  // exact identifier match. That would draft outreach to different recipients.
  return ids;
}

export function summarizeLeads(leads: AiGtmLead[]): string {
  if (!leads.length) return "No matching leads.";
  const lines = leads.slice(0, 8).map((lead, i) => {
    const name = companyName(lead) || "Unknown company";
    const person = [lead.first_name, lead.last_name].filter(Boolean).join(" ");
    const email = lead.email || "no email";
    return `${i + 1}. ${name}${person ? ` Not recorded ${person}` : ""} (${email})`;
  });
  const more = leads.length > 8 ? `\n…and ${leads.length - 8} more.` : "";
  return `Found ${leads.length} lead${leads.length === 1 ? "" : "s"}:\n${lines.join("\n")}${more}`;
}
