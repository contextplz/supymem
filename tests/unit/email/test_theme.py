"""Desktop tokens, hosted logo and auth delivery stay consistent across services."""
import importlib.util
import re
from html import escape
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from PIL import Image

from core import email_theme as theme

ROOT = Path(__file__).resolve().parents[3]


def test_email_tokens_match_console(monkeypatch):
    monkeypatch.setenv("PUBLIC_PORTAL_URL", "https://app.supymem.com")
    css = (ROOT / "apps/console/src/styles/tokens.css").read_text()
    tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", css.split("@media", 1)[0]))
    for token, value in {
        "--accent": theme.BRAND, "--bg-0": theme.BG, "--bg-card": theme.CARD,
        "--bg-2": theme.SURFACE, "--fg-0": theme.TEXT, "--fg-2": theme.MUTED,
        "--accent-ink": theme.INK, "--warn": theme.WARN, "--ok": theme.OK,
        "--r-3": theme.RADIUS, "--r-2": theme.RADIUS_SM, "--r-control": theme.RADIUS_CONTROL,
    }.items():
        assert tokens[token] == value, token
    assert tokens["--font-sans"].replace(" ", "").replace('"', "'") == theme.FONT.replace(" ", "")
    for token, value in {"--accent-dim": theme.INFO_BG, "--warn-dim": theme.WARN_BG,
                         "--ok-dim": theme.OK_BG, "--line": theme.BORDER}.items():
        red, green, blue, alpha = map(float, re.findall(r"[\d.]+", tokens[token]))
        assert value == "#" + "".join(f"{round(channel * alpha + 255 * (1 - alpha)):02x}" for channel in (red, green, blue))
    if "prefers-color-scheme: dark" not in css:
        assert theme.BRAND_DARK_BG == theme.BRAND
    logo = ROOT / "apps/console/public/email/supymem-logo.png"
    with Image.open(logo) as image:
        assert image.format == "PNG" and image.size == (128, 128)
    assert logo.stat().st_size < 10_000


@pytest.mark.parametrize("purpose", ["email_verify", "magic_link", "password_reset", "workspace_invite"])
def test_gateway_adds_branded_html_without_changing_plain_text(monkeypatch, purpose):
    spec = importlib.util.spec_from_file_location("gateway_email_theme_test", ROOT / "services/gateway/app/mail.py")
    mail = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mail)
    sent = []
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: SimpleNamespace(send_email=lambda **message: sent.append(message)))
    monkeypatch.setenv("SES_FROM_EMAIL", "hello@supymem.com")
    monkeypatch.setenv("PUBLIC_PORTAL_URL", "https://console.example.test")
    hostile = '<script>alert("x")</script>'
    if purpose == "workspace_invite":
        action_url = "https://console.example.test/invite?token=" + hostile
        mail.send_workspace_invite(to="maya@example.test", invite_url=action_url)
        subject = "You have been invited to a Supymem venture"
        plain = ("A Supymem founder invited you to help operate a venture.\n\n"
                 f"Review and accept the invitation:\n{action_url}\n\n"
                 "This one-use invitation expires in seven days. If you were not expecting it, you can ignore this message.\n")
    else:
        action_url = f"https://console.example.test/auth/consume?purpose={purpose}&token={hostile}"
        mail.send_auth_mail(to="maya@example.test", purpose=purpose, token=hostile)
        subject = {"email_verify": "Verify your Supymem email", "magic_link": "Your Supymem sign-in link",
                   "password_reset": "Reset your Supymem password"}[purpose]
        plain = f"{subject}\n\nUse this one-time token in the app, or open:\n{action_url}\n"
    assert len(sent) == 1
    message = sent[0]["Message"]
    assert message["Subject"] == {"Data": subject}
    assert message["Body"]["Text"] == {"Data": plain}
    html = message["Body"]["Html"]["Data"]
    assert message["Body"]["Html"]["Charset"] == "UTF-8"
    assert theme.logo_header() in html
    assert f'href="{escape(action_url)}"' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert theme.CSS in html and f"background:{theme.BRAND}" in html
    assert "<!--[if mso]>" in html


@pytest.mark.parametrize("base", ["javascript:alert(1)", "https://user@example.test", "https://example.test\n", "http://["])
def test_logo_base_url_reuses_validated_portal_origin(monkeypatch, base):
    monkeypatch.setenv("PUBLIC_PORTAL_URL", base)
    assert 'src="https://app.supymem.com/email/supymem-logo.png"' in theme.logo_header()
