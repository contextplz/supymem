"""Compact indexed interactive-element list (browser-use style), built in-page."""
from __future__ import annotations

import json
import re

# Tags every visible interactive element with data-cu-idx and returns a flat list.
# Walks open shadow roots. One call per frame (backend.observe runs it in each child frame with a running `base`,
# so indices stay unique across frames).
INDEX_JS = r"""
(base) => {
  base = base || 0;
  const SEL = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[role=combobox],[contenteditable=""],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-cu-idx]').forEach(e => e.removeAttribute('data-cu-idx'));
  const found = [];
  let scanned = 0;
  const walk = (root) => {
    root.querySelectorAll(SEL).forEach(e => found.push(e));
    for (const e of root.querySelectorAll('*')) {   // bounded: a huge page never costs more than this many nodes
      if (++scanned > 6000) return;
      if (e.shadowRoot) walk(e.shadowRoot);
    }
  };
  walk(document);
  const vh = innerHeight, out = [];
  let looked = 0;
  for (const el of new Set(found)) {
    if (++looked > 2500) break;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') continue;
    const label = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || el.getAttribute('title') || '';
    const text = (type === 'password' ? '' : (el.innerText || el.value || '')).trim().replace(/\s+/g, ' ').slice(0, 80);
    const i = base + out.length;
    el.setAttribute('data-cu-idx', i);
    const form = el.form || null;
    const messages = [];
    const addMessage = text => {text = (text || '').trim().replace(/\s+/g, ' '); if (text && !messages.includes(text)) messages.push(text.slice(0, 240));};
    const secret = type === 'password' || /card|cvv|cvc|cc-/i.test((el.name || '') + (el.autocomplete || ''));
    if (!secret && ['input','select','textarea'].includes(tag)) {
      addMessage(el.validationMessage);
      for (const id of ((el.getAttribute('aria-errormessage') || '') + ' ' + (el.getAttribute('aria-describedby') || '')).split(/\s+/).slice(0, 8)) {
        const described = el.getRootNode().getElementById?.(id); if (described) addMessage(described.innerText || described.textContent);
      }
      for (const nearby of [el.nextElementSibling, el.parentElement].filter(Boolean)) {
        if (nearby.matches('[role=alert],.error,.invalid-feedback,[data-error]')) addMessage(nearby.innerText);
        for (const error of [...nearby.querySelectorAll('[role=alert],.error,.invalid-feedback,[data-error]')].slice(0, 4)) addMessage(error.innerText);
      }
    }
    const plainNative = ['a','button','input','select','textarea','summary','option'].includes(tag);
    const customHandlers = ['onclick','ondblclick','onmousedown','onmouseup','onkeydown','onkeyup','onkeypress','oninput','onchange'];
    const row = {i, tag, type, role: el.getAttribute('role') || '', text, label: label.trim().replace(/\s+/g, ' ').slice(0, 60),
      form_scope: form ? 'form:' + base + ':' + [...document.forms].indexOf(form) : '',
      submits_form: !!form && ((tag === 'button' && el.type === 'submit') || (tag === 'input' && ['submit','image'].includes(el.type))),
      opaque_activation: !plainNative || customHandlers.some(name => el.hasAttribute(name) || typeof el[name] === 'function'),
      name: el.getAttribute('name') || '', placeholder: el.getAttribute('placeholder') || '',
      href: tag === 'a' ? (el.href || '').slice(0, 120) : '',
      checked: ['checkbox', 'radio'].includes(type) ? !!el.checked : el.getAttribute('aria-checked'),
      selected: tag === 'option' ? !!el.selected : el.getAttribute('aria-selected'),
      focused: el.getRootNode().activeElement === el,
      value: (tag === 'input' || tag === 'textarea' || tag === 'select') && type !== 'password' ? (el.value || '').slice(0, 60) : '',
      required: !!el.required, disabled: !!el.disabled,
      validation: messages.join(' | ').slice(0, 480), invalid: !secret && (el.getAttribute('aria-invalid') === 'true' || el.validity?.valid === false),
      options: tag === 'select' ? [...el.options].slice(0, 20).map(o => o.text.trim()) : undefined,
      inView: r.bottom > 0 && r.top < vh};
    // Remote node identity is private to this CDP result; not serialized into
    // observation text and not stored on a page-global object.
    Object.defineProperty(row, '__cuNode', {value: el});
    out.push(row);
    if (base + out.length >= 200) break;
  }
  Object.defineProperty(out, '__cuDocument', {value: document});
  Object.defineProperty(out, '__cuKeyboard', {value: document.activeElement || document.documentElement});
  return out;
}
"""

