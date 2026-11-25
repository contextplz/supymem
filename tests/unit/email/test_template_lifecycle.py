"""Readable lifecycle copy and hostile-input safety, independent of delivery state."""
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def templates():
    load_company_os_app()
    from app.email import templates
    return templates


class _Email(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ctas, self.hrefs, self.images, self.paragraphs_before_cta = [], [], [], 0
        self.seen_heading = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            self.seen_heading = True
        if tag == "p" and self.seen_heading and not self.ctas:
            self.paragraphs_before_cta += 1
        if tag == "a" and attrs.get("class") == "primary-cta":
            self.ctas.append(attrs["href"])
        if tag == "a" and attrs.get("href"):
            self.hrefs.append(attrs["href"])
        if tag == "img":
            self.images.append(attrs)


def test_every_template_has_short_subject_one_cta_and_plain_text(templates, monkeypatch):
    from app.chat.navigation_map import hidden_href
    from app.email.fixtures import FIXTURES
    monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", "Supymem, Fixture address")
    assert FIXTURES.keys() == templates.TEMPLATES.keys()
    for name, ctx in FIXTURES.items():
        marketing = templates.category_of(name) != templates.TRANSACTIONAL
        subject, html, plain = templates.render(name, ctx,
            unsubscribe_url="https://gateway.supymem.com/public/email/unsubscribe?t=fixture" if marketing else "",
            prefs_url="https://gateway.supymem.com/public/email/unsubscribe?t=fixture&view=preferences" if marketing else "")
        parsed = _Email(html)
        for url in [*parsed.hrefs, *re.findall(r"https?://[^\s<>]+", plain)]:
            assert not hidden_href(urlsplit(url).path), (name, url)
        assert len(subject.split()) <= 6, name
        assert "\n" not in subject and "\r" not in subject
        assert len(parsed.ctas) == 1 and parsed.ctas[0] in plain, name
        assert parsed.paragraphs_before_cta <= 3, name
        assert "Charlie" in plain and "Supymem, Fixture address" in plain
        assert ("Unsubscribe:" in plain) == marketing
        assert ("Email settings:" in plain) == marketing
        assert parsed.images == [{"src": templates.app_url() + "/email/supymem-logo.png", "width": "64", "height": "64",
                                  "alt": "Supymem", "style": "display:block;border:0;border-radius:14px"}]
        assert f"background:{templates.BG}" in html and f"color:{templates.TEXT}" in html
        assert f"font-family:{templates.FONT}" in html and f"border-radius:{templates.RADIUS}" in html
        assert not any(color in html for color in ("#6f36a5", "#8b52c7", "#f4f1f7", "#2a2530")), name
        assert "<!--[if mso]>" in html and "prefers-color-scheme:dark" in html
        assert len(html.encode()) < 50_000


@pytest.mark.parametrize("path", ["//evil.example", "/\\evil.example", "/%5Cevil.example", "/%2fevil.example", "/%252fevil.example", "/a%0Ab", "javascript:alert(1)"])
def test_context_path_cannot_leave_app_origin(templates, path):
    _, html, plain = templates.render("milestone", {"what": "plan", "path": path})
    assert _Email(html).ctas == [templates.app_url() + "/"]
    assert "evil.example" not in html and "javascript:" not in plain


def test_context_text_is_escaped_and_subject_does_not_use_user_input(templates):
    hostile = '<script>alert("x")</script>\r\nInjected: yes'
    _, html, plain = templates.render("milestone", {"what": "research", "first_name": hostile,
        "venture_name": hostile, "verdict": hostile, "detail": hostile, "items": [hostile], "path": "/v/ven_demo/research"})
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Injected: yes" in plain and "\r" not in plain
    assert _Email(html).ctas == [templates.app_url() + "/v/ven_demo?chat=1"]


@pytest.mark.parametrize("idea", ["Order tracking for wholesalers", "Reading circles for commuters"])
def test_learning_digest_escapes_recorded_text_and_keeps_one_decision_link(templates, idea):
    hostile = idea + ' <script>alert("x")</script>\r\n\u2014 result'
    subject, html, plain = templates.render("weekly_learning", {"path": "/v/ven_learning/market-map",
        "changes": [hostile], "ended_tests": [hostile], "budgets": [idea + ": 70.00 credits remaining"],
        "surprise": hostile, "recorded_spend": "20.00 credits", "next_step": hostile},
        unsubscribe_url="https://example.test/opt-out", prefs_url="https://example.test/preferences")
    assert subject == "Your week of learning" and templates.category_of("weekly_learning") == "digests"
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert idea in plain and "\u2014" not in plain and "\r" not in plain
    assert _Email(html).ctas == [templates.app_url() + "/v/ven_learning/market-map"]
    assert "Unsubscribe:" in plain and "Email settings:" in plain
    assert "credits" not in plain.lower() and "Your budget left" not in plain
    assert "Recorded test cost" not in plain and "70.00" not in html and "20.00" not in html
    _, html, plain = templates.render("weekly_learning", {"path": "/connections", "changes": [idea],
        "next_step": "Open Keys and connections and review your connected Stripe account."})
    assert _Email(html).ctas == [templates.app_url() + "/connections"]
    assert idea in plain


@pytest.mark.parametrize("idea", ["Order tracking for wholesalers", "Reading circles for commuters"])
def test_exhausted_email_buy_link_opens_existing_account_purchase(templates, idea):
    _, html, plain = templates.render("credits_exhausted", {"venture_name": idea,
        "path": "/v/ven_learning/settings", "percent": 100})
    assert "Buy more" in plain and "credits" not in plain.lower() and "100" not in plain
    assert _Email(html).ctas == [templates.app_url() + "/account#account-buy"]


@pytest.mark.parametrize("name", [None, "", "Hello I", "Untitled venture", "New venture", "I want to build a thing", "My idea"])
def test_placeholder_venture_names_fall_back(templates, name):
    _, _, plain = templates.render("milestone", {"what": "research", "venture_name": name})
    assert "research for your venture" in plain


def test_beta_invite_uses_gateway_download_and_encoded_code(templates, monkeypatch):
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")
    _, html, plain = templates.render("beta_invite", {"code": "AB&?/#", "inviter": "Sam"})
    url = "https://gateway.supymem.com/public/beta/download?code=AB%26%3F%2F%23"
    assert _Email(html).ctas == [url] and url in plain


def test_mac_download_uses_admission_code_and_configured_gateway(templates, monkeypatch):
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.example.test")
    url = "https://gateway.example.test/public/beta/download?code=ABCD2345"
    _, html, plain = templates.render("get_mac_app", {"admission_code": "abcd2345", "path": "/download"})
    assert _Email(html).ctas == [url] and url in plain
    assert templates.app_url() + "/download" not in plain
    _, html, plain = templates.render("welcome", {"web_signup": True, "admission_code": "ABCD2345"})
    assert url in html and url in plain
    assert _Email(html).ctas == [templates.app_url() + "/new"]
    _, _, plain = templates.render("welcome", {"web_signup": False, "admission_code": "ABCD2345"})
    assert "Get Supymem for Mac" not in plain


@pytest.mark.parametrize("code", [None, "", "SHORT", "A&CD2345", "ＡBCD2345", "ABCD2345\n", 12345678])
def test_mac_download_never_falls_back_without_valid_admission(templates, code):
    ctx = {"web_signup": True, "admission_code": code, "path": "/download"}
    _, _, welcome = templates.render("welcome", ctx)
    assert "Get Supymem for Mac" not in welcome
    with pytest.raises(ValueError, match="email_admission_code_required"):
        templates.render("get_mac_app", ctx)


def test_honest_waitlist_and_outreach_copy(templates):
    from app.email.fixtures import FIXTURES
    for count in (1, 10, 50, 100):
        subject, _, plain = templates.render("milestone", {"what": "waitlist", "count": count})
        assert len(subject.split()) <= 6 and "unverified interest" in plain
        assert "not proof of demand or revenue" in plain
    _, _, draft = templates.render("outreach_drafts_ready", {"count": 3})
    assert "Nothing is sent until you approve." in draft
    _, _, sent = templates.render("outreach_sent", {"count": 3})
    assert "approved outreach sent 3 messages through Gmail" in sent
    assert "connected Gmail" in sent and "View sent outreach" not in sent
    assert "sent messages in Supymem" not in sent
    for name in ("outreach_drafts_ready", "replies_received"):
        _, html, plain = templates.render(name, FIXTURES[name])
        assert _Email(html).ctas == [templates.app_url() + "/v/ven_demo?chat=1"]
        assert "Open venture chat" in plain and "Shared saved history" in plain
    _, html, _ = templates.render("outreach_sent", FIXTURES["outreach_sent"])
    assert _Email(html).ctas == [templates.app_url() + "/v/ven_demo?chat=1"]
    for percent in (80, 95):
        _, _, credits = templates.render("credits_low", {"percent": percent})
        assert "Buy more" in credits and "%" not in credits and "credits" not in credits


def test_research_findings_follow_primary_cta(templates):
    _, html, plain = templates.render("milestone", {"what": "research", "verdict": "Worth testing",
        "items": ["First finding", "Second finding", "Third finding", "Omitted fourth finding"]})
    assert html.index('class="primary-cta"') < html.index("Research verdict") < html.index("First finding")
    assert "Omitted fourth finding" not in plain


def test_beta_feedback_uses_venture_and_email_reply(templates):
    from app.email.fixtures import FIXTURES
    _, html, plain = templates.render("beta_feedback", FIXTURES["beta_feedback"])
    assert _Email(html).ctas == [templates.app_url() + "/v/ven_demo?chat=1"]
    assert "Open your venture" in plain and "Reply to this email to share your answer." in plain
    assert "feedback in Supymem" not in plain


def test_nudge_uses_actual_next_step_and_exact_link(templates):
    _, html, plain = templates.render("nudge_d5", {"next_step": "Your landing page is ready to publish.",
        "cta_label": "Review your landing page", "path": "/v/ven_demo/site"})
    assert "Your landing page is ready to publish." in plain
    assert _Email(html).ctas == [templates.app_url() + "/v/ven_demo?chat=1"]
    _, _, final = templates.render("nudge_d30", {})
    assert "last check-in" in final


def test_internal_alerts_never_echo_personal_context(templates):
    private = "private report content with user@example.test"
    for template in ("email_health_alert", "error_alert", "god_mode_admin"):
        _, html, plain = templates.render(template, {"detail": private, "source": private, "code": private})
        assert private not in html and private not in plain


def test_health_alert_uses_existing_admin_screen_and_operator_status_route(templates):
    _, html, plain = templates.render("email_health_alert", {"reason": "config_errors"})
    assert _Email(html).ctas == [templates.app_url() + "/admin/settings"]
    assert "Open admin settings" in plain and "Inspect GET /admin/email/status" in plain
    assert "Check email configuration" not in plain


@pytest.mark.parametrize("path", [
    *[f"/v/ven_demo/{screen}" for screen in (
        "research", "gtm", "site", "tasks", "plan", "execution", "builds", "analytics", "settings",
        "connections", "now", "experiments", "suggestions", "context", "guide", "org", "people",
        "charter", "market", "feedback", "evolution", "import", "simulate", "device-work-orders/ord_demo",
    )],
    "/v/ven_demo/site?build=1", "/v/ven_demo/builds?app=app_demo&run=bld_demo#build-review",
    "/v/ven_demo/tasks?task=task_demo#review", "/v/ven_demo/%72esearch", "/v/ven%5Fdemo/research",
    "/gtm?venture=ven_demo", "/tasks?venture=ven_demo", "/execution?venture=ven_demo",
    "/connections?venture=ven_demo", "/ops/outreach?venture=ven_demo", "/v/ven_demo?chat=1",
])
@pytest.mark.parametrize("template", ["milestone", "needs_you", "action_failed", "task_finished", "progress_summary", "nudge_d5"])
def test_venture_ctas_use_chat_or_local_connections_in_both_email_bodies(templates, template, path):
    from app.chat.navigation_map import hidden_href
    from app.core.local_mode import is_local

    ctx = {"path": path, "what": "research"}
    _, html, plain = templates.render(template, ctx)
    destination = path if is_local() and urlsplit(path).path == "/connections" else "/v/ven_demo?chat=1"
    url = templates.app_url() + destination
    assert _Email(html).ctas == [url] and url in plain
    assert ctx["path"] == path
    for url in [*_Email(html).hrefs, *re.findall(r"https?://[^\s<>]+", plain)]:
        assert not hidden_href(urlsplit(url).path), url


@pytest.mark.parametrize("key", ["_venture_id", "venture_id"])
def test_global_cta_uses_context_venture(templates, key):
    _, html, plain = templates.render("action_failed", {key: "ven_demo", "path": "/tasks/task_demo/review"})
    url = templates.app_url() + "/v/ven_demo?chat=1"
    assert _Email(html).ctas == [url] and url in plain


@pytest.mark.parametrize("venture_id", [None, "", "..", "//evil.example", "ven_demo/research", "ven_demo?next=/gtm", "ven_demo\n"])
def test_hidden_cta_without_safe_venture_falls_back_to_app_home(templates, venture_id):
    _, html, plain = templates.render("action_failed", {"_venture_id": venture_id, "path": "/gtm"})
    assert _Email(html).ctas == [templates.app_url() + "/"]
    assert "/gtm" not in plain and "evil.example" not in html


def test_unsubscribe_preferences_and_recovery_links_are_unchanged(templates):
    unsub = "https://gateway.supymem.com/public/email/unsubscribe?t=fixture"
    prefs = templates.app_url() + "/account?email_preferences=1"
    _, html, plain = templates.render("milestone", {"path": "/v/ven_demo/research"},
                                    unsubscribe_url=unsub, prefs_url=prefs)
    assert _Email(html).hrefs == [templates.app_url() + "/v/ven_demo?chat=1", unsub, prefs]
    assert unsub in plain and prefs in plain
    _, html, plain = templates.render("venture_archived", {"_venture_id": "ven_demo", "path": "/"})
    assert _Email(html).ctas == [templates.app_url() + "/"]
    assert "Recently deleted" in plain
