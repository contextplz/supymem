"""One social toolkit's release pin must not authorize another toolkit."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(autouse=True)
def hosted_toolkit_pins(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


def test_linkedin_pin_cannot_enable_or_version_reddit(monkeypatch):
    load_company_os_app()
    from app.content import composio_social

    monkeypatch.setenv("COMPOSIO_API_KEY", "test-only")
    monkeypatch.delenv("COMPOSIO_TOOLKIT_VERSION", raising=False)
    monkeypatch.setenv("COMPOSIO_LINKEDIN_TOOLKIT_VERSION", "20260922_00")
    monkeypatch.delenv("COMPOSIO_REDDIT_TOOLKIT_VERSION", raising=False)
    assert composio_social.connectable("linkedin")
    assert not composio_social.connectable("reddit")
    monkeypatch.setenv("COMPOSIO_TWITTER_TOOLKIT_VERSION", "20260920_00")
    assert composio_social._toolkit_version("x") == "20260920_00"

    calls = []
    monkeypatch.setattr(composio_social, "_request", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or {"successful": True})
    composio_social.execute_tool("LINKEDIN_GET_MY_INFO", user_id="content:wsp_test",
                                 connected_account_id="ca_test", arguments={})
    assert calls[-1][1]["json_body"]["version"] == "20260922_00"
    assert "dangerously_skip_version_check" not in calls[-1][1]["json_body"]
    with pytest.raises(composio_social.ComposioSocialError, match="No social toolkit"):
        composio_social.execute_tool("GMAIL_SEND_EMAIL", user_id="content:wsp_test",
                                     connected_account_id="ca_test", arguments={})


@pytest.mark.parametrize("domain", ["industrial_suppliers", "home_bakers"])
@pytest.mark.parametrize("platform,slug", [("cal", "CAL_GET_BOOKINGS"), ("calendly", "CALENDLY_LIST_EVENTS"),
    ("google_calendar", "GOOGLECALENDAR_FIND_EVENT")])
def test_booking_reads_use_their_own_toolkit_pin_without_changing_gmail(monkeypatch, domain, platform, slug):
    load_company_os_app()
    from app.connections import bookings
    from app.content import composio_social
    from app.credits import metered
    from app.gtm.supymem import composio_gmail

    monkeypatch.setenv("GMAIL_TOOLKIT_VERSION", "20260921_00")
    monkeypatch.delenv("COMPOSIO_TOOLKIT_VERSION", raising=False)
    toolkit = composio_social.TOOLKIT_SLUG.get(platform, platform).upper()
    monkeypatch.setenv(f"COMPOSIO_{toolkit}_TOOLKIT_VERSION", "20261010_00")
    calls = []
    monkeypatch.setattr(composio_gmail, "_request", lambda *args, **kwargs: calls.append(kwargs["json"]) or {"successful": True, "data": {"items": []}})
    monkeypatch.setattr(metered, "charge_sync", lambda **kwargs: nullcontext(SimpleNamespace(record_micro=lambda cost: None)))
    account = {"workspace_id": "wsp_" + domain, "platform": platform, "provider_user_id": "usr_" + domain,
        "connected_account_id": "owned_" + domain}
    bookings._call(account, slug, {})
    assert calls[-1]["version"] == "20261010_00" and "dangerously_skip_version_check" not in calls[-1]
    assert calls[-1]["user_id"] == account["provider_user_id"] and calls[-1]["connected_account_id"] == account["connected_account_id"]
    monkeypatch.delenv(f"COMPOSIO_{toolkit}_TOOLKIT_VERSION")
    bookings._call(account, slug, {})
    assert calls[-1]["version"] == "latest" and calls[-1]["dangerously_skip_version_check"] is True
    composio_gmail.execute_tool("GMAIL_GET_PROFILE", user_id=account["provider_user_id"], connected_account_id=account["connected_account_id"], arguments={})
    assert calls[-1]["version"] == "20260921_00" and "dangerously_skip_version_check" not in calls[-1]
