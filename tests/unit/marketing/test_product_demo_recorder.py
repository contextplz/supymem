"""Product demo recorder — mocked capture, no headed browser."""

from __future__ import annotations

from pathlib import Path

from tests.company_os_harness import load_company_os_app


def test_linkedin_url_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/demo.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/demo.db")
    load_company_os_app()
    from app.marketing.media.demo_recorder import record_product_demo

    out = record_product_demo(
        workspace_id="ws_test",
        url="https://www.linkedin.com/feed/",
        headline="nope",
    )
    assert out["ok"] is False
    assert out["error"] == "linkedin_host_blocked"


def test_file_scheme_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/demo.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/demo.db")
    load_company_os_app()
    from app.marketing.media.demo_recorder import validate_demo_url

    assert validate_demo_url("file:///etc/passwd") == "url_scheme_blocked"
    assert validate_demo_url("http://127.0.0.1:3100/content") is None


def test_scripted_demo_mocks_playwright_and_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/demo.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/demo.db")
    load_company_os_app()
    from app.marketing.media import demo_recorder
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    raw = tmp_path / "raw.webm"
    raw.write_bytes(b"webm-fake" * 50)
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"\x00" * 2000)

    def fake_capture(**kwargs):
        assert "linkedin" not in kwargs["url"]
        return {"ok": True, "path": str(raw), "title": "Console", "final_url": kwargs["url"]}

    def fake_encode(src, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00" * 2000)
        return {"ok": True, "path": str(dest)}

    monkeypatch.setattr(demo_recorder, "capture_walkthrough", fake_capture)
    monkeypatch.setattr(demo_recorder, "encode_mp4", fake_encode)
    monkeypatch.setattr(demo_recorder, "encode_short_clip", fake_encode)
    monkeypatch.setattr(
        demo_recorder,
        "ffmpeg_available",
        lambda: {"ok": True, "path": "/usr/bin/ffmpeg"},
    )

    desk = ensure_desk(LOCAL_PERSON_ID)
    out = demo_recorder.record_product_demo(
        workspace_id=desk.id,
        url="http://127.0.0.1:3100/content",
        headline="Content studio",
        seconds=12,
    )
    assert out["ok"] is True
    assert out["result"]["engine"] == "playwright_ffmpeg"
    assert Path(out["result"]["path"]).is_file()
    assert out["asset"]["review_status"] == "pending"


def test_ingest_rejects_etc_passwd(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/demo.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/demo.db")
    load_company_os_app()
    from app.marketing.media.demo_recorder import ingest_export

    out = ingest_export(
        workspace_id="ws_test",
        src=Path("/etc/passwd"),
        title="nope",
    )
    assert out["ok"] is False
    assert out["error"] == "ingest_path_rejected"


def test_ingest_recordly_mp4(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/demo.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/demo.db")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    src = downloads / "recordly.mp4"
    src.write_bytes(b"\x00" * 4000)
    monkeypatch.setenv("RECORD_DEMO_INGEST_ROOT", str(downloads))
    load_company_os_app()
    from app.marketing.media.demo_recorder import ingest_export
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    out = ingest_export(
        workspace_id=desk.id,
        src=src,
        title="Recordly export",
        source="recordly_export",
    )
    assert out["ok"] is True
    assert out["result"]["engine"] == "recordly_export"
    assert Path(out["result"]["path"]).is_file()


def test_recordly_bridge_never_claims_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDLY_VENDOR", str(tmp_path / "missing"))
    load_company_os_app()
    from app.marketing.adapters import recordly_bridge

    info = recordly_bridge.probe()
    assert info["license"] == "AGPL-3.0"
    assert info["in_process"] is False
    assert info["cli"] is False


def test_video_adapter_routes_product_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/demo.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/demo.db")
    load_company_os_app()
    import asyncio

    from app.marketing.adapters.media_adapters import VideoStudioAdapter
    from app.marketing.media import demo_recorder
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    called = {}

    def fake_record(**kwargs):
        called.update(kwargs)
        return {"ok": True, "status": "done", "result": {"engine": "playwright_ffmpeg"}}

    monkeypatch.setattr(demo_recorder, "record_product_demo", fake_record)
    adapter = VideoStudioAdapter()
    out = asyncio.run(
        adapter.run(
            {
                "workspace_id": desk.id,
                "kind": "generate_video",
                "payload": {"engine": "product_demo", "url": "http://127.0.0.1:3100/media"},
            }
        )
    )
    assert out["ok"] is True
    assert called["url"] == "http://127.0.0.1:3100/media"
