const BLOCK_RE = /^(```|\s*(?:[-*+]|\d+[.)])\s)/m;

/**
 * Brief version of a chat reply: first 2 sentences / 280 chars of the prose
 * before any code fence or list. Never cuts inside code or a list: a reply that
 * opens with one is returned whole.
 */
export function briefBody(md: string, maxChars = 280): { text: string; truncated: boolean } {
  // Essential content stays whole: questions and anything short enough to read at a glance.
  if (/\?/.test(md) || (md.split("\n").length <= 8 && md.length <= 700)) return { text: md, truncated: false };
  const block = md.search(BLOCK_RE);
  const prose = (block < 0 ? md : md.slice(0, block)).trimEnd();
  if (!prose.trim()) return { text: md, truncated: false };
  let out = "", count = 0;
  for (const m of prose.matchAll(/[^.!?\n]+(?:[.!?]+(?=\s|$)|\n\n|$)\s*/g)) {
    if (count === 2 || (out && out.length + m[0].length > maxChars)) break;
    out += m[0]; count++;
  }
  out = (out || prose).trimEnd();
  if (out.length > maxChars) out = out.slice(0, maxChars).replace(/\s+\S*$/, "") + "…";
  return { text: out, truncated: out !== md.trimEnd() };
}

/** Drop a paragraph that repeats an earlier one verbatim (backend sometimes prepends the question it also structures). */
export function dedupeParagraphs(md: string): string {
  const seen = new Set<string>();
  // Fenced blocks (drafts) legitimately repeat greetings and sign-offs; dedupe only the prose between them.
  return md.split(/(```[^\n`]*\n[\s\S]*?(?:\n?```|$))/).map((part, index) => index % 2 ? part : part.split(/\n{2,}/).filter((p) => {
    const k = p.replace(/\s+/g, " ").trim().toLowerCase();
    if (k.length < 12) return true;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  }).join("\n\n")).join("");
}
