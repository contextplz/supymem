"""Browser backend: observe() -> Observation, act(action dict) -> result dict.

Thin adapter shape for the engine (the engine's PR is not merged yet): the engine
owns planning, progress memory, checkpoints and notifications; this module only
observes and performs one step. Results carry ``status``: ok | refused | captcha | error,
so the engine's safety / CAPTCHA->notify flow can act on them without parsing prose.
Everything runs over CDP on a background tab; nothing activates the Chrome window.
"""
from __future__ import annotations

import re
import os
import stat
import copy
import uuid
from collections import deque
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .dom import FIND_TEXT_JS, FORM_STATE_JS, FRAME_TEXT_JS, INDEX_JS, PAGE_READBACK_JS, PAGE_TEXT_JS, render_elements, format_form_state

# Playwright raises these before any input reaches the page, so they are retryable, never effect-uncertain.
_PRE_DELIVERY = ("Unknown key", "cannot be filled", "Element is not")
_FILLABLE_JS = """e => {
  const t = (e.type || '').toLowerCase(), tag = e.tagName.toLowerCase();
  if (e.isContentEditable) return 'ok';
  if (tag === 'textarea') return e.readOnly || e.disabled ? 'readonly' : 'ok';
  if (tag === 'input') return ['file','checkbox','radio','button','submit','reset','image','hidden'].includes(t) ? 'input type ' + t
    : (e.readOnly || e.disabled ? 'readonly' : 'ok');
  return 'a ' + tag + ' element';
}"""
ACTIONS = ("navigate", "click", "double_click", "right_click", "type", "select", "scroll", "press", "new_tab", "switch_tab",
           "close_tab", "wait", "extract", "upload", "hover", "back", "forward", "screenshot")
BROWSER_APPS = ("google chrome", "chrome", "safari", "arc", "firefox", "brave", "microsoft edge", "browser")
_PAYMENT_TEXT = re.compile(r"\b(pay now|place (your )?order|complete (purchase|order)|buy now|purchase|confirm (payment|order)|subscribe and pay|pay \$)", re.I)
_CARD_FIELD = re.compile(r"card|cvv|cvc|cc-(number|csc|exp)|security.?code", re.I)
_CAPTCHA_JS = r"""() => {
  if (document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="challenges.cloudflare"],.g-recaptcha,.h-captcha,.cf-turnstile')) return true;
  const human = /verify (that )?you are (a )?human|are you a robot|unusual traffic|i(?:'m| am) not a robot|human verification|verification challenge/i;
  const transcription = /(?:type|enter)\s+(?:the\s+)?(?:letters|characters)\b|(?:solve|complete|enter|type)\s+(?:(?:the|this|a)\s+)?captcha\b/i;
  for (const form of document.querySelectorAll('form')) {
    const text = (form.innerText || '').slice(0, 3000);
    if (form.querySelector('input:not([type=hidden]),textarea') && (human.test(text) || transcription.test(text))) return true;
  }
  return human.test((document.body && document.body.innerText || '').slice(0, 3000));
}"""


class SafetyRefusal(Exception):
    pass


class _Bindings(dict):
    """element index -> node handle, resolved on first use. Binding every indexed element eagerly cost two CDP round
    trips each (hundreds per observation on a busy public page); an action only ever needs one or two of them."""

    def __init__(self):
        super().__init__()
        self.sources = []   # (base, end, snapshot handle kept alive until the next observation)

    def get(self, index, default=None):
        if index in self:
            return self[index]
        for base, end, handle in self.sources:
            if base <= index < end:
                try:
                    item = handle.get_property(str(index - base))
                    try:
                        node = item.get_property('__cuNode').as_element()
                    finally:
                        item.dispose()
                except Exception:
                    return default
                if node is not None:
                    self[index] = node
                    return node
        return default

    def dispose_all(self):
        handles = list(self.values()) + [h for _, _, h in self.sources]
        self.sources = []
        self.clear()
        for handle in handles:
            try:
                handle.dispose()
            except Exception:
                pass  # Closing CDP/process remains the independent cleanup gate.


