"""Public waitlist URLs and hosted wiring; local tool bytes stay unchanged."""

from __future__ import annotations

import json
import os
import re

_INTEREST_URL_RE = re.compile(
    r"https?://[^/\s\"'<>]+/w/[A-Za-z0-9_-]+/interest"
    r"|/w/[A-Za-z0-9_-]+/interest",
    re.I,
)


def public_gateway_base() -> str:
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import read_settings
        from app.pagebuilder.local_publish import _https_url
        base = read_settings("publishing")["values"].get("site_url")
        return _https_url(base) if base else f"http://127.0.0.1:{os.environ.get('CORE_PORT') or '3300'}"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        core = os.environ.get("CORE_PORT") or "3300"
        return f"http://127.0.0.1:{core}"
    return (
        os.environ.get("PUBLIC_SITE_BASE")
        or os.environ.get("PUBLIC_GATEWAY_URL")
        or "https://gateway.supymem.com"
    ).rstrip("/")


def public_interest_url(workspace_id: str) -> str:
    return f"{public_gateway_base()}/w/{workspace_id}/interest"


def rewrite_waitlist_html(html: str, workspace_id: str, *, native_submit: bool = False, venture_id: str | None = None) -> str:
    """Keep local tool bytes unchanged; wire hosted forms to the public gateway."""
    if not html or not workspace_id:
        return html
    from app.core.local_mode import is_local
    if is_local():
        from app.pagebuilder.local_publish import _file, _validate_files

        _validate_files([_file("index.html", html.encode("utf-8"), "text/html; charset=utf-8")])
        return html
    action = public_interest_url(workspace_id)
    out = _INTEREST_URL_RE.sub(action, html)
    out = re.sub(
        r'<script\s+data-supymem-waitlist="1">[\s\S]*?</script>',
        "",
        out,
        flags=re.I,
    )
    script = _native_script(action) if native_submit else _boot_script(action)
    lowered = out.lower()
    idx = lowered.rfind("</body>")
    if idx >= 0:
        out = out[:idx] + script + out[idx:]
    else:
        out = out + script
    return _inject_product_analytics(out, workspace_id)


def _inject_product_analytics(html: str, workspace_id: str) -> str:
    """Embed an anonymous pageview/CTA beacon when an ingest key exists."""
    try:
        from app.product_analytics.beacon import inject_into_html
        from app.apps.keys import ensure_ingest_key
        from app.db.supymem_models import FounderApp
        from app.state import db
        from sqlalchemy import select

        with db()() as session:
            app = session.scalar(
                select(FounderApp)
                .where(FounderApp.workspace_id == workspace_id, FounderApp.deleted_at.is_(None))
                .order_by(FounderApp.created_at.asc())
            )
            if app is None:
                return html
            app_id = app.id
        key = ensure_ingest_key(app_id)["secret"]
        return inject_into_html(html, key=key)
    except Exception:
        return html


def _native_script(action: str) -> str:
    """Opaque public pages use a real form navigation, never a fake success UI."""
    return """<script data-supymem-waitlist="1">
(function () {
  var ACTION = """ + json.dumps(action) + """;
  function scan() {
    document.querySelectorAll("form").forEach(function (form) {
      var label = (form.getAttribute("action") || "") + " " + form.id + " " + (form.name || "");
      if (!/interest|waitlist/i.test(label) && !form.querySelector('input[name="email"]')) return;
      var clone = form.cloneNode(true);
      clone.removeAttribute("onsubmit");
      clone.action = ACTION;
      clone.method = "post";
      clone.target = "_self";
      clone.enctype = "application/x-www-form-urlencoded";
      clone.querySelectorAll("button,input").forEach(function (control) {
        ["formaction", "formmethod", "formtarget", "formenctype", "onclick"].forEach(function (name) {
          control.removeAttribute(name);
        });
      });
      form.parentNode.replaceChild(clone, form);
      clone.addEventListener("submit", function (event) {
        event.preventDefault();
        event.stopImmediatePropagation();
        HTMLFormElement.prototype.submit.call(clone);
      }, true);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan);
  else scan();
})();
</script>"""


def _boot_script(action: str) -> str:
    action_js = json.dumps(action)
    return f"""
<script data-supymem-waitlist="1">
(function () {{
  var ACTION = {action_js};
  function wire(form) {{
    if (!form || form.getAttribute("data-supymem-wired")) return;
    var clone = form.cloneNode(true);
    if (!form.parentNode) return;
    form.parentNode.replaceChild(clone, form);
    clone.setAttribute("action", ACTION);
    clone.setAttribute("data-supymem-wired", "1");
    clone.addEventListener("submit", function (event) {{
      event.preventDefault();
      event.stopImmediatePropagation();
      var button = clone.querySelector("button, input[type=submit]");
      if (button) button.disabled = true;
      var fd = new FormData(clone);
      var payload = {{
        email: String(fd.get("email") || ""),
        note: String(fd.get("note") || ""),
        source: String(fd.get("source") || "direct"),
        site_version: parseInt(String(fd.get("site_version") || "1"), 10) || 1
      }};
      var xhr = new XMLHttpRequest();
      xhr.open("POST", ACTION);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.onload = function () {{
        if (xhr.status < 200 || xhr.status >= 300) {{
          xhr.onerror();
          return;
        }}
        clone.hidden = true;
        var thanks = document.getElementById("interest-thanks") || document.getElementById("form-thanks");
        var err = document.getElementById("form-error");
        if (thanks) {{
          thanks.hidden = false;
        }} else {{
          var p = document.createElement("p");
          p.id = "interest-thanks";
          p.textContent = "Thanks, that's recorded.";
          clone.parentNode.insertBefore(p, clone.nextSibling);
        }}
        if (err) {{ err.hidden = true; err.textContent = ""; }}
      }};
      xhr.onerror = function () {{
        if (button) button.disabled = false;
        var fail = document.getElementById("form-error");
        if (fail) {{
          fail.hidden = false;
          fail.textContent = "Could not record that. Check the connection and try again.";
        }}
      }};
      xhr.send(JSON.stringify(payload));
    }}, true);
  }}
  function scan() {{
    document.querySelectorAll("form").forEach(function (form) {{
      var blob = (form.getAttribute("action") || "") + " " + (form.id || "") + " " + (form.getAttribute("name") || "");
      if (/interest|waitlist/i.test(blob) || form.querySelector("input[name=email]")) wire(form);
    }});
  }}
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan);
  else scan();
}})();
</script>
"""


__all__ = [
    "public_gateway_base",
    "public_interest_url",
    "rewrite_waitlist_html",
]