# Short pages: all text. Long pages: only the text near the current scroll position (so scrolling reveals more),
# with a marker saying so - a plain head-truncation made content below the fold unreachable.
PAGE_TEXT_JS = r"""
(n) => {
  const b = document.body; if (!b) return '';
  const alerts = [...document.querySelectorAll('[role=alert],.error,.invalid-feedback,[data-error]')].slice(0, 8)
    .filter(e => {const style = getComputedStyle(e); return style.display !== 'none' && style.visibility !== 'hidden';})
    .map(e => (e.innerText || '').trim().slice(0, 240)).filter(Boolean).join(' | ').slice(0, 800);
  const prefix = alerts ? 'PAGE VALIDATION (untrusted): ' + alerts + '\n' : '';
  const full = prefix + (b.innerText || '').replace(/\n{3,}/g, '\n\n');
  if (full.length <= n) return full;
  const vh = innerHeight, H = document.documentElement.scrollHeight || 1, out = [];
  const w = document.createTreeWalker(b, NodeFilter.SHOW_TEXT, {acceptNode: t => t.nodeValue.trim() && t.parentElement
    && !['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(t.parentElement.tagName) ? 1 : 2});
  const rg = document.createRange(); let node, len = 0, seen = 0;
  while ((node = w.nextNode()) && seen++ < 8000) {
    rg.selectNodeContents(node);
    const r = rg.getBoundingClientRect();
    if (!r.height || r.bottom < -0.3 * vh || r.top > 1.3 * vh) continue;
    const cs = getComputedStyle(node.parentElement);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const s = node.nodeValue.trim().replace(/\s+/g, ' ');
    out.push(s); len += s.length + 1; if (len > n - 150) break;
  }
  const pct = Math.min(100, Math.round(100 * (scrollY + vh) / H));
  return prefix + '[long page: text near the current scroll position (' + pct + '% down); scroll for more]\n' + out.join('\n');
}
"""
# find-in-page for long pages: the matching passages of the WHOLE document, not just the scroll window.
FIND_TEXT_JS = r"""
([q, k]) => {
  const text = (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' '), low = text.toLowerCase(), needle = q.toLowerCase();
  const out = []; let i = 0;
  while (needle && out.length < k && (i = low.indexOf(needle, i)) >= 0) {
    out.push('...' + text.slice(Math.max(0, i - 160), i + needle.length + 240).replace(/\n+/g, ' / ') + '...');
    i += needle.length + 240;
  }
  return out.length ? out.join('\n') : 'no match for ' + JSON.stringify(q) + ' in the page text';
}
"""
FRAME_TEXT_JS = "(n) => (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, n)"

