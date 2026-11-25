"""Signed public settings: real forms, scanner-safe GETs and irreversible opt-outs at send time."""
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from tests.unit.test_email_engine import _needs_context, env as env  # noqa: F401


def test_signed_preferences_form_saves_all_categories_without_login(env):
    from app.email import service

    main, _ = env
    service.set_prefs("usr_a", {"digests": False})
    client = TestClient(main.create_app())
    link = service.preferences_url("usr_a")
    parsed = urlsplit(link)
    path = parsed.path + "?" + parsed.query
    before = service.get_prefs("usr_a")
    page = client.get(path)
    assert page.status_code == 200 and page.text.count('type="checkbox"') == 4
    assert "Opt in" in page.text and "prefers-color-scheme:dark" in page.text
    assert service.get_prefs("usr_a") == before  # opening a link never changes consent
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["cache-control"] == "no-store"
    saved = client.post(path, data={"digests": "on", "product_updates": "on"})
    assert saved.status_code == 200 and "preferences are saved" in saved.text
    assert service.get_prefs("usr_a") == {"product_updates": True, "digests": True,
                                         "engagement_reminders": False, "suggestions": False}
    assert client.post(path, content="", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 200
    assert not any(service.get_prefs("usr_a").values())
    assert service.get_prefs("usr_b")["product_updates"] is True


def test_category_or_tampered_tokens_cannot_change_other_preferences(env):
    from app.email import service

    main, _ = env
    client = TestClient(main.create_app())
    before = service.get_prefs("usr_a")
    category = service.make_token("usr_a", "suggestions")
    path = "/public/email/unsubscribe?t=" + category + "&view=preferences"
    assert "not valid" in client.get(path).text
    assert "not valid" in client.post(path, data={"digests": "on"}).text
    token = parse_qs(urlsplit(service.preferences_url("usr_a")).query)["t"][0]
    broken = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert "not valid" in client.post("/public/email/unsubscribe?t=" + broken + "&view=preferences", data={"digests": "on"}).text
    assert service.get_prefs("usr_a") == before


def test_one_click_and_queued_email_respect_unsubscribe(env):
    from app.email import service

    main, state = env
    client = TestClient(main.create_app())
    mid = service.send_email("usr_a", "milestone", {"what": "plan"}, ref="queued-before-optout")
    link = urlsplit(service.unsubscribe_url("usr_a", "product_updates"))
    path = link.path + "?" + link.query
    assert client.get(path).status_code == 200
    assert service.get_prefs("usr_a")["product_updates"] is True
    assert client.post(path, content="List-Unsubscribe=One-Click").status_code == 200
    assert not service.get_prefs("usr_a")["product_updates"]
    assert service.deliver(mid) == "suppressed"
    essential = service.send_email("usr_a", "needs_you", _needs_context(state, {"reason": "approval"}), ref="essential-after-optout")
    assert service.deliver(essential) == "sent"


def test_public_preferences_rejects_unknown_duplicate_or_oversized_form(env):
    from app.email import service

    main, _ = env
    client = TestClient(main.create_app())
    link = urlsplit(service.preferences_url("usr_a"))
    path = link.path + "?" + link.query
    before = service.get_prefs("usr_a")
    for raw in ("transactional=on", "digests=on&digests=on", "digests=no", "x=" + "a" * 4096):
        assert client.post(path, content=raw, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert client.post(path, json={"digests": True}).status_code == 400
    assert service.get_prefs("usr_a") == before
