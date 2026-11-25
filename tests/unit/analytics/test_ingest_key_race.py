"""Ingest key get-or-create must not mint duplicate active public keys."""

from concurrent.futures import ThreadPoolExecutor


def test_ensure_ingest_key_is_single_active_under_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ingest_keys.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app import state
    from app.apps.keys import ensure_ingest_key, list_keys
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.db.supymem_models import FounderApp
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    from core.ids import new_id

    state.reset_db_factory()
    owner = "usr_ingest_" + new_id("x")[4:12]
    ensure_subject_person(owner)
    workspace = create_workspace(kind="company", name="Ingest race workspace", owner_id=owner)
    app_id = new_id("app")
    venture_id = new_id("ven")
    now = utcnow()
    with state.db()() as session:
        session.add(Venture(
            id=venture_id,
            workspace_id=workspace.id,
            name="Ingest race",
            raw_idea="race",
            stage="idea",
            created_at=now,
            updated_at=now,
        ))
        session.add(FounderApp(
            id=app_id,
            workspace_id=workspace.id,
            venture_id=venture_id,
            slug="ingest-race",
            name="Ingest race",
            kind="web",
            state="draft",
            created_at=now,
            updated_at=now,
        ))
        session.commit()

    def once():
        return ensure_ingest_key(app_id)["prefix"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        prefixes = list(pool.map(lambda _: once(), range(16)))

    assert len(set(prefixes)) == 1
    active = [
        row for row in list_keys(app_id)
        if row["state"] == "active" and row["prefix"].startswith("pk_app_")
    ]
    assert len(active) == 1
    assert active[0]["prefix"] == prefixes[0]
