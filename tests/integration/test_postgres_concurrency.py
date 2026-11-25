"""Postgres concurrency and append-only acceptance for the credit ledger (P0a).

Skipped unless ``TEST_POSTGRES_URL`` is set and the server accepts connections.
"""

from __future__ import annotations

import os
import sys
import types
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url

from tests.integration.test_postgres_auth_security import service_modules

ROOT = Path(__file__).resolve().parents[2]
COMPANY_OS = ROOT / "services" / "company_os"

N_WRITERS = 8
GRANT_MICRO = 10_000


def _raw_url() -> str | None:
    return os.environ.get("TEST_POSTGRES_URL")


def _psycopg_conninfo(url: str) -> str:
    for prefix in (
        "postgresql+psycopg_async://",
        "postgresql+psycopg://",
        "postgres://",
    ):
        if url.startswith(prefix):
            return "postgresql://" + url.removeprefix(prefix)
    return url


def _postgres_reachable() -> bool:
    url = _raw_url()
    if not url:
        return False
    try:
        import psycopg

        with psycopg.connect(_psycopg_conninfo(url), connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="TEST_POSTGRES_URL not set or Postgres unreachable",
)


def _normalize(url: str) -> str:
    with service_modules("company_os"):
        from app.db.session import normalize_db_url

        return normalize_db_url(url)


def _admin_engine() -> Engine:
    url = make_url(_normalize(_raw_url() or ""))
    admin = url.set(database="postgres")
    return create_engine(admin, isolation_level="AUTOCOMMIT", pool_pre_ping=True)


def _create_database() -> tuple[str, str]:
    name = f"cos_p0a_{uuid.uuid4().hex[:12]}"
    engine = _admin_engine()
    with engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    engine.dispose()
    url = make_url(_normalize(_raw_url() or "")).set(database=name)
    return name, url.render_as_string(hide_password=False)


def _drop_database(name: str) -> None:
    engine = _admin_engine()
    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    engine.dispose()


def _alembic_cfg(db_url: str):
    from alembic.config import Config

    cfg = Config(str(COMPANY_OS / "alembic.ini"))
    cfg.set_main_option("script_location", str(COMPANY_OS / "app/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@contextmanager
def _company_context(db_url: str, tmp_path: Path | None = None):
    # A previous scaffold/gateway test can leave a different app package cached.
    # Both Python identity and environment belong to this scoped fixture only.
    with pytest.MonkeyPatch.context() as patch, service_modules("company_os"):
        patch.setenv("COMPANY_OS_DB_URL", db_url)
        if tmp_path is not None:
            patch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
            patch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "wm.sqlite3"))
        yield


def _alembic_upgrade(db_url: str) -> None:
    from alembic import command

    old = os.getcwd()
    os.chdir(COMPANY_OS)
    try:
        with _company_context(db_url):
            command.upgrade(_alembic_cfg(db_url), "head")
    finally:
        os.chdir(old)


def _alembic_downgrade_base(db_url: str) -> None:
    from alembic import command

    old = os.getcwd()
    os.chdir(COMPANY_OS)
    try:
        with _company_context(db_url):
            command.downgrade(_alembic_cfg(db_url), "base")
    finally:
        os.chdir(old)


def _boot_app(db_url: str, tmp_path: Path) -> None:
    os.environ["COMPANY_OS_DB_URL"] = db_url
    os.environ["COMPANY_OS_DATA_DIR"] = str(tmp_path)
    os.environ["COMPANY_OS_WORKING_MEMORY_PATH"] = str(tmp_path / "wm.sqlite3")
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app import state
    from app.admin import audit as audit_mod
    from app.credits.policy import invalidate

    state.reset_db_factory()
    audit_mod.reset_trigger_flag()
    state.db()
    invalidate()


def _seqs_for_account(account_id: str) -> list[int]:
    from app.credits.ledger import entries_for_account

    items = entries_for_account(account_id, limit=200)["items"]
    # newest first from search; oldest-first for monotonicity
    return list(reversed([int(row.seq) for row in items]))


def _assert_strictly_increasing(seqs: list[int], *, label: str) -> None:
    assert seqs, f"{label}: no seq values"
    assert len(seqs) == len(set(seqs)), f"{label}: duplicate seq {seqs}"
    assert all(a < b for a, b in zip(seqs, seqs[1:])), (
        f"{label}: seq not strictly increasing: {seqs}"
    )


