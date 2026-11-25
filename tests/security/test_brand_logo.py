"""Real logo uploads preserve previous assets and cannot serve active SVG content."""
import io

import pytest
from PIL import Image

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


def png():
    data = io.BytesIO()
    Image.new("RGB", (3, 3), "blue").save(data, format="PNG")
    return data.getvalue()


def test_logo_upload_reports_partial_preview_and_serves_safe_headers(client, monkeypatch):
    from app.firstlook import service
    from app.firstlook.brand_assets import custom_logo_path
    vid, wid, _ = venture(client)
    async def unavailable(*args, **kwargs):
        raise service.FirstLookError("preview_unavailable", "Test preview unavailable")
    monkeypatch.setattr(service, "generate_first_look", unavailable)
    path = f"/ventures/{vid}/brand/logo"
    response = request(client, "POST", path, files={"file": ("logo.png", png(), "image/png")})
    assert response.status_code == 200, response.text
    assert response.json()["site_refreshed"] is False
    assert "path" not in response.json()
    original = custom_logo_path(wid).read_bytes()
    bad = request(client, "POST", path, files={"file": ("logo.png", b"not an image", "image/png")})
    assert bad.status_code == 422
    assert custom_logo_path(wid).read_bytes() == original
    assert client.get(f"/w/{wid}/brand/logo").status_code == 404
    served = request(client, "GET", f"/workspaces/{wid}/brand/logo")
    assert served.status_code == 200
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in served.headers["content-security-policy"]
    assert request(client, "POST", path, "usr_bob", files={"file": ("logo.png", png(), "image/png")}).status_code == 404


@pytest.mark.parametrize("source", [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><use href="https://outside.example/file.svg#x"/></svg>',
    '<!DOCTYPE svg [<!ENTITY x "value">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://outside.example/x)"/></svg>',
])
def test_active_svg_is_rejected_before_existing_logo_changes(client, tmp_path, monkeypatch, source):
    from app.firstlook.brand_assets import save_logo
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    path = save_logo("wsp_logo", png(), "original.png")
    with pytest.raises(ValueError):
        save_logo("wsp_logo", source.encode(), "replacement.svg")
    assert path.read_bytes() == png()


def test_static_svg_with_internal_gradient_remains_supported(client, tmp_path, monkeypatch):
    from app.firstlook.brand_assets import save_logo
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="a"><stop offset="0" stop-color="red"/></linearGradient></defs><rect width="10" height="10" fill="url(#a)"/></svg>'
    path = save_logo("wsp_logo", svg, "logo.svg")
    assert path.read_bytes() == svg
