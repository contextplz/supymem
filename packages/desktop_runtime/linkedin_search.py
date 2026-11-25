"""Background LinkedIn people search on the founder's Mac.

Every minute, while the founder is signed in, ask Core for a search order. An order is a list of
exact Google search URLs (https://www.google.com/search?q=site:linkedin.com/in ...). They load headless (no window) in the
founder's Supymem Chrome profile, one after another, and the results pages go back to Core, which
keeps the linkedin.com/in links. Nothing is clicked; every request to linkedin.com is blocked and every
navigation away from www.google.com is aborted. A Google CAPTCHA stops the order (needs_human); it is
never solved here.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("desktop_runtime.linkedin_search")

POLL_SECONDS = 60  # a queued "Find more" starts within a minute; Core marks the Mac seen on every check
SIGNED_OUT_RETRY_SECONDS = 5
MAX_PAGE_CHARS = 3_000_000
_STRIP_JS = ("() => { for (const e of document.querySelectorAll('script,style,noscript,svg,link')) e.remove();"
             " return document.documentElement.outerHTML; }")


def google_search_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme == "https" and parts.hostname == "www.google.com" and parts.path == "/search" and "q=" in parts.query


def blocked(url: str, *, navigation: bool) -> bool:
    """LinkedIn is never loaded; the tab never leaves Google."""
    host = (urlsplit(url or "").hostname or "").lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com") or host.endswith(".licdn.com"):
        return True
    return navigation and host != "www.google.com"


def captcha(final_url: str, html: str) -> bool:
    return urlsplit(final_url or "").path.startswith("/sorry") or "g-recaptcha" in (html or "") or 'id="captcha-form"' in (html or "")


def _guard(route):
    request = route.request
    if blocked(request.url, navigation=request.is_navigation_request() and request.frame.parent_frame is None):
        return route.abort("blockedbyclient")
    return route.continue_()


def run_order(order: dict, account_id: str, *, open_browser=None) -> dict:
    """Read each Google results page of one order. Returns the body for Core's results endpoint."""
    urls = [item["url"] for item in order.get("queries") or [] if google_search_url(item.get("url"))][:5]
    pages, hit_captcha = [], False
    with (open_browser or _browser)(account_id) as context:
        context.route("**/*", _guard)
        page = context.new_page()
        for index, url in enumerate(urls):
            if index:
                time.sleep(random.uniform(6, 14))  # a person's pace, not a crawler's
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            final_url = page.url
            html = page.evaluate(_STRIP_JS)[:MAX_PAGE_CHARS]
            pages.append({"url": url, "final_url": final_url, "html": html})
            if captcha(final_url, html):
                hit_captcha = True  # stop; the founder clears it in their own Chrome
                break
        page.close()
    return {"pages": pages, "captcha": hit_captcha}


class _browser:
    """The founder's persistent Supymem Chrome profile (same one Mac tasks use), HEADLESS, connected over CDP.

    Research is background-only: no window, no Dock icon, no focus change. Cookies persist in the profile.
    A profile held by a running Mac task raises browser_profile_in_use; the search waits for a later poll."""

    def __init__(self, account_id):
        from desktop_runtime.cu_ai_browser import ChromeFactory
        data_dir = Path(os.environ.get("SUPYMEM_DATA_DIR") or Path.home() / "Library" / "Application Support" / "Supymem")
        self.factory = ChromeFactory(data_dir / "browser", account_id=account_id, profile_email="", headless=True)

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(self.factory.start_endpoint(), timeout=8000)
            return self.browser.contexts[0]
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        for close in (lambda: self.browser.close(), self.factory.stop, self.playwright.stop):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort cleanup of our own Chrome
                pass


def poll_once(request, account_id: str) -> str:
    """request(method, path, body) -> httpx.Response, already signed as this device."""
    response = request("GET", "/api/desktop/linkedin-searches/next?headless=1", None)  # this build never shows a window
    order = response.json().get("order") if response.is_success else None
    if not order:
        return "idle"
    try:
        body = run_order(order, account_id)
    except Exception as error:  # noqa: BLE001 - Core re-offers the order after its timeout
        if "browser_profile_in_use" in str(error):
            log.info("linkedin_search_profile_busy")  # a Mac task owns the profile: never show anything, retry later
            return "busy"
        log.warning("linkedin_search_failed", extra={"error_class": type(error).__name__})
        return "failed"
    request("POST", f"/api/ventures/{order['venture_id']}/linkedin/searches/{order['id']}/results", body)
    return "needs_human" if body["captcha"] else "done"


def start(request, account_id, *, interval: float = POLL_SECONDS) -> threading.Thread:
    """Daemon loop; account_id() returns the signed-in account or None (then it waits)."""
    def loop():
        while True:
            account = None
            try:
                account = account_id()
                if account:
                    poll_once(request, account)  # the first check-in right after launch marks the Mac seen
            except Exception as error:  # noqa: BLE001 - never take the bridge down
                log.info("linkedin_search_poll_skipped", extra={"error_class": type(error).__name__})
            time.sleep(interval if account else min(interval, SIGNED_OUT_RETRY_SECONDS))
    thread = threading.Thread(target=loop, name="linkedin-search", daemon=True)
    thread.start()
    return thread
