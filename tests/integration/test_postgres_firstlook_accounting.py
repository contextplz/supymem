"""Actual migrated ledger and signed confirmation: unknown firstlook stays held."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


@pytest.fixture
def firstlook(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.llm import founder_provider
    from app.firstlook import service, serve
    from llm_gateway import sole
    d = journey_db
    from tests.firstlook_legacy import seed_legacy_page

    seed_legacy_page(d["wid"], d["vid"])
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "live")
    monkeypatch.setenv("FIRSTLOOK_ENGINE", "compose")
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "DeepSeek-V4-Pro")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-firstlook-test-key-not-dialed")
    secret = "firstlook-accounting-synthetic-assertion-only" * 2
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "firstlook-test-vault-at-least-32-bytes")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "firstlook-admin@example.test")
    d = {**d, "assertion_secret": secret}
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(
        source="platform_default", model="DeepSeek-V4-Pro", provider="azure_foundry"))
    monkeypatch.setattr(serve, "_commit_site", lambda *a, **k: None)
    async def no_code(*args, **kwargs):
        raise AssertionError("Unknown/fallback/reuse must not launch a coding agent")
    monkeypatch.setattr(service, "try_code_html", no_code)
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    before = get_account_by_id(account.id)
    calls = []

    def install(*, error=None, usage=None, body="not JSON"):
        async def transport(messages, **kwargs):
            from app.db.models import CreditReservation
            with d["factory"]() as session:
                row = session.scalars(select(CreditReservation).where(
                    CreditReservation.account_id == account.id,
                    CreditReservation.purpose == "firstlook.copy",
                    CreditReservation.status == "reconciling")).one()
                assert row.workspace_id == d["wid"]
            calls.append(kwargs)
            if error is not None:
                raise error
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content=body))])
        monkeypatch.setattr(sole, "transmit", transport)

    def run():
        from app.tenancy import bind
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return asyncio.run(service.generate_first_look(d["wid"], "Bakery board", venture_id=d["vid"]))

    def rows():
        from app.db.models import CreditReservation, CreditLedgerEntry, SiteVersion
        with d["factory"]() as session:
            holds = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == account.id, CreditReservation.purpose == "firstlook.copy")).all()
            spends = session.scalars(select(CreditLedgerEntry).where(
                CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend")).all()
            drafts = session.scalars(select(SiteVersion).where(SiteVersion.workspace_id == d["wid"], SiteVersion.version > 0)).all()
            return [dict(id=r.id, status=r.status, amount=r.amount_micro, settled=r.settled_micro) for r in holds], len(spends), len(drafts)
    return d, install, run, rows, calls, before, lambda: get_account_by_id(account.id)


def assert_held(f):
    holds, spends, drafts = f[3]()
    assert len(holds) == 1 and holds[0]["status"] == "reconciling" and holds[0]["settled"] is None
    assert spends == drafts == 0
    assert f[6]().spent_micro == f[5].spent_micro
    assert f[6]().reserved_micro == f[5].reserved_micro + holds[0]["amount"]
    from app.credits.reserve import sweep_expired
    from core.clock import utcnow
    sweep_expired(utcnow() + timedelta(days=2))
    assert f[3]()[0][0]["status"] == "reconciling"
    assert len(f[4]) == 1 and f[4][0]["single_attempt"] is True


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 5},
    {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 7}])
def test_unknown_usage_preserves_actual_hold_no_site_and_no_next_dial(firstlook, usage):
    from llm_gateway.errors import ProviderCallError
    firstlook[1](usage=usage)
    with pytest.raises(ProviderCallError):
        firstlook[2]()
    assert_held(firstlook)
    with pytest.raises(ProviderCallError):
        firstlook[2]()
    assert len(firstlook[4]) == 1 and len(firstlook[3]()[0]) == 1


@pytest.mark.parametrize("error", [TimeoutError("Synthetic timeout"), ConnectionError("Synthetic connection"), asyncio.CancelledError()])
def test_transport_failure_or_cancel_preserves_actual_hold(firstlook, error):
    from llm_gateway.errors import ProviderCallError
    firstlook[1](error=error)
    with pytest.raises(asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else ProviderCallError):
        firstlook[2]()
    assert_held(firstlook)


@pytest.mark.parametrize("tokens", [0, 12])
def test_known_unusable_output_settles_actual_usage_then_free_plain_draft(firstlook, tokens):
    firstlook[1](usage={"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens})
    view = firstlook[2]()
    holds, spends, drafts = firstlook[3]()
    assert len(holds) == 1 and holds[0]["status"] == "settled" and holds[0]["settled"] is not None
    assert spends == drafts == 1 and view.fallback is True
    assert firstlook[6]().reserved_micro == firstlook[5].reserved_micro
    assert view.cost_usd == holds[0]["settled"] / 1_000_000
    assert len(firstlook[4]) == 1


def test_known_honest_copy_settles_then_saves_a_real_nonfallback_draft(firstlook, monkeypatch):
    from app.firstlook import compose, service
    from tests.integration.test_postgres_firstlook_publication import COPY
    monkeypatch.setattr(service, "try_code_html", compose.try_code_html)
    firstlook[1](usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, body=json.dumps(COPY))
    view = firstlook[2]()
    holds, spends, drafts = firstlook[3]()
    assert view.fallback is False and view.copy["headline"] == COPY["headline"]
    assert holds[0]["status"] == "settled" and spends == drafts == 1
    assert view.cost_usd == holds[0]["settled"] / 1_000_000
    assert len(firstlook[4]) == 1


def test_provider_503_keeps_actual_hold_and_never_saves_a_draft(firstlook):
    from llm_gateway.errors import ProviderCallError
    firstlook[1](error=ProviderCallError("provider_down", "Synthetic failure", status=503, retryable=True))
    with pytest.raises(ProviderCallError):
        firstlook[2]()
    assert_held(firstlook)


def test_exact_rejection_release_is_confirmed_without_draft(firstlook):
    from llm_gateway.errors import ProviderCallError
    firstlook[1](error=ProviderCallError("rate_limited", "Synthetic rejection", status=429, retryable=True))
    with pytest.raises(ProviderCallError) as error:
        firstlook[2]()
    assert error.value.unspent_confirmed is True
    assert firstlook[3]()[0][0]["status"] == "released" and firstlook[3]()[1:] == (0, 0)
    assert firstlook[6]().reserved_micro == firstlook[5].reserved_micro
    assert len(firstlook[4]) == 1


def test_signed_confirm_saves_honest_pause_and_second_confirm_never_dials(firstlook, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.firstlook import service
    from app.tenancy import bind
    from core.assertion import reset_nonce_store, sign_assertion
    d = firstlook[0]
    reset_nonce_store()
    # Legitimate prior free draft, no provider; the actual paid rewrite then fails unknown.
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        asyncio.run(service.generate_first_look(d["wid"], "Bakery board", venture_id=d["vid"], allow_model=False))
    firstlook[1](error=TimeoutError("Synthetic timeout"))
    with TestClient(main.create_app()) as client:
        path = f'/ventures/{d["vid"]}/chat/confirm'
        for _ in range(2):
            token = sign_assertion(d["assertion_secret"], sub=d["actor"], email="", role="user", path=path, method="POST")
            response = client.post(path, headers={"X-COS-Assertion": token, "X-Workspace-ID": d["wid"]},
                json={"action": "site.rewrite", "payload": {"instruction": "Explain the concept clearly"}})
            assert response.status_code == 200, response.text
            saved = response.json()
            assert saved["kind"] == "needs_you" and "outcome is not confirmed" in saved["body_md"]
            from app.chat.navigation_map import _HIDDEN  # chat-first links became read-only in-chat views; none may redial
            readonly = {target["action"] for _, target in _HIDDEN} - {"pagebuilder.build"}
            assert all(action.get("action") in readonly | {None} for action in saved["actions"])
    assert len(firstlook[4]) == 1 and len(firstlook[3]()[0]) == 1
    assert firstlook[3]()[0][0]["status"] == "reconciling" and firstlook[3]()[1:] == (0, 1)


def test_signed_website_unknown_is_closed_pause_not_variant_limit_and_no_fresh_dial(firstlook):
    from fastapi.testclient import TestClient
    from core.assertion import reset_nonce_store, sign_assertion
    from app import main
    from app.firstlook import service
    from app.tenancy import bind
    d = firstlook[0]
    reset_nonce_store()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        asyncio.run(service.generate_first_look(d["wid"], "Bakery board", venture_id=d["vid"], allow_model=False))
    firstlook[1](usage=None)
    path = f'/workspaces/{d["wid"]}/site'
    with TestClient(main.create_app()) as client:
        for _ in range(2):
            token = sign_assertion(d["assertion_secret"], sub=d["actor"], email="", role="user", path=path, method="POST")
            response = client.post(path, headers={"X-COS-Assertion": token, "X-Workspace-ID": d["wid"]},
                json={"venture_id": d["vid"], "idea_text": "A bakery order board"})
            assert response.status_code == 409, response.text
            error = response.json()["detail"]["error"]
            assert error["code"] == "provider_outcome_unknown" and error["retryable"] is False
            assert "outcome is not confirmed" in error["message"] and "variant" not in error["message"].lower()
    assert len(firstlook[4]) == 1 and firstlook[3]()[1:] == (0, 1)
    assert len(firstlook[3]()[0]) == 1 and firstlook[3]()[0][0]["status"] == "reconciling"


def test_signed_website_foreign_actor_cannot_dispatch(firstlook):
    from fastapi.testclient import TestClient
    from core.assertion import reset_nonce_store, sign_assertion
    from app import main
    from app.authz.workspace import ensure_subject_person
    d = firstlook[0]
    reset_nonce_store()
    firstlook[1](usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    path = f'/workspaces/{d["wid"]}/site'
    foreign = "usr_firstlook_foreign"
    ensure_subject_person(foreign)
    token = sign_assertion(d["assertion_secret"], sub=foreign, email="", role="user", path=path, method="POST")
    with TestClient(main.create_app()) as client:
        response = client.post(path, headers={"X-COS-Assertion": token},
            json={"venture_id": d["vid"], "idea_text": "A bakery order board"})
    assert response.status_code in {403, 404}
    assert firstlook[4] == [] and firstlook[3]() == ([], 0, 0)


def test_signed_free_variant_preserves_copy_despite_held_paid_rewrite(firstlook):
    from fastapi.testclient import TestClient
    from core.assertion import reset_nonce_store, sign_assertion
    from app import main
    from app.firstlook import service
    from app.tenancy import bind
    from llm_gateway.errors import ProviderCallError
    d = firstlook[0]
    reset_nonce_store()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        original = asyncio.run(service.generate_first_look(d["wid"], "Bakery board", venture_id=d["vid"], allow_model=False))
    firstlook[1](usage=None)
    with pytest.raises(ProviderCallError):
        firstlook[2]()
    path = f'/workspaces/{d["wid"]}/site/try-another'
    token = sign_assertion(d["assertion_secret"], sub=d["actor"], email="", role="user", path=path, method="POST")
    with TestClient(main.create_app()) as client:
        response = client.post(path, headers={"X-COS-Assertion": token},
            json={"venture_id": d["vid"], "idea_text": "Bakery board"})
    assert response.status_code == 200, response.text
    assert response.json()["copy_hash"] == original.copy_hash
    assert response.json()["variant_index"] == original.variant_index + 1
    assert response.json()["cost_usd"] == 0.0
    assert len(firstlook[4]) == 1 and firstlook[3]()[1:] == (0, 2)
    assert firstlook[3]()[0][0]["status"] == "reconciling"


def test_release_failure_becomes_unknown_and_prevents_a_fresh_paid_rewrite(firstlook, monkeypatch):
    from app.credits import gate
    from llm_gateway.errors import ProviderCallError
    def broken_release(*args, **kwargs):
        raise RuntimeError("Synthetic release failure")
    monkeypatch.setattr(gate, "release", broken_release)
    firstlook[1](error=ProviderCallError("bad_api_key", "Synthetic rejection", status=401))
    with pytest.raises(ProviderCallError) as error:
        firstlook[2]()
    assert error.value.code == "provider_outcome_unknown"
    assert_held(firstlook)
    with pytest.raises(ProviderCallError):
        firstlook[2]()
    assert len(firstlook[4]) == 1


def test_known_usage_settlement_failure_cannot_become_a_ready_draft(firstlook, monkeypatch):
    from app.credits import gate
    from llm_gateway.errors import ProviderCallError
    def broken_settle(*args, **kwargs):
        raise RuntimeError("Synthetic settlement failure")
    monkeypatch.setattr(gate, "settle", broken_settle)
    firstlook[1](usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6})
    with pytest.raises(ProviderCallError) as error:
        firstlook[2]()
    assert error.value.code == "provider_outcome_unknown"
    assert_held(firstlook)