@pytest.fixture(scope="module")
def pg_migrated(tmp_path_factory):
    name, db_url = _create_database()
    tmp = tmp_path_factory.mktemp("pg_migrated")
    try:
        _alembic_upgrade(db_url)
        with _company_context(db_url, tmp):
            _boot_app(db_url, tmp)
            yield db_url
    finally:
        _drop_database(name)


def test_alembic_upgrade_and_downgrade_roundtrip(monkeypatch) -> None:
    # Reproduce the late full-suite collision: an earlier app service owns this
    # package name but does not contain Company OS's feature-catalog module.
    foreign = types.ModuleType("app.analytics")
    foreign.__path__ = [str(ROOT / "config/scaffold/app/analytics")]
    monkeypatch.setitem(sys.modules, "app.analytics", foreign)
    for key in ("app.analytics.feature_catalog", "app.analytics.control_catalog"):
        monkeypatch.delitem(sys.modules, key, raising=False)
    name, db_url = _create_database()
    try:
        _alembic_upgrade(db_url)
        engine = create_engine(db_url)
        tables = set(inspect(engine).get_table_names())
        engine.dispose()
        assert "credit_ledger" in tables
        assert "admin_audit_log" in tables
        _alembic_downgrade_base(db_url)
        engine = create_engine(db_url)
        leftover = set(inspect(engine).get_table_names())
        engine.dispose()
        leftover.discard("alembic_version")
        assert not leftover, f"tables survived downgrade base: {sorted(leftover)}"
        _alembic_upgrade(db_url)
    finally:
        _drop_database(name)


def test_concurrent_grants_same_account(pg_migrated: str) -> None:
    from tests.credit_fixtures import ensure_test_wallet
    from app.credits.accounts import get_account_by_id
    from app.credits.grants import grant
    from app.credits.ledger import verify_chain
    from app.credits.policy import default_venture_credits
    from app.credits.units import credits_to_micro

    account = ensure_test_wallet(f"ven_same_{uuid.uuid4().hex[:8]}")
    raised: list[BaseException] = []
    rows = []

    def write(i: int):
        return grant(
            account_id=account.id,
            amount_micro=GRANT_MICRO,
            reason=f"same-account concurrent {i}",
            actor_kind="system",
            actor_id="p0a",
            idempotency_key=f"p0a:same:{account.id}:{i}",
        )

    with ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        futures = [pool.submit(write, i) for i in range(N_WRITERS)]
        for f in as_completed(futures):
            try:
                rows.append(f.result())
            except BaseException as exc:
                raised.append(exc)

    assert not raised, f"workers raised: {raised!r}"
    assert len(rows) == N_WRITERS

    ok, detail = verify_chain(account.id)
    assert ok, detail
    seqs = _seqs_for_account(account.id)
    _assert_strictly_increasing(seqs, label="same-account")

    refreshed = get_account_by_id(account.id)
    assert refreshed is not None
    expected = credits_to_micro(default_venture_credits()) + N_WRITERS * GRANT_MICRO
    assert refreshed.granted_micro == expected


def test_concurrent_grants_different_accounts(pg_migrated: str) -> None:
    from tests.credit_fixtures import ensure_test_wallet
    from app.credits.accounts import get_account_by_id
    from app.credits.grants import grant
    from app.credits.ledger import verify_chain
    from app.credits.units import credits_to_micro
    from app.credits.policy import default_venture_credits

    accounts = [
        ensure_test_wallet(f"ven_diff_{uuid.uuid4().hex[:8]}") for _ in range(N_WRITERS)
    ]
    raised: list[BaseException] = []
    rows = []

    def write(account):
        return grant(
            account_id=account.id,
            amount_micro=GRANT_MICRO,
            reason="cross-account concurrent",
            actor_kind="system",
            actor_id="p0a",
            idempotency_key=f"p0a:diff:{account.id}",
        )

    with ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        futures = [pool.submit(write, acct) for acct in accounts]
        for f in as_completed(futures):
            try:
                rows.append(f.result())
            except BaseException as exc:
                raised.append(exc)

    assert not raised, f"workers raised: {raised!r}"
    assert len(rows) == N_WRITERS
    seqs = [int(row.seq) for row in rows]
    assert len(seqs) == len(set(seqs)), f"global seq collision: {seqs}"

    initial = credits_to_micro(default_venture_credits())
    for account in accounts:
        ok, detail = verify_chain(account.id)
        assert ok, f"{account.id}: {detail}"
        _assert_strictly_increasing(
            _seqs_for_account(account.id), label=account.id
        )
        refreshed = get_account_by_id(account.id)
        assert refreshed is not None
        assert refreshed.granted_micro == initial + GRANT_MICRO


