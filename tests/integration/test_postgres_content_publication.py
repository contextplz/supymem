"""Immutable target preparation on migrated PG; no approval or send transport."""
import json

import pytest
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError, DBAPIError

from tests.integration.test_postgres_auth_security import pg_database as pg_database, concurrent
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_content_generation import content_db as content_db, start, work


@pytest.fixture()
def publication(content_db, monkeypatch):
    from app.content import publication as pub
    from app.content.generation import posts
    from app.db.models import ContentAccount
    from app.credits.policy import set_policy
    from app.tenancy import bind
    from core.ids import new_id
    from core.clock import utcnow
    d = content_db
    topics, _, _ = start(d)
    work(d, topics)
    draft, _, _ = start(d, "draft", key="publication-fixture-draft", platform="x")
    work(d, draft)
    aid = new_id("cac")
    with d["factory"]() as session:
        session.add(ContentAccount(id=aid, workspace_id=d["wid"], platform="x", external_account="provider-fixture-user",
            handle="fixture-author", status="active", access_secret_ref="vault:fixture-access-not-real",
            scopes_json='["tweet.write"]', created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    rates = {"version": "fixture-contract-v1", "rates": {"content_publish:native_x": {
        "action": "content_publish", "provider": "native_x", "source": "fixture-price-contract", "price_usd": "0.003"}}}
    set_policy("outbound_provider_rates", rates, "test")
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        post = posts(d["wid"], d["vid"])[0]
        d["pid"], d["chosen"] = post["id"], {"account_id": aid, "expected_digest": post["edit_digest"]}
        d["quote"] = pub.quote(d["wid"], d["vid"], d["pid"], d["chosen"])
    d["account"] = aid
    d["rates"] = rates
    d["provider_calls_before"] = len(d["calls"])
    # Every later assertion uses only persisted model fixtures, never calls a
    # model, social publisher, OAuth refresher or secret reader.
    def denied(*args, **kwargs):
        raise AssertionError("Publication preparation attempted external I/O")
    from app.content import composio_social
    from app.content.oauth import flow
    from app.approvals import external
    from llm_gateway import sole
    monkeypatch.setattr(sole, "transmit", denied)
    monkeypatch.setattr(composio_social, "publish", denied)
    monkeypatch.setattr(flow, "access_token", denied)
    monkeypatch.setattr(external, "execute_metered_external_action", denied)
    yield d


def prepare(d, **overrides):
    from app.content import publication as pub
    from app.tenancy import bind
    q = d["quote"]
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        return pub.prepare(d["wid"], d["vid"], d["pid"], d["chosen"], **{
            "idempotency_key": "publication-fixture-intent", "quote_digest": q["quote_digest"], "ceiling_micro": q["ceiling_micro"], **overrides})


def test_concurrent_intent_stores_exact_company_target_without_approval_job_or_charge(publication):
    from app.db.models import Approval, Job, CreditAccount, ContentPublicationRun, ContentPost, EventLog
    d = publication
    with d["factory"]() as session:
        before = (session.scalar(select(func.count()).select_from(Approval)), session.scalar(select(func.count()).select_from(Job)),
            session.scalar(select(CreditAccount.spent_micro).where(CreditAccount.venture_id == d["vid"])))
    rows = concurrent(lambda: prepare(d))
    assert len({r["publication_id"] for r in rows}) == 1
    row = rows[0]
    assert row["state"] == "waiting_approval" and row["approval_id"] is None and row["job_id"] is None
    assert row["execution_available"] is False and row["target"] == d["quote"]["target"]
    assert row["target"]["venture_id"] == d["vid"] and row["target"]["account_id"] == d["account"]
    assert "vault:" not in json.dumps(row) and not row["result"]
    with d["factory"]() as session:
        after = (session.scalar(select(func.count()).select_from(Approval)), session.scalar(select(func.count()).select_from(Job)),
            session.scalar(select(CreditAccount.spent_micro).where(CreditAccount.venture_id == d["vid"])))
        assert before == after
        assert session.get(ContentPost, d["pid"]).state == "draft"
        assert session.scalar(select(func.count()).select_from(ContentPublicationRun).where(ContentPublicationRun.post_id == d["pid"])) == 1
        audits = list(session.scalars(select(EventLog).where(EventLog.topic == "content.publication_prepared", EventLog.target_ref == row["publication_id"])))
        assert len(audits) == 1 and audits[0].actor_id == d["actor"] and audits[0].actor_kind == "person"
    assert len(d["calls"]) == d["provider_calls_before"]


@pytest.mark.parametrize("change", ["account", "rate", "price", "route", "generation", "scope", "edit"])
def test_changed_account_price_scope_or_post_cannot_reuse_quote(publication, monkeypatch, change):
    from app.content import publication as pub
    from app.content.generation import edit_post, posts
    from app.db.models import ContentAccount, Venture
    from app.credits.policy import set_policy
    from app.tenancy import bind
    from core.ids import new_id
    from core.clock import utcnow
    d = publication
    expected = "content_publication_quote_changed"
    if change in {"rate", "price"}:
        if change == "rate":
            d["rates"]["version"] = "fixture-contract-v2"
        else:
            d["rates"]["rates"]["content_publish:native_x"]["price_usd"] = "0.004"
        set_policy("outbound_provider_rates", d["rates"], "test")
    elif change == "route":
        from app.content.platforms import x
        monkeypatch.setattr(x, "TWEETS_URL", "https://different-provider.example.test/2/tweets")
    elif change in {"account", "generation"}:
        with d["factory"]() as session:
            row = session.get(ContentAccount, d["account"])
            if change == "account":
                row.external_account = "different-provider-identity"
            else:
                row.auth_generation += 1
            session.commit()
    elif change == "scope":
        sibling = new_id("ven")
        with d["factory"]() as session:
            session.add(Venture(id=sibling, workspace_id=d["wid"], name="Sibling", raw_idea="Other", stage="draft", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        d["vid"] = sibling
        expected = "content_publication_not_found"
    else:
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            edit_post(d["wid"], d["vid"], d["pid"], body="Keep bakery orders and notes together so everyone can read the latest change before preparing the next order.",
                title="", expected_digest=d["chosen"]["expected_digest"])
            # Even with the latest edit digest, edited text lacks a current
            # real model review and cannot inherit a previous critic pass.
            d["chosen"]["expected_digest"] = posts(d["wid"], d["vid"])[0]["edit_digest"]
        expected = "content_publication_fresh_review_required"
    with pytest.raises(pub.PublicationError, match=expected):
        prepare(d)


def test_foreign_account_and_missing_account_are_uniform_and_earlier_venture_never_pays(publication):
    from app.content import publication as pub
    from app.db.models import ContentAccount, Venture, CreditAccount
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from app.credits.accounts import ensure_account
    from core.ids import new_id
    from core.clock import utcnow
    d = publication
    wid = create_workspace(kind="company", name="Other owned workspace", owner_id=d["actor"]).id
    aid, sibling = new_id("cac"), new_id("ven")
    with d["factory"]() as session:
        session.add(ContentAccount(id=aid, workspace_id=wid, platform="x", status="active", external_account="foreign", created_at=utcnow(), updated_at=utcnow()))
        session.add(Venture(id=sibling, workspace_id=d["wid"], name="Sibling credit account", raw_idea="Other", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    sibling_account = ensure_account(sibling, workspace_id=d["wid"], owner_person_id=d["actor"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        for account_id in [aid, "cac_missing"]:
            with pytest.raises(pub.PublicationError, match="^content_publication_not_found$"):
                pub.quote(d["wid"], d["vid"], d["pid"], {**d["chosen"], "account_id": account_id})
    row = prepare(d)
    assert row["target"]["credit_account_id"] != sibling_account.id
    with d["factory"]() as session:
        account = session.get(CreditAccount, sibling_account.id)
        assert account.spent_micro == account.reserved_micro == 0


def test_db_spec_is_immutable_and_scope_fks_and_unresolved_claim_hold(publication):
    from app.content import publication as pub
    from app.db.models import ContentPublicationRun, ContentAccount
    from core.ids import new_id
    from core.clock import utcnow
    from app.workspaces.service import create_workspace
    d = publication
    other_workspace = create_workspace(kind="company", name="Foreign account scope", owner_id=d["actor"]).id
    foreign_account = new_id("cac")
    with d["factory"]() as session:
        session.add(ContentAccount(id=foreign_account, workspace_id=other_workspace, platform="x", external_account="foreign-real-row",
            status="active", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    result = prepare(d)
    with pytest.raises(pub.PublicationError, match="content_publication_unresolved_intent"):
        prepare(d, idempotency_key="a-second-reviewed-intent")
    with d["factory"]() as session:
        row = session.get(ContentPublicationRun, result["publication_id"])
        baseline = {c.name: getattr(row, c.name) for c in ContentPublicationRun.__table__.columns}
        row.spec_json = "{}"
        with pytest.raises(DBAPIError, match="immutable"):
            session.commit()
        session.rollback()
    for changes in [{"venture_id": "missing-venture"}, {"source_run_id": "missing-source"},
            {"account_id": foreign_account}, {"credit_account_id": "missing-credit"}, {"review_run_id": "missing-review"}]:
        with d["factory"]() as session:
            session.add(ContentPublicationRun(**{**baseline, "id": new_id("cpr"), "idempotency_hash": new_id("fixture"), "state": "cancelled", **changes}))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
    with d["factory"]() as session:
        # Mutable operational state can advance without changing the approved
        # target; an uncertain operation continues owning its post.
        session.execute(update(ContentPublicationRun).where(ContentPublicationRun.id == result["publication_id"]).values(state="needs_reconciliation"))
        session.get(ContentAccount, d["account"]).status = "revoked"
        session.commit()
    replay = prepare(d)
    assert replay["publication_id"] == result["publication_id"] and replay["state"] == "needs_reconciliation"
    assert replay["target"] == result["target"]


def test_missing_rates_unsupported_platform_and_bare_critic_never_fake_readiness(publication):
    from app.content import publication as pub
    from app.db.models import ContentPost, ContentAccount
    from app.credits.policy import set_policy
    from app.tenancy import bind
    d = publication
    set_policy("outbound_provider_rates", {}, "test")
    with pytest.raises(pub.PublicationError, match="outbound_rate_configuration_missing"):
        prepare(d)
    set_policy("outbound_provider_rates", d["rates"], "test")
    with d["factory"]() as session:
        session.get(ContentPost, d["pid"]).platform = "bluesky"
        session.get(ContentAccount, d["account"]).platform = "bluesky"
        session.commit()
    with pytest.raises(pub.PublicationError, match="content_publication_platform_unsupported"):
        prepare(d)
    with d["factory"]() as session:
        session.get(ContentPost, d["pid"]).platform = "x"
        session.get(ContentAccount, d["account"]).platform = "x"
        session.get(ContentPost, d["pid"]).critic_json = '{"verdict":"publish","score":1}'
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(pub.PublicationError, match="content_publication_fresh_review_required"):
            pub.quote(d["wid"], d["vid"], d["pid"], d["chosen"])


def test_another_member_cannot_recover_actor_bound_publication_approval_intent(publication):
    from app.authz.workspace import ensure_subject_person
    from app.content import publication as pub
    from app.db.models import WorkspaceMember
    from core.clock import utcnow
    from core.ids import new_id
    d = publication
    other = new_id("usr")
    ensure_subject_person(other)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id=new_id("wm"), workspace_id=d["wid"], person_id=other,
            role="contributor", can_approve=0, created_at=utcnow()))
        session.commit()
    d["actor"] = other
    with pytest.raises(pub.PublicationError, match="content_publication_quote_changed"):
        prepare(d)


def test_publication_migration_roundtrip_preserves_authoring_schema(pg_database, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    from tests.integration.test_postgres_auth_security import ROOT, service_modules
    url, engine = pg_database
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    with service_modules("company_os"):
        cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
        command.downgrade(cfg, "s008staticqa")
        assert "content_publication_runs" not in inspect(engine).get_table_names()
        assert "generation_run_id" in {c["name"] for c in inspect(engine).get_columns("content_posts")}
        command.upgrade(cfg, "s009contentpub")
        assert "content_publication_runs" in inspect(engine).get_table_names()
        assert "auth_generation" in {c["name"] for c in inspect(engine).get_columns("content_accounts")}
