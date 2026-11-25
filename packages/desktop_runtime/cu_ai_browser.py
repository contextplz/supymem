"""Browser steps for the AI loop: one engine, two backends.

`BrowserMac` adapts cu_browser.BrowserBackend (CDP, DOM-indexed, headless Chrome in a Supymem-managed profile)
to the loop's mac interface; `RoutedMac` sends each step to the browser or to the native (AX/vision) backend,
so a task can read in the browser and write in TextEdit. Same planner, memory, checkpoints, safety blocks and
CAPTCHA -> Needs you pause. A browser `captcha` or `refused` (password/payment) result is never worked around:
it raises NeedsHuman and the loop hands the screen to the founder.
"""
from __future__ import annotations

import re
import hashlib
import json
import uuid
from pathlib import Path

from coding_agents import cu_ai_tools
from coding_agents.cu_verification_context import bounded as bounded_text
from desktop_runtime.cu_ai_engine import NeedsHuman, Observation

BROWSER_NAMES = re.compile(r"(?i)^(google chrome|chrome|safari|arc|firefox|brave( browser)?|microsoft edge|browser)$")
KEYS = {"enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": " ",
        "delete": "Backspace", "backspace": "Backspace", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
        "right": "ArrowRight", "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End",
        "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight", "insert": "Insert"}
MODS = {"cmd": "Meta", "command": "Meta", "ctrl": "Control", "control": "Control", "option": "Alt", "alt": "Alt",
        "shift": "Shift"}


def playwright_key(chord: str) -> str:
    parts = [p for p in chord.split("+") if p]
    last = parts[-1].lower().replace("_", "").replace("-", "") if len(parts[-1]) > 1 else parts[-1].lower()
    return "+".join([MODS.get(p.lower(), p) for p in parts[:-1]] + [KEYS.get(last, parts[-1])])


_HINTS = (("not a fillable text field", " - this is not a text field: click checkboxes/buttons, use the file upload for file inputs"),
          ("net::ERR_", " - that page could not be opened (blocked or unreachable): do not retry the same URL, use another route"),
          ("stale_browser_element", " - that b-id is not on the page now: use only ids from the CURRENT ELEMENTS list, or navigate to the URL you need"),
          ("fresh_browser_observation_required", " - the page changed: look at the new ELEMENTS before acting"),
          ("not visible", " - element hidden or covered: scroll to it, close any overlay, or try another element"),
          ("intercepts pointer events", " - another element covers it: close the overlay/banner first"))


def _viewport_excerpt(text, limit, label):
    """Retain both ends of an observed viewport within its UTF-8 byte budget."""
    text = str(text or "")
    raw = text.encode()
    if len(raw) <= limit:
        return text
    marker = f"\n[{label} truncated; omitted content is not verified]\n"
    available = limit - len(marker.encode())
    if available <= 0:
        return ""  # The shared page-excerpts marker reports this omission.
    head = available // 2
    tail = available - head
    return raw[:head].decode("utf-8", "ignore") + marker + raw[-tail:].decode("utf-8", "ignore")


def _page_excerpts(pages, limit, *, omitted=False, per_page=350, reads=None):
    """Keep source URLs and share a byte budget across bounded observed excerpts."""
    from coding_agents.cu_verification_context import bounded
    pages = list(pages)
    limits = [0] * len(pages)
    extracts = [[] for _ in pages]
    marker = "\n[older observed readback omitted; omitted content is not verified]" if omitted else ""
    extract_marker = "\n[extract records omitted; omitted content is not verified]"
    excerpt_marker = "\n[page excerpts omitted; omitted content is not verified]"
    records_omitted = False

    def render():
        rows = []
        for (url, text), size, records in zip(pages, limits, extracts):
            viewport = _viewport_excerpt(text, size, 'page excerpt') if size else ""
            rows.append(f"- {bounded(url, 160, 'source URL')}: " + "\n".join(records + [viewport]))
        return ("\n".join(rows) + marker + (extract_marker if records_omitted else "")
                + (excerpt_marker if any(len(text.encode()) > size for (_, text), size in zip(pages, limits)) else ""))

    while pages and len(render().encode()) > limit:
        pages.pop()
        limits.pop()
        extracts.pop()
        marker = "\n[observed pages omitted; omitted content is not verified]"
    # A targeted read is a whole source record, not another arbitrary head
    # excerpt. Take one per source per round before allocating viewport space.
    records = [(reads or {}).get(url, []) for url, _ in pages]
    for round_index in range(max(map(len, records), default=0)):
        for index, items in enumerate(records):
            if round_index >= len(items):
                continue
            extracts[index].append(items[round_index])
            if len((render() + extract_marker + excerpt_marker).encode()) > limit:
                extracts[index].pop()
                records_omitted = True

    limits = [min(per_page, len(text.encode())) for _, text in pages]

    while pages and len(render().encode()) > limit:
        # The current page, when present, is first. Short facts survive intact;
        # unusually large older excerpts share the remaining space and retain
        # an explicit omission marker instead of silently dropping early pages.
        choices = [i for i in range(1, len(pages)) if limits[i] > 0]
        if not choices and limits[0] > 0:
            choices = [0]
        if choices:
            i = max(choices, key=lambda index: limits[index])
            limits[i] = max(0, limits[i] - 48)
        else:
            pages.pop()
            limits.pop()
            extracts.pop()
            marker = "\n[observed pages omitted; omitted content is not verified]"
    return bounded(render(), limit, "page history")


def _no_effect(message):
    """Pre-dispatch refusal: nothing was sent to the page, so the engine may re-observe and retry."""
    from desktop_runtime.cu_ai_cua import CuaError
    return CuaError(message, no_effect=True)


