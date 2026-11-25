"""Brand-kit quality: reject drawings, require real pixels."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from tests.company_os_harness import load_company_os_app


def _blob(path: Path) -> None:
    img = Image.new("RGB", (720, 960), (48, 42, 38))
    d = ImageDraw.Draw(img)
    d.ellipse((210, 140, 510, 460), fill=(198, 156, 130))
    img.save(path, quality=86)


def _photo(path: Path) -> None:
    img = Image.new("RGB", (800, 1000), (30, 34, 40))
    px = img.load()
    for y in range(1000):
        for x in range(800):
            px[x, y] = ((x * 7 + y * 3) % 180 + 40, (x * 3 + y * 5) % 160 + 30, (x + y) % 120 + 40)
    img.save(path, quality=88)


def test_rejects_ellipse_founder(tmp_path):
    load_company_os_app()
    from app.marketing.media.quality import looks_synthetic, reject_reason

    blob = tmp_path / "face.jpg"
    _blob(blob)
    assert looks_synthetic(blob, kind="founder")
    assert reject_reason("founder", blob)


def test_accepts_noisy_product_photo(tmp_path):
    load_company_os_app()
    from app.marketing.media.quality import looks_synthetic, reject_reason

    photo = tmp_path / "ui.jpg"
    _photo(photo)
    assert not looks_synthetic(photo, kind="product")
    assert reject_reason("product", photo) is None


def test_compose_still_blocks_without_kit(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/media.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/media.db")
    load_company_os_app()
    from app.marketing.media import compositor
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    out = compositor.compose_still(workspace_id=desk.id, headline="Real product. Real pixels.")
    assert out["ok"] is False
    assert out["error"] == "brand_kit_required"
