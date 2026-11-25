"""Turn research markdown into clean HTML and PDF — no raw # / * artifacts."""

from __future__ import annotations

import html
import re
from io import BytesIO
from typing import Iterable
from urllib.parse import urlsplit

from app.core.local_mode import is_local


_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.+)$")
_HR = re.compile(r"^\s*(?:---|\*\*\*|___)\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)")
_CODE = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^```")


def _inline(text: str) -> str:
    """Escape then apply a small set of safe inline markdown transforms."""
    escaped = html.escape(text)

    def link(m: re.Match[str]) -> str:
        label = m.group(1)
        href = html.escape(m.group(2), quote=True)
        return f'<a href="{href}">{label}</a>'

    # Re-run on original then escape carefully: apply on escaped with placeholders.
    # Simpler path: work on raw then escape segments.
    parts: list[str] = []
    cursor = 0
    for m in _LINK.finditer(text):
        parts.append(html.escape(text[cursor : m.start()]))
        href = m.group(2)
        safe = True
        if is_local():
            try:
                target = urlsplit(href)
                safe = not any(ord(character) <= 32 or character == "\\" for character in href) and (
                    (target.scheme in {"http", "https"} and bool(target.hostname)
                     and target.username is None and target.password is None
                     and (target.port is None or target.port > 0))
                    or (target.scheme == "mailto" and bool(target.path)) or href.startswith("#"))
            except ValueError:
                safe = False
        label = html.escape(m.group(1))
        parts.append(f'<a href="{html.escape(href, quote=True)}">{label}</a>' if safe else label)
        cursor = m.end()
    parts.append(html.escape(text[cursor:]))
    out = "".join(parts)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", out)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    return out


def markdown_to_blocks(markdown: str) -> list[tuple[str, str | list[str]]]:
    """Parse markdown into (kind, content) blocks for HTML/PDF."""
    blocks: list[tuple[str, str | list[str]]] = []
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    i = 0
    in_fence = False
    fence_buf: list[str] = []
    para: list[str] = []
    list_items: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            blocks.append(("p", " ".join(para).strip()))
            para = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append(("ul", list_items[:]))
            list_items = []

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if _FENCE.match(line.strip()):
            if in_fence:
                flush_para()
                flush_list()
                blocks.append(("pre", "\n".join(fence_buf)))
                fence_buf = []
                in_fence = False
            else:
                flush_para()
                flush_list()
                in_fence = True
            i += 1
            continue
        if in_fence:
            fence_buf.append(line)
            i += 1
            continue
        if not line.strip():
            flush_para()
            flush_list()
            i += 1
            continue
        if _HR.match(line):
            flush_para()
            flush_list()
            blocks.append(("hr", ""))
            i += 1
            continue
        hm = _HEADING.match(line)
        if hm:
            flush_para()
            flush_list()
            level = len(hm.group(1))
            blocks.append((f"h{level}", hm.group(2).strip()))
            i += 1
            continue
        bm = _BULLET.match(line)
        if bm:
            flush_para()
            list_items.append(bm.group(3).strip())
            i += 1
            continue
        # Strip leftover emphasis-only junk lines like lone "**"
        if line.strip() in {"*", "**", "***", "#", "##", "###"}:
            i += 1
            continue
        flush_list()
        para.append(line.strip())
        i += 1

    flush_para()
    flush_list()
    if fence_buf:
        blocks.append(("pre", "\n".join(fence_buf)))
    return blocks


def markdown_to_html(
    markdown: str,
    *,
    title: str = "Validation research",
    subtitle: str | None = None,
) -> str:
    blocks = markdown_to_blocks(markdown)
    body: list[str] = []
    for kind, content in blocks:
        if kind.startswith("h") and isinstance(content, str):
            body.append(f"<{kind}>{_inline(content)}</{kind}>")
        elif kind == "p" and isinstance(content, str):
            body.append(f"<p>{_inline(content)}</p>")
        elif kind == "ul" and isinstance(content, list):
            items = "".join(f"<li>{_inline(item)}</li>" for item in content)
            body.append(f"<ul>{items}</ul>")
        elif kind == "pre" and isinstance(content, str):
            body.append(f"<pre>{html.escape(content)}</pre>")
        elif kind == "hr":
            body.append("<hr />")

    sub = f"<p class='subtitle'>{html.escape(subtitle)}</p>" if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{html.escape(title)}</title>
<style>
  :root {{
    --ink: #152033;
    --muted: #5b6b82;
    --line: #d8dee8;
    --accent: #3f5bd9;
    --bg: #f7f8fb;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 15px/1.65 "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  }}
  .sheet {{
    max-width: 820px;
    margin: 32px auto;
    padding: 48px 56px;
    background: #fff;
    border: 1px solid var(--line);
    box-shadow: 0 18px 50px rgba(21, 32, 51, 0.08);
  }}
  .brand {{
    margin: 0 0 8px;
    color: var(--accent);
    font: 600 11px/1.2 ui-sans-serif, system-ui, sans-serif;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }}
  h1 {{
    margin: 0 0 8px;
    font: 650 28px/1.2 ui-sans-serif, system-ui, sans-serif;
    letter-spacing: -0.03em;
  }}
  .subtitle {{
    margin: 0 0 28px;
    color: var(--muted);
    font: 14px/1.5 ui-sans-serif, system-ui, sans-serif;
  }}
  h2 {{
    margin: 28px 0 10px;
    padding-top: 8px;
    border-top: 1px solid var(--line);
    font: 650 18px/1.3 ui-sans-serif, system-ui, sans-serif;
  }}
  h3, h4 {{
    margin: 20px 0 8px;
    font: 650 15px/1.35 ui-sans-serif, system-ui, sans-serif;
  }}
  p {{ margin: 0 0 12px; }}
  ul {{ margin: 0 0 14px; padding-left: 1.2rem; }}
  li {{ margin: 0 0 6px; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  hr {{ border: 0; border-top: 1px solid var(--line); margin: 24px 0; }}
  pre {{
    padding: 14px 16px;
    overflow: auto;
    border-radius: 8px;
    background: #f0f3f8;
    font: 12px/1.5 ui-monospace, Menlo, monospace;
  }}
  strong {{ font-weight: 650; }}
  @media print {{
    body {{ background: #fff; }}
    .sheet {{ margin: 0; border: 0; box-shadow: none; max-width: none; }}
  }}
</style>
</head>
<body>
  <article class="sheet">
    <p class="brand">Company OS · Research</p>
    <h1>{html.escape(title)}</h1>
    {sub}
    {"".join(body)}
  </article>
</body>
</html>
"""


def _pdf_safe(text: str) -> str:
    """Helvetica core fonts are Latin-1 — normalize fancy punctuation."""
    replacements = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "-",
        "\u00a0": " ",
        "\u2026": "...",
    }
    out = text
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    return out.encode("latin-1", errors="replace").decode("latin-1")


def _pdf_plain_lines(blocks: Iterable[tuple[str, str | list[str]]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for kind, content in blocks:
        if kind.startswith("h") and isinstance(content, str):
            rows.append((kind, _pdf_safe(re.sub(r"[#*_`]", "", content).strip())))
        elif kind == "p" and isinstance(content, str):
            clean = _LINK.sub(r"\1 (\2)", content)
            clean = re.sub(r"[*_`#]", "", clean)
            rows.append(("p", _pdf_safe(clean.strip())))
        elif kind == "ul" and isinstance(content, list):
            for item in content:
                clean = _LINK.sub(r"\1 (\2)", item)
                clean = re.sub(r"[*_`#]", "", clean)
                rows.append(("li", _pdf_safe(clean.strip())))
        elif kind == "pre" and isinstance(content, str):
            rows.append(("pre", _pdf_safe(content)))
        elif kind == "hr":
            rows.append(("hr", ""))
    return rows


