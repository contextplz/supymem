"""Signed hosted requests cannot enter legacy host execution or shared-file lanes."""
import pytest
from tests.security.test_core_signed_boundary import client as client, headers, make_workspace

CASES = [
    ("demo", {"url": "http://169.254.169.254/latest/meta-data"}),
    ("demo", {"ingest_path": "/srv/data/marketing_media/other/private.mp4"}),
    ("demo", {"launch_recordly": True}),
    ("video", {"engine": "product_demo", "ingest_path": "/root/Movies/private.mp4"}),
    ("video", {"engine": "recordly", "launch_recordly": True}),
    ("image", {"allow_remote": True, "scene": "private founder input"}),
    ("browser", {"url": "file:///etc/passwd", "attach": True}),
    ("browser", {"url": "http://127.0.0.1:9222"}),
    ("desktop", {"app": "recordly"}),
    ("ads", {"platform": "meta", "approval_id": "forged"}),
]

@pytest.mark.parametrize("endpoint,payload", CASES)
def test_hosted_media_rejects_before_any_execution(client, monkeypatch, endpoint, payload):
    from app.marketing.media import service
    wid = make_workspace(client, "usr_alice")
    def forbidden(*args, **kwargs):
        pytest.fail("Hosted request reached legacy work or recording")
    for name in ("run_generate_image", "run_generate_video", "run_record_product_demo",
                 "run_operate_browser", "run_operate_app", "run_create_ad", "_record_job"):
        monkeypatch.setattr(service, name, forbidden)
    path = f"/api/v1/marketing/media/{endpoint}"
    assert client.post(path, json=payload, params={"workspace_id": wid}).status_code == 401
    response = client.post(path, json=payload, params={"workspace_id": wid}, headers=headers(path, "POST"))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "hosted_media_unavailable"

@pytest.mark.parametrize("path,payload", [
    ("/api/v1/marketing/lab/jobs", {"kind": "generate_video", "payload": {"ingest_path": "/shared/private.mp4"}}),
    ("/api/v1/marketing/ads/enable", {"platform": "meta", "approval_id": "forged"}),
])
def test_hosted_alias_cannot_enqueue_work(client, monkeypatch, path, payload):
    from app.marketing import lab
    wid = make_workspace(client, "usr_alice")
    def forbidden(*args, **kwargs):
        pytest.fail("Hosted alias admitted work")
    monkeypatch.setattr(lab, "enqueue_lab_jobs", forbidden)
    monkeypatch.setattr(lab, "enqueue_ads_enable_job", forbidden)
    response = client.post(path, json=payload, params={"workspace_id": wid}, headers=headers(path, "POST"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "hosted_media_unavailable"


def test_hosted_capabilities_never_probe_host_or_credentials(client, monkeypatch):
    from app.marketing.media import capabilities as caps
    wid = make_workspace(client, "usr_alice")
    def forbidden(*args, **kwargs):
        pytest.fail("Hosted capability read probed host or global credentials")
    for name in ("comfy_available", "_bfl_key", "_gemini_key", "ffmpeg_available", "google_ads_available", "meta_ads_available"):
        monkeypatch.setattr(caps, name, forbidden)
    monkeypatch.setattr(caps.recordly_bridge, "probe", forbidden)
    monkeypatch.setattr(caps.openclaw_bridge, "browser_available", forbidden)
    path = "/api/v1/marketing/media/capabilities"
    response = client.get(path, params={"workspace_id": wid}, headers=headers(path))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["execution_location"] == "hosted_server"
    assert result["local_first"] is False and result["legacy_execution_available"] is False
    assert result["image"]["compositor"]["ok"] is False
    assert "127.0.0.1" not in response.text and "connection refused" not in response.text.lower()


def test_scoped_upload_read_and_review_remain_available(client, monkeypatch, tmp_path):
    from app.marketing.media import store
    monkeypatch.setattr(store, "MEDIA_ROOT", tmp_path / "media")
    wid = make_workspace(client, "usr_alice")
    foreign = make_workspace(client, "usr_bob")
    path = "/api/v1/marketing/assets"
    response = client.post(path, params={"workspace_id": wid}, headers=headers(path, "POST"),
        files={"file": ("notes.pdf", b"%PDF-1.4 owned upload", "application/pdf")}, data={"kind": "other"})
    assert response.status_code == 200 and response.json()["ok"], response.text
    asset = response.json()["asset"]
    file_path = f"{path}/{asset['id']}/file"
    owned = client.get(file_path, params={"workspace_id": wid}, headers=headers(file_path))
    assert owned.status_code == 200 and owned.content == b"%PDF-1.4 owned upload"
    assert client.get(file_path, params={"workspace_id": foreign}, headers=headers(file_path, subject="usr_bob")).status_code == 404
    assert client.get(path, params={"workspace_id": foreign}, headers=headers(path)).status_code == 404
    review = f"{path}/{asset['id']}/review"
    assert client.post(review, params={"workspace_id": wid}, headers=headers(review, "POST"), json={"status": "approved"}).status_code == 200


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.asyncio
async def test_queued_media_adapters_block_without_signed_http(client, monkeypatch, environment):
    from app.marketing.adapters import media_adapters as adapters
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("APP_ENV", environment)
    def forbidden(*args, **kwargs):
        pytest.fail("Queued hosted work entered provider, browser or GUI")
    monkeypatch.setattr(adapters.image_gen, "generate_image", forbidden)
    monkeypatch.setattr(adapters.video_gen, "generate_video", forbidden)
    monkeypatch.setattr(adapters.recordly_bridge, "launch_gui", forbidden)
    monkeypatch.setattr(adapters.probes, "create_paused_ad", forbidden)
    monkeypatch.setattr(adapters.probes, "browser_probe", forbidden)
    for adapter in (adapters.ImageStudioAdapter, adapters.VideoStudioAdapter, adapters.DesktopOperatorAdapter,
                    adapters.BrowserOperatorAdapter, adapters.AdsAdapter, adapters.AdsMetaAdapter, adapters.AdsGoogleAdapter):
        outcome = await adapter().run({"workspace_id": "owned", "kind": "generate_video",
            "payload": {"launch_recordly": True, "ingest_path": "/shared/foreign.mp4"}})
        assert outcome["status"] == "blocked" and outcome["error"] == "hosted_media_unavailable"

@pytest.mark.asyncio
async def test_explicit_local_adapter_keeps_local_execution(client, monkeypatch):
    from app.marketing.adapters import media_adapters as adapters
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("APP_ENV", "local")
    calls = []
    def render(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status": "done"}
    monkeypatch.setattr(adapters.image_gen, "generate_image", render)
    outcome = await adapters.ImageStudioAdapter().run({"workspace_id": "owned", "payload": {"headline": "Owned photo"}})
    assert outcome["ok"] and calls[0]["workspace_id"] == "owned"
    assert calls[0]["allow_remote"] is False
