"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { sanitizeDiagramSvg } from "@/lib/diagramSanitize";
import styles from "./DiagramCard.module.css";

/** Chat message part: {type:"diagram", title:string, svg:string}. */
export type DiagramMessagePart = { type: "diagram"; title: string; svg: string };

const DIAGRAM_STYLES = `
:root { --text-primary:#111827; --text-secondary:#475569; --border:rgba(32,24,18,.1); --surface-1:#ffffff; font-family:system-ui,sans-serif; color-scheme:light; }
html,body { margin:0; padding:0; overflow:hidden; background:var(--surface-1); }
svg { width:100%; height:auto; display:block; font-family:system-ui,sans-serif; }
.t,.th { font-size:14px; font-weight:400; fill:var(--text-primary); }
.th { font-weight:500; }
.ts { font-size:12px; font-weight:400; fill:var(--text-secondary); }
.box { fill:var(--surface-1); stroke:var(--border); stroke-width:.5px; }
.arr { stroke:var(--text-secondary); stroke-width:1.5px; fill:none; }
.c-purple { --r-fill:#ede9fe; --r-stroke:#7c3aed; --r-text:#4c1d95; --r-secondary:#6d28d9; }
.c-teal { --r-fill:#ccfbf1; --r-stroke:#0f766e; --r-text:#134e4a; --r-secondary:#115e59; }
.c-coral { --r-fill:#ffe4e6; --r-stroke:#e11d48; --r-text:#881337; --r-secondary:#be123c; }
.c-gray { --r-fill:#f1f5f9; --r-stroke:#64748b; --r-text:#0f172a; --r-secondary:#475569; }
.c-blue { --r-fill:#dbeafe; --r-stroke:#2563eb; --r-text:#1e3a8a; --r-secondary:#1d4ed8; }
.c-amber { --r-fill:#fef3c7; --r-stroke:#d97706; --r-text:#78350f; --r-secondary:#b45309; }
:is(.c-purple,.c-teal,.c-coral,.c-gray,.c-blue,.c-amber) rect { fill:var(--r-fill); stroke:var(--r-stroke); }
:is(.c-purple,.c-teal,.c-coral,.c-gray,.c-blue,.c-amber) .th { fill:var(--r-text); }
:is(.c-purple,.c-teal,.c-coral,.c-gray,.c-blue,.c-amber) .ts { fill:var(--r-secondary); }
.node { cursor:pointer; }
.node:hover { opacity:.85; }
[onclick]:focus-visible { outline:2px solid var(--text-secondary); outline-offset:2px; }
`;

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    try { link.click(); } finally { link.remove(); }
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export function renderDiagramPart(part: unknown, onPrompt: (text: string) => void) {
  if (!part || typeof part !== "object") return null;
  const diagram = part as Partial<DiagramMessagePart>;
  return diagram.type === "diagram" && typeof diagram.title === "string" && typeof diagram.svg === "string"
    ? <DiagramCard title={diagram.title} svg={diagram.svg} onPrompt={onPrompt} /> : null;
}

