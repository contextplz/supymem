"""SQLAlchemy engine / session factory — SQLite WAL, Postgres pooling."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

log = logging.getLogger("app.db.session")

_DEFAULT_SYNC = "sqlite:///./data/company_os.db"
_DEFAULT_ASYNC = "sqlite+aiosqlite:///./data/company_os.db"

# Used when serialize_writes() is called without a key on Postgres. A missing
# key is a call-site bug; this constant still serialises writers so the ledger
# never silently races. Fits in signed int32 so the bigint/int overloads of
# pg_advisory_xact_lock are unambiguous.
_UNSCOPED_ADVISORY_LOCK = 0x434F5350  # 'COSP'

_SEQ_TABLES = frozenset({"credit_ledger", "admin_audit_log"})
_SQLITE_TIMEOUT_S = 30

# Honour order matches .env.example / env_contract: COMPANY_OS_DB_URL, then
# DATABASE_URL (existing ECS / RDS convention), then DB_URL.
_DB_URL_ENV_NAMES = ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL")


def _is_local() -> bool:
    """Keep hosted scripts able to load this module without the app package."""
    if "SUPYMEM_MODE" not in os.environ:
        return False
    from app.core.local_mode import is_local

    return is_local()


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    """Return the first configured database URL, or the local file default."""
    source: Mapping[str, str] = os.environ if env is None else env
    for name in _DB_URL_ENV_NAMES:
        raw = (source.get(name) or "").strip()
        if raw:
            return raw
    if (source.get("SUPYMEM_MODE") or "").strip().lower() == "local":
        from app.db.local_sqlite import default_database_url

        return default_database_url(source)
    return ""


def resolve_source_database_url(env: Mapping[str, str], directory: Path) -> str:
    """Validate every inherited DB setting before the source runner opens any database."""
    retry = "in your checkout .env and terminal environment, then rerun ./start --web."
    if env.get("RDS_PROXY_ENDPOINT", "").strip():
        raise ValueError(f"Your browser app uses SQLite. Remove RDS_PROXY_ENDPOINT {retry}")

    selected = None
    for name in _DB_URL_ENV_NAMES:
        raw = env.get(name, "").strip()
        if not raw:
            continue
        try:
            parsed = make_url(raw)
        except (ArgumentError, ValueError):
            parsed = None
        if (parsed is None or parsed.drivername not in {"sqlite", "sqlite+pysqlite", "sqlite+aiosqlite"}
                or not parsed.database or parsed.database == ":memory:" or parsed.query
                or any(part is not None for part in (parsed.username, parsed.password, parsed.host, parsed.port))):
            raise ValueError(f"Your browser app needs a SQLite file. Remove {name} {retry}")
        path = Path(parsed.database).resolve()
        if not path.is_relative_to(directory) or path == directory:
            raise ValueError(f"Your database is outside your data folder. Set COMPANY_OS_DATA_DIR to its containing folder, "
                             f"or correct {name} {retry}")
        if selected is not None and selected != path:
            raise ValueError(f"Your database settings disagree. Set {name} to the same SQLite file as your other database setting {retry}")
        selected = path

    legacy, desktop = directory / "local.db", directory / "database.sqlite3"
    existing = [path for path in (legacy, desktop) if path.exists()]
    if selected is None:
        if len(existing) > 1:
            raise ValueError(f"You have two database files in your data folder. Set COMPANY_OS_DB_URL to the SQLite file you want {retry}")
        selected = existing[0] if existing else legacy
        if not selected.resolve().is_relative_to(directory):
            raise ValueError(f"Your database points outside your data folder. Set COMPANY_OS_DATA_DIR to the folder containing your SQLite file {retry}")
    elif existing and not selected.exists():
        raise ValueError(f"Your data folder already has a database. Set COMPANY_OS_DB_URL to your existing SQLite file, "
                         f"or choose an empty COMPANY_OS_DATA_DIR {retry}")
    url = URL.create("sqlite", database=str(selected)).render_as_string(hide_password=False)
    if make_url(url).database != str(selected) or (selected.exists() and not selected.is_file()):
        raise ValueError(f"Your database path cannot be used. Set COMPANY_OS_DATA_DIR and COMPANY_OS_DB_URL to a usable SQLite file {retry}")
    return url


def normalize_db_url(url: str, *, asyncio: bool = False) -> str:
    """Accept sqlite / postgres URLs and pin the driver we actually ship.

    Bare ``postgresql://`` (RDS, libpq, Heroku ``postgres://``) is rewritten to
    ``postgresql+psycopg://`` so SQLAlchemy does not look for psycopg2.
    """
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")

    if asyncio:
        if url.startswith("sqlite://") and not url.startswith("sqlite+"):
            return "sqlite+aiosqlite://" + url.removeprefix("sqlite://")
        if url.startswith("postgresql+psycopg://"):
            return "postgresql+psycopg_async://" + url.removeprefix(
                "postgresql+psycopg://"
            )
        if url.startswith("postgresql://"):
            return "postgresql+psycopg_async://" + url.removeprefix("postgresql://")
        return url

    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url.removeprefix("sqlite+aiosqlite://")
    if url.startswith("postgresql+psycopg_async://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql+psycopg_async://")
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _is_sqlite_url(url: str) -> bool:
    return url.startswith(("sqlite:", "sqlite+"))


def _apply_sqlite_pragmas(
    dbapi_conn: object, _connection_record: object = None, *, initialize_wal: bool = True,
) -> None:
    """Configure the connection; only startup needs to check the persistent journal mode."""
    cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_TIMEOUT_S * 1000}")
        if initialize_wal:
            deadline = time.monotonic() + _SQLITE_TIMEOUT_S
            while True:
                try:
                    cursor.execute("PRAGMA journal_mode")
                    if cursor.fetchall()[0][0].lower() != "wal":
                        cursor.execute("PRAGMA journal_mode=WAL")
                        cursor.fetchall()
                    break
                except sqlite3.OperationalError as error:
                    code = getattr(error, "sqlite_errorcode", 0) & 255
                    if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=FULL")
    finally:
        cursor.close()


def _sqlite_savepoint(connection: Connection, _name: str | None) -> None:
    """Keep savepoints inside a real outer transaction and acquire write intent before reads."""
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _register_sqlite_pragmas(engine: Engine | AsyncEngine) -> None:
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if sync_engine.dialect.name == "sqlite":
        if not event.contains(sync_engine, "first_connect", _apply_sqlite_pragmas):
            event.listen(sync_engine, "first_connect", _apply_sqlite_pragmas)
            event.listen(sync_engine, "connect", partial(_apply_sqlite_pragmas, initialize_wal=False))
            event.listen(sync_engine, "savepoint", _sqlite_savepoint)


def _apply_postgres_utc(connection: Any, _record: Any) -> None:
    # Our TIMESTAMP columns store UTC without a timezone. Psycopg adapts aware
    # values as timestamptz, so PostgreSQL must cast them through UTC rather than
    # the server/operator's timezone. Commit this session invariant independently
    # of application transactions, including rollback and pool reuse.
    previous = connection.autocommit
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'UTC'")
        finally:
            cursor.close()
    finally:
        connection.autocommit = previous


def ensure_utc_sessions(engine: Engine | AsyncEngine) -> None:
    """Pin every new supported PostgreSQL/psycopg session to canonical UTC."""
    sync = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if sync.dialect.name == "postgresql" and not event.contains(sync, "connect", _apply_postgres_utc):
        event.listen(sync, "connect", _apply_postgres_utc)


def _sqlite_connect_kwargs(url: str) -> dict[str, Any]:
    """File-backed SQLite needs a connection per session so writers can contend.

    QueuePool would cap concurrent credit reserves at pool_size+overflow and make
    the atomic UPDATE race untestable. NullPool + busy_timeout lets SQLite
    serialise BEGIN IMMEDIATE writers instead of the pool.
    """
    kwargs: dict[str, Any] = {
        "connect_args": {"timeout": _SQLITE_TIMEOUT_S, "check_same_thread": False},
    }
    is_memory = make_url(url).database in (None, "", ":memory:")
    if not is_memory:
        kwargs["poolclass"] = NullPool
    return kwargs


def _ensure_sqlite_parent(url: str) -> None:
    prefixes = ("sqlite:///", "sqlite+aiosqlite:///")
    for prefix in prefixes:
        if url.startswith(prefix):
            path = Path(url.removeprefix(prefix))
            if str(path) not in (":memory:",) and path.parent != Path("."):
                path.parent.mkdir(parents=True, exist_ok=True)
            return


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    if _is_local():
        url = url or resolve_database_url()
    url = normalize_db_url(database_endpoint(url or _DEFAULT_SYNC))
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if _is_sqlite_url(url):
        kwargs.update(_sqlite_connect_kwargs(url))
        if _is_local():
            from app.db.local_sqlite import migrate_sqlite

            migrate_sqlite(url)
        else:
            _ensure_sqlite_parent(url)
    else:
        kwargs["pool_pre_ping"] = True
        kwargs.update(pool_settings())
    engine = create_engine(url, **kwargs)
    _register_sqlite_pragmas(engine)
    ensure_utc_sessions(engine)
    return engine


def make_async_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    if _is_local():
        url = url or resolve_database_url()
    url = normalize_db_url(database_endpoint(url or _DEFAULT_ASYNC), asyncio=True)
    kwargs: dict[str, Any] = {"echo": echo}
    if _is_sqlite_url(url):
        kwargs.update(_sqlite_connect_kwargs(url))
        if _is_local():
            from app.db.local_sqlite import migrate_sqlite

            migrate_sqlite(url)
        else:
            _ensure_sqlite_parent(url)
    else:
        kwargs["pool_pre_ping"] = True
        kwargs.update(pool_settings())
    engine = create_async_engine(url, **kwargs)
    _register_sqlite_pragmas(engine)
    ensure_utc_sessions(engine)
    return engine


def database_endpoint(url: str) -> str:
    endpoint = os.environ.get("RDS_PROXY_ENDPOINT", "").strip()
    parsed = make_url(normalize_db_url(url))
    if endpoint and parsed.get_backend_name() == "postgresql":
        return parsed.set(host=endpoint).render_as_string(hide_password=False)
    return url


def pool_settings() -> dict[str, int]:
    size = int(os.environ.get("DB_POOL_SIZE", "10"))
    overflow = int(os.environ.get("DB_MAX_OVERFLOW", "20"))
    if size < 1 or overflow < 0:
        raise ValueError("DB_POOL_SIZE must be positive and DB_MAX_OVERFLOW nonnegative")
    return {"pool_size": size, "max_overflow": overflow}


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    import app.db.nodash  # noqa: F401  (registers the no-dash mapper events)

    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def make_async_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def serialize_writes(session: Session, *, key: str | None = None) -> None:
    """Serialise writers that compute a value from a prior read.

    Must be the first statement in the transaction on SQLite (BEGIN IMMEDIATE).

    * SQLite: global write lock via ``BEGIN IMMEDIATE``. ``key`` is ignored —
      SQLite cannot lock a subset of rows this way, and the global lock covers
      both the per-account hash chain and global ``seq`` allocation.
    * PostgreSQL: transaction-scoped advisory lock on ``key``
      (``pg_advisory_xact_lock``), released on commit *and* rollback. A missing
      key falls back to a process-wide constant and logs a warning — never
      silently unserialised.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        transaction = session.get_transaction()
        if transaction is not None and session.info.get("_sqlite_write_transaction") is transaction:
            return
        session.execute(text("BEGIN IMMEDIATE"))
        session.info["_sqlite_write_transaction"] = session.get_transaction()
        return
    if dialect == "postgresql":
        if key:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )
            return
        log.warning(
            "serialize_writes called without a key on postgresql; "
            "falling back to unscoped lock id %s — this is a call-site bug",
            hex(_UNSCOPED_ADVISORY_LOCK),
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _UNSCOPED_ADVISORY_LOCK},
        )
        return
    raise RuntimeError(f"serialize_writes: unsupported dialect {dialect!r}")


