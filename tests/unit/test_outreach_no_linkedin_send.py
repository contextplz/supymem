"""OT-04: LinkedIn live send is structurally impossible."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app

ROOT = Path(__file__).resolve().parents[2]
REL = ROOT / "services" / "company_os" / "app" / "gtm" / "relationships"
FORBIDDEN = ("unipile_client.start_chat", "send_invitation")


def test_relationships_modules_do_not_reference_unipile_send():
    hits: list[str] = []
    for path in REL.glob("*.py"):
        src = path.read_text()
        for needle in FORBIDDEN:
            if needle in src:
                hits.append(f"{path.name}:{needle}")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "start_chat",
                "send_invitation",
            }:
                hits.append(f"{path.name}:attr.{node.attr}")
    assert hits == []


def test_tick_linkedin_never_calls_unipile(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot04.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/ot04.db")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.setenv("UNIPILE_API_KEY", "fake-key")
    load_company_os_app()
    from app.state import reset_db_factory
    from app.gtm.supymem.linkedin import enroll_linkedin, tick_linkedin
    from app.gtm.supymem.pipeline import ingest_leads

    def _explode(*_a, **_k):
        raise AssertionError("Unipile start_chat must not run")

    monkeypatch.setattr("app.gtm.supymem.unipile.start_chat", _explode)
    monkeypatch.setattr(
        "app.gtm.supymem.linkedin._unipile_send",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("tick_linkedin must not call _unipile_send")
        ),
    )
    reset_db_factory()
    ingest_leads(
        "ws_li",
        [
            {
                "first_name": "Ada",
                "email": "ada@sample-analytics.test",
                "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
                "email_confidence": "safe",
                "organization": "Analytical",
                "source": "test",
            }
        ],
        enrich=False,
    )
    enrolled = enroll_linkedin("ws_li")
    assert enrolled["enrolled"]
    tick = tick_linkedin("ws_li")
    assert tick["processed"]
    assert tick["processed"][0].get("simulated") is True
    assert tick["provider"] == "human_or_simulated"


def test_linkedin_actions_send_dm_raises_compliance_even_in_dry_run(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot45.db")
    monkeypatch.setenv("CONTENT_DRY_RUN", "true")
    monkeypatch.setenv("UNIPILE_API_KEY", "fake-key")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "fake-account")
    load_company_os_app()
    from app.content import linkedin_actions
    from app.gtm.relationships.compliance import ComplianceError

    def _explode(*_a, **_k):
        raise AssertionError("Unipile start_chat must not run")

    monkeypatch.setattr("app.gtm.supymem.unipile.start_chat", _explode)
    monkeypatch.setattr("app.gtm.supymem.unipile.send_invitation", _explode)
    with pytest.raises(ComplianceError):
        linkedin_actions.execute(
            workspace_id="ws_ot45",
            action="send_dm",
            body="hello from dry-run",
            target_urn="https://www.linkedin.com/in/example/",
        )
    with pytest.raises(ComplianceError):
        linkedin_actions._send_dm(
            workspace_id="ws_ot45",
            body="hello",
            target_urn="urn:li:person:1",
        )
    with pytest.raises(ComplianceError):
        linkedin_actions.execute(
            workspace_id="ws_ot45",
            action="send_connection",
            body="note",
            target_urn="https://www.linkedin.com/in/example/",
        )


def test_linkedin_actions_source_has_no_unipile_send_calls():
    src = (
        ROOT
        / "services"
        / "company_os"
        / "app"
        / "content"
        / "linkedin_actions.py"
    ).read_text()
    assert "unipile_client.start_chat" not in src
    assert "unipile_client.send_invitation" not in src
    assert "send_message_in_chat" not in src