@dataclass
class Observation:
    url: str
    title: str
    elements: list[dict]
    text: str
    tabs: list[dict]
    captcha: bool = False
    screenshot: bytes | None = None
    timings: dict = field(default_factory=dict)
    labeled_facts: str = ""
    extra_text: str = ""     # open shadow-root text + tooltips the plain page text misses
    table_nums: list = field(default_factory=list)   # numeric table columns, summed exactly by the adapter

    def render(self) -> str:
        tabs = " | ".join(f'{"*" if t["active"] else ""}{t["i"]}:{t["title"][:30]}' for t in self.tabs)
        flag = "\n!! CAPTCHA/human check present: stop and notify the founder" if self.captcha else ""
        return f"URL: {self.url}\nTitle: {self.title}\nTabs: {tabs}{flag}\n--- elements ---\n{render_elements(self.elements)}\n--- text ---\n{self.text}"


def is_browser_step(step: dict) -> bool:
    """Engine routing: browser steps (a URL, a browser app, or an explicit browser action) come here."""
    if step.get("backend") == "browser" or str(step.get("app", "")).lower() in BROWSER_APPS:
        return True
    return bool(step.get("url"))


class BrowserBackend:
    def __init__(self, cdp_endpoint: str, *, download_dir: Path, allow_passwords: bool = False,
                 text_limit: int = 2000, owned_context: bool = False):
        if type(owned_context) is not bool:
            raise ValueError("owned_browser_context_invalid")
        self._owned_tabs = owned_context
        self._tab_bindings, self._tab_indices = {}, {}
        from playwright.sync_api import sync_playwright
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.allow_passwords, self.text_limit = allow_passwords, text_limit
        self._pw = sync_playwright().start()
        for attempt in range(4):   # Playwright's default 180 s connect wait made a rare stalled handshake look like a hung run
            try:
                self.browser = self._pw.chromium.connect_over_cdp(cdp_endpoint, timeout=4000)
                break
            except Exception:
                if attempt == 3:
                    self._pw.stop()
                    raise
                time.sleep(0.3)
        self.context = self.browser.contexts[0]
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._element_bindings = {}
        self._indexed_page = None
        self.downloads: list[str] = []
        self.dialogs: list[dict] = []
        self.dialog_policy = None   # (kind, message) -> bool, set by the adapter that knows the task
        self.context.on("page", self._watch)
        for p in self.context.pages:
            self._watch(p)

    def _evidence_state(self):
        if not hasattr(self, "_action_receipts"):
            self._action_receipts = deque(maxlen=16)
            self._response_receipts = deque(maxlen=16)
            self._page_scopes = {}
            self._action_seq = 0
            self._active_action = None
            self._pending_requests = {}
            self._document_generations = {}

    def reset_completion_evidence(self):
        self._evidence_state()
        self._action_receipts.clear()
        self._response_receipts.clear()
        self._pending_requests.clear()
        self._page_scopes.clear()
        self._action_seq = 0
        self._active_action = None

    def _scope(self, page):
        self._evidence_state()
        return self._page_scopes.setdefault(id(page), uuid.uuid4().hex)

    def completion_evidence(self):
        """Local readback/transport facts, never business acceptance or user-task proof."""
        self._evidence_state()
        return copy.deepcopy({"v": 1, "source": "owned_browser_backend",
            "actions": list(self._action_receipts), "responses": list(self._response_receipts)})

    def _request(self, page, request):
        # Bind responses to the operation that actually initiated the request,
        # not whichever operation happens to be active when it finishes.
        if self._active_action is None:
            return
        if len(self._pending_requests) >= 16:
            self._pending_requests.pop(next(iter(self._pending_requests)))
        self._pending_requests[request] = (self._active_action, self._scope(page))

    def _response(self, page, response):
        # Never inspect URL, credentials, request/response headers or payloads.
        try:
            request = response.request
            bound = self._pending_requests.pop(request, None)
            if bound is None or bound[1] != self._scope(page):
                return
            status = response.status
            method = request.method
            resource = request.resource_type
            if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                return
            method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER"
            resource = resource if resource in {"document", "xhr", "fetch"} else "other"
            self._response_receipts.append({"action_seq": bound[0], "page_scope": bound[1],
                                           "method": method, "status": status, "resource": resource})
        except Exception:
            return  # Optional evidence cannot change action execution.

    def _field_state(self, index):
        try:
            value = self._el(index).evaluate("e => ({type:e.type||'', checked:typeof e.checked==='boolean'?e.checked:null})")
            kind = value.get("type")
            return {"field_type": kind if kind in {"checkbox", "radio", "text", "email", "password", "search", "number", "textarea", "select-one"} else "other",
                    "checked": value.get("checked") if kind in {"checkbox", "radio"} and isinstance(value.get("checked"), bool) else None}
        except Exception:
            return {}

    def _navigation(self, page, frame):
        if frame is page.main_frame:
            key = id(page)
            self._document_generations[key] = self._document_generations.get(key, 0) + 1
            if page is getattr(self, '_indexed_page', None):
                # Event callbacks only invalidate; remote handles are disposed
                # during the next explicit observation/close, not reentrantly.
                self._indexed_page = None
                self.snapshot_id = None

    def _watch(self, page) -> None:
        self._evidence_state()
        self._scope(page)
        page.on("framenavigated", lambda frame: self._navigation(page, frame))
        page.on("dialog", self._on_dialog)
        page.on("download", lambda d: self._save(d))
        page.on("request", lambda request: self._request(page, request))
        page.on("response", lambda response: self._response(page, response))

    def _on_dialog(self, dialog) -> None:
        """Native alert/confirm/prompt: decided by the caller's policy and always reported, never silently dismissed.
        alert/beforeunload are harmless; confirm is accepted only when the policy allows; prompt is never answered."""
        try:
            kind, message = dialog.type, str(dialog.message or "")[:160]
            policy = getattr(self, "dialog_policy", None)
            accept = kind in {"alert", "beforeunload"} or (kind == "confirm" and callable(policy) and bool(policy(kind, message)))
            if not hasattr(self, "dialogs"):
                self.dialogs = []
            self.dialogs.append({"type": kind, "message": message, "accepted": accept})
            dialog.accept() if accept else dialog.dismiss()
        except Exception:
            try:
                dialog.dismiss()
            except Exception:
                pass

    def _save(self, d) -> None:
        try:
            self._store_download(d)
        except Exception:
            # Download event exceptions must not expose source paths/URLs.
            raise RuntimeError('browser_download_failed') from None

    def _store_download(self, d) -> None:
        # Suggested names are presentation hints, never destination authority.
        suffix = Path(str(d.suggested_filename).replace('\\', '/')).suffix
        suffix = suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,10}', suffix) else '.bin'
        leaf = 'download-' + uuid.uuid4().hex + suffix
        temporary = '.partial-' + uuid.uuid4().hex
        source = d.path()  # Local owned CDP artifact; remote transports fail closed.
        if source is None:
            raise RuntimeError('browser_download_unavailable')
        directory = source_fd = output_fd = None
        created = committed = False
        try:
            directory = os.open(self.download_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
                raise RuntimeError('browser_download_limit')
            output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            created = True
            total, deadline = 0, time.monotonic() + 15
            while block := os.read(source_fd, 1024 * 1024):
                total += len(block)
                if total > 64 * 1024 * 1024 or time.monotonic() > deadline:
                    raise RuntimeError('browser_download_limit')
                pending = memoryview(block)
                while pending:
                    written = os.write(output_fd, pending)
                    if written <= 0:
                        raise RuntimeError('browser_download_write_failed')
                    pending = pending[written:]
            os.fsync(output_fd)
            current, held = os.stat(self.download_dir, follow_symlinks=False), os.fstat(directory)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise RuntimeError('browser_download_directory_changed')
            os.link(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            committed = True  # Exclusive link cannot overwrite a prior file/symlink.
            current = os.stat(self.download_dir, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise RuntimeError('browser_download_directory_changed')
            self.downloads.append(str(self.download_dir / leaf))
        except Exception:
            if committed:
                os.unlink(leaf, dir_fd=directory)
            raise
        finally:
            try:
                if created:
                    os.unlink(temporary, dir_fd=directory)
            finally:
                for descriptor in (output_fd, source_fd, directory):
                    if descriptor is not None:
                        os.close(descriptor)

    def _clear_snapshot(self):
        self._indexed_page = None
        self.snapshot_id = None
        self._tab_bindings, self._tab_indices = {}, {}
        bindings = getattr(self, "_element_bindings", {})
        handles = [getattr(self, "_document_handle", None), getattr(self, "_keyboard_handle", None)]
        self._document_handle = self._keyboard_handle = None
        if isinstance(bindings, _Bindings):
            bindings.dispose_all()
        else:
            handles += list(bindings.values())
        for handle in handles:
            try:
                handle.dispose()
            except Exception:
                pass  # Closing CDP/process remains the independent cleanup gate.
        self._element_bindings = {}

    def close(self) -> None:
        self._clear_snapshot()
        try:
            self.browser.close()  # CDP connection only; the dedicated Chrome is stopped by ChromeSession
        finally:
            self._pw.stop()

    # observation ---------------------------------------------------------------------
    def observe(self, *, screenshot: bool = False) -> Observation:
        """One snapshot; if the page navigates underneath it (soft navs, redirects), settle and look again."""
        for attempt in range(3):
            try:
                return self._observe_once(screenshot=screenshot)
            except Exception as error:
                transient = "Execution context was destroyed" in str(error) or "fresh_browser_observation_required" in str(error)
                if not transient or attempt == 2:
                    raise
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                    self.page.wait_for_timeout(250)
                except Exception:
                    pass

    def _observe_once(self, *, screenshot: bool = False) -> Observation:
        t0 = time.monotonic()
        p = self.page
        self._clear_snapshot()
        generation = self._document_generations.get(id(p), 0)
        snapshot = p.evaluate_handle(INDEX_JS, 0)
        bindings = _Bindings()
        try:
            elements = snapshot.json_value()
            for row in elements:
                row["document_scope"] = "main"
            self._document_handle = snapshot.get_property('__cuDocument')
            self._keyboard_handle = snapshot.get_property('__cuKeyboard').as_element()
            bindings.sources.append((0, len(elements), snapshot))
        except Exception:
            snapshot.dispose()
            bindings.dispose_all()
            raise
        frame_text = ""
        for frame in [f for f in p.frames if f is not p.main_frame and not f.is_detached()][:4]:  # iframes: same indexing, own frame
            try:
                if frame.url in ("", "about:blank"):
                    continue
                fsnap = frame.evaluate_handle(INDEX_JS, len(elements))
                try:
                    rows = fsnap.json_value()
                    for row in rows:
                        row["inView"] = True  # Playwright scrolls a frame element into view on click
                        row["document_scope"] = "frame"
                    bindings.sources.append((len(elements), len(elements) + len(rows), fsnap))
                    elements += rows
                except Exception:
                    fsnap.dispose()
                    raise
                frame_text += " [iframe] " + frame.evaluate(FRAME_TEXT_JS, 400)
            except Exception:
                continue  # a frame navigating away mid-observe is skipped, never fatal
        self._element_bindings = bindings
        self._indexed_page = p
        self._indexed_url = p.url
        self._indexed_generation = generation
        self.snapshot_id = uuid.uuid4().hex
        t1 = time.monotonic()
        readback = p.evaluate(PAGE_READBACK_JS, self.text_limit)
        main_text = readback["text"]
        obs = Observation(
            url=p.url, title=p.title(), elements=elements, text=main_text[:max(0, self.text_limit - len(frame_text))] + frame_text,
            tabs=self._observe_tabs(p),
            captcha=bool(p.evaluate(_CAPTCHA_JS)), labeled_facts=readback["labeled_facts"],
            extra_text=readback.get("extra", ""), table_nums=readback.get("tables") or [])
        if screenshot:  # CDP Page.captureScreenshot: works on background tabs
            obs.screenshot = p.screenshot(type="jpeg", quality=60)
        if self._document_generations.get(id(p), 0) != generation or self._indexed_page is not p:
            self._clear_snapshot()
            raise RuntimeError('fresh_browser_observation_required')
        obs.timings = {"index_s": round(t1 - t0, 3), "total_s": round(time.monotonic() - t0, 3)}
        return obs

    def form_state(self) -> str:
        """Current non-secret form control values on the active page ('' when unreadable)."""
        try:
            return format_form_state(self.page.evaluate(FORM_STATE_JS))
        except Exception:
            return "FORM STATE unavailable; omitted content is not verified"

    @staticmethod
    def _bind(snapshot, rows, base, bindings):
        for row in rows:
            item = snapshot.get_property(str(row['i'] - base))
            try:
                node = item.get_property('__cuNode').as_element()
                if node is None:
                    raise RuntimeError('browser_snapshot_unavailable')
                bindings[row['i']] = node
            finally:
                item.dispose()

    def owned_tab_state(self):
        # Stable local identity/count, never a freshly minted observation token.
        if not getattr(self, '_owned_tabs', False) or self.page.is_closed():
            return None
        pages = self.context.pages
        if not any(page is self.page for page in pages):
            return None
        return (len(pages), self._scope(self.page))

    def completion_snapshot_current(self, snapshot_id, url):
        """Read-only identity check; live custody alone cannot validate an old DOM."""
        try:
            if not snapshot_id or snapshot_id != self.snapshot_id or self.page.url != url or self.owned_tab_state() is None:
                return False
            self._assert_page_scope()
            return snapshot_id == self.snapshot_id and self.page.url == url and self.owned_tab_state() is not None
        except Exception:
            return False

    def _observe_tabs(self, active):
        rows = []
        for i, page in enumerate(self.context.pages[:32]):
            token = "t" + uuid.uuid4().hex if getattr(self, '_owned_tabs', False) else None
            if token is not None:
                self._tab_bindings[token] = (page, self._document_generations.get(id(page), 0), page.url)
                self._tab_indices[i] = token
            rows.append({"i": i, "id": token, "title": page.title() if page is active else page.url,
                         "active": page is active})
        return rows

    def _tab_target(self, action):
        if not getattr(self, '_owned_tabs', False):
            raise SafetyRefusal('owned_browser_tabs_required')
        self._assert_page_scope()
        token = action.get('tab_id')
        if token is not None and 'tab' in action:
            raise RuntimeError('stale_browser_tab')
        if token is None:
            index = action.get('tab')
            if index is None and action.get('action') == 'close_tab':
                matches = [candidate for candidate, bound in self._tab_bindings.items() if bound[0] is self.page]
                token = matches[0] if len(matches) == 1 else None
            elif type(index) is not int:
                raise RuntimeError('stale_browser_tab')
            else:
                token = self._tab_indices.get(index)  # legacy index bound by observation, never live order
        binding = self._tab_bindings.get(token) if type(token) is str else None
        if binding is None:
            raise RuntimeError('stale_browser_tab')
        page, generation, url = binding
        if (page.is_closed() or not any(page is candidate for candidate in self.context.pages)
                or self._document_generations.get(id(page), 0) != generation or page.url != url):
            raise RuntimeError('stale_browser_tab')
        self._guard_url(url, blank=True)
        return page

    @staticmethod
    def _guard_url(url, *, blank=False):
        if blank and url == 'about:blank':
            return
        if type(url) is not str or len(url) > 2000 or any(ord(c) < 33 for c in url):
            raise SafetyRefusal('browser_tab_url_invalid')
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
            parsed.port
        except ValueError:
            raise SafetyRefusal('browser_tab_url_invalid') from None

    # actions -------------------------------------------------------------------------
    def _assert_page_scope(self):
        if self.page is not getattr(self, '_indexed_page', None) or self.page.url != self._indexed_url:
            raise RuntimeError('fresh_browser_observation_required')
        try:
            same = self.page.evaluate('(d) => d === document', self._document_handle)
        except Exception:
            same = False
        if same is not True or self.page is not self._indexed_page or self._indexed_generation != self._document_generations.get(id(self.page), 0):
            raise RuntimeError('fresh_browser_observation_required')

    def _el(self, index: int):
        self._assert_page_scope()
        node = self._element_bindings.get(int(index))
        if node is None:
            raise RuntimeError('stale_browser_element')
        try:
            valid = node.evaluate("(e, i) => e.isConnected && e.getAttribute('data-cu-idx') === String(i)", int(index))
        except Exception:
            valid = False
        if valid is not True:
            raise RuntimeError('stale_browser_element')
        return node

    def check_safety(self, action: dict, info: dict | None = None) -> None:
        kind = action.get("action")
        if kind in {"click", "double_click", "right_click", "type", "select", "press", "upload"}:
            # This must precede input delivery. Merely reporting a CAPTCHA
            # after filling/submitting it cannot enforce the human-only gate.
            if self.page.evaluate(_CAPTCHA_JS):
                raise SafetyRefusal("human verification challenge needs the founder")
        if kind == "navigate" or kind == "new_tab" and action.get("url") is not None and action.get("url") != "":
            self._guard_url(action.get("url", ""))
        if kind in ("click", "double_click", "right_click", "type", "select", "upload") and info:
            blob = " ".join(str(info.get(k, "")) for k in ("text", "label", "name", "placeholder", "autocomplete"))
            if kind in {"click", "double_click", "right_click"} and _PAYMENT_TEXT.search(blob):
                raise SafetyRefusal("payment submission needs the founder, not the agent")
            if kind == "type" and _CARD_FIELD.search(blob):
                raise SafetyRefusal("card details are never typed by the agent")
            if kind == "type" and info.get("type") == "password" and not (
                    self.allow_passwords and action.get("authorized") is True):
                raise SafetyRefusal("password fields need explicit task authorization")

    def _info(self, index: int) -> dict:
        return self._el(index).evaluate(
            "e => ({text:(e.innerText||e.value||'').slice(0,80), label:e.getAttribute('aria-label')||'', name:e.name||'',"
            "placeholder:e.placeholder||'', type:e.type||'', autocomplete:e.autocomplete||''})")

    def act(self, action: dict) -> dict:
        t0 = time.monotonic()
        kind = action.get("action")
        self._evidence_state()
        self._action_seq += 1
        receipt = {"seq": self._action_seq, "page_scope": self._scope(self.page),
                   "action": kind if kind in ACTIONS else "other", "status": "error"}
        self._active_action = self._action_seq
        # Only our own admission checks can prove that no input was delivered.
        # A Playwright timeout may happen after a click/fill took effect.
        self._dispatch_started = False
        try:
            if kind not in ACTIONS:
                raise ValueError(f"unknown action {kind!r}")
            if kind in {"new_tab", "switch_tab", "close_tab"}:
                if not getattr(self, '_owned_tabs', False):
                    raise SafetyRefusal('owned_browser_tabs_required')
                self._assert_page_scope()
            if kind in {"press", "scroll"}:
                self._assert_page_scope()
            info = self._info(action["index"]) if kind in ("click", "double_click", "right_click", "type", "select", "upload") else None
            self.check_safety(action, info)
            index = action.get("index")
            if kind in {"click", "double_click", "right_click", "type", "select"} and isinstance(index, int) and not isinstance(index, bool):
                receipt["element_index"] = index
                before = self._field_state(index)
                if before:
                    receipt["before"] = before
            data = self._do(kind, action)
            if (kind in {"new_tab", "switch_tab", "close_tab"}
                    or self.page is not getattr(self, '_indexed_page', None)
                    or self.page.url != getattr(self, '_indexed_url', None)):
                self._clear_snapshot()  # Navigation/popup/tab transitions retire old authority.
            if ("element_index" in receipt or kind == "press") and self._indexed_page is self.page:
                try:
                    next_focus = self.page.evaluate_handle('(d) => d === document ? document.activeElement : null', self._document_handle).as_element()
                    if next_focus is not None:
                        if self._keyboard_handle is not None:
                            self._keyboard_handle.dispose()
                        self._keyboard_handle = next_focus
                except Exception:
                    self._indexed_page = None
                    self.snapshot_id = None
            if "element_index" in receipt:
                after = self._field_state(index)
                if after:
                    receipt["after"] = after
                if kind in {"type", "select"} and not action.get("submit"):
                    try:
                        if kind == "select":
                            actual = self._el(index).evaluate("e => [...e.selectedOptions].map(option => option.label)")
                            receipt["action_input_matched"] = isinstance(actual, list) and action.get('value') in actual
                        else:
                            actual = self._el(index).evaluate("e => e.value")
                            receipt["action_input_matched"] = isinstance(actual, str) and actual == action.get("text")
                    except Exception:
                        receipt["input_readback"] = "unavailable"
            status = "captcha" if self.page.evaluate(_CAPTCHA_JS) else "ok"
            receipt["status"] = status
            return {"status": status, "action": kind, "data": data, "seconds": round(time.monotonic() - t0, 3)}
        except SafetyRefusal as e:
            receipt["status"] = "refused"
            return {"status": "refused", "action": kind, "error": str(e), "no_effect": not self._dispatch_started,
                    "seconds": round(time.monotonic() - t0, 3)}
        except Exception as e:  # engine decides retry/replan from the message
            return {"status": "error", "action": kind, "error": f"{type(e).__name__}: {str(e)[:200]}",
                    "no_effect": not self._dispatch_started or any(marker in str(e) for marker in _PRE_DELIVERY),
                    "seconds": round(time.monotonic() - t0, 3)}
        finally:
            self._active_action = None
            self._action_receipts.append(receipt)

    def _goto(self, page, url):
        """goto, treating a file download (navigation that never commits a page) as success once the file is stored."""
        seen = len(getattr(self, "downloads", ()))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as error:
            if "Download is starting" not in str(error):
                raise
            for _ in range(50):
                if len(getattr(self, "downloads", ())) > seen:
                    return
                page.wait_for_timeout(100)
            raise RuntimeError("browser_download_not_stored") from None

    def _do(self, kind: str, a: dict):
        p = self.page
        if kind == "navigate":
            # A GET is idempotent: a transport error (blocked/failed navigation) never leaves an uncertain effect.
            self._goto(p, a["url"])
        elif kind in {"click", "double_click", "right_click"}:
            before, url0 = len(self.context.pages), p.url
            target = self._el(a["index"])
            # Check actionability without delivering a click. Covered/hidden
            # elements remain safely retryable; actual input failures do not.
            target.click(trial=True, timeout=4000)
            self._dispatch_started = True
            if kind == "double_click":
                target.dblclick(timeout=4000)
            elif kind == "right_click":
                target.click(button="right", timeout=4000)
            else:
                target.click(timeout=4000)
            if a.get("wait_load"):
                for _ in range(4):  # click() itself waits for an initiated navigation; this only catches a popup/new tab
                    if len(self.context.pages) > before or p.url != url0:
                        break
                    p.wait_for_timeout(50)
                if len(self.context.pages) > before:  # target=_blank opened a background tab: follow it
                    self.page = self.context.pages[-1]
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=4000)
                except Exception:
                    # The click itself returned successfully. A page that is
                    # still loading requires a fresh observation, never replay.
                    pass
        elif kind == "type":
            loc = self._el(a["index"])
            kindness = loc.evaluate(_FILLABLE_JS)   # preflight: fill on a button/checkbox/file input delivers nothing
            if kindness != "ok":
                raise RuntimeError(f"Element is not a fillable text field ({kindness}): click a checkbox or button, use upload for a file input")
            self._dispatch_started = True
            loc.fill(a["text"], timeout=4000)
            if a.get("submit"):
                loc.press("Enter")
        elif kind == "select":
            target = self._el(a["index"])
            if target.evaluate("e => e.tagName") != "SELECT":
                raise RuntimeError("Element is not a <select>: type into text fields, click checkboxes and radios")
            self._dispatch_started = True
            target.select_option(label=a["value"], timeout=4000)
        elif kind == "hover":
            self._el(a["index"]).hover(timeout=4000)   # reveals menus/tooltips; commits nothing, so never effect-uncertain
        elif kind == "scroll":
            sign = -1 if a.get("direction") == "up" else 1
            self._dispatch_started = True
            self._document_handle.evaluate('(d, amount) => d.defaultView.scrollBy(0, amount)', sign * int(a.get('amount', 600)))
        elif kind == "press":
            if self._keyboard_handle is None or self._keyboard_handle.evaluate('e => e.isConnected') is not True:
                raise RuntimeError('fresh_browser_observation_required')
            target = self._keyboard_handle
            target.focus()
            if p.evaluate('(e) => e === document.activeElement', target) is not True:
                raise RuntimeError('fresh_browser_observation_required')
            if target.evaluate("e => (e.type || '').toLowerCase()") == 'password' and a['key'] not in {'Tab', 'Shift+Tab', 'Escape'} and not (
                    self.allow_passwords and a.get('authorized') is True):
                raise SafetyRefusal('password fields need explicit task authorization')
            self._dispatch_started = True
            target.press(a['key'])
        elif kind == "new_tab":
            if len(self.context.pages) >= 32:
                raise SafetyRefusal('owned_browser_tab_limit')
            self.page = self.context.new_page()  # background: no bringToFront
            if a.get("url"):
                self._goto(self.page, a["url"])   # a blank tab is not an input effect; a failed GET is retryable
        elif kind == "switch_tab":
            self.page = self._tab_target(a)
        elif kind == "close_tab":
            tab = self._tab_target(a)
            if len(self.context.pages) <= 1:
                raise SafetyRefusal('owned_browser_last_tab_required')
            self._dispatch_started = True
            tab.close()
            if tab is p:
                self.page = self.context.pages[-1]
        elif kind == "wait":
            if a.get("seconds"):
                time.sleep(min(float(a["seconds"]), 10))
            else:
                p.wait_for_load_state("networkidle", timeout=10000)
        elif kind == "extract":
            if "index" in a:
                return self._el(a["index"]).inner_text()
            if a.get("query"):
                return p.evaluate(FIND_TEXT_JS, [str(a["query"])[:200], 6])
            return p.evaluate("""() => [...document.querySelectorAll('table')].slice(0, 5).map(t =>
                [...t.rows].map(r => [...r.cells].map(c => c.innerText.trim())))""") if a.get("what") == "tables" \
                else p.evaluate(PAGE_TEXT_JS, int(a.get("limit", 8000)))
        elif kind == "upload":
            target = self._el(a["index"])
            if target.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'") is not True:
                raise RuntimeError("Element is not a file input")
            path = Path(str(a.get("path", ""))).expanduser()
            if not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
                raise RuntimeError("Element is not uploadable: the file path does not exist or is over 25 MB")
            self._dispatch_started = True
            target.set_input_files(str(path))
        elif kind == "back":
            self._dispatch_started = True
            p.go_back(wait_until="domcontentloaded", timeout=15000)
        elif kind == "forward":
            self._dispatch_started = True
            p.go_forward(wait_until="domcontentloaded", timeout=15000)
        elif kind == "screenshot":
            return p.screenshot(type="jpeg", quality=60)
        return None
