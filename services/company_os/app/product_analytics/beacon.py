"""Anonymous product analytics beacons, first-party only in local mode.

Privacy: events carry only closed names and path/host props. No cookie, no
visitor identifier, no email, form values, chat text, or DOM content. Local
events may carry your referral-link code for attribution. The browser
beacon honors Global Privacy Control, Do Not Track, automation (webdriver),
and the ``supymem_check=1`` marker used by Supymem's own live-site reviews.
Server-side ``signup`` is emitted only after a consented waitlist insert.
"""

from __future__ import annotations

import json
import logging
import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from app.core.local_mode import is_local

log = logging.getLogger("app.product_analytics.beacon")

ALLOWED_BROWSER = frozenset({"landing_view", "page_view", "cta_click", "signup"})
CHECK_PARAM = "supymem_check"
CHECK_VALUE = "1"


def public_events_url() -> str:
    if is_local():
        from app.core.local_secrets import read_settings
        from app.product_analytics.service import enabled
        from app.secrets.detect import looks_like_secret

        if not enabled("page_measurement"):
            return ""
        from app.firstlook.waitlist import public_gateway_base

        base = str(read_settings("publishing")["values"].get("site_url") or public_gateway_base()).strip().rstrip("/")
        try:
            parsed = urlsplit(base)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme not in {"http", "https"} or not host or host == "supymem.com" or host.endswith(".supymem.com")
                    or parsed.username or parsed.password or parsed.query or parsed.fragment or "\\" in base or "%" in parsed.netloc
                    or looks_like_secret(base)
                    or any(ord(character) >= 128 for character in parsed.netloc)
                    or any(character.isspace() or ord(character) < 32 for character in base)
                    or parsed.port is not None and not 1 <= parsed.port <= 65535):
                return ""
        except ValueError:
            return ""
        return f"{base}/e"
    base = (
        os.environ.get("PUBLIC_EVENTS_URL")
        or os.environ.get("PUBLIC_GATEWAY_URL")
        or os.environ.get("PUBLIC_SITE_BASE")
        or "https://gateway.supymem.com"
    ).rstrip("/")
    return f"{base}/e"


def with_check_marker(url: str) -> str:
    """Mark a URL so the browser beacon refuses to count Supymem's own checks."""
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != CHECK_PARAM]
    query.append((CHECK_PARAM, CHECK_VALUE))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment or ""))


def _app_for_venture(venture_id: str) -> tuple[str, str] | None:
    from app.apps.keys import ensure_ingest_key
    from app.product_analytics.target_app import select_venture_app
    from app.state import db

    with db()() as session:
        app = select_venture_app(session, venture_id)
        if app is None:
            return None
        app_id = app.id
    key = ensure_ingest_key(app_id)
    return app_id, key["secret"]


def _app_for_workspace(workspace_id: str) -> tuple[str, str] | None:
    from app.apps.keys import ensure_ingest_key
    from app.db.supymem_models import FounderApp
    from app.state import db

    with db()() as session:
        app = session.scalar(
            select(FounderApp)
            .where(FounderApp.workspace_id == workspace_id, FounderApp.deleted_at.is_(None))
            .order_by(FounderApp.created_at.asc())
        )
        if app is None:
            return None
        app_id = app.id
    key = ensure_ingest_key(app_id)
    return app_id, key["secret"]


def emit_browser_event(
    *,
    venture_id: str | None = None,
    workspace_id: str | None = None,
    event: str,
    props: dict[str, Any] | None = None,
) -> bool:
    """Store one allowlisted app_events row. Never raises into the request path."""
    name = (event or "").strip()
    if name not in ALLOWED_BROWSER:
        return False
    if is_local():
        from app.product_analytics.service import enabled
        if not enabled("page_measurement"):
            return False
    try:
        resolved = None
        if venture_id:
            resolved = _app_for_venture(venture_id)
        elif workspace_id:
            resolved = _app_for_workspace(workspace_id)
        if resolved is None:
            return False
        _app_id, key = resolved
        from app.product_analytics.service import ingest_public

        return bool(ingest_public({"key": key, "event": name, "props": props or {}}))
    except Exception:
        log.exception("hosted product analytics emit failed for %s", name)
        return False


