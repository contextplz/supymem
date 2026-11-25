"""Migrated Postgres: the founder's post, its read-back and connected reads, with only the provider seam faked."""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db

DOMAINS = [("hospital compliance officers", "We audit vendor contracts before every review."),
           ("fraud leads at regional lenders", "Fraud rings reuse devices across lenders.")]


@pytest.fixture()
def posting(journey_db, monkeypatch):
    from app.connections import crm
    from app.content import composio_social, engine
    from app.content.oauth import callbacks
    from app.content.platforms import PublishResult
    from app.credits.accounts import ensure_account
    from app.credits.policy import get_policy, set_policy
    from app.db.models import ContentAccount, ContentOAuthState, CreditAccount
    from app.experiments import tools
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    from core.ids import new_id

    d = journey_db
    for key, value in {"SUPYMEM_SOCIAL_POST_ENABLED": "1", "SUPYMEM_OUTBOUND_EXECUTION_ENABLED": "true", "CONTENT_DRY_RUN": "false",
                       "COMPOSIO_API_KEY": "fixture"}.items():
        monkeypatch.setenv(key, value)
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with d["factory"]() as session:
        session.get(CreditAccount, account.id).granted_micro = 100_000_000
        session.add(ContentAccount(id=new_id("cac"), workspace_id=d["wid"], platform="linkedin", external_account="member-1", handle="founder",
            status="active", author_urn="urn:li:person:1", scopes_json="[]", created_at=utcnow(), updated_at=utcnow(),
            extra_json=json.dumps({"auth_via": "composio", "connected_account_id": "ca_li", "composio_user_id": "content:" + d["wid"]})))
        session.commit()
    set_policy("outbound_provider_rates", {"version": "fixture-social-v1", "rates": {"content_publish:composio_linkedin": {
        "action": "content_publish", "provider": "composio_linkedin", "source": "fixture", "price_usd": "0.01"}}}, "test")
    d.update(sent=[], reads=[], flags={"experiments_tools_v2": True})
    monkeypatch.setattr(tools, "get_policy", lambda key, *a: d["flags"] if key == "feature_flags" else get_policy(key, *a))

    async def allow(_workspace, _platform):
        return True, ""

    def publish(**kwargs):
        d["sent"].append(kwargs)
        return PublishResult(ok=True, mode="live", external_id="urn:li:share:9", external_url="https://www.linkedin.com/feed/update/urn:li:share:9")

    monkeypatch.setattr(engine, "_warden_allows", allow)
    monkeypatch.setattr(composio_social, "publish", publish)
    monkeypatch.setattr(callbacks, "_verified_connection", lambda _account: None)
    monkeypatch.setattr(composio_gmail, "_request", lambda *_a, **_k: {"items": [{"slug": slug, "input_parameters": {"type": "object", "properties": {
        "post_id": {"type": "string"}, "form_id": {"type": "string"}, "limit": {"type": "integer"}}}}
        for slug in ("LINKEDIN_GET_POST_CONTENT", "TYPEFORM_GET_FORM_RESPONSES", "HUBSPOT_LIST_CONTACTS")]})

    def execute_tool(slug, **kwargs):
        d["reads"].append((slug, kwargs))
        return {"successful": True, "data": {"id": "urn:li:share:9", "results": [], "items": []}}

    monkeypatch.setattr(composio_gmail, "execute_tool", execute_tool)

    def connection(_workspace, platform):
        return {"provider_user_id": d["actor"], "connected_account_id": "ca_" + platform, "outcome": "connected", "actor_subject": d["actor"]}

    monkeypatch.setattr(crm, "_connection", connection)
    from app.tasks import usage
    monkeypatch.setattr(usage, "before_tool", lambda: None)
    yield d


@pytest.mark.parametrize("audience,words", DOMAINS)
def test_post_is_metered_published_once_and_read_back(posting, audience, words):
    from app.chat import posts
    from app.db.models import Approval, ContentPost, CreditAccount
    from app.tenancy import bind

    d = posting
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        reply = asyncio.run(posts.publish(d["vid"], words, ["linkedin"], d["actor"]))
        again = asyncio.run(posts.publish(d["vid"], words, ["linkedin"], d["actor"]))
    assert "Posted to LinkedIn: https://www.linkedin.com" in reply["body_md"], reply
    assert "I checked and it is live." in reply["body_md"] and "already posted" in again["body_md"]
    assert [call["body"] for call in d["sent"]] == [words]
    assert [slug for slug, _ in d["reads"]] == ["LINKEDIN_GET_POST_CONTENT"]
    with d["factory"]() as session:
        post = session.scalar(select(ContentPost).where(ContentPost.workspace_id == d["wid"]))
        assert post.state == "published" and post.external_id == "urn:li:share:9" and post.dry_run is False
        credit = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert credit.spent_micro > 0 and credit.reserved_micro == 0
        approval = session.scalar(select(Approval).where(Approval.subject_ref == post.id))
        assert approval.status == "approved" and approval.decided_by == d["actor"] and approval.decision_note


def test_another_workspace_member_cannot_decide_and_nothing_is_sent(posting):
    from app.chat import posts
    from app.tenancy import bind

    d = posting
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        reply = asyncio.run(posts.publish(d["vid"], DOMAINS[0][1], ["linkedin"], "usr_not_a_member"))
    assert "Nothing went public" in reply["body_md"] and not d["sent"]


@pytest.mark.parametrize("platform,operation,slug", [("hubspot", "contacts", "HUBSPOT_LIST_CONTACTS"), ("typeform", "responses", "TYPEFORM_GET_FORM_RESPONSES")])
def test_connected_reads_are_scoped_and_read_only(posting, platform, operation, slug):
    from app.connections import crm
    from app.tenancy import bind

    d = posting
    values = {"form_id": "form_1"} if platform == "typeform" else {}
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        result = crm._reader(d["vid"], platform, lambda read, _wid: {"data": read(operation, values)})
        refused = crm._reader(d["vid"], platform, lambda read, _wid: read("deals" if platform == "typeform" else "responses", {}))
    assert result["status"] == "connected" and d["reads"][0][0] == slug
    assert d["reads"][0][1]["connected_account_id"] == "ca_" + platform and refused["status"] == "unavailable" and len(d["reads"]) == 1