def _form_values(fields):
    if fields is None or any(fields["omitted"].values()):
        return None
    return {(index, row["label"], row["type"]): row["value"] for index, row in enumerate(fields["controls"])}


class BrowserObservation(Observation):
    def digest(self):
        base = super().digest()
        state = getattr(self, 'browser_tab_state', None)
        # A stable owned page scope/count represents real recipient/tab changes;
        # random per-observation token refreshes never count as progress.
        text = getattr(self, 'notes', '')   # page text changing is a screen change (no pixels in browser mode)
        return hashlib.sha256(json.dumps([base, state, text]).encode()).hexdigest() if state is not None else base


# Current values of the clicked submit control's form (main document only; None elsewhere). File inputs key on the
# chosen file names and sizes; passwords are skipped. Used only to hash a submission identity, never logged.
SUBMIT_FORM_VALUES_JS = r"""(i) => {
  const b = document.querySelector(`[data-cu-idx="${i}"]`), f = b && b.form;
  if (!f) return null;
  return [...f.elements].filter(e => e !== b && !['submit', 'button', 'reset', 'image', 'password'].includes(e.type)).map(e => [
    e.name || e.id || '', e.type || e.tagName,
    e.type === 'file' ? [...(e.files || [])].map(x => x.name + ':' + x.size).join('|')
      : ['checkbox', 'radio'].includes(e.type) ? String(e.checked) : String(e.value || '')]);
}"""