def script_tag(*, key: str, endpoint: str | None = None) -> str:
    """Anonymous measurement reference for a coding-tool brief in local mode."""
    if is_local() and (not isinstance(key, str) or not re.fullmatch(r"pk_app_[A-Za-z0-9_-]{1,80}", key)):
        return ""
    configured = public_events_url() if is_local() else None
    if is_local() and (not configured or endpoint and endpoint != configured):
        return ""
    endpoint = endpoint or configured or public_events_url()
    key_js = json.dumps(key).replace("<", "\\u003c")
    endpoint_js = json.dumps(endpoint).replace("<", "\\u003c")
    hosted_hosts = '' if is_local() else 'if (h === "supymem.com" || h.slice(-12) === ".supymem.com") return true;'
    local_signup = '''document.addEventListener("supymem:signup", function () { send("signup", viewProps); });''' if is_local() else ''
    local_click = r'''var button = node.closest("a[href],button[data-measurement-click]");
    if (button) {
      var point = button.getAttribute("data-price-point") || "";
      try {
        var match = /\/pricing\/[A-Za-z0-9_-]+\/([A-Za-z0-9_-]+)$/.exec(new URL(button.href, location.href).pathname);
        if (match) point = match[1];
      } catch (e) {}
      send("cta_click", Object.assign({}, viewProps, {price_point: point}));
    }''' if is_local() else ''
    return f"""
<script data-supymem-analytics="1">
(function () {{
  /* Anonymous: closed event names and path/host only. No identifiers or cookies.
     Honors Global Privacy Control, Do Not Track, automation, and supymem_check=1.
     A marked entry sets a tab sessionStorage flag and rewrites same-site /
     measurement endpoint links so waitlist hops keep the marker. */
  var KEY = {key_js};
  var ENDPOINT = {endpoint_js};
  var CHECK_FLAG = "supymem_check_tab";
  function hasUrlMarker() {{
    return /(?:^|[?&])supymem_check=1(?:&|$)/.test(location.search || "");
  }}
  function markCheckTab() {{
    try {{
      if (hasUrlMarker()) sessionStorage.setItem(CHECK_FLAG, "1");
    }} catch (e) {{}}
  }}
  function checkTabActive() {{
    try {{
      return sessionStorage.getItem(CHECK_FLAG) === "1";
    }} catch (e) {{ return false; }}
  }}
  function gatewayHost() {{
    try {{ return (new URL(ENDPOINT)).hostname.toLowerCase(); }} catch (e) {{ return ""; }}
  }}
  function isSupymemHost(hostname) {{
    if (!hostname) return false;
    var h = String(hostname).toLowerCase();
    if (h === location.hostname.toLowerCase()) return true;
    {hosted_hosts}
    var gw = gatewayHost();
    return !!gw && h === gw;
  }}
  function withCheckMarker(href) {{
    try {{
      var u = new URL(href, location.href);
      if (!isSupymemHost(u.hostname)) return href;
      u.searchParams.set("supymem_check", "1");
      return u.href;
    }} catch (e) {{ return href; }}
  }}
  function rewriteMarkedLinks() {{
    if (!hasUrlMarker() && !checkTabActive()) return;
    try {{
      var nodes = document.querySelectorAll("a[href]");
      for (var i = 0; i < nodes.length; i++) {{
        var a = nodes[i];
        var href = a.getAttribute("href");
        if (!href || href.charAt(0) === "#" || /^javascript:/i.test(href)) continue;
        var next = withCheckMarker(href);
        if (next && next !== href) a.setAttribute("href", next);
      }}
    }} catch (e) {{}}
  }}
  markCheckTab();
  rewriteMarkedLinks();
  function shouldSkip() {{
    try {{
      if (navigator.webdriver) return true;
      if (navigator.globalPrivacyControl === true) return true;
      if (navigator.doNotTrack === "1" || window.doNotTrack === "1") return true;
      if (hasUrlMarker() || checkTabActive()) return true;
    }} catch (e) {{ return true; }}
    return false;
  }}
  function send(event, props) {{
    if (shouldSkip()) return;
    try {{
      var body = JSON.stringify({{ key: KEY, event: event, props: props || {{}} }});
      // text/plain avoids a CORS preflight; sendBeacon cannot complete one.
      if (navigator.sendBeacon) {{
        navigator.sendBeacon(ENDPOINT, new Blob([body], {{ type: "text/plain;charset=UTF-8" }}));
        return;
      }}
      fetch(ENDPOINT, {{
        method: "POST", mode: "cors", credentials: "omit",
        headers: {{ "Content-Type": "text/plain;charset=UTF-8" }}, body: body
      }}).catch(function () {{}});
    }} catch (e) {{}}
  }}
  var viewProps = {{ path: (location.pathname || "/").slice(0, 200) }};
  var referralCode = "";
  try {{
    if (document.referrer) viewProps.referrer_host = (new URL(document.referrer)).hostname;
  }} catch (e) {{}}
  try {{
    var qs = new URLSearchParams(location.search);
    referralCode = qs.get("ref") || "";
    if (!/^[A-Za-z0-9_-]{{1,80}}$/.test(referralCode)) referralCode = "";
    {'if (referralCode) viewProps.ref = referralCode;' if is_local() else ''}
    ["utm_source", "utm_medium", "utm_campaign", "utm_content", "variant", "experiment_id"].forEach(function (k) {{
      var v = qs.get(k);
      if (v) viewProps[k] = v.slice(0, 80);
    }});
    var variantMatch = /\\/variant_([A-Za-z0-9_-]+)\\.html$/.exec(location.pathname);
    if (variantMatch) viewProps.variant = variantMatch[1];
    var primary = document.querySelector('a[id$="waitlist_cta"]');
    if (primary) {{
      var attributed = new URL(primary.href, location.href);
      ["experiment_id", "variant", "utm_campaign"].forEach(function (k) {{
        if (!viewProps[k] && attributed.searchParams.get(k)) viewProps[k] = attributed.searchParams.get(k).slice(0, 80);
      }});
    }}
  }} catch (e) {{}}
  send("landing_view", viewProps);
  {local_signup}
  document.addEventListener("click", function (ev) {{
    var node = ev.target;
    if (!node || !node.closest) return;
    {local_click}
    var link = node.closest("a[href]");
    if (!link) return;
    try {{
      var destination = new URL(link.href, location.href);
      if (destination.protocol === "https:" &&
          destination.hostname.toLowerCase() === gatewayHost() &&
          /^\\/public\\/waitlists\\/[A-Za-z0-9_-]+(?:\\/pricing\\/[A-Za-z0-9_-]+\\/[A-Za-z0-9_-]+)?\\/?$/.test(destination.pathname)) {{
        ["variant", "utm_source", "utm_medium", "utm_campaign", "experiment_id"].forEach(function (k) {{
          if (viewProps[k]) destination.searchParams.set(k, viewProps[k]);
        }});
        if (referralCode) destination.searchParams.set("ref", referralCode);
        link.href = destination.toString();
        var clickProps = Object.assign({{}}, viewProps);
        var priceMatch = /\\/pricing\\/[A-Za-z0-9_-]+\\/([A-Za-z0-9_-]+)$/.exec(destination.pathname);
        if (priceMatch) {{
          clickProps.price_point = priceMatch[1];
          clickProps.price = link.getAttribute("data-price") || "";
        }}
        {'' if is_local() else 'send("cta_click", clickProps);'}
      }}
    }} catch (e) {{}}
  }}, true);
}})();
</script>
"""


