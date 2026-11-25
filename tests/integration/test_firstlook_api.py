"""First-look generation + API hermetic tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.company_os_harness import company_os_test_client, load_company_os_app
from tests.firstlook_legacy import seed_legacy_page


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/fl.db")
    monkeypatch.setenv("LLM_OFFLINE", "true")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "fallback")
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    main = load_company_os_app()
    from app import state as state_mod
    from app.onboarding.registry import reset_steps_cache

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_steps_cache()
    with company_os_test_client(main.create_app()) as c:
        yield c


@pytest.mark.asyncio
async def test_generate_writes_html_and_site_version(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "data"))
    from app.firstlook.service import generate_first_look
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    seed_legacy_page(desk.id)
    result = await generate_first_look(
        desk.id,
        "A weekly digest of local farm produce for city cooks",
        venture_name="FarmNote",
        archetype="content",
    )
    assert result.version == 1
    assert result.url
    path = Path(result.path)
    assert path.exists()
    html = path.read_text(encoding="utf-8")
    assert "FarmNote" in html
    assert "Trusted by" not in html
    assert "data-layout=" in html

    r = client.get(f"/workspaces/{desk.id}/site")
    assert r.status_code == 200
    assert r.json()["version"] == 1


@pytest.mark.asyncio
async def test_interest_endpoint(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "data"))
    from app.firstlook.service import generate_first_look
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    seed_legacy_page(desk.id)
    await generate_first_look(
        desk.id,
        "Notify me about open tennis courts nearby",
        venture_name="CourtPing",
    )
    r = client.post(
        f"/w/{desk.id}/interest",
        data={"email": "a@b.co", "note": "want evenings", "site_version": 1},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_email"] is True
    assert body["site_version"] == 1
    assert "email" not in body

    encoded = client.post(
        f"/w/{desk.id}/interest",
        content="email=form%40test.co&note=evenings&source=direct&site_version=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert encoded.status_code == 201, encoded.text
    assert encoded.json()["has_email"] is True

    as_json = client.post(
        f"/w/{desk.id}/interest",
        json={"email": "json@test.co", "source": "direct", "site_version": 1},
    )
    assert as_json.status_code == 201, as_json.text
    assert as_json.json()["has_email"] is True


@pytest.mark.asyncio
async def test_try_another_look_is_free(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "data"))
    from app.firstlook.service import generate_first_look, try_another_look
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    seed_legacy_page(desk.id)
    idea = "A booking page for neighbourhood bakers"
    first = await generate_first_look(desk.id, idea, venture_name="BakerBook", archetype="local")
    second = await try_another_look(desk.id, idea, venture_name="BakerBook", archetype="local")
    assert second.version == 2
    assert second.variant_index == 1
    assert second.cost_usd == 0.0
    assert second.copy_hash == first.copy_hash
    assert second.brandkit_hash != first.brandkit_hash
    assert Path(first.path).read_text(encoding="utf-8") != Path(second.path).read_text(encoding="utf-8")


def test_hosted_css_survives_empty_disk(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "data"))
    from app.firstlook import versions as versions_mod
    from app.firstlook.serve import write_site
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    html = (
        '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head>'
        "<body><h1>Studiohour</h1><p>Book a room on a page.</p></body></html>"
    )
    css = "body{background:#f6f3ee;color:#1b1a17}"
    path, url = write_site(
        html,
        workspace_id=desk.id,
        version=1,
        prefer_http=True,
        extras={"styles.css": css},
    )
    versions_mod.persist(
        workspace_id=desk.id,
        stage="first_look",
        template="local_builder",
        archetype="landing",
        brandkit_hash="",
        copy_hash="abc123",
        copy={
            "headline": "Studiohour",
            "source": "local_builder",
            "page_html": html,
            "page_files": {"styles.css": css},
        },
        brandkit={},
        variant_index=0,
        path=str(path),
        url=url,
        sha256="deadbeef",
        fallback=False,
        published=True,
    )
    import shutil

    shutil.rmtree(tmp_path / "data" / "workspaces", ignore_errors=True)
    page = client.get(f"/w/{desk.id}/site/")
    assert page.status_code == 200, page.text
    assert "Studiohour" in page.text
    assert "background:#f6f3ee" in page.text
    asset = client.get(f"/w/{desk.id}/site/styles.css")
    assert asset.status_code == 200, asset.text
    assert "background:#f6f3ee" in asset.text


def test_old_public_site_version_does_not_borrow_draft_css(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path / "data"))
    from app.firstlook import versions as versions_mod
    from app.firstlook.serve import write_site
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    v1_html = (
        '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head>'
        "<body><h1>Studiohour</h1><p>First hosted look.</p></body></html>"
    )
    v2_html = (
        '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head>'
        "<body><h1>Studiohour</h1><p>Updated copy.</p></body></html>"
    )
    css = "body{font-family:Georgia,serif}"
    path1, url1 = write_site(v1_html, workspace_id=desk.id, version=1, prefer_http=True)
    versions_mod.persist(
        workspace_id=desk.id,
        stage="first_look",
        template="local_builder",
        archetype="landing",
        brandkit_hash="",
        copy_hash="v1",
        copy={"headline": "Studiohour", "page_html": v1_html, "page_files": {}},
        brandkit={},
        variant_index=0,
        path=str(path1),
        url=url1,
        sha256="v1",
        fallback=False,
        published=True,
    )
    path2, url2 = write_site(
        v2_html,
        workspace_id=desk.id,
        version=2,
        prefer_http=True,
        extras={"styles.css": css},
    )
    versions_mod.persist(
        workspace_id=desk.id,
        stage="first_look",
        template="local_builder",
        archetype="landing",
        brandkit_hash="",
        copy_hash="v2",
        copy={"headline": "Studiohour", "page_html": v2_html, "page_files": {"styles.css": css}},
        brandkit={},
        variant_index=0,
        path=str(path2),
        url=url2,
        sha256="v2",
        fallback=False,
    )
    import shutil

    shutil.rmtree(tmp_path / "data" / "workspaces", ignore_errors=True)
    page = client.get(f"/w/{desk.id}/site/?v=1")
    assert page.status_code == 200, page.text
    assert "First hosted look" in page.text
    assert "font-family:Georgia,serif" not in page.text
    assert client.get(f"/w/{desk.id}/site/styles.css?v=1").status_code == 404