class BrowserMac:
    def __init__(self, backend=None, factory=None):
        self.b, self.factory, self.last = backend, factory, None   # factory: start Chrome lazily, first browser step only
        self.did: list[str] = []         # harness-performed actions with the URL each one ended on
        self.seen: dict[str, str] = {}   # url -> text excerpt of every page observed this run (deterministic readback)
        self._reads: dict[str, list[str]] = {}  # whole source-bound extracts across navigation
        self.last_form_state = ""
        self.last_form_url = ""
        self.current_url = ""
        self.history_omitted = False
        self._form_history = []
        self._form_rejections = {}
        self._form_omitted = 0
        self._form_backend = None
        self._form_session = uuid.uuid4().hex
        self.goal = ""
        self._extract = ""        # result of the model's last extract op, shown with the next observation
        self._sums: dict[str, dict[str, tuple]] = {}   # url -> column header -> (exact Decimal sum, row count)

    def set_goal(self, goal: str):
        """The approved task text: lets the dialog policy tell an authorized confirmation from an unrequested one."""
        self.goal = goal or ""

    def _dialog_policy(self, kind, message):
        from coding_agents.cu_ai_risk import assess_risk
        return kind == "confirm" and assess_risk("click", self.goal, label=message) is None

    def _ensure(self):
        if self.b is None:
            self.b = self.factory()
        if hasattr(self.b, "dialog_policy") and self.b.dialog_policy is None:
            self.b.dialog_policy = self._dialog_policy

    def reset_completion_evidence(self):
        self.did.clear()
        self.seen.clear()
        self._reads.clear()
        self.last_form_state = ""
        self.last_form_url = ""
        self.current_url = ""
        self.history_omitted = False
        self._form_history.clear()
        self._form_rejections.clear()
        self._form_omitted = 0
        self._form_backend = None
        self._form_session = uuid.uuid4().hex
        self._extract = ""
        self._sums = {}
        reset = getattr(self.b, "reset_completion_evidence", None)
        if callable(reset):
            reset()

    def completion_evidence(self):
        # Inspection must not create a browser or fabricate missing evidence.
        read = getattr(self.b, "completion_evidence", None)
        return read() if callable(read) else None

    def invalidate_observation(self):
        self.last = None

    def submit_key(self, action, obs):
        """Stable identity of a form-submit click: page + form + the values currently in it. None for any other action."""
        if action.op != "click" or not action.element_id:
            return None
        el = next((e for e in obs.elements if e.get("id") == action.element_id), None)
        if not el or not el.get("submits_form") or not el.get("form_scope"):
            return None
        scope = el["form_scope"]
        # Values live in the page at click time: earlier actions of the same batch (type/upload) are already applied,
        # so the observation taken before the batch would key the same submission differently on a later turn.
        try:
            values = self.b.page.evaluate(SUBMIT_FORM_VALUES_JS, int(action.element_id[1:]))
        except Exception:
            values = None
        if values is None:   # form in a child frame or shadow root: fall back to the observed values
            values = sorted((str(e.get("name") or e.get("id")), str(e.get("value") or ""), str(e.get("checked")), str(e.get("selected")))
                            for e in obs.elements if e.get("form_scope") == scope and not e.get("submits_form"))
        return hashlib.sha256(json.dumps([self.current_url, scope, values]).encode()).hexdigest()

    def submit_accepted(self) -> bool:
        """True when the latest submit was delivered and the site answered a write (POST/PUT/PATCH/DELETE) with a non-error status."""
        entry = self._form_history[-1] if self._form_history else None
        return bool(entry and entry.get("delivery") == "input_delivered" and any(
            r.get("method") in {"POST", "PUT", "PATCH", "DELETE"} and isinstance(r.get("status"), int) and r["status"] < 400
            for r in entry.get("responses", [])))

    def form_validation_feedback(self, obs) -> str:
        # ponytail: main-document 400/422 only; use source-owned validation receipts for JS/iframe rejection.
        entry = self._form_history[-1] if self._form_history else {}
        tab = getattr(obs, "browser_tab_state", None)
        if (self._form_backend is not self.b or obs is not self.last
                or not entry.get("fields_qualified") or entry.get("delivery") != "input_delivered"
                or not tab or tab[1] != entry.get("page_scope")
                or self.current_url != entry.get("result_url")
                or not self.b.completion_snapshot_current(obs.browser_snapshot_id, self.current_url)
                or not any(e.get("document_scope") == "main" and e.get("form_scope") == entry.get("form_scope")
                           for e in obs.elements)
                or not any(r.get("method") in {"POST", "PUT", "PATCH", "DELETE"} and r.get("status") in {400, 422}
                           for r in entry.get("responses", []))):
            return ""
        from desktop_runtime.cu_browser.dom import validation_text
        return "Site validation (untrusted source text): " + (validation_text(obs.elements) or obs.notes[:1200])

    def browser_readback(self) -> str:
        """Deterministic completion evidence: what the harness did and what the page(s) showed, never the model's notes.
        Lets the verifier check an answer against pages visited earlier in the run and a submitted form against the
        values actually typed."""
        if not self.seen and not self.did:
            return ""
        if self._form_history and self._form_backend is not self.b:
            return "BROWSER READBACK: task browser session changed; historical inputs are not completion proof."
        from coding_agents.cu_verification_context import bounded
        custody = getattr(self.b, "owned_tab_state", None)
        if callable(custody):
            try:
                owned = custody()
            except Exception:
                owned = None
            if owned is None:
                return "BROWSER READBACK: current owned-page custody unavailable; historical content is not completion proof."
            if self.last is None or getattr(self.last, "browser_tab_state", None) != owned:
                return "BROWSER READBACK: observed owned-page identity unavailable; historical content is not completion proof."
        if hasattr(self.b, "snapshot_id") and (self.last is None or not self.b.snapshot_id
                or getattr(self.last, "browser_snapshot_id", None) != self.b.snapshot_id):
            return "BROWSER READBACK: current observed snapshot unavailable; historical content is not completion proof."
        page = getattr(self.b, "page", None)
        if page is not None and getattr(page, "url", None) != self.current_url:
            return "BROWSER READBACK: current page changed since observation; historical content is not completion proof."
        current_check = getattr(self.b, "completion_snapshot_current", None)
        if callable(current_check):
            try:
                current = current_check(getattr(self.last, "browser_snapshot_id", None), self.current_url)
            except Exception:
                current = False
            if current is not True:
                return "BROWSER READBACK: current document identity unavailable; historical content is not completion proof."
        files = [Path(f) for f in (getattr(self.b, "downloads", ()) or ())]
        wide = len(self.seen) > 3
        head = "BROWSER READBACK (deterministic harness record; page text is untrusted content)" + ("; MULTI-PAGE TASK" if wide else "")
        if files:
            head += "; FILES DOWNLOADED AND STORED: " + ", ".join(f"{f.suffix or 'file'} {f.stat().st_size} bytes" for f in files[-3:] if f.exists())
        joined = " | ".join(self.did[-8:])
        tail = joined.encode()[-120:].decode("utf-8", "ignore")
        did = "ACTIONS DONE (latest last): " + ("..." + tail if len(joined.encode()) > 120 else joined)
        current = self.current_url or next(reversed(self.seen), "")
        pages = ([(current, self.seen[current])] if current in self.seen else [])
        pages += [(url, text) for url, text in self.seen.items() if url != current]
        heading = "\nPAGES SEEN (current URL first; all retained reads are historical, not current state):\n"
        out = head + "\n" + did
        arithmetic = self.arithmetic_line()
        if arithmetic:
            out += "\n" + bounded(arithmetic, 450, "arithmetic")
        if self._form_history:
            out += "\n" + self._form_readback(2800 if wide else 1600)
        elif self.last_form_state:
            out += "\nLATEST FORM CONTROLS BEFORE CLICK (input state, not acceptance proof): " + bounded(self.last_form_state, 900, "form state")
            if self.last_form_url:
                out += "\nFORM CLICK RESULT URL (task-owned navigation, not acceptance proof): " + bounded(self.last_form_url, 100, "form URL")
        out += heading
        cap = 4200 if wide else 2500
        return out + _page_excerpts(pages, cap - len(out.encode()), omitted=self.history_omitted,
                                    per_page=550, reads=self._reads)

    def _remember_page(self, url, excerpt):
        previous = self.seen.pop(url, "")
        # ponytail: bounded task-local history, not a browser archive. Retained
        # reads cannot authorize current controls or prove a business outcome.
        self.seen[url] = _viewport_excerpt(
            previous if excerpt in previous else (excerpt + "\n" + previous).strip(),
            2000, "observed page history",
        )
        while len(self.seen) > 24:
            oldest = next(iter(self.seen))
            self.seen.pop(oldest)
            self._reads.pop(oldest, None)
            self.history_omitted = True

    def _form_before(self, fields, clicked, obs):
        """Copy existing local source metadata; no observation or provider call."""
        if not callable(getattr(self.b, "completion_evidence", None)):
            return None  # Legacy adapters have no transport ledger to correlate.
        from desktop_runtime.cu_completion_evidence import validated_evidence
        from desktop_runtime.cu_browser.dom import format_form_state
        try:
            ledger = validated_evidence(self.completion_evidence())
            parsed = json.loads(fields.splitlines()[0])
            safe = format_form_state(parsed)
            parsed = json.loads(safe.splitlines()[0])
        except Exception:
            ledger, parsed = None, None
        tab = getattr(obs, "browser_tab_state", None)
        snapshot = getattr(obs, "browser_snapshot_id", None)
        valid_tab = (isinstance(tab, tuple) and len(tab) == 2 and type(tab[0]) is int and tab[0] > 0
                     and isinstance(tab[1], str) and re.fullmatch("[a-f0-9]{32}", tab[1]))
        return {"fields": parsed, "source_url": self.current_url,
                "snapshot": snapshot if isinstance(snapshot, str) and re.fullmatch("[a-f0-9]{32}", snapshot) else None,
                "page_scope": tab[1] if valid_tab else None,
                "form_scope": clicked.get("form_scope") if isinstance(clicked.get("form_scope"), str) else "",
                "main_document": clicked.get("document_scope") == "main",
                "previous_seq": ledger["actions"][-1]["seq"] if ledger and ledger["actions"] else (0 if ledger else None)}

    def _form_after(self, before, index, result):
        from desktop_runtime.cu_completion_evidence import validated_evidence
        try:
            ledger = validated_evidence(self.completion_evidence())
        except Exception:
            ledger = None
        receipt = ledger["actions"][-1] if ledger and ledger["actions"] else {}
        previous = before["previous_seq"]
        correlated = (previous is not None and receipt.get("seq") == previous + 1
                      and receipt.get("action") == "click" and receipt.get("element_index") == index
                      and receipt.get("page_scope") == before["page_scope"]
                      and receipt.get("status") == result.get("status") and before["snapshot"] is not None)
        status = ("not_dispatched" if result.get("no_effect") is True else
                  "correlation_unavailable" if not correlated else
                  "input_delivered" if result.get("status") == "ok" else "delivery_unconfirmed")
        qualified = (correlated and status != "not_dispatched" and before["fields"] is not None
                     and before["main_document"] and bool(re.fullmatch(r"form:\d+:\d+", before["form_scope"])))
        entry = {"action_seq": receipt.get("seq") if correlated else None,
                 "delivery": status, "fields_qualified": bool(qualified), "responses": []}
        if correlated:
            entry.update(page_scope=before["page_scope"], snapshot=before["snapshot"], form_scope=before["form_scope"])
            for row in ledger["responses"]:
                if row["action_seq"] == receipt["seq"] and row["page_scope"] == receipt["page_scope"]:
                    entry["responses"].append({k: row[k] for k in ("method", "status", "resource")})
        if qualified:
            entry["fields"] = before["fields"]
        for key, url in (("source_url", before["source_url"]), ("result_url", getattr(getattr(self.b, "page", None), "url", ""))):
            # Keep identities whole. An omitted URL cannot become a prefix match.
            from urllib.parse import urlsplit
            try:
                parsed = urlsplit(url)
                safe = isinstance(url, str) and len(url.encode()) <= 160 and not parsed.username and not parsed.password
            except (ValueError, TypeError, AttributeError):
                safe = False
            if safe:
                entry[key] = url
            else:
                entry[key + "_omitted"] = True
        if self._form_backend is not None and self._form_backend is not self.b:
            self._form_history.clear()
            self._form_rejections.clear()
            self._form_omitted = 0
            self._form_session = uuid.uuid4().hex
        self._form_backend = self.b
        if (qualified and status == "input_delivered" and "source_url" in entry and any(row["method"] in {"POST", "PUT", "PATCH", "DELETE"}
                and row["status"] in {400, 422} for row in entry["responses"])):
            rejection_key = entry["source_url"], entry["page_scope"], entry["form_scope"]
            previous = self._form_rejections.get(rejection_key, {"fields": None, "corrections": {}, "attempts": 0})
            old_values, new_values = _form_values(previous["fields"]), _form_values(entry["fields"])
            if old_values is not None and new_values is not None:
                for field, value in new_values.items():
                    if field in old_values and old_values[field] != value:
                        previous["corrections"][field] = previous["corrections"].get(field, 0) + 1
            self._form_rejections[rejection_key] = {**previous, "fields": entry["fields"], "attempts": previous["attempts"] + 1}
        self._form_history.append(entry)
        if len(self._form_history) > 8:
            self._form_omitted += len(self._form_history) - 8
            del self._form_history[:-8]

    def _form_readback(self, limit):
        entries = list(self._form_history)
        omitted = self._form_omitted
        def render():
            return json.dumps({"v": 1, "session": self._form_session, "entries": entries,
                               "omitted_entries": omitted}, ensure_ascii=False, separators=(",", ":"))
        while entries and len(render().encode()) > limit:
            entries.pop(0)
            omitted += 1
        text = render()
        if omitted:
            text += "\n[whole form snapshots omitted; omitted content is not verified]"
        if any(any(entry.get("fields", {}).get("omitted", {}).values()) for entry in entries):
            text += "\n[form fields omitted; omitted content is not verified]"
        return ("FORM INPUT HISTORY (chronological main-document fields before clicks; historical input, not submitted payload or acceptance proof):\n" + text)

    # engine focus-guard interface: the browser is driven over CDP, never by global keystrokes
    def frontmost(self):
        return ("Chrome (CDP)", 0)

    def focused_role(self):
        return None

    def activate(self, name):
        return None

    def observe(self) -> Observation:
        self._ensure()
        o = self.b.observe()
        from coding_agents.cu_verification_context import bounded
        labeled_facts = bounded(getattr(o, "labeled_facts", ""), 450, "labeled facts")
        safe_elements = []
        for original in o.elements:
            e = dict(original)
            if str(e.get("type") or "").casefold() == "password" or e.get("role") == "AXSecureTextField":
                secret = e.get("value")
                # The adapter cannot assume every backend honored extraction policy.
                e["type"], e["value"], e["text"], e["placeholder"], e["href"] = "password", "", "", "", ""
                if secret and e.get("label") == secret:
                    e["label"] = ""
                if secret and e.get("name") == secret:
                    e["name"] = ""
                e["label"] = e.get("label") or e.get("name") or "Password field"
            safe_elements.append(e)
        elements = [{"id": f"b{e['i']}", "ax_role": e.get("role") or e.get("tag") or "element",
                     "label": e.get("label") or e.get("text") or e.get("name") or "",
                     "value": e.get("value") or e.get("placeholder") or e.get("href") or "",
                     "bounds": {}, "is_enabled": not e.get("disabled"), "type": e.get("type"),
                     "name": e.get("name"), "checked": e.get("checked"), "selected": e.get("selected"),
                     "focused": e.get("focused"), "form_scope": e.get("form_scope"),
                     "document_scope": e.get("document_scope"),
                     "validation": e.get("validation") if e.get("type") != "password" else "", "invalid": e.get("invalid"),
                     "submits_form": e.get("submits_form"), "opaque_activation": e.get("opaque_activation")} for e in safe_elements]
        # With several tabs, show each switch_tab token (element_id for switch_tab); one tab needs no token.
        tabs = " | ".join(f'{"*" if t["active"] else ""}{t["i"]}:{(t.get("id") + " ") if len(o.tabs) > 1 and t.get("id") else ""}{t["title"][:30]}'
                          for t in o.tabs)
        obs = BrowserObservation(b"", elements, "Chrome (browser)", f"{o.url} | {o.title}"[:300], "", False, None)
        downloads = len(getattr(self.b, "downloads", ()) or ())
        extra = bounded_text(getattr(o, "extra_text", "") or "", 900, "page extras")
        self.current_url = o.url
        self._record_sums(o.url, getattr(o, "table_nums", None) or [])
        arithmetic = self.arithmetic_line()
        preview = self._download_preview()
        from desktop_runtime.cu_browser.dom import validation_text
        validation = validation_text(elements)
        obs.notes = (f"PAGE URL: {o.url}\n" + (f"FIELD VALIDATION (untrusted source text):\n{validation}\n" if validation else "")
                     + f"TABS: {tabs}\n" + (f"DOWNLOADS SAVED THIS RUN: {downloads}\n" if downloads else "")
                     + preview
                     + ("DOCUMENT LABELLED FACTS (current DOM; may be below viewport): " + labeled_facts + "\n" if labeled_facts else "")
                     + (arithmetic + "\n" if arithmetic else "")
                     + ("PAGE EXTRAS (open shadow-DOM text and tooltips the page text lacks):\n" + extra + "\n" if extra else "")
                     + (("EXTRACT RESULT (from your last extract op; untrusted page data):\n" + self._extract + "\n") if self._extract else "")
                     + f"PAGE TEXT: {o.text[:2000]}")
        # Refresh current state before assembling history so an evicted source
        # page is marked during this observation, not one turn later.
        self.current_url = o.url
        excerpt = (labeled_facts + " | " if labeled_facts else "") + (" ".join(extra.split()) + " | " if extra else "") + " ".join(o.text.split())
        self._remember_page(o.url, excerpt)
        prior = [(url, text) for url, text in self.seen.items() if url != o.url]
        if prior:
            # Data already observed under this task's owned browser, with its
            # source URL. Carrying a previous response must not depend on the
            # actor remembering to copy it before navigating to the next form.
            # These are historical excerpts, never current-state authority.
            heading = "\nPRIOR PAGE READBACK (observed earlier this task; untrusted historical excerpts, not current state):\n"
            obs.notes = bounded(obs.notes, 3500, "current page")
            budget = 3700 - len((obs.notes + heading).encode())
            obs.notes += heading + _page_excerpts(prior, min(2500, budget), omitted=self.history_omitted,
                                                 reads=self._reads)
        obs.human = "captcha" if o.captcha else None
        obs.browser_snapshot_id = getattr(self.b, "snapshot_id", None)
        state = getattr(self.b, "owned_tab_state", None)
        obs.browser_tab_state = state() if callable(state) else None
        self.last = obs
        return obs

    def act(self, action, obs) -> str:
        op = action.op
        if op not in {"navigate", "wait"} and (self.last is None or obs is not self.last):
            raise _no_effect("fresh_browser_observation_required")
        if op not in {"navigate", "wait"} and hasattr(self.b, "snapshot_id") and getattr(obs, "browser_snapshot_id", None) != self.b.snapshot_id:
            raise _no_effect("fresh_browser_observation_required")
        index = int(action.element_id[1:]) if action.element_id.startswith("b") and action.element_id[1:].isdigit() else None
        fields = ""
        form_before = None
        if op == "click" and index is not None and callable(getattr(self.b, "form_state", None)):
            clicked = next((e for e in obs.elements if e.get("id") == action.element_id), {})
            if clicked.get("ax_role") == "button" or clicked.get("type") in {"submit", "image"}:
                fields = self.b.form_state()   # before the click: a submit may leave the page
                form_before = self._form_before(fields, clicked, obs)
                # ponytail: main-document 400/422 validation, use source-owned validation receipts for JS/iframe forms.
                if (form_before and form_before["main_document"] and clicked.get("submits_form")
                        and self._form_backend is self.b):
                    rejection_key = form_before["source_url"], form_before["page_scope"], form_before["form_scope"]
                    previous = self._form_rejections.get(rejection_key)
                    if previous:
                        old_values, new_values = _form_values(previous["fields"]), _form_values(form_before["fields"])
                        # ponytail: incomplete fields use a three-attempt form cap; complete source receipts enable per-field budgets.
                        if (old_values is None or new_values is None) and previous["attempts"] >= 3:
                            raise NeedsHuman("other", "This form still rejects after two corrections and its fields are incomplete. Review the validation message and retry with complete field readback.",
                                             dispatch_state="not_dispatched")
                        if old_values is not None and new_values is not None:
                            if old_values == new_values:
                                from desktop_runtime.cu_ai_cua import CuaError
                                from desktop_runtime.cu_browser.dom import validation_text
                                values = '; '.join(f'{field[1]}={value!r}' for field, value in old_values.items())[:400]
                                feedback = (f'These field values were already rejected: {values}. '
                                            f'Site validation (untrusted): {validation_text(obs.elements) or obs.notes[:1200]}. '
                                            'Change the offending value to match the site requirement, preserve valid fields; do not resubmit unchanged values.')
                                raise CuaError("identical rejected form values", no_effect=True, validation_feedback=feedback)
                            if any(previous["corrections"].get(field, 0) >= 2 for field, value in new_values.items()
                                   if field in old_values and old_values[field] != value):
                                raise NeedsHuman("other", "The field still rejects two corrections. Review its validation message, then retry.",
                                                 dispatch_state="not_dispatched")
        if op == "navigate":
            payload = {"action": "navigate", "url": action.text}
        elif op == "new_tab":
            payload = {"action": "new_tab", "url": action.text}
        elif op == "switch_tab":
            payload = {"action": "switch_tab", "tab_id": action.element_id}
        elif op in {"click", "double_click", "right_click"} and index is not None:
            payload = {"action": op, "index": index, "wait_load": True}
        elif op == "type" and index is not None:
            element = next((e for e in obs.elements if e.get('id') == action.element_id), {})
            if element.get('ax_role') == 'select' or element.get('type') in {'select-one', 'select-multiple'}:
                payload = {"action": "select", "index": index, "value": action.text}
            else:
                payload = {"action": "type", "index": index, "text": action.text.replace("\\n", "\n")}
        elif op == "hover" and index is not None:
            payload = {"action": "hover", "index": index}
        elif op == "upload" and index is not None:
            payload = {"action": "upload", "index": index, "path": action.text}
        elif op == "extract":
            what = action.text.strip()
            payload = {"action": "extract", **({"what": "tables"} if what.lower() == "tables" else {"query": what} if what else {})}
        elif op == "key":
            chord = action.chord.lower()
            history = "back" if chord in {"cmd+left", "alt+left"} else "forward" if chord in {"cmd+right", "alt+right"} else None
            payload = {"action": history} if history else {"action": "press", "key": playwright_key(action.chord)}
        elif op == "scroll":
            payload = {"action": "scroll", "direction": action.direction, "amount": 200 * max(action.amount, 1)}
        elif op == "wait":
            payload = {"action": "wait", "seconds": action.ms / 1000}
        elif op == "type":
            payload = {"action": "press", "key": action.text[:1]} if len(action.text) == 1 else None
            if payload is None:
                raise _no_effect("browser type needs an element_id like b3 (a field from the list)")
        else:
            raise _no_effect(f"{op} does not apply to a web page (use b<N> element ids, navigate, key, scroll)")
        self._ensure()
        if op != "extract":
            self._extract = ""
        if hasattr(self.b, "dialogs"):
            self.b.dialogs.clear()
        self._cursor = self._target_point(index) if op in {"click", "double_click", "right_click", "type", "hover"} else None
        try:
            result = self.b.act(payload)
        except BaseException:
            if form_before is not None:
                self._form_after(form_before, index, {"status": "error"})
            raise
        if form_before is not None:
            self._form_after(form_before, index, result)
        status = result.get("status")
        if status == "captcha":
            raise NeedsHuman("captcha", "human verification on the page")
        if status == "refused":
            raise NeedsHuman("refused", str(result.get("error", ""))[:150],
                             dispatch_state="not_dispatched" if result.get("no_effect") is True else "unconfirmed")
        if status != "ok":
            # Preserve the source-owned dispatch fact. In particular a timeout
            # after input is not proof that nothing happened; retrying can submit
            # or increment twice.
            error = str(result.get("error", "browser action failed"))
            hint = next((text for key, text in _HINTS if key in error), "")
            if result.get("no_effect") is True:
                raise _no_effect(error[:110] + hint)
            from desktop_runtime.cu_ai_cua import CuaError
            raise CuaError(error[:110] + hint)
        if op in {"new_tab", "switch_tab"}:
            self.last = None
        if form_before is not None:
            from desktop_runtime.cu_browser.dom import format_form_state
            fields = format_form_state(self._form_history[-1]["fields"]) if self._form_history[-1]["fields_qualified"] else ""
        self._log_done(action, obs, fields)
        note = ""
        if op == "extract":
            self._extract = bounded_text(str(result.get("data")), 1500, "extract result")
            source = self.current_url
            read = "EXTRACT " + bounded_text(action.text, 160, "query") + ": " + self._extract
            records = self._reads.setdefault(source, [])
            if read not in records:
                records.insert(0, read)
            while len("\n".join(records).encode()) > 2000:
                records.pop()
                self.history_omitted = True
        dialogs = list(getattr(self.b, "dialogs", ()) or ())
        if dialogs:
            note = "; ".join(f"a native {d['type']} dialog '{d['message']}' was " + ("accepted" if d["accepted"] else
                    "dismissed because the task did not clearly authorize it: use needs_you if it is required") for d in dialogs)
            self.did.append("dialog " + note[:100])
            del self.did[:-16]
        return f"{payload['action']} ok ({result.get('seconds')}s)" + (cu_ai_tools.PRIVATE + note if note else "")

    def _record_sums(self, url, columns):
        """Exact decimal sum of every numeric table column on this page (re-observing a page replaces its entry)."""
        from decimal import Decimal, InvalidOperation
        found = {}
        for column in columns[:8]:
            try:
                values = [Decimal(re.sub(r"[^\d.+-]", "", v)) for v in column["values"][:400]]
            except (InvalidOperation, KeyError, TypeError):
                continue
            header = str(column.get("header") or "column")[:30]
            total, count = found.get(header, (Decimal(0), 0))
            found[header] = (total + sum(values), count + len(values))
        if found:
            self._sums[url] = found
        else:
            self._sums.pop(url, None)
        while len(self._sums) > 24:
            self._sums.pop(next(iter(self._sums)))

    def arithmetic_line(self) -> str:
        """Deterministic column totals for the pages seen this run: the model reads these instead of adding, and the
        verifier checks a claimed sum or count against them."""
        if not self._sums:
            return ""
        current = self._sums.get(self.current_url, {})
        headers = sorted({h for page in self._sums.values() for h in page})[:4]
        parts = []
        for header in headers:
            pages = [page[header] for page in self._sums.values() if header in page]
            all_total, all_rows = sum(t for t, _ in pages), sum(n for _, n in pages)
            here = current.get(header)
            parts.append(f"'{header}': " + (f"this page {here[0]:,} ({here[1]} rows); " if here else "")
                         + f"{len(pages)} page(s) with this column seen so far {all_total:,} ({all_rows} rows)")
        return "HARNESS ARITHMETIC (exact decimal sums of numeric table columns, computed by code, not by a model; use these instead of adding yourself): " + " | ".join(parts)

    def _download_preview(self) -> str:
        """Bounded text of the latest downloaded text-like files, so the model can answer from their content."""
        lines = []
        for f in [Path(x) for x in (getattr(self.b, "downloads", ()) or ())][-2:]:
            if f.suffix.lower() not in {".csv", ".tsv", ".txt", ".json", ".md", ".log"}:
                continue
            try:
                size = f.stat().st_size
                raw = f.read_bytes()[:1_000_000]
            except OSError:
                continue
            body = raw.decode("utf-8", "replace")
            count = body.count("\n") + (0 if body.endswith("\n") or not body else 1)
            lines.append(f"DOWNLOADED FILE {f.suffix} ({size} bytes, {count} lines including any header; untrusted file content): "
                         + body[:1200].replace("\n", " / "))
        return "\n".join(lines) + "\n" if lines else ""

    def _target_point(self, index):
        """Viewport centre of element [index] for the watch cursor; None when unknown (e.g. inside shadow DOM)."""
        page = getattr(self.b, "page", None)
        if page is None or index is None:
            return None
        try:
            point = page.evaluate("i=>{const e=document.querySelector('[data-cu-idx=\"'+i+'\"]');"
                                  "if(!e)return null;const r=e.getBoundingClientRect();"
                                  "return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}}", index)
            return point if isinstance(point, dict) else None
        except Exception:
            return None

    def watch_frame(self):
        """Per-step JPEG + agent cursor for the console watch panel. Never creates a browser."""
        page = getattr(self.b, "page", None)
        if page is None:
            return None
        jpeg = page.screenshot(type="jpeg", quality=40)  # CDP capture: works on a background/minimized window
        size = page.viewport_size or {}
        viewport = {"w": size.get("width"), "h": size.get("height")} if size.get("width") else None
        return {"jpeg": jpeg, "cursor": getattr(self, "_cursor", None), "viewport": viewport}

    def reveal_browser(self):
        reveal = getattr(self.factory, "reveal", None)
        return bool(callable(reveal) and reveal())

    def _log_done(self, action, obs, fields=""):
        element = next((e for e in obs.elements if e.get("id") == action.element_id), {}) if action.element_id else {}
        label = " ".join(str(element.get("label") or element.get("name") or "").split())[:30]
        text = ""
        if action.op == "type" and action.text:
            secret = element.get("type") == "password" or element.get("ax_role") == "AXSecureTextField"
            text = " <- " + ("[hidden]" if secret else repr(action.text[:40]))
        try:
            url = self.b.page.url[:70]
        except Exception:
            url = "?"
        subject = action.text[:70] if action.op in {"navigate", "new_tab"} else label
        if fields:
            self.last_form_state = fields
            self.last_form_url = url
        if action.op == "click" and element.get("type") in {"checkbox", "radio"}:
            text = " (now " + ("unchecked" if element.get("type") == "checkbox" and element.get("checked") else "checked") + ")"
        self.did.append(f"{action.op} {subject}{text}{' [form: ' + fields + ']' if fields else ''} -> {url}".replace("\n", " "))
        del self.did[:-16]

    def close(self):
        from desktop_runtime.cu_cleanup import CleanupError, cleanup_codes, cleanup_trace
        codes, traces = [], []
        try:
            if self.b is not None:
                self.b.close()
        except Exception as error:
            codes.extend(cleanup_codes(error, "browser_cdp_close_unconfirmed"))
            traces.extend(cleanup_trace(error))
        stop = getattr(self.factory, "stop", None)
        try:
            if stop:
                stop()
        except Exception as error:
            codes.extend(cleanup_codes(error, "browser_process_close_unconfirmed"))
            traces.extend(cleanup_trace(error))
        if codes:
            error = CleanupError("cu_browser_close_unconfirmed", codes)
            error.cleanup_trace = traces[-32:]
            raise error from None