class _MeasurementScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and any(name == "data-supymem-analytics" for name, value in attrs):
            self.current = [self.get_starttag_text()]

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current) + "</script>")
            self.current = None


def inject_into_html(html: str, *, key: str | None, endpoint: str | None = None) -> str:
    if is_local():
        reference = script_tag(key=key, endpoint=endpoint).strip() if key else ""
        checker = _MeasurementScripts()
        checker.feed(html)
        if checker.current is not None or checker.scripts != ([reference] if reference else []):
            raise ValueError(
                "Ask your coding tool to update the page's measurement script to match your saved settings, "
                "then try again; connect a tool in Settings > Keys and connections > Coding tools."
            )
        return html
    if not html or (not key and not is_local()):
        return html
    cleaned = re.sub(
        r'<script\b[^>]*\sdata-supymem-analytics\s*=\s*[\'"]1[\'"][^>]*>[\s\S]*?</script\s*>',
        "",
        html,
        flags=re.I,
    )
    if not key:
        return cleaned
    tag = script_tag(key=key, endpoint=endpoint)
    lowered = cleaned.lower()
    idx = lowered.rfind("</body>")
    if idx >= 0:
        return cleaned[:idx] + tag + cleaned[idx:]
    return cleaned + tag


__all__ = [
    "ALLOWED_BROWSER",
    "CHECK_PARAM",
    "CHECK_VALUE",
    "emit_browser_event",
    "inject_into_html",
    "public_events_url",
    "script_tag",
    "with_check_marker",
]