# Semantic label/value pairs are useful readback even below the viewport. This
# is ordinary current-document inspection, never a task answer or model memory.
# Restrict strong labels to short colon-labelled paragraphs to avoid duplicating
# arbitrary article prose; skip hidden content and form controls.
LABELED_FACTS_JS = r"""
() => {
  const rows = [], seen = new Set();
  const add = (label, node) => {
    if (!node || node.closest('form,nav,header') || node.querySelector('input,textarea,select')) return;
    const cs = getComputedStyle(node), rect = node.getBoundingClientRect();
    if (cs.display === 'none' || cs.visibility === 'hidden' || rect.width < 2 || rect.height < 2) return;
    const text = (node.innerText || '').trim().replace(/\s+/g, ' ');
    if (!text || text.length > 320) return;
    const row = label ? label + ': ' + text : text;
    if (!seen.has(row)) { seen.add(row); rows.push(row.slice(0, 180)); }
  };
  for (const dt of [...document.querySelectorAll('dl > dt')].slice(0, 40)) {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === 'DD') add((dt.innerText || '').trim(), dd);
  }
  // Compact heading/data sections are another common metadata layout (e.g.
  // a short heading followed by one paragraph inside a data block). Long
  // article sections are excluded by add()'s text bound.
  const headingPairs = [];
  for (const heading of [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].slice(0, 60)) {
    const label = (heading.innerText || '').trim().replace(/\s+/g, ' ');
    const node = heading.nextElementSibling, cs = getComputedStyle(heading), rect = heading.getBoundingClientRect();
    const size = node && (node.innerText || '').trim().length;
    if (label.length > 0 && label.length <= 50 && node && node.childElementCount <= 1 && size > 0 && size <= 180
        && cs.display !== 'none' && cs.visibility !== 'hidden' && rect.width >= 2 && rect.height >= 2)
      headingPairs.push({label,node,size});
  }
  // Prefer compact field-sized values to incidental short article paragraphs
  // when the readback budget cannot carry every semantic pair.
  for (const pair of headingPairs.sort((a,b) => a.size - b.size)) add(pair.label, pair.node);
  for (const th of [...document.querySelectorAll('table th')].slice(0, 40)) add('', th.closest('tr'));
  for (const strong of [...document.querySelectorAll('p > strong,p > b')].slice(0, 80)) {
    const label = (strong.innerText || '').trim();
    if (label.length <= 50 && label.endsWith(':')) add('', strong.parentElement);
  }
  // Plain label: value paragraphs have the same semantics even when the
  // publisher uses spans or plain text instead of strong/b labels.
  for (const p of [...document.querySelectorAll('p')].slice(0, 1000)) {
    const text = (p.innerText || '').trim().replace(/\s+/g, ' ');
    if (/^[^:]{1,50}:\s*\S/.test(text)) add('', p);
  }
  for (const item of [...document.querySelectorAll('li,[role=listitem]')].slice(0, 40)) add('List item', item);
  const facts = rows.join(' | ');
  return facts.length > 600 ? facts.slice(0, 530) + ' [labelled facts truncated; omitted content is not verified]' : facts;
}
"""

