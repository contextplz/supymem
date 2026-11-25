const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const ELEMENTS = new Set("svg g defs title desc rect path circle ellipse line polyline polygon text tspan textPath marker linearGradient radialGradient stop clipPath mask pattern symbol a image use".split(" "));
const PROMPT_HANDLER = /^sendPrompt\('[^'"\\()<>\x00-\x1f\x7f\u2028\u2029]{0,200}'\)$/;

function decodeXml(text: string): string {
  return text.replace(/&(#x[\da-fA-F]+|#\d+|amp|lt|gt|quot|apos);|&/g, (entity, name: string | undefined) => {
    if (!name) throw new Error("Invalid entity");
    if (!name.startsWith("#")) return ({ amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" } as Record<string, string>)[name];
    const code = name.startsWith("#x") ? parseInt(name.slice(2), 16) : Number(name.slice(1));
    if (!(code === 9 || code === 10 || code === 13 || code >= 32 && code <= 0xd7ff || code >= 0xe000 && code <= 0xfffd || code >= 0x10000 && code <= 0x10ffff)) throw new Error("Invalid character");
    return String.fromCodePoint(code);
  });
}

function escapeXml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function sanitizeDiagramSvg(svg: string): string | null {
  if (typeof svg !== "string" || !/^<svg(?=[\s/>])/.test(svg) || svg.length > 65536 || new TextEncoder().encode(svg).length > 65536 || /[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]/u.test(svg)) return null;
  try {
    const stack: { name: string; keep: boolean }[] = [];
    const output: string[] = [];
    const tag = /<(\/?)([A-Za-z_][\w:.-]*)((?:\s+[A-Za-z_][\w:.-]*\s*=\s*(?:"[^"<]*"|'[^'<]*'))*)\s*(\/?)>/y;
    let position = 0;
    let hasRoot = false;
    while (position < svg.length) {
      if (svg.startsWith("<!--", position)) {
        const end = svg.indexOf("-->", position + 4);
        if (end < 0 || svg.slice(position + 4, end).includes("--") || svg[end - 1] === "-") return null;
        position = end + 3;
        continue;
      }
      if (svg.startsWith("<![CDATA[", position)) {
        const end = svg.indexOf("]]>", position + 9);
        if (end < 0 || !stack.length) return null;
        if (stack[stack.length - 1].keep) output.push(escapeXml(svg.slice(position + 9, end)));
        position = end + 3;
        continue;
      }
      if (svg[position] !== "<") {
        const next = svg.indexOf("<", position);
        const end = next < 0 ? svg.length : next;
        const text = svg.slice(position, end);
        if ((!stack.length && text.trim()) || text.includes("]]>")) return null;
        const decoded = decodeXml(text);
        if (stack.length && stack[stack.length - 1].keep) output.push(escapeXml(decoded));
        position = end;
        continue;
      }
      tag.lastIndex = position;
      const match = tag.exec(svg);
      if (!match) return null;
      const [, closing, name, rawAttributes, selfClosing] = match;
      position = tag.lastIndex;
      if (closing) {
        if (rawAttributes || selfClosing) return null;
        const opened = stack.pop();
        if (!opened || opened.name !== name) return null;
        if (opened.keep) output.push(`</${name}>`);
        continue;
      }
      const attributes = new Map<string, string>();
      for (const attribute of rawAttributes.matchAll(/([A-Za-z_][\w:.-]*)\s*=\s*("[^"]*"|'[^']*')/g)) {
        if (attributes.has(attribute[1])) return null;
        attributes.set(attribute[1], decodeXml(attribute[2].slice(1, -1)));
      }
      if (!stack.length) {
        if (hasRoot || name !== "svg" || attributes.has("xmlns") && attributes.get("xmlns") !== SVG_NAMESPACE) return null;
        hasRoot = true;
      }
      const externalHref = [...attributes].some(([key, value]) => ["href", "xlink:href"].includes(key.toLowerCase()) && !value.startsWith("#"));
      const keep = (!stack.length || stack[stack.length - 1].keep) && ELEMENTS.has(name)
        && (!attributes.has("xmlns") || attributes.get("xmlns") === SVG_NAMESPACE)
        && (!(name === "image" || name === "use") || !externalHref);
      if (keep) {
        const cleaned = new Map<string, string>();
        for (const [key, value] of attributes) {
          const lower = key.toLowerCase();
          if (lower.startsWith("on") && !(lower === "onclick" && PROMPT_HANDLER.test(value))) continue;
          if (["href", "xlink:href"].includes(lower)) {
            if (value.startsWith("#") && (lower === "href" || !attributes.has("href"))) cleaned.set("href", value);
            continue;
          }
          if (lower === "style" && /url\s*\(|expression\s*\(|\\|\/\*/i.test(value)) continue;
          if (lower === "xmlns" || key.includes(":") || lower === "src" || lower === "srcdoc") continue;
          cleaned.set(lower.startsWith("on") ? lower : key, value);
        }
        if (!stack.length) cleaned.set("xmlns", SVG_NAMESPACE);
        output.push(`<${name}${[...cleaned].map(([key, value]) => ` ${key}="${escapeXml(value)}"`).join("")}${selfClosing ? "/" : ""}>`);
      }
      if (!selfClosing) stack.push({ name, keep });
    }
    if (stack.length || !hasRoot) return null;
    if (typeof DOMParser !== "undefined") {
      const document = new DOMParser().parseFromString(svg, "image/svg+xml");
      if (document.querySelector("parsererror") || document.documentElement.localName !== "svg" || document.documentElement.namespaceURI && document.documentElement.namespaceURI !== SVG_NAMESPACE) return null;
    }
    return output.join("");
  } catch {
    return null;
  }
}
