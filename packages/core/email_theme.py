"""Email-safe equivalents of apps/console/src/styles/tokens.css."""
from __future__ import annotations

import os
from html import escape
from urllib.parse import urlsplit

BRAND = "#2563eb"
BRAND_DARK_BG = BRAND
BG, CARD, SURFACE, BORDER = "#f6f8fc", "#ffffff", "#eef2f8", "#e9e8e7"
TEXT, MUTED, INK = "#111827", "#5b6475", "#ffffff"
FONT = "-apple-system,BlinkMacSystemFont,system-ui,'Segoe UI',sans-serif"
RADIUS, RADIUS_SM, RADIUS_CONTROL = "14px", "10px", "8px"
INFO_BG, WARN_BG, OK_BG = "#e9effd", "#f8ede1", "#e4f1ea"
WARN, OK = "#b45309", "#15803d"


def _base_url(key: str, default: str) -> str:
    value = os.environ.get(key, default).rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return default
    return value if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username and not any(ord(c) < 32 for c in value) else default


def app_url() -> str:
    return _base_url("PUBLIC_PORTAL_URL", "https://app.supymem.com")


def logo_header() -> str:
    return (f'<img src="{escape(app_url() + "/email/supymem-logo.png")}" width="64" height="64" '
            'alt="Supymem" style="display:block;border:0;border-radius:14px">')


# ponytail: console has no dark tokens, update these overrides when the app adds a dark palette.
CSS = f"""
body,table,td,a{{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}}
table{{border-collapse:separate}}td{{word-break:break-word}}a{{overflow-wrap:anywhere}}
@media (max-width:620px){{.outer{{padding:20px 12px !important}}.pad{{padding-left:22px !important;padding-right:22px !important}}h1.txt{{font-size:26px !important}}}}
@media (prefers-color-scheme:dark){{
 .bg{{background:{BG} !important}} .card{{background:{CARD} !important;border-color:{BORDER} !important}}
 .txt,h1.txt{{color:{TEXT} !important}} .muted,.muted a{{color:{MUTED} !important}}
 .btn,.dot-done{{background:{BRAND} !important;color:{INK} !important}} .primary-cta{{color:{INK} !important}}
 .foot{{border-top-color:{BORDER} !important}}
 .c-info{{background:{INFO_BG} !important;border-left-color:{BRAND} !important}}
 .c-warn{{background:{WARN_BG} !important;border-left-color:{WARN} !important}}
 .c-ok{{background:{OK_BG} !important;border-left-color:{OK} !important}}
 .stat,.dot-todo{{background:{SURFACE} !important}} .brand,.accent{{color:{BRAND} !important}}
 .dot-current{{background:{CARD} !important;color:{BRAND} !important;border-color:{BRAND} !important}}
}}"""


def auth_html(subject: str, body: str, action_url: str, action_label: str) -> str:
    paragraphs = "".join(f'<p class="txt" style="margin:0 0 16px;line-height:1.6;color:{TEXT}">'
                         f'{escape(part).replace(chr(10), "<br>")}</p>' for part in body.strip().split("\n\n"))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{escape(subject)}</title><style>{CSS}</style></head>
<body class="bg" style="margin:0;padding:0;background:{BG};font-family:{FONT}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="bg" style="background:{BG}"><tr><td class="outer" align="center" style="padding:36px 12px">
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
<tr><td class="pad" style="padding:0 4px 18px">{logo_header()}</td></tr>
<tr><td class="card pad" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:{RADIUS};padding:36px">
<h1 class="txt" style="margin:0 0 20px;font-size:30px;line-height:1.2;color:{TEXT}">{escape(subject)}</h1>
{paragraphs}
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px"><tr><td class="btn" bgcolor="{BRAND}" style="background:{BRAND};border-radius:{RADIUS_CONTROL};mso-padding-alt:14px 24px">
<a class="primary-cta" href="{escape(action_url)}" style="display:inline-block;padding:14px 24px;font-size:16px;font-weight:700;color:{INK};text-decoration:none;border-radius:{RADIUS_CONTROL};font-family:{FONT};line-height:1.4">{escape(action_label)}</a>
</td></tr></table></td></tr></table><!--[if mso]></td></tr></table><![endif]-->
</td></tr></table></body></html>"""