def markdown_to_pdf_bytes(
    markdown: str,
    *,
    title: str = "Validation research",
    subtitle: str | None = None,
) -> bytes:
    """Render a clean PDF. Uses fpdf2 when available; otherwise a minimal text PDF."""
    try:
        from fpdf import FPDF
    except ImportError:
        return _minimal_text_pdf(title, subtitle, markdown)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(18, 18, 18)

    pdf.set_text_color(63, 91, 217)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "COMPANY OS  ·  RESEARCH", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_text_color(21, 32, 51)
    pdf.set_font("Helvetica", "B", 18)
    pdf.multi_cell(0, 8, _pdf_safe(title))
    pdf.set_x(pdf.l_margin)
    if subtitle:
        pdf.set_text_color(91, 107, 130)
        pdf.set_font("Helvetica", size=11)
        pdf.multi_cell(0, 6, _pdf_safe(subtitle))
        pdf.set_x(pdf.l_margin)
        pdf.ln(2)

    usable = pdf.epw
    for kind, text in _pdf_plain_lines(markdown_to_blocks(markdown)):
        pdf.set_x(pdf.l_margin)
        if kind == "hr":
            pdf.ln(2)
            continue
        if not text and kind != "hr":
            continue
        if kind == "h1":
            pdf.set_font("Helvetica", "B", 15)
            pdf.set_text_color(21, 32, 51)
            pdf.ln(4)
            pdf.multi_cell(usable, 7, text)
        elif kind in {"h2", "h3", "h4"}:
            pdf.set_font("Helvetica", "B", 12 if kind == "h2" else 11)
            pdf.set_text_color(21, 32, 51)
            pdf.ln(3)
            pdf.multi_cell(usable, 6, text)
        elif kind == "li":
            pdf.set_font("Helvetica", size=10)
            pdf.set_text_color(21, 32, 51)
            pdf.multi_cell(usable, 5, f"  -  {text}")
        elif kind == "pre":
            pdf.set_font("Courier", size=9)
            pdf.set_text_color(50, 60, 75)
            pdf.multi_cell(usable, 4, text[:3000])
        else:
            pdf.set_font("Helvetica", size=10)
            pdf.set_text_color(21, 32, 51)
            pdf.multi_cell(usable, 5, text)
            pdf.ln(1)

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def _minimal_text_pdf(title: str, subtitle: str | None, markdown: str) -> bytes:
    """Tiny PDF fallback with Helvetica text only (no external deps)."""
    lines = [title]
    if subtitle:
        lines.append(subtitle)
    lines.append("")
    for kind, text in _pdf_plain_lines(markdown_to_blocks(markdown)):
        if kind == "li":
            lines.append(f"• {text}")
        elif kind == "hr":
            lines.append("-" * 40)
        elif isinstance(text, str):
            lines.append(text)

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content_lines = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"]
    first = True
    for line in lines[:120]:
        chunk = esc(line[:110])
        if first:
            content_lines.append(f"({chunk}) Tj")
            first = False
        else:
            content_lines.append("T*")
            content_lines.append(f"({chunk}) Tj")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects = []
    objects.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
    objects.append(b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n")
    objects.append(
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
    )
    objects.append(
        f"4 0 obj<< /Length {len(stream)} >>stream\n".encode()
        + stream
        + b"\nendstream\nendobj\n"
    )
    objects.append(b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)
