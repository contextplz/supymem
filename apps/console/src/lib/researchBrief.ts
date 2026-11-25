/** Pure helpers for the scannable research view. Presentation only. */

/** Em dashes read as AI filler: render as a comma. */
export function noDash(text: string): string {
  return text.replace(/\bno_customer_conversation\b/gi, "No customer conversation recorded yet")
    .replace(/[ \t]*[\u2014\u2013][ \t]*/g, ", ");
}

export function plainText(text: string): string {
  return text.replace(/&(?:amp|lt|gt|quot|apos|nbsp);|&#(?:\d+|x[\da-f]+);/gi, entity => {
    const named: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&nbsp;": " " };
    const point = /^&#x/i.test(entity) ? parseInt(entity.slice(3, -1), 16) : Number(entity.slice(2, -1));
    return named[entity.toLowerCase()] ?? (point > 0 && point <= 0x10ffff ? String.fromCodePoint(point) : entity);
  }).replace(/!\[[^\]]*\]\([^\n]*?\)|!\[[^\]]*\]\[[^\]]*\]/g, " ")
    .replace(/\[([^\]]*)\]\([^\n]*?\)|\[([^\]]*)\]\[[^\]]*\]/g, (_, label: string, reference: string) => label || reference || "")
    .replace(/^\s*(?:\[[^\]]+\]:\s*\S+.*|(?:[-+*•]|\d+[.)]|>+)\s+)/gm, " ")
    .replace(/(?<!\S)(?:[-*•]|\d+[.)])\s+/g, " ")
    .replace(/<(?:script|style)\b[^>]*>[\s\S]*?<\/(?:script|style)>/gi, " ")
    .replace(/<[^>]+>|https?:\/\/\S+/g, " ").replace(/[*_`#~]+/g, "")
    .replace(/\s+/g, " ").trim();
}

export function productDescription(text: string): string {
  const clean = text.replace(/!\[[^\]]*\]\([^\n]*?\)/g, " ")
    .replace(/\[([^\]]*)\]\(([^\n]*?)\)/g, (_, label: string, url: string) =>
      /#(?:main|content)\b|\/(?:cart|login|account)\b/i.test(url) || /^(?:skip\b|home$|menu$|sign (?:in|up)$|log in$|contact(?: us)?$|about(?: us)?$|shop(?: now)?$|privacy|terms|cookie|free bag|.*join today)/i.test(label.replace(/\*/g, "").trim()) ? " " : label)
    .replace(/<(?:nav|header|footer|script|style)\b[^>]*>[\s\S]*?<\/(?:nav|header|footer|script|style)>/gi, " ")
    .replace(/<[^>]+>|https?:\/\/\S+|[*_`#]+/g, " ")
    .replace(/\bskip to (?:main )?content\b|\b\d(?:\.\d)?\s*[★☆][^.]*?\b(?:reviews?|ratings?)\b/gi, " ")
    .split(/\r?\n/).filter(line => !/^\s*(?:home|menu|sign in|log in|privacy policy|terms of service|cookie settings|all rights reserved)\s*$/i.test(line))
    .join("\n");
  const description = plainText(clean).split(/(?<=[.!?])\s+/).slice(0, 2).join(" ");
  return description.length <= 280 ? description : `${description.slice(0, 277).replace(/\s+\S*$/, "").replace(/[., ]+$/, "")}…`;
}

/** First sentence of text, in full. `max` is kept for callers but never cuts content. */
export function oneLine(text: string | undefined, _max = 90): string {
  const src = noDash((text || "").replace(/\s+/g, " ").trim());
  return src.match(/^.+?[.!?](?=\s|$)/)?.[0] || src;
}

/** Verdict headline in full. `n` is kept for callers but never cuts content. */
export function headline(text: string | undefined, _n = 6): string {
  return noDash((text || "").trim().replace(/\s+/g, " "));
}

/** Hostname without "www.", or "" for anything that is not an http(s) URL. */
export function sourceDomain(url: string | null | undefined): string {
  try {
    const u = new URL(url || "");
    return /^https?:$/.test(u.protocol) ? u.hostname.replace(/^www\./, "") : "";
  } catch { return ""; }
}

/** 0-3 filled segments for an evidence dimension; unknown is always 0 (never implies strength). */
export function evidenceSegments(status: string | undefined, supportCount = 0): number {
  if (!status || status === "unknown") return 0;
  return Math.min(3, Math.max(1, supportCount));
}

/** How many dimensions have any evidence at all, e.g. 3 of 7. Honest coverage, not a score. */
export function evidenceCoverage(dims: Record<string, { status?: string }> | undefined): { known: number; total: number } {
  const all = Object.values(dims || {});
  return { known: all.filter((d) => d.status && d.status !== "unknown").length, total: all.length };
}