def test_update_and_delete_raise_on_hash_chained_tables(pg_migrated: str) -> None:
    from app.admin.audit import record
    from tests.credit_fixtures import ensure_test_wallet
    from app.state import db

    account = ensure_test_wallet(f"ven_trig_{uuid.uuid4().hex[:8]}")
    audit_row = record(action="session.read", outcome="allow", actor_email="p0a@x.com")
    factory = db()
    with factory() as session:
        assert session.scalar(text("SELECT count(*) FROM credit_ledger WHERE account_id = :id"),
                              {"id": account.id}) > 0
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("UPDATE credit_ledger SET reason = 'x' WHERE account_id = :id"),
                {"id": account.id},
            )
            session.commit()
        session.rollback()
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("DELETE FROM credit_ledger WHERE account_id = :id"),
                {"id": account.id},
            )
            session.commit()
        session.rollback()
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("UPDATE admin_audit_log SET action = 'x' WHERE id = :id"),
                {"id": audit_row.id},
            )
            session.commit()
        session.rollback()
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("DELETE FROM admin_audit_log WHERE id = :id"),
                {"id": audit_row.id},
            )
            session.commit()


def test_rolled_back_nextval_leaves_seq_gap_and_chain_still_verifies(
    pg_migrated: str,
) -> None:
    """Pins the non-transactional nextval behaviour. Do not 'fix' this back to gapless."""
    from tests.credit_fixtures import ensure_test_wallet
    from app.credits.grants import grant
    from app.credits.ledger import verify_chain
    from app.credits._txn import serialize_writes, session_factory

    account = ensure_test_wallet(f"ven_gap_{uuid.uuid4().hex[:8]}")
    before = _seqs_for_account(account.id)
    assert before, "The sequence test requires a real initial ledger row."
    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=account.id)
        burned = session.scalar(
            text("SELECT nextval(pg_get_serial_sequence('credit_ledger', 'seq'))")
        )
        assert burned is not None
        session.rollback()

    grant(
        account_id=account.id,
        amount_micro=GRANT_MICRO,
        reason="after burned seq",
        actor_kind="system",
        actor_id="p0a",
        idempotency_key=f"p0a:gap:{account.id}",
    )
    after = _seqs_for_account(account.id)
    ok, detail = verify_chain(account.id)
    assert ok, detail
    _assert_strictly_increasing(after, label="gapped account")
    assert int(burned) not in after
    assert max(after) > max(before)
    # Contiguous would mean max-min+1 == count. The burned nextval must leave a hole.
    assert max(after) - min(after) + 1 > len(after)


def test_create_all_owns_a_seq_sequence_and_grant_works(tmp_path: Path) -> None:
    """create_all (not alembic) must still produce a usable Postgres sequence."""
    name, db_url = _create_database()
    try:
        with _company_context(db_url, tmp_path):
            _boot_app(db_url, tmp_path)
            from sqlalchemy import text as sql_text

            from app.credits.accounts import get_account_by_id
            from tests.credit_fixtures import ensure_test_wallet
            from app.credits.grants import grant
            from app.credits.ledger import verify_chain
            from app.credits.policy import invalidate
            from app.state import db

            factory = db()
            with factory() as session:
                seq_name = session.scalar(
                    sql_text("SELECT pg_get_serial_sequence('credit_ledger', 'seq')")
                )
                audit_seq = session.scalar(
                    sql_text("SELECT pg_get_serial_sequence('admin_audit_log', 'seq')")
                )
                session.execute(
                    sql_text(
                        "INSERT INTO credit_policies "
                        "(key, value_json, updated_at, updated_by) VALUES "
                        "('default_venture_credits', '5000', CURRENT_TIMESTAMP, 'system') "
                        "ON CONFLICT (key) DO NOTHING"
                    )
                )
                session.commit()
            assert seq_name, "create_all did not own a sequence on credit_ledger.seq"
            assert audit_seq, "create_all did not own a sequence on admin_audit_log.seq"
            invalidate()

            account = ensure_test_wallet(f"ven_ca_{uuid.uuid4().hex[:8]}")
            grant(
                account_id=account.id,
                amount_micro=GRANT_MICRO,
                reason="create_all path",
                actor_kind="system",
                actor_id="p0a",
                idempotency_key=f"p0a:ca:{account.id}",
            )
            ok, detail = verify_chain(account.id)
            assert ok, detail
            refreshed = get_account_by_id(account.id)
            assert refreshed is not None
            assert refreshed.granted_micro > 0
    finally:
        _drop_database(name)
