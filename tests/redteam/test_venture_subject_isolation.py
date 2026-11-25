"""Every venture/workspace route is scoped by the gateway session subject."""

from __future__ import annotations

import sys
import types
from datetime import timedelta

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/isolated.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)

    main = load_company_os_app()
    from app import state
    from fastapi.testclient import TestClient

    state.reset_db_factory()
    with TestClient(main.create_app()) as c:
        yield c


def _create(client, subject: str, idea: str) -> dict:
    response = client.post(
        "/ventures",
        json={"raw_idea": idea, "start": False},
        headers={"X-Subject": subject},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_portfolio_and_all_scoped_routes_are_subject_isolated(client):
    a = _create(
        client,
        "usr_alice",
        "A private planning workspace for independent architecture studios",
    )
    b = _create(
        client,
        "usr_bob",
        "A private scheduling workspace for mobile veterinary clinics",
    )

    alice_items = client.get(
        "/ventures", headers={"X-Subject": "usr_alice"}
    ).json()["items"]
    bob_items = client.get(
        "/ventures", headers={"X-Subject": "usr_bob"}
    ).json()["items"]
    assert {item["id"] for item in alice_items} == {a["id"]}
    assert {item["id"] for item in bob_items} == {b["id"]}

    # The central middleware protects every /ventures/{id}/... route, including
    # endpoints that do not declare their own Header dependency.
    for suffix in ("", "/dashboard", "/chat", "/tasks", "/agents/live"):
        denied = client.get(
            f"/ventures/{a['id']}{suffix}",
            headers={"X-Subject": "usr_bob"},
        )
        assert denied.status_code == 404, (suffix, denied.text)

    # Workspace-level settings and member data have the same ownership boundary.
    denied_workspace = client.get(
        f"/workspaces/{a['workspace_id']}",
        headers={"X-Subject": "usr_bob"},
    )
    assert denied_workspace.status_code == 404

    own = client.get(
        f"/ventures/{a['id']}",
        headers={"X-Subject": "usr_alice"},
    )
    assert own.status_code == 200


@pytest.mark.parametrize("path", ["/ventures", "/ventures/deleted"])
def test_portfolio_reads_preserve_owned_and_foreign_archives(client, path):
    from sqlalchemy import select

    from app.db.models import ChatMessage, Venture, VentureDocument, Workspace
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    expected = {}
    for subject in ("usr_alice", "usr_bob"):
        expected[subject] = {}
        for status in ("active", "recoverable", "expired", "purged"):
            item = _create(client, subject, f"Private {status} plans for {subject}")
            expected[subject][status] = item["id"]
            with db()() as session:
                venture = session.get(Venture, item["id"])
                workspace = session.get(Workspace, item["workspace_id"])
                venture.name = workspace.name = f"{subject} {status}"
                venture.stage = "purged" if status == "purged" else "idea"
                venture.created_at = workspace.created_at = now - timedelta(days=20)
                venture.updated_at = workspace.updated_at = now - timedelta(days=10)
                if status != "active":
                    age = 1 if status == "recoverable" else 8
                    venture.archived_at = workspace.archived_at = now - timedelta(days=age)
                workspace.settings_json = '{"customer_note":"Preserve my settings"}'
                session.add(ChatMessage(
                    id=f"chat-{item['id']}",
                    venture_id=item["id"],
                    workspace_id=item["workspace_id"],
                    role="user",
                    body_md=f"Private {status} conversation for {subject}",
                    created_at=now,
                ))
                session.add(VentureDocument(
                    id=f"doc-{item['id']}",
                    venture_id=item["id"],
                    kind="note",
                    title=f"{status} customer document",
                    body_md=f"Private {status} document for {subject}",
                    created_at=now,
                    updated_at=now,
                ))
                session.commit()

    def stored_rows():
        with db()() as session:
            return {
                model.__tablename__: session.execute(
                    select(model.__table__).order_by(model.id)
                ).mappings().all()
                for model in (Venture, Workspace, ChatMessage, VentureDocument)
            }

    before = stored_rows()
    for subject in ("usr_alice", "usr_bob", "usr_no_workspaces"):
        response = client.get(path, headers={"X-Subject": subject})
        assert response.status_code == 200, response.text
        status = "active" if path == "/ventures" else "recoverable"
        ids = {expected[subject][status]} if subject in expected else set()
        assert {item["id"] for item in response.json()["items"]} == ids
        assert stored_rows() == before
