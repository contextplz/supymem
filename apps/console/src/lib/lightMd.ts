/** Small markdown subset for chat and task briefs. Escapes HTML first. */
export function renderLightMd(md: string): string {
  const escaped = md
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const blocks = escaped.split(/\n{2,}/).map((block) => {
    const lines = block.split("\n");
    const list = lines.every((line) => /^\s*[-*]\s+/.test(line) || !line.trim());
    if (list) {
      const items = lines
        .filter((line) => line.trim())
        .map((line) => `<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`)
        .join("");
      return `<ul>${items}</ul>`;
    }
    return `<p>${inline(lines.join("<br />"))}</p>`;
  });
  return blocks.join("");
}

function inline(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
