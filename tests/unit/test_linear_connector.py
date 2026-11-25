"""Linear adapter + webhook freshness (T10.46–T10.47)."""

from __future__ import annotations

import time

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def connectors(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/lin.db")
    monkeypatch.setenv("CONNECTORS_MODE", "local")
    load_company_os_app()
    from app.connectors.fakes import reset_fakes
    from app.connectors.registry import KNOWN, get_connector, reset_registry

    reset_registry()
    reset_fakes()
    return get_connector, KNOWN


def test_registry_covers_github_and_linear(connectors) -> None:
    get_connector, known = connectors
    assert known == frozenset({"github", "linear"})
    for name in known:
        adapter = get_connector(name)
        assert adapter.provider == name
        assert adapter.scopes
        assert adapter.read_specs
        assert adapter.auth_start("wsp_x", [])


@pytest.mark.asyncio
async def test_every_adapter_refuses_execute_without_a_ticket(connectors) -> None:
    get_connector, known = connectors
    from app.connectors.protocol import Action, ConnectionView, TicketRequired

    for name in known:
        adapter = get_connector(name)
        if not adapter.write_capabilities:
            continue
        cap = adapter.write_capabilities[0]
        con = ConnectionView(
            id="con_x",
            workspace_id="wsp_x",
            provider=name,
            external_account="acme",
            status="active",
            auth_kind="pat",
            secret_ref="vault:x",
            scopes=(cap.required_scope,),
        )
        with pytest.raises(TicketRequired):
            await adapter.execute(con, Action(capability=cap.id, target="x"), ticket=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_linear_fake_lists_teams(connectors) -> None:
    get_connector, _ = connectors
    from app.connectors.protocol import ConnectionView

    adapter = get_connector("linear")
    con = ConnectionView(
        id="con_lin",
        workspace_id="wsp_x",
        provider="linear",
        external_account="acme",
        status="active",
        auth_kind="api_key",
        secret_ref="vault:lin",
        scopes=("teams:read",),
    )
    page = await adapter._perform_list(con, "team", None)
    refs = {item.ref for item in page.items}
    assert "team_eng" in refs


def test_linear_webhook_rejects_tamper_and_stale(connectors) -> None:
    get_connector, _ = connectors
    from app.connectors.linear import webhooks as lin
    from app.connectors.webhooks import WebhookRejected

    adapter = get_connector("linear")
    secret = "hook-secret"
    body, headers = adapter.simulate_delivery(secret)
    event = lin.verify(body, headers, secret)
    assert event.provider == "linear"

    tampered = dict(headers)
    tampered["Linear-Signature"] = "00" * 32
    with pytest.raises(WebhookRejected):
        lin.verify(body, tampered, secret)

    stale_ms = (time.time() * 1000) - (6 * 60 * 1000)
    stale_body, stale_headers = adapter.simulate_delivery(secret, sent_ms=stale_ms)
    with pytest.raises(WebhookRejected):
        lin.verify(stale_body, stale_headers, secret)