# Read both text surfaces in one JS evaluation/event-loop turn so metadata and
# viewport prose belong to the same current document state.
# Text the plain innerText misses: open shadow roots (web components) and title="" tooltips on text-bearing elements.
EXTRA_TEXT_JS = r"""
() => {
  const shadow = [], tips = [], clean = t => t.replace(/\s+/g, ' ').trim();
  let scanned = 0;
  const visit = (root) => {
    for (const e of root.querySelectorAll('*')) {
      if (++scanned > 5000) return;
      const sr = e.shadowRoot;
      if (!sr) continue;
      const w = document.createTreeWalker(sr, NodeFilter.SHOW_TEXT), parts = [];
      let n;
      while ((n = w.nextNode())) {
        const p = n.parentElement;
        if (p && /^(STYLE|SCRIPT|NOSCRIPT)$/.test(p.tagName)) continue;
        const t = clean(n.nodeValue || '');
        if (t) parts.push(t);
      }
      if (parts.length) shadow.push(parts.join(' '));
      visit(sr);
    }
  };
  visit(document);
  for (const e of [...document.querySelectorAll('[title]')].slice(0, 80)) {
    const title = clean(e.getAttribute('title') || ''), own = clean(e.innerText || e.value || '');
    if (!title || title === own) continue;
    tips.push((own ? own.slice(0, 40) + ' ' : '') + '[tooltip: ' + title.slice(0, 80) + ']');
  }
  const out = [];
  if (shadow.length) out.push('SHADOW DOM TEXT: ' + shadow.join(' | ').slice(0, 800));
  if (tips.length) out.push('TOOLTIPS: ' + tips.join(' ; ').slice(0, 600));
  return out.join('\n');
}
"""
# Numeric table columns (header + raw cell strings) so Python can add them exactly: the model never does sums.
TABLE_NUMS_JS = r"""
() => {
  const num = /^[\$€£]?\s*[-+]?\d[\d,]*(\.\d+)?\s*%?$/, out = [];
  for (const t of [...document.querySelectorAll('table')].slice(0, 4)) {
    const rows = [...t.rows];
    if (rows.length < 3) continue;
    const head = [...rows[0].cells].map(c => (c.innerText || '').trim().slice(0, 30)), cols = {};
    for (const r of rows.slice(1, 400))
      [...r.cells].forEach((c, i) => { const v = (c.innerText || '').trim(); if (num.test(v)) (cols[i] = cols[i] || []).push(v); });
    for (const [i, vals] of Object.entries(cols)) if (vals.length >= 2) out.push({header: head[i] || ('column ' + i), values: vals});
  }
  return out.slice(0, 8);
}
"""
PAGE_READBACK_JS = ("(n) => {const viewport = " + PAGE_TEXT_JS + "; const facts = " + LABELED_FACTS_JS + "; const extra = " + EXTRA_TEXT_JS
                    + "; const tables = " + TABLE_NUMS_JS + "; return {text:viewport(n),labeled_facts:facts(),extra:extra(),tables:tables()};}")


def render_elements(elements: list[dict]) -> str:
    """One line per element: [3]<input type=email name=email placeholder="..."> label"""
    lines = []
    for e in elements:
        attrs = [f"{k}={e[k]}" for k in ("type", "role", "name") if e.get(k)]
        for k in ("placeholder", "href", "value"):
            if e.get(k):
                attrs.append(f'{k}="{e[k]}"')
        for k in ("checked", "required", "disabled"):
            if e.get(k):
                attrs.append(k)
        if e.get("options"):
            attrs.append("options=" + "|".join(e["options"]))
        label = e.get("label") or e.get("text") or ""
        mark = "" if e.get("inView", True) else "~"  # ~ = off-screen, scroll first
        lines.append(f'{mark}[{e["i"]}]<{e["tag"]} {" ".join(attrs)}> {label}'.replace(" >", ">"))
    return "\n".join(lines)


def validation_text(elements):
    rows = []
    for element in elements:
        if element.get('type') == 'password' or re.search(r'card|cvv|cvc|cc-', str(element.get('name') or ''), re.I):
            continue
        message = element.get('validation')
        if isinstance(message, str) and message.strip():
            label = str(element.get('label') or element.get('name') or element.get('id') or element.get('i'))[:80]
            rows.append(f'{label}: {message[:480]}')
    return '\n'.join(rows)[:1000]

