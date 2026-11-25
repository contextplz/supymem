"""Secret slots: local values stay encrypted; AI/tool/chat never see them."""

from __future__ import annotations

import json

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/sec.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / ".env"))
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    load_company_os_app()
    from app import state as state_mod
    from app.secrets.service import reset_secret_slots
    from app.api import secrets_api

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_secret_slots()
    # Secret slots are a local-only helper. Mount them only in this isolated
    # fixture; the hosted Core router must not expose global .env writes.
    app = load_company_os_app().create_app()
    app.include_router(secrets_api.router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as c:
        yield c
    reset_secret_slots()


def test_hosted_core_does_not_mount_global_secret_writer(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    app = load_company_os_app().create_app()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/secret-slots/{slot_id}/value" not in paths
    assert "/secret-slots/request" not in paths


@pytest.mark.parametrize("domain", ["invoice-workflow", "home-gardening"])
def test_desktop_model_keys_use_encrypted_settings_not_plaintext(tmp_path, monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.secrets import service
    from app.secrets.catalog import slot_for_provider
    from app.core.local_secrets import get_secret

    def unsafe_write(*args, **kwargs):
        pytest.fail("Your desktop model key reached the legacy workspace writer")

    monkeypatch.setattr(service, "_put_vault", unsafe_write)
    spec = slot_for_provider("openai")
    secret = f"test-only-{domain}-credential"
    saved = service.save_secret(spec.id, {spec.env_keys[0]: secret})
    assert saved["armed"] is True
    assert secret not in json.dumps(saved)
    assert get_secret("model", "openai") == secret
    assert all(secret.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
def test_credential_files_are_private_on_creation_update_and_mirror(tmp_path, monkeypatch, domain):
    import os
    import stat
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "services" / "company_os"))
    from app.secrets import dotenv_file

    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("FOUNDER_CREDENTIAL", raising=False)
    root = tmp_path / domain
    root.mkdir(mode=0o755)
    target = root / ".env"
    repo = tmp_path / "repo"
    repo.mkdir()
    mirror = repo / ".env"
    mirror.write_text("EXISTING=preserved\n")
    mirror.chmod(0o644)
    monkeypatch.setattr(dotenv_file, "REPO_ROOT", repo)
    monkeypatch.setattr(dotenv_file, "secrets_env_path", lambda: target)
    original_umask = os.umask(0o022)
    try:
        dotenv_file.upsert_env("FOUNDER_CREDENTIAL", f"synthetic-{domain}")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(mirror.stat().st_mode) == 0o600
        assert "EXISTING=preserved" in mirror.read_text()
        target.chmod(0o644)
        dotenv_file.upsert_env("FOUNDER_CREDENTIAL", f"updated-{domain}", mirror=False)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert dotenv_file.read_env_file(target)["FOUNDER_CREDENTIAL"] == f"updated-{domain}"
        previous = target.read_bytes()

        def cannot_secure(*args):
            raise PermissionError("synthetic permission failure")

        with monkeypatch.context() as permissions:
            permissions.setattr(dotenv_file.os, "fchmod", cannot_secure)
            with pytest.raises(PermissionError, match="synthetic permission failure"):
                dotenv_file.upsert_env("FOUNDER_CREDENTIAL", "synthetic-replacement", mirror=False)
        assert target.read_bytes() == previous
        assert os.environ["FOUNDER_CREDENTIAL"] == f"updated-{domain}"
    finally:
        os.umask(original_umask)


def test_catalog_lists_aws_and_openai(client: TestClient) -> None:
    body = client.get("/secret-slots").json()
    ids = {item["id"] for item in body["items"]}
    assert "aws.credentials" in ids
    assert "openai.api_key" in ids
    blob = json.dumps(body)
    assert "sk-proj-" not in blob
    assert "wJalr" not in blob


def test_request_then_save_writes_env_not_status(client: TestClient, tmp_path) -> None:
    opened = client.post(
        "/secret-slots/request",
        json={"slot_id": "aws.credentials", "reason": "Deploy"},
    )
    assert opened.status_code == 200, opened.text
    pending = opened.json()
    assert pending["status"] == "pending"
    assert pending["env_refs"] == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    assert "AKIA" not in opened.text
    assert "wJalr" not in opened.text

    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    saved = client.post(
        "/secret-slots/aws.credentials/value",
        json={
            "values": {
                "AWS_ACCESS_KEY_ID": "AKIATESTEXAMPLE12",
                "AWS_SECRET_ACCESS_KEY": secret,
            }
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["armed"] is True
    assert secret not in saved.text
    assert "AKIATESTEXAMPLE12" not in saved.text
    from app.core.local_mode import is_local

    assert body["fingerprints"]["AWS_SECRET_ACCESS_KEY"] == ("set" if is_local() else secret[-4:])
    if is_local():
        from app.core.local_secrets import get_secret

        assert get_secret("publishing", "secret_access_key") == secret
        assert get_secret("publishing", "access_key_id") == "AKIATESTEXAMPLE12"
        assert not (tmp_path / ".env").exists()
        assert all(secret.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    else:
        env = (tmp_path / ".env").read_text()
        assert "AWS_SECRET_ACCESS_KEY=" in env
        assert secret in env

    status = client.get("/secret-slots/aws.credentials/status").json()
    assert status["armed"] is True
    assert secret not in json.dumps(status)


@pytest.mark.asyncio
async def test_mcp_tool_rejects_a_value_argument(client: TestClient) -> None:
    from app.mcp import tools

    result = await tools.call_tool("request_secret", {"slot_id": "openai.api_key", "value": "sk-secret-leak"})
    assert result["error"]["code"] == "value_not_allowed"
    assert "sk-secret-leak" not in json.dumps(result)


@pytest.mark.asyncio
async def test_mcp_tool_opens_form_only(client: TestClient) -> None:
    from app.mcp import tools

    result = await tools.call_tool("request_secret", {"provider": "aws", "reason": "Need AWS"})
    assert result["status"] == "pending"
    assert result["env_refs"] == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    assert "value" not in result.get("form", {}).get("fields", [{}])[0]


@pytest.mark.asyncio
async def test_chat_withholds_pasted_key(client: TestClient, tmp_path) -> None:
    from sqlalchemy import select

    from app.chat.router import handle_message
    from app.db.models import ChatMessage, Venture
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    now = utcnow()
    vid = new_id("ven")
    factory = db()
    with factory() as session:
        session.add(
            Venture(
                id=vid,
                workspace_id=None,
                name="SecretCo",
                raw_idea="secret test",
                stage="draft",
                autonomy_level="L1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    leak = "sk-proj-THISMUSTNEVERBELONGEDINCHAT999"
    reply = await handle_message(vid, leak)
    assert reply["kind"] == "needs_you"
    assert leak not in json.dumps(reply)
    with factory() as session:
        rows = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid)).all()
        blob = " ".join(r.body_md for r in rows)
    assert leak not in blob
    assert "withheld" in blob.lower()


def test_zernio_key_is_a_boundary_secret(client: TestClient) -> None:
    from app.boundary import Segment
    from app.boundary.detectors import patterns

    findings = patterns.scan(Segment(text="post with zn_abcdef0123456789ffff", path="chat"))
    assert any(f.rule == "zernio" for f in findings)


def test_vault_values_block_transmit(client: TestClient, tmp_path) -> None:
    from app.boundary.detectors.vault import clear_value_providers, known_values
    from app.connectors.secrets import reset_store, store
    from app.secrets.boundary import register_vault_boundary

    reset_store()
    clear_value_providers()
    register_vault_boundary()
    mystery = "unshaped-vault-secret-value-9999"
    store().put(workspace_id="wsp_x", provider="other", key="custom", value=mystery)
    assert mystery in known_values("wsp_x")
    clear_value_providers()
    reset_store()


def test_zernio_key_scrubbed_from_child_env(client: TestClient) -> None:
    from coding_agents.env import child_env, mark_company_os_owned

    mark_company_os_owned(["ZERNIO_API_KEY"])
    env = child_env({"ZERNIO_API_KEY": "sk_" + "ab" * 16, "PATH": "/usr/bin"})
    assert "ZERNIO_API_KEY" not in env
