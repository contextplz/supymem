"""Real persistence with static negotiated receipts and no reflected personal data."""
import pytest

from tests.security.test_connector_oauth_boundary import client
from tests.security.test_core_signed_boundary import make_workspace


@pytest.mark.parametrize("accept,html", [("text/html", True), ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
    ("application/json", False), ("*/*", False), ("text/html;q=0", False),
    ("application/json;q=1,text/html;q=0.5", False), ("text/html;q=nan", False)])
def test_receipt_representation_and_rate_headers_after_persistence(client, accept, html):
    from app.firstlook.interest import list_leads
    wid = make_workspace(client, "usr_alice")
    response = client.post(
        f"/w/{wid}/interest",
        data={"email": "private@example.test", "note": "<script>private note</script>"},
        headers={"Accept": accept, "Referer": "https://example.test/page"},
    )
    assert response.status_code == 201
    assert response.headers["x-ratelimit-limit"] == "30"
    assert int(response.headers["x-ratelimit-remaining"]) >= 0
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["vary"] == "Accept"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "sandbox; default-src 'none'" in response.headers["content-security-policy"]
    assert [lead["email"] for lead in list_leads(wid)] == ["private@example.test"]
    if html:
        assert response.headers["content-type"].startswith("text/html")
        assert "Your interest has been recorded" in response.text
        assert "private" not in response.text and "<script" not in response.text
    else:
        assert response.json()["received"] is True
        assert response.json()["has_email"] is True
        assert response.json()["note"] == "<script>private note</script>"
        assert response.json()["referrer"] == "https://example.test/page"


@pytest.mark.parametrize("accept", ["text/html", "application/json"])
def test_rate_limited_reply_keeps_retry_headers_and_does_not_persist(client, monkeypatch, accept):
    from app.api import firstlook_api
    from app.firstlook.interest import count_for
    from app.surfaces.ratelimit import Limiter
    wid = make_workspace(client, "usr_alice")
    monkeypatch.setattr(firstlook_api, "_INTEREST", Limiter(capacity=1, per_seconds=100000))
    assert client.post(f"/w/{wid}/interest", data={"email":"first@example.test"}).status_code == 201
    response = client.post(f"/w/{wid}/interest", data={"email":"private@example.test"}, headers={"Accept":accept})
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert count_for(wid) == 1
    assert "private" not in response.text and "recorded" not in response.text
    if accept == "application/json":
        assert response.json()["detail"]["error"]["code"] == "rate_limited"


@pytest.mark.parametrize("accept", ["text/html", "application/json"])
def test_invalid_json_is_not_a_successful_interest_record(client, accept):
    from app.firstlook.interest import count_for
    wid = make_workspace(client, "usr_alice")
    response = client.post(f"/w/{wid}/interest", content='{"private":', headers={"Accept":accept,"Content-Type":"application/json"})
    assert response.status_code == 422 and count_for(wid) == 0
    assert "private" not in response.text and "recorded" not in response.text
    assert "x-ratelimit-limit" in response.headers


@pytest.mark.parametrize("after_write", [False, True])
def test_html_failure_never_claims_success_or_repeats_personal_data(client, monkeypatch, caplog, after_write):
    from app.api import firstlook_api
    from app.firstlook.interest import count_for
    wid = make_workspace(client, "usr_alice")
    record = firstlook_api.fl_service.record_interest
    def fail(*args, **kwargs):
        if after_write:
            record(*args, **kwargs)
        raise RuntimeError("private@example.test private database detail")
    monkeypatch.setattr(firstlook_api.fl_service, "record_interest", fail)
    response = client.post(f"/w/{wid}/interest", data={"email":"private@example.test"}, headers={"Accept":"text/html"})
    assert response.status_code == 500
    assert "Submission not confirmed" in response.text and "may have been saved" in response.text
    assert "private" not in response.text and "recorded" not in response.text
    assert count_for(wid) == int(after_write)
    assert response.headers["cache-control"].startswith("no-store")
    records = [record for record in caplog.records if record.getMessage() == "interest_submission_unconfirmed"]
    assert len(records) == 1
    record = records[0]
    assert record.error_type == "RuntimeError" and record.ws == wid
    assert record.request_id == response.headers["x-request-id"]
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    from core.logging import JsonFormatter
    formatted = JsonFormatter().format(record)
    assert "private@example.test" not in formatted and "private database detail" not in formatted
    assert "private@example.test" not in caplog.text and "private database detail" not in caplog.text
