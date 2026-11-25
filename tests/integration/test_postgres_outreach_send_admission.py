"""PostgreSQL owns outreach admission across separate database connections."""
import asyncio
import hashlib
import select
import subprocess
import sys
import time
import uuid

import pytest
from sqlalchemy import text

from tests.integration.test_postgres_auth_security import pg_database as pg_database, service_modules


@pytest.mark.parametrize("abort", [RuntimeError, asyncio.CancelledError])
def test_pg_send_admission_is_nonblocking_across_connections_and_releases_on_abort(pg_database, monkeypatch, abort):
    _, engine = pg_database
    with service_modules("company_os"):
        from app.autopilot import send_admission
        from app.db.session import make_session_factory

        factory = make_session_factory(engine)
        monkeypatch.setattr(send_admission, "db", lambda: factory)
        venture = "owned-pg-outreach-admission-" + uuid.uuid4().hex
        with pytest.raises(abort):
            with send_admission.try_send_admission(venture) as owner:
                assert owner is True
                with send_admission.try_send_admission(venture) as duplicate:
                    assert duplicate is False
                with send_admission.try_send_admission(venture + "-other") as separate:
                    assert separate is True
                raise abort()
        with send_admission.try_send_admission(venture) as recovered:
            assert recovered is True


@pytest.mark.parametrize("domain", ["fleet-inspection", "home-gardening"])
@pytest.mark.parametrize("old_first", [True, False])
def test_pg_mixed_version_senders_share_production_lock(pg_database, monkeypatch, domain, old_first):
    _, engine = pg_database
    with service_modules("company_os"):
        from app.autopilot import send_admission
        from app.db.session import make_session_factory

        factory = make_session_factory(engine)
        monkeypatch.setattr(send_admission, "db", lambda: factory)
        venture = domain + "-" + uuid.uuid4().hex
        key = int.from_bytes(hashlib.sha256(("outreach-send:" + venture).encode()).digest()[:8], "big", signed=True)
        if old_first:
            with factory() as old:
                assert old.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
                with send_admission.try_send_admission(venture) as duplicate:
                    assert duplicate is False
                assert not old.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
                    {"scope": venture})
        else:
            with send_admission.try_send_admission(venture) as owner:
                assert owner is True
                assert engine.pool.checkedout() == 0
                with factory() as old:
                    assert not old.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
        with factory() as old:
            assert old.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
            assert not old.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
                {"scope": venture})


@pytest.mark.parametrize("domain", ["fleet-inspection", "home-gardening"])
def test_pg_sigkill_orphaned_fence_is_reclaimable(pg_database, monkeypatch, domain):
    url, engine = pg_database
    with service_modules("company_os"):
        from app.autopilot import send_admission
        from app.db.session import make_session_factory

        factory = make_session_factory(engine)
        monkeypatch.setattr(send_admission, "db", lambda: factory)
        venture = domain + "-" + uuid.uuid4().hex
        child = subprocess.Popen([sys.executable, "-u", "-c", "\n".join([
            "import sys, time",
            "from app.autopilot import send_admission",
            "from app.db.session import make_engine, make_session_factory",
            "factory = make_session_factory(make_engine(sys.argv[1]))",
            "send_admission.db = lambda: factory",
            "with send_admission.try_send_admission(sys.argv[2]) as admitted:",
            "    print(admitted, flush=True)",
            "    time.sleep(60)",
        ]), url, venture], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert select.select([child.stdout], [], [], 15)[0], "Sender did not finish admission"
            assert child.stdout.readline().strip() == "True"
            with send_admission.try_send_admission(venture) as blocked:
                assert blocked is False
            with factory() as session:
                original = session.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
                    {"scope": venture})
                assert original
        finally:
            child.kill()
            child.wait(timeout=10)
        with factory() as session:
            assert session.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
                {"scope": venture}) == original
        for attempt in range(100):
            with send_admission.try_send_admission(venture) as recovered:
                if recovered:
                    with factory() as session:
                        assert session.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
                            {"scope": venture}) != original
                    break
            time.sleep(0.1)
        assert recovered is True
        with factory() as session:
            assert not session.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
                {"scope": venture})
