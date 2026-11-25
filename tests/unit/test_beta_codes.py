"""Beta-code Mac download: verify, recorded redirects, no raw IP, owner-only counts."""
from datetime import datetime, timezone

import pytest
from core.assertion import sign_assertion
from fastapi.testclient import TestClient

from tests.company_os_harness import COMPANY_OS, load_company_os_app

SECRET = "beta-code-test-secret-over-thirty-two-characters"
OWNER = "usr_cog_owner"
RAW_IP = "203.0.113.77"
HASH = "h" * 64


@pytest.fixture
def client(tmp_path, monkeypatch):
    if not (COMPANY_OS / "app/api/beta_api.py").is_file():
        pytest.skip("Hosted beta downloads are not in the public local release.")
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/beta.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "admin@example.test")
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "beta-test-vault-secret-over-thirty-two-characters")
    main = load_company_os_app()
    from app import state
    from app.api import beta_api
    from app.db.supymem_models import BetaCode

    state.reset_db_factory()
    beta_api._LIMIT._buckets.clear()
    app = TestClient(main.create_app(), follow_redirects=False)
    with state.db()() as s:
        now = datetime.now(timezone.utc)
        s.add_all([BetaCode(code="ABCD2345", owner_person_id=OWNER, label="t", active=True, created_at=now),
                   BetaCode(code="OFFOFF22", owner_person_id=OWNER, label="off", active=False, created_at=now),
                   BetaCode(code="ONEUSE22", owner_person_id="usr_other", label="one", active=True, created_at=now, max_uses=1)])
        s.commit()
    return app


def _hdr(path, method="GET", sub=OWNER):
    return {"X-COS-Assertion": sign_assertion(SECRET, sub=sub, email="", role="user", path=path, method=method)}


def test_verify_good_case_insensitive_bad_and_inactive(client):
    ok = client.post("/public/beta/verify", json={"code": " abcd2345 "}, headers={"X-Client-Hash": HASH})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert ok.json()["download_url"].endswith("/public/beta/download?code=ABCD2345")
    assert client.post("/public/beta/verify", json={"code": "OFFOFF22"}, headers={"X-Client-Hash": HASH}).json()["reason"] == "disabled"
    assert client.post("/public/beta/verify", json={"code": "ZZZZZZZZ"}, headers={"X-Client-Hash": HASH}).json()["reason"] == "mistyped"
    for bad in ("ZZZZZZZZ", "OFFOFF22", "short", "", "ABCD2345; DROP"):
        r = client.post("/public/beta/verify", json={"code": bad}, headers={"X-Client-Hash": HASH})
        assert r.status_code == 404 and r.json() == {"ok": False, "error": "invalid_code", "reason": "disabled" if bad == "OFFOFF22" else "mistyped"}


def test_verify_exhausted_code_preserves_error_and_adds_reason(client):
    from app import state
    from app.db.supymem_models import BetaCode
    with state.db()() as session:
        session.get(BetaCode, "ONEUSE22").max_uses = 0
        session.commit()
    response = client.post("/public/beta/verify", json={"code": "ONEUSE22"}, headers={"X-Client-Hash": HASH})
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "invalid_code", "reason": "exhausted"}


def test_verify_rate_limited_per_ip_hash(client):
    codes = [client.post("/public/beta/verify", json={"code": "NOPENOPE"}, headers={"X-Client-Hash": "a" * 64}).status_code
             for _ in range(12)]
    assert codes[:10] == [404] * 10 and 429 in codes[10:]
    assert client.post("/public/beta/verify", json={"code": "ABCD2345"}, headers={"X-Client-Hash": "b" * 64}).status_code == 200


def test_download_records_row_redirects_and_stores_no_raw_ip(client):
    from app import state
    from app.db.supymem_models import BetaCodeDownload

    r = client.get("/public/beta/download?code=abcd2345", headers={"X-Client-Hash": HASH, "X-Client-UA": "UA/1 " + "x" * 300,
                                                                    "X-Forwarded-For": RAW_IP})
    assert r.status_code == 302 and r.headers["location"].endswith("/Supymem.dmg")
    assert client.get("/public/beta/download?code=NOPE2345", headers={"X-Client-Hash": HASH}).status_code == 404
    with state.db()() as s:
        rows = s.query(BetaCodeDownload).all()
    assert len(rows) == 1 and rows[0].code == "ABCD2345" and rows[0].ip_hash == HASH
    assert len(rows[0].user_agent) == 120 and rows[0].platform == "macos-aarch64"
    assert RAW_IP not in " ".join(str(v) for v in vars(rows[0]).values())


def test_downloads_do_not_spend_onboarding_capacity(client):
    h = {"X-Client-Hash": HASH}
    assert client.get("/public/beta/download?code=ONEUSE22", headers=h).status_code == 302
    assert client.get("/public/beta/download?code=ONEUSE22", headers=h).status_code == 302
    assert client.post("/public/beta/verify", json={"code": "ONEUSE22"}, headers=h).status_code == 200


def test_owner_view_requires_signed_identity_and_is_owner_scoped(client):
    client.get("/public/beta/download?code=ABCD2345", headers={"X-Client-Hash": HASH})
    client.get("/public/beta/download?code=ABCD2345", headers={"X-Client-Hash": HASH})
    assert client.get("/beta/codes").status_code == 401
    assert client.get("/beta/codes", headers={"X-Subject": OWNER}).status_code == 401  # unsigned
    mine = client.get("/beta/codes", headers=_hdr("/beta/codes")).json()["codes"]
    assert {c["code"]: c["downloads"] for c in mine} == {"ABCD2345": 2, "OFFOFF22": 0}
    other = client.get("/beta/codes", headers=_hdr("/beta/codes", sub="usr_nobody")).json()["codes"]
    assert len(other) == 1 and other[0]["max_uses"] == 2
    assert other[0]["successful_invitations"] == 0 and other[0]["remaining"] == 2
    assert other[0]["code"] not in {c["code"] for c in mine}


def test_migration_seeds_founder_code(tmp_path, monkeypatch):
    from alembic import command
    from sqlalchemy import create_engine, text
    from tests.unit.db.test_supymem_migrations import _alembic_cfg

    url = f"sqlite:///{tmp_path}/m.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    # Select Core explicitly: this test also runs alone or after gateway imports.
    load_company_os_app()
    import logging
    logger = logging.getLogger("core.migration_existing_application_logger")
    logger.disabled = False
    command.upgrade(_alembic_cfg(url), "head")
    assert not logger.disabled, "Alembic must preserve existing application diagnostics"
    with create_engine(url).connect() as c:
        row = c.execute(text("SELECT code, owner_person_id, label, active FROM beta_codes")).one()
    assert row[0] == "5BN4EKZD" and row[1] == "usr_cog_72943502a13ed9e35eff9e9a870e786a" and row[3]
