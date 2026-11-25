"""API tokens, idempotency and rate limits (41 §4, §8; 47 I-tokens)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/tok.db")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    main.create_app()
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    ensure_desk(LOCAL_PERSON_ID)
    return LOCAL_PERSON_ID


# ---------------------------------------------------------------------------
# minting and verification
# ---------------------------------------------------------------------------


def test_minted_token_is_returned_once_and_never_stored_raw(app_env) -> None:
    from app.db.models import ApiToken
    from app.state import db
    from app.surfaces import tokens

    minted = tokens.mint_token(
        person_id=app_env, label="ci", scopes=["read", "work:create"]
    )
    assert minted.token.startswith("cos_")

    with db()() as session:
        rows = session.query(ApiToken).all()
    assert len(rows) == 1
    # The plaintext must not appear anywhere in the stored row.
    assert minted.token not in rows[0].hash
    assert "argon2" in rows[0].hash or "$argon2" in rows[0].hash

    verified = tokens.verify_token(minted.token)
    assert verified.person_id == app_env
    assert verified.scopes == frozenset({"read", "work:create"})


def test_unknown_scope_is_refused(app_env) -> None:
    from app.surfaces import tokens

    with pytest.raises(tokens.TokenError) as exc:
        tokens.mint_token(person_id=app_env, label="bad", scopes=["autonomy:write"])
    assert exc.value.code == "invalid_scope"


def test_revoked_token_fails_immediately_and_is_audited(app_env) -> None:
    from app.db.models import EventLog
    from app.state import db
    from app.surfaces import tokens

    minted = tokens.mint_token(person_id=app_env, label="temp", scopes=["read"])
    tokens.revoke_token(minted.id, actor_id=app_env)

    with pytest.raises(tokens.TokenError) as exc:
        tokens.verify_token(minted.token)
    assert exc.value.code == "token_revoked"

    with db()() as session:
        topics = [r.topic for r in session.query(EventLog).all()]
    assert "token.revoked" in topics
    assert "token.rejected" in topics


def test_expired_token_fails(app_env) -> None:
    from app.surfaces import tokens

    minted = tokens.mint_token(
        person_id=app_env, label="short", scopes=["read"], ttl_days=-1
    )
    with pytest.raises(tokens.TokenError) as exc:
        tokens.verify_token(minted.token)
    assert exc.value.code == "token_expired"


def test_garbage_token_is_invalid(app_env) -> None:
    from app.surfaces import tokens

    for bad in ("", "nope", "cos_notarealtoken"):
        with pytest.raises(tokens.TokenError):
            tokens.verify_token(bad)


# ---------------------------------------------------------------------------
# the four-way minimum a token is subject to
# ---------------------------------------------------------------------------


def test_token_cannot_hold_a_capability_with_no_scope(app_env) -> None:
    from app.authz.effective import effective

    for capability in ("change_autonomy", "connect_systems", "offboard", "invite"):
        verdict = effective(
            capability,
            role="owner",
            autonomy_level="L4",
            token_scopes=frozenset({"read", "work:create", "work:approve"}),
        )
        assert not verdict.allowed, capability
        assert verdict.limited_by == "token"
        assert "human" in verdict.reason


def test_browser_session_may_do_what_a_token_may_not(app_env) -> None:
    from app.authz.effective import effective

    verdict = effective(
        "change_autonomy", role="owner", autonomy_level="L1", token_scopes=None
    )
    assert verdict.allowed


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_same_key_replays_the_first_response(app_env) -> None:
    from app.surfaces import idempotency

    first = idempotency.replay_or_reserve("v1.engagements:tok_a", "key-1")
    assert not first.found and not first.in_flight

    # A concurrent retry while the first is still running.
    during = idempotency.replay_or_reserve("v1.engagements:tok_a", "key-1")
    assert during.in_flight

    idempotency.store_response(
        "v1.engagements:tok_a", "key-1", {"id": "eng_1"}, status_code=201
    )
    replay = idempotency.replay_or_reserve("v1.engagements:tok_a", "key-1")
    assert replay.found
    assert replay.status_code == 201
    assert replay.body == {"id": "eng_1"}


def test_keys_are_scoped_per_token(app_env) -> None:
    from app.surfaces import idempotency

    idempotency.replay_or_reserve("v1.engagements:tok_a", "shared")
    idempotency.store_response("v1.engagements:tok_a", "shared", {"id": "a"})
    other = idempotency.replay_or_reserve("v1.engagements:tok_b", "shared")
    assert not other.found


def test_release_lets_a_failed_request_be_retried(app_env) -> None:
    from app.surfaces import idempotency

    idempotency.replay_or_reserve("scope", "k")
    idempotency.release("scope", "k")
    again = idempotency.replay_or_reserve("scope", "k")
    assert not again.found and not again.in_flight


def test_records_older_than_a_day_are_purged(app_env) -> None:
    from datetime import timedelta

    from app.db.models import IdempotencyRecord
    from app.state import db
    from app.surfaces import idempotency
    from core.clock import utcnow

    idempotency.replay_or_reserve("scope", "old")
    idempotency.store_response("scope", "old", {"id": "x"})
    with db()() as session:
        row = session.get(IdempotencyRecord, "scope:old")
        row.created_at = utcnow() - timedelta(hours=25)
        session.commit()

    assert idempotency.purge_expired() == 1
    assert not idempotency.replay_or_reserve("scope", "old").found


# ---------------------------------------------------------------------------
# rate limits
# ---------------------------------------------------------------------------


def test_bucket_refuses_then_refills() -> None:
    from app.surfaces.ratelimit import Limiter

    limiter = Limiter(capacity=3, per_seconds=3.0)
    assert [limiter.check("t", now=0).allowed for _ in range(3)] == [True, True, True]

    blocked = limiter.check("t", now=0)
    assert not blocked.allowed
    assert blocked.remaining == 0
    assert blocked.retry_after_s >= 1

    assert limiter.check("t", now=2.0).allowed


def test_buckets_are_per_key() -> None:
    from app.surfaces.ratelimit import Limiter

    limiter = Limiter(capacity=1, per_seconds=60.0)
    assert limiter.check("a", now=0).allowed
    assert not limiter.check("a", now=0).allowed
    assert limiter.check("b", now=0).allowed