class RoutedMac:
    """Route per step: web page -> browser backend; native app -> AX/vision backend. Mixed tasks switch freely."""

    def __init__(self, native, browser: BrowserMac, start: str = "native"):
        self.native, self.browser, self.mode = native, browser, start

    def __getattr__(self, name):   # anything else (switch_app, fronted, ...) is the native backend's
        return getattr(self.native, name)

    def watch_frame(self):
        return self.browser.watch_frame() if self.mode == "browser" else None

    def reveal_browser(self):
        return self.browser.reveal_browser()

    def invalidate_observation(self):
        for backend in (self.native, self.browser):
            invalidate = getattr(backend, "invalidate_observation", None)
            if callable(invalidate):
                invalidate()

    def reset_completion_evidence(self):
        self.browser.reset_completion_evidence()

    def set_goal(self, goal):
        self.browser.set_goal(goal)

    def submit_key(self, action, obs):
        return self.browser.submit_key(action, obs) if self.mode == "browser" else None

    def submit_accepted(self):
        return self.browser.submit_accepted()

    def form_validation_feedback(self, obs):
        return self.browser.form_validation_feedback(obs)

    def browser_readback(self):
        return self.browser.browser_readback()

    def completion_evidence(self):
        return self.browser.completion_evidence()

    @property
    def _active(self):
        return self.browser if self.mode == "browser" else self.native

    def frontmost(self):
        return self._active.frontmost()

    def focused_role(self):
        return self._active.focused_role()

    def activate(self, name):
        return self._active.activate(name)

    def observe(self):
        return self._active.observe()

    def act(self, action, obs):
        op = action.op
        if op in {"navigate", "new_tab", "switch_tab"} or action.element_id.startswith("b") and self.mode == "browser":
            self.mode = "browser"
            return self.browser.act(action, obs)
        if op == "open_path":
            self.mode = "native"
            return self.native.act(action, obs)
        if op in {"open_app", "switch_app"}:
            if BROWSER_NAMES.match(action.app.strip()):
                self.mode = "browser"
                return f"web pages are driven in the browser backend: now in browser mode ({action.app}); use navigate"
            self.mode = "native"
            return self.native.act(action, obs)
        return self._active.act(action, obs)

    def close(self):
        from desktop_runtime.cu_cleanup import CleanupError, cleanup_codes, cleanup_trace, public_cleanup_trace
        codes, traces = [], []
        for part, fallback in ((self.browser, "browser_cdp_close_unconfirmed"),
                               (self.native, "native_backend_close_unconfirmed")):
            if hasattr(part, "close"):
                try:
                    part.close()
                except Exception as error:
                    codes.extend(cleanup_codes(error, fallback))
                    traces.extend(cleanup_trace(error))
                else:
                    traces.extend(public_cleanup_trace(getattr(part, "cleanup_trace", None)))
        self.cleanup_trace = traces[-32:]
        if codes:
            error = CleanupError("cu_backend_close_unconfirmed", codes)
            error.cleanup_trace = traces[-32:]
            raise error from None


