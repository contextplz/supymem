"""Canonical UTC sessions preserve timestamps under non-UTC PostgreSQL defaults."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from tests.integration.test_postgres_auth_security import pg_database as pg_database, service_modules


def non_utc_url(url):
    return make_url(url).update_query_dict({"options":"-c timezone=Asia/Kolkata"}).render_as_string(hide_password=False)


def test_company_engine_utc_survives_rollbacks_reconnects_and_roundtrips(pg_database):
    url, _ = pg_database
    with service_modules("company_os"):
        from app.db.session import make_engine
        engine = make_engine(non_utc_url(url))
    aware = datetime(2026, 10, 3, 0, 1, 2, tzinfo=timezone.utc)
    naive = aware.replace(tzinfo=None)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SHOW TIME ZONE")).scalar_one() == "UTC"
            connection.rollback()
            assert connection.execute(text("SHOW TIME ZONE")).scalar_one() == "UTC"
            connection.execute(text("CREATE TEMP TABLE utc_receipt (aware_value timestamp, naive_value timestamp)"))
            connection.execute(text("INSERT INTO utc_receipt VALUES (:aware,:naive)"), {"aware":aware,"naive":naive})
            connection.commit()
            values = connection.execute(text("SELECT aware_value,naive_value FROM utc_receipt")).one()
            assert tuple(values) == (naive, naive)
        engine.dispose()
        with engine.connect() as connection:
            assert connection.execute(text("SHOW TIME ZONE")).scalar_one() == "UTC"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_async_company_engine_also_owns_utc_session(pg_database):
    url, _ = pg_database
    with service_modules("company_os"):
        from app.db.session import make_async_engine
        engine = make_async_engine(non_utc_url(url))
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SHOW TIME ZONE"))).scalar_one() == "UTC"
            await connection.rollback()
            assert (await connection.execute(text("SHOW TIME ZONE"))).scalar_one() == "UTC"
    finally:
        await engine.dispose()