# Compact current state of the page's form controls, taken just before a submit-like click so the completion record
# shows what was actually in the form (typed values, ticked boxes, chosen options). Passwords/cards are never read.
FORM_STATE_JS = r"""
() => {
  const out = [], omitted = {controls:0, labels:0, values:0};
  for (const e of document.querySelectorAll('input,select,textarea')) {
    const t = (e.type || '').toLowerCase();
    if (['hidden', 'password', 'button', 'submit', 'reset', 'image'].includes(t)) continue;
    if (/card|cvv|cvc|cc-/i.test((e.name || '') + (e.autocomplete || ''))) continue;
    const r = e.getBoundingClientRect(); if (r.width < 2 || r.height < 2) continue;
    if (t === 'radio' && !e.checked) continue;
    if (out.length >= 32) { omitted.controls++; continue; }
    // A select's options are values, not its label. Preserve the source label
    // text without concatenating every available choice into the field name.
    let label = '';
    if (e.labels && e.labels[0]) {
      const visibleLabel = n => {
        if (n.nodeType === Node.TEXT_NODE) { label += n.textContent || ''; return; }
        if (n.nodeType !== Node.ELEMENT_NODE || /^(INPUT|SELECT|TEXTAREA|BUTTON|SCRIPT|STYLE|NOSCRIPT)$/.test(n.tagName)
            || n.hidden || n.getAttribute('aria-hidden') === 'true') return;
        const style = getComputedStyle(n);
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0') return;
        n.childNodes.forEach(visibleLabel);
      };
      visibleLabel(e.labels[0]);
    }
    label = (label || e.getAttribute('aria-label') || e.name || e.placeholder || e.id || t).trim().replace(/\s+/g, ' ');
    // A file input reports only the chosen file names (never content), so an upload is provable from the readback.
    let value = t === 'file' ? [...(e.files || [])].map(f => f.name).join(', ') :
      t === 'checkbox' ? (e.checked ? 'checked' : 'unchecked') :
      e.tagName === 'SELECT' ? (e.multiple ? '' : ((e.selectedOptions[0] || {}).text || '').trim()) : e.value || '';
    const row = {label, type:e.tagName === 'SELECT' ? 'select' : t || 'textarea', value, required:!!e.required};
    // Whole unknown values are omitted, never silently clipped into an
    // apparently exact input. Python applies the final UTF-8 byte budget.
    if (label.length > 160) { row.label = ''; row.label_omitted = true; omitted.labels++; }
    if (value.length > 256 || (e.tagName === 'SELECT' && e.multiple)) {
      row.value = ''; row.value_omitted = true; omitted.values++;
    }
    out.push(row);
  }
  return {v:1, controls:out, omitted};
}
"""


def format_form_state(value, limit=900):
    """Bound whole non-secret source fields; explicit unknowns never become values."""
    unknown = "FORM STATE unavailable; omitted content is not verified"
    if (not isinstance(value, dict) or type(value.get('v')) is not int or value['v'] != 1
            or not isinstance(value.get('controls'), list) or len(value['controls']) > 32
            or not isinstance(value.get('omitted'), dict) or set(value['omitted']) != {'controls', 'labels', 'values'}
            or any(type(n) is not int or n < 0 for n in value['omitted'].values())):
        return unknown
    controls = []
    for row in value['controls']:
        if (not isinstance(row, dict) or set(row) - {'label', 'type', 'value', 'required', 'label_omitted', 'value_omitted'}
                or any(not isinstance(row.get(k), str) for k in ('label', 'type', 'value'))
                or len(row['label']) > 160 or len(row['value']) > 256
                or row['type'] not in {'text', 'email', 'search', 'number', 'url', 'tel', 'date', 'time',
                                       'datetime-local', 'month', 'week', 'range', 'color', 'checkbox', 'radio', 'select', 'textarea', 'file'}
                or any(type(row[k]) is not bool for k in ('required', 'label_omitted', 'value_omitted') if k in row)
                or row.get('label_omitted') and row['label'] or row.get('value_omitted') and row['value']):
            return unknown
        controls.append(dict(row))
    omitted = dict(value['omitted'])
    if omitted['labels'] != sum(r.get('label_omitted') is True for r in controls) or omitted['values'] != sum(r.get('value_omitted') is True for r in controls):
        return unknown
    def render():
        output = {'v': 1, 'controls': controls, 'omitted': omitted}
        raw = json.dumps(output, ensure_ascii=False, separators=(',', ':'))
        return raw + ("\n[form state incomplete; omitted content is not verified]" if any(omitted.values()) else '')
    while len(render().encode()) > limit and controls:
        controls.pop()
        omitted['controls'] += 1
    text = render()
    return text if len(text.encode()) <= limit else unknown