class ChromeFactory:
    """Starts the dedicated Chrome (CDP) + BrowserBackend once. Nothing starts until the first browser step.

    Default: headless, fresh tmp profile per session (optionally a copy of the account in
    SUPYMEM_CU_CHROME_PROFILE_EMAIL). With `account_id`: the persistent per-account Supymem profile
    (app data dir, 0700), seeded once from that copy, reused so sign-ins persist, never deleted here, and
    visible-but-minimized unless the policy asks for headless (SUPYMEM_CU_BROWSER_MODE=headless)."""

    def __init__(self, data_dir, *, profile_email=None, account_id=None, headless=None):
        import os
        from pathlib import Path
        self.data_dir, self.session = Path(data_dir), None
        self._owned_profile = None
        self.profile_email, self.account_id = profile_email, account_id
        mode = os.environ.get("SUPYMEM_CU_BROWSER_MODE", "")
        self.headless = headless if headless is not None else (mode == "headless" if mode else account_id is None)

    def _email(self):
        import os
        return (os.environ.get("SUPYMEM_CU_CHROME_PROFILE_EMAIL", "")
                if self.profile_email is None else self.profile_email).strip()

    def __call__(self):
        from desktop_runtime import cu_browser
        endpoint = self.start_endpoint()
        try:
            return cu_browser.BrowserBackend(endpoint, download_dir=self.data_dir / "downloads", owned_context=True)
        except BaseException:
            try:
                self.stop()
            except Exception:
                raise RuntimeError("browser_factory_cleanup_unconfirmed") from None
            raise

    def start_endpoint(self):
        """Start Chrome and return its loopback CDP endpoint. Callers own the connection and must call stop()."""
        from desktop_runtime import cu_browser
        from desktop_runtime.cu_browser.chrome import SEEDED_MARKER
        import tempfile
        from pathlib import Path
        if self.session is not None or self._owned_profile is not None:
            raise RuntimeError("browser_factory_session_active")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        persistent = self.account_id is not None
        try:
            if persistent:
                profile = cu_browser.persistent_profile_dir(self.data_dir, self.account_id)
                cu_browser.release_stale_singleton(profile)  # raises browser_profile_in_use if another run owns it
            else:
                # A fresh profile dir per session: a stale one from a crashed run would hold Chrome's singleton lock.
                profile = Path(tempfile.mkdtemp(prefix="chrome_profile_", dir=self.data_dir))
                self._owned_profile = profile
            email = self._email()
            if email and not (persistent and (profile / SEEDED_MARKER).exists()):
                try:
                    cu_browser.prepare_profile_copy(email, profile)  # never copies Login Data
                    if persistent:
                        (profile / SEEDED_MARKER).write_text("1")  # seed once; later sign-ins live in the profile
                except LookupError:
                    pass
            self.session = cu_browser.ChromeSession(profile, headless=self.headless)
            return self.session.start()
        except BaseException:
            try:
                self.stop()
            except Exception:
                raise RuntimeError("browser_factory_cleanup_unconfirmed") from None
            raise

    def reveal(self):
        """Bring the Supymem browser window forward. Founder's click only; the harness never calls this."""
        return bool(self.session is not None and not self.headless and self.session.reveal())

    def stop(self):
        if self.session is not None:
            self.session.stop()
        if self._owned_profile is not None:
            import shutil
            try:
                shutil.rmtree(self._owned_profile)  # only the exact factory-created profile
            except Exception:
                raise RuntimeError("browser_profile_cleanup_unconfirmed") from None
        self.session, self._owned_profile = None, None