export function DiagramCard({ title, svg, onPrompt }: {
  title: string; svg: string; onPrompt: (text: string) => void;
}) {
  const sanitized = useMemo(() => sanitizeDiagramSvg(svg), [svg]);
  const id = useId();
  const iframe = useRef<HTMLIFrameElement>(null);
  const [theme, setTheme] = useState<"light">("light");
  const [height, setHeight] = useState(150);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState("");
  const label = title.replace(/_/g, " ").trim().toLowerCase();
  const heading = label ? label[0].toUpperCase() + label.slice(1) : "Diagram";
  const filename = title.trim().replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 100) || "diagram";

  useEffect(() => {
    if (!sanitized) return;
    setTheme("light");  // the app is light-only
  }, [sanitized]);

  useEffect(() => {
    if (!sanitized) return;
    const receive = (event: MessageEvent) => {
      if (!iframe.current?.contentWindow || event.source !== iframe.current.contentWindow) return;
      const data = event.data;
      if (!data || typeof data !== "object" || data.source !== "supymem-diagram" || data.id !== id) return;
      if (data.type === "height" && typeof data.h === "number" && Number.isFinite(data.h) && data.h > 0 && data.h <= 32768) setHeight(Math.ceil(data.h));
      if (data.type === "prompt" && typeof data.text === "string") {
        const text = data.text.trim().slice(0, 200);
        if (text) onPrompt(text);
      }
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [id, onPrompt, sanitized]);

  const srcDoc = useMemo(() => `<!doctype html><html data-theme="${theme}"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"><style>${DIAGRAM_STYLES}</style></head><body>${sanitized || ""}<script>
(() => {
  const id = ${JSON.stringify(id).replace(/</g, "\\u003c")};
  const post = data => parent.postMessage({source:'supymem-diagram', id, ...data}, '*');
  window.sendPrompt = text => { if (typeof text === 'string') post({type:'prompt', text}); };
  const svg = document.querySelector('svg');
  const resize = () => post({type:'height', h:Math.ceil(svg.getBoundingClientRect().height)});
  new ResizeObserver(resize).observe(svg);
  resize();
  document.querySelectorAll('[onclick]').forEach(node => {
    node.setAttribute('tabindex', '0');
    node.setAttribute('role', 'button');
    if (!node.getAttribute('aria-label')) node.setAttribute('aria-label', node.textContent.trim() || 'Ask about this node');
    node.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); node.dispatchEvent(new MouseEvent('click', {bubbles:true})); }
    });
  });
})();
</script></body></html>`, [id, sanitized, theme]);

  async function downloadPng() {
    if (!sanitized) return;
    setNotice("");
    setExporting(true);
    try {
      const root = new DOMParser().parseFromString(sanitized, "image/svg+xml").documentElement;
      const viewBox = (root.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
      const validViewBox = viewBox.length === 4 && viewBox.every(Number.isFinite) && viewBox[2] > 0 && viewBox[3] > 0;
      const dimension = (attribute: string) => {
        const value = root.getAttribute(attribute) || "";
        return /^\d+(?:\.\d+)?(?:px)?$/.test(value) ? parseFloat(value) : 0;
      };
      const width = dimension("width") || (validViewBox ? viewBox[2] : iframe.current?.clientWidth || 640);
      const imageHeight = dimension("height") || (validViewBox ? viewBox[3] : height);
      if (width <= 0 || imageHeight <= 0 || width * imageHeight > 16777216 || Math.max(width, imageHeight) > 8192) throw new Error("Export dimensions unavailable");
      root.setAttribute("width", String(width));
      root.setAttribute("height", String(imageHeight));
      root.setAttribute("data-theme", theme);
      const stylesheet = document.createElementNS("http://www.w3.org/2000/svg", "style");
      stylesheet.textContent = DIAGRAM_STYLES;
      root.prepend(stylesheet);
      const image = new Image();
      await new Promise<void>((resolve, reject) => {
        image.onload = () => resolve();
        image.onerror = () => reject(new Error("Image unavailable"));
        image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(root))}`;
      });
      const canvas = document.createElement("canvas");
      canvas.width = Math.ceil(width * 2);
      canvas.height = Math.ceil(imageHeight * 2);
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas unavailable");
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("PNG unavailable");
      downloadBlob(blob, `${filename}.png`);
    } catch {
      setNotice("PNG download is unavailable. Please try Download SVG.");
    } finally {
      setExporting(false);
    }
  }

  if (!sanitized) return null;
  return <section className={styles.card} aria-label={`${heading} diagram`}>
    <header className={styles.header}>
      <h3 className={styles.title}>{heading}</h3>
      <button className={styles.button} type="button" aria-label="Download SVG" onClick={() => {
        setNotice("");
        try { downloadBlob(new Blob([sanitized], { type: "image/svg+xml" }), `${filename}.svg`); }
        catch { setNotice("Download is unavailable. Please try again."); }
      }}>Download SVG</button>
      <button className={styles.button} type="button" aria-label="Download PNG" disabled={exporting} onClick={downloadPng}>Download PNG</button>
    </header>
    <iframe ref={iframe} className={styles.frame} title={`${heading} diagram`} sandbox="allow-scripts" srcDoc={srcDoc} style={{ height }} scrolling="no" referrerPolicy="no-referrer" />
    {notice && <p className={styles.notice} role="status">{notice}</p>}
  </section>;
}