def begin_immediate(session: Session) -> None:
    """Deprecated alias for ``serialize_writes(session, key=None)``.

    Unmigrated call sites still serialise (globally on Postgres) rather than
    racing. Pass a real ``key`` via ``serialize_writes`` instead.
    """
    serialize_writes(session, key=None)


def allocate_seq(session: Session, table: str) -> int:
    """Next globally-unique ``seq`` for a hash-chained table.

    PostgreSQL: ``nextval`` of the column's owned sequence. ``nextval`` is
    non-transactional, so a rolled-back transaction burns a value and ``seq``
    may contain gaps. The invariant is strictly increasing (and UNIQUE), not
    contiguous.

    SQLite: ``max(seq) + 1``, which is safe under ``BEGIN IMMEDIATE``.
    """
    if table not in _SEQ_TABLES:
        raise ValueError(f"refusing to allocate seq for unknown table {table!r}")
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        value = session.scalar(
            text(f"SELECT nextval(pg_get_serial_sequence('{table}', 'seq'))")
        )
        if value is None:
            raise RuntimeError(
                f"pg_get_serial_sequence('{table}', 'seq') returned NULL; "
                "the column is not BIGSERIAL/IDENTITY. create_all and alembic "
                "must both produce a sequence owned by this column."
            )
        return int(value)
    max_seq = session.scalar(text(f"SELECT MAX(seq) FROM {table}"))
    return int(max_seq or 0) + 1


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@asynccontextmanager
async def async_session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def verify_pragmas(engine: Engine) -> dict[str, object]:
    """Return engine durability settings for doctor/tests.

    SQLite: journal_mode / foreign_keys / busy_timeout / synchronous.
    PostgreSQL: a cheap equivalent (version + timezone) so callers that still
    invoke this after pointing at RDS do not fail on PRAGMA.
    """
    dialect = engine.dialect.name
    if dialect == "sqlite":
        with engine.connect() as conn:
            journal = conn.execute(text("PRAGMA journal_mode")).scalar()
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
            busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
            sync = conn.execute(text("PRAGMA synchronous")).scalar()
        return {
            "dialect": dialect,
            "journal_mode": journal,
            "foreign_keys": fk,
            "busy_timeout": busy,
            "synchronous": sync,
        }
    if dialect == "postgresql":
        with engine.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar()
            timezone = conn.execute(text("SHOW timezone")).scalar()
        return {
            "dialect": dialect,
            "server_version": version,
            "timezone": timezone,
        }
    return {"dialect": dialect}
