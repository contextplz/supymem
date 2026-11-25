function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderEmphasis(text: string): string {
  return escapeHtml(text.replace(/[ \t]*\u2014[ \t]*/g, ", "))
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

/** Emphasis plus clickable bare http(s) URLs; trailing punctuation (and an unmatched ")") stays outside the link. */
function renderPlain(text: string): string {
  let html = "";
  let previous = 0;
  for (const m of text.matchAll(/https?:\/\/[^\s<>"']+/gi)) {
    let url = m[0];
    for (;;) {
      const last = url[url.length - 1];
      const open = (url.match(/\(/g) ?? []).length;
      const close = (url.match(/\)/g) ?? []).length;
      if (/[.,;:!?\]}]/.test(last) || (last === ")" && close > open)) url = url.slice(0, -1);
      else break;
    }
    if (!/^https?:\/\/[^/?#]/i.test(url)) continue;
    const index = m.index ?? previous;
    html += renderEmphasis(text.slice(previous, index));
    html += `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>`;
    previous = index + url.length;
  }
  return html + renderEmphasis(text.slice(previous));
}

function safeLink(href: string): boolean {
  return /^(?:https?:\/\/|mailto:)/i.test(href)
    || (href.startsWith("/") && !href.startsWith("//") && !href.includes("\\"));
}

function linkEnd(text: string, start: number): number {
  let depth = 0;
  for (let index = start; index < text.length && !/\s/.test(text[index]); index++) {
    if (text[index] === "(") depth++;
    else if (text[index] === ")") {
      if (depth === 0) return index;
      depth--;
    }
  }
  return -1;
}

/** Render the small Markdown subset used in chat without accepting HTML or unsafe links. */
export function renderLightMd(md: string): string {
  const token = /`([^`\n]+)`|\[([^\]\n]+)\]\(/g;
  let html = "";
  let previous = 0;
  for (let match = token.exec(md); match; match = token.exec(md)) {
    const index = match.index;
    const end = match[1] === undefined ? linkEnd(md, token.lastIndex) : token.lastIndex;
    if (match[1] === undefined && end <= token.lastIndex) continue;
    html += renderPlain(md.slice(previous, index));
    if (match[1] !== undefined) {
      html += `<code>${escapeHtml(match[1])}</code>`;
    } else {
      const href = md.slice(token.lastIndex, end);
      token.lastIndex = end + 1;
      if (safeLink(href)) {
        const external = /^https?:\/\//i.test(href) ? ` target="_blank" rel="noopener noreferrer"` : "";
        html += `<a href="${escapeHtml(href)}"${external}>${escapeHtml(match[2].replace(/[ \t]*\u2014[ \t]*/g, ", "))}</a>`;
      } else {
        html += renderEmphasis(`[${match[2]}]`) + `(${escapeHtml(href)})`;
      }
    }
    previous = token.lastIndex;
  }
  return html + renderPlain(md.slice(previous));
}

export type MdSegment = { code: boolean; text: string };

/** Split fenced ``` blocks from prose so the chat can show them as copyable blocks, never as raw fences. */
export function splitFences(md: string): MdSegment[] {
  const out: MdSegment[] = [];
  let previous = 0;
  for (const m of md.matchAll(/```[^\n`]*\n([\s\S]*?)(?:\n?```|$)/g)) {
    const index = m.index ?? previous;
    if (md.slice(previous, index).trim()) out.push({ code: false, text: md.slice(previous, index) });
    out.push({ code: true, text: m[1] });
    previous = index + m[0].length;
  }
  if (md.slice(previous).trim() || out.length === 0) out.push({ code: false, text: md.slice(previous) });
  return out;
}
