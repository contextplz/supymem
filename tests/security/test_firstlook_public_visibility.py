"""Published versions are the only public HTML, asset and logo authority."""
from __future__ import annotations

import hashlib

import pytest

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers, make_workspace


def save_version(wid, tmp_path, *, marker, published=False, css=True):
    from app.firstlook import versions
    number = versions.next_version(wid)
    directory = tmp_path / wid / str(number)
    directory.mkdir(parents=True)
    body = f'<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body>{marker}<img src="/w/{wid}/brand/logo"></body></html>'
    path = directory / "index.html"
    path.write_text(body)
    return versions.persist(workspace_id=wid, stage="first_look", template="test", archetype="test",
        brandkit_hash="", copy_hash="", copy={"page_html": body, "page_files": {"styles.css": f"/* {marker} */ body{{color:red}}"} if css else {}},
        brandkit={"monogram": marker[:2]}, variant_index=0, path=str(path), url=f"/w/{wid}/site/",
        sha256=hashlib.sha256(body.encode()).hexdigest(), published=published)


@pytest.mark.parametrize("suffix", ["site/", "site/index.html", "site/styles.css", "brand/logo"])
def test_draft_and_missing_versions_are_never_public(client, tmp_path, suffix):
    wid = make_workspace(client, "usr_alice")
    save_version(wid, tmp_path, marker="PRIVATE_DRAFT")
    for params in ({}, {"v": 1}, {"v": 999}):
        response = client.get(f"/w/{wid}/{suffix}", params=params)
        assert response.status_code == 404
        assert "PRIVATE_DRAFT" not in response.text
        assert "no-store" in response.headers["cache-control"]


def test_latest_public_stays_published_and_never_borrows_draft_css(client, tmp_path):
    from app.firstlook import service
    wid = make_workspace(client, "usr_alice")
    save_version(wid, tmp_path, marker="PUBLIC_RELEASE", published=True, css=False)
    save_version(wid, tmp_path, marker="PRIVATE_NEWER")
    assert service.current_site(wid)["version"] == 2
    assert service.public_site(wid)["version"] == 1
    page = client.get(f"/w/{wid}/site/")
    assert page.status_code == 200
    assert "PUBLIC_RELEASE" in page.text and "PRIVATE_NEWER" not in page.text
    assert client.get(f"/w/{wid}/site/styles.css").status_code == 404
    assert client.get(f"/w/{wid}/site/?v=2").status_code == 404
    assert client.get(f"/w/{wid}/site/?v=999").status_code == 404


def test_published_assets_come_from_record_not_stale_disk_and_revoke_immediately(client, tmp_path):
    from app.db.models import SiteVersion
    from app.state import db
    wid = make_workspace(client, "usr_alice")
    row = save_version(wid, tmp_path, marker="PUBLIC_RELEASE", published=True)
    from pathlib import Path
    Path(row.path).write_text("PRIVATE_DISK_REPLACEMENT")
    (Path(row.path).parent / "styles.css").write_text("PRIVATE_DISK_CSS")
    page = client.get(f"/w/{wid}/site/")
    asset = client.get(f"/w/{wid}/site/styles.css")
    assert page.status_code == asset.status_code == 200
    assert "PUBLIC_RELEASE" in page.text and "PRIVATE_DISK" not in page.text
    assert "PUBLIC_RELEASE" in asset.text and "PRIVATE_DISK" not in asset.text
    assert "no-store" in asset.headers["cache-control"]
    assert client.get(f"/w/{wid}/site/unrecorded.js").status_code == 404
    with db()() as session:
        session.get(SiteVersion, row.id).published = 0
        session.commit()
    for suffix in ("site/", "site/styles.css", "brand/logo"):
        response = client.get(f"/w/{wid}/{suffix}", headers={"If-None-Match": '"previous"', "If-Modified-Since": "Fri, 11 Sep 2026 00:00:00 GMT"})
        assert response.status_code == 404
        assert "no-store" in response.headers["cache-control"]


def test_logo_uses_published_snapshot_not_mutable_draft_upload(client, tmp_path, monkeypatch):
    from app.firstlook.brand_assets import save_logo
    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    wid = make_workspace(client, "usr_alice")
    original = b'<svg xmlns="http://www.w3.org/2000/svg"><text>PUBLIC_LOGO</text></svg>'
    newer = b'<svg xmlns="http://www.w3.org/2000/svg"><text>PRIVATE_LOGO</text></svg>'
    save_logo(wid, original, "logo.svg")
    save_version(wid, tmp_path, marker="PUBLIC", published=True)
    save_logo(wid, newer, "logo.svg")
    save_version(wid, tmp_path, marker="PRIVATE")
    response = client.get(f"/w/{wid}/brand/logo")
    assert response.status_code == 200 and response.content == original
    assert client.get(f"/w/{wid}/brand/logo?v=2").status_code == 404
    path = f"/workspaces/{wid}/brand/logo"
    assert client.get(path).status_code == 401
    # Signed nonmembers receive the established workspace-concealing 404.
    denied = client.get(path, headers=headers(path, subject="usr_bob"))
    assert denied.status_code == 404
    assert "PRIVATE_LOGO" not in denied.text
    response = client.get(path, headers=headers(path))
    assert response.status_code == 200 and response.content == newer


def test_private_preview_requires_signed_member_and_keeps_draft_assets_inline(client, tmp_path):
    wid = make_workspace(client, "usr_alice")
    save_version(wid, tmp_path, marker="PRIVATE_PREVIEW")
    path = f"/workspaces/{wid}/site/preview"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers(path, subject="usr_bob")).status_code == 404
    response = client.get(path, headers=headers(path))
    assert response.status_code == 200, response.text
    assert "PRIVATE_PREVIEW" in response.json()["html"]
    assert "data:image/svg+xml;base64," in response.json()["html"]
    html = response.json()["html"]
    assert html.startswith('<meta http-equiv="Content-Security-Policy"')
    assert "script-src 'none';" in html
    assert "script-src 'unsafe-inline'" not in html
    assert "style-src 'unsafe-inline' data:;" in html
    assert "data:text/css;base64," in html or "body{color:red}" in html
    assert response.json()["published"] is False
    assert "no-store" in response.headers["cache-control"]
    metadata = f"/workspaces/{wid}/site"
    assert client.get(metadata, headers=headers(metadata)).json()["published"] is False
    assert client.get(f"/w/{wid}/site/").status_code == 404


def test_legacy_public_html_requires_unchanged_recorded_hash(client, tmp_path):
    import json
    from pathlib import Path
    from app.db.models import SiteVersion
    from app.state import db
    wid = make_workspace(client, "usr_alice")
    row = save_version(wid, tmp_path, marker="LEGACY_PUBLISHED", published=True)
    with db()() as session:
        session.get(SiteVersion, row.id).copy_json = json.dumps({"headline": "Legacy"})
        session.commit()
    assert "LEGACY_PUBLISHED" in client.get(f"/w/{wid}/site/").text
    Path(row.path).write_text("<html><body>PRIVATE_REPLACEMENT_LONG_ENOUGH_TO_RENDER</body></html>")
    response = client.get(f"/w/{wid}/site/")
    assert response.status_code == 404
    assert "PRIVATE_REPLACEMENT" not in response.text


def test_published_version_pins_js_images_and_css_dependencies(client, tmp_path):
    from html.parser import HTMLParser
    from app.firstlook import versions
    wid = make_workspace(client, "usr_alice")
    for version in (1, 2):
        marker = f"RELEASE_{version}"
        body = ('<!doctype html><html><head><link rel="stylesheet" href="./styles.css">'
                '<script src="app.js?cache=original"></script></head><body>'
                '<img src="hero.svg#mark" srcset="hero.svg 1x, ./hero.svg 2x">'
                '<a href="about.html">About</a><img src="introduced.svg"><div style="background:url(./hero.svg)">Page</div></body></html>')
        files = {"about.html": '<script src="app.js"></script><img src="hero.svg">', "app.js": f'window.release="{marker}";', "hero.svg": f'<svg>{marker}</svg>',
                 "styles.css": '@import "theme.css"; @import url("alternate.css"); .hero{background:url(he\\72o.svg#mark)}',
                 "theme.css": f'/* {marker} */ .theme{{background:url("./hero.svg")}}',
                 "alternate.css": f'/* {marker} */ .alternative{{background:url(hero.svg)}}'}
        if version == 2:
            files["introduced.svg"] = "<svg>RELEASE_2_ONLY</svg>"
        path = tmp_path / f"release-{version}.html"
        path.write_text(body)
        versions.persist(workspace_id=wid, stage="first_look", template="test", archetype="test",
            brandkit_hash="", copy_hash="", copy={"page_html":body,"page_files":files}, brandkit={},
            variant_index=0,path=str(path),url=f"/w/{wid}/site/",sha256=hashlib.sha256(body.encode()).hexdigest(),published=True)
    class Attributes(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows = []
        def handle_starttag(self, tag, attrs): self.rows.append((tag,dict(attrs)))
    html = client.get(f"/w/{wid}/site/?v=1").text
    parsed = Attributes()
    parsed.feed(html)
    script = next(attrs["src"] for tag,attrs in parsed.rows if tag == "script" and "src" in attrs)
    image = next(attrs for tag,attrs in parsed.rows if tag == "img")
    assert script == f"/w/{wid}/site/app.js?cache=original&v=1"
    assert image["src"] == f"/w/{wid}/site/hero.svg?v=1#mark"
    assert image["srcset"].count("v=1") == 2
    assert f'/w/{wid}/site/theme.css?v=1' in html
    assert f'/w/{wid}/site/alternate.css?v=1' in html
    assert f'/w/{wid}/site/hero.svg?v=1#mark' in html
    for target in (script, image["src"].split("#")[0], f"/w/{wid}/site/theme.css?v=1", f"/w/{wid}/site/alternate.css?v=1"):
        response = client.get(target)
        assert response.status_code == 200
        assert "RELEASE_1" in response.text and "RELEASE_2" not in response.text
    about_url = next(attrs["href"] for tag,attrs in parsed.rows if tag == "a")
    assert about_url == f"/w/{wid}/site/about.html?v=1"
    about = Attributes()
    about.feed(client.get(about_url).text)
    for tag, attrs in about.rows:
        if "src" in attrs:
            assert "v=1" in attrs["src"]
            assert "RELEASE_1" in client.get(attrs["src"]).text
    css = client.get(f"/w/{wid}/site/styles.css?v=1").text
    assert f'/w/{wid}/site/theme.css?v=1' in css
    assert f'/w/{wid}/site/alternate.css?v=1' in css
    assert f'/w/{wid}/site/hero.svg?v=1#mark' in css
    imported = client.get(f"/w/{wid}/site/theme.css?v=1").text
    assert f'/w/{wid}/site/hero.svg?v=1' in imported
    introduced = next(attrs["src"] for tag,attrs in parsed.rows if tag == "img" and "introduced" in attrs.get("src", ""))
    assert introduced == f"/w/{wid}/site/introduced.svg?v=1"
    assert client.get(introduced).status_code == 404
    assert client.get(f"/w/{wid}/site/introduced.svg").status_code == 200
    # Unversioned requests still select latest published; the v1 page never uses them.
    assert "RELEASE_2" in client.get(f"/w/{wid}/site/app.js").text


def test_public_documents_have_opaque_origin_and_native_waitlist(client, tmp_path):
    from app.firstlook.waitlist import public_interest_url
    from app.product_analytics.beacon import public_events_url
    wid = make_workspace(client, "usr_alice")
    save_version(wid, tmp_path, marker="PUBLIC", published=True)
    events = public_events_url()
    for suffix in ("site/", "site/index.html", "site/styles.css"):
        page = client.get(f"/w/{wid}/{suffix}")
        policy = page.headers["content-security-policy"]
        directives = dict((part.strip().split(" ", 1) + [""])[:2] for part in policy.split(";"))
        assert directives["sandbox"] == "allow-scripts allow-forms"
        # Anonymous beacon may POST path/host events; only the events endpoint is allowed.
        assert directives["connect-src"] == events
        assert directives["base-uri"] == directives["object-src"] == directives["frame-src"] == "'none'"
        assert directives["form-action"] == public_interest_url(wid)
        assert "allow-same-origin" not in policy
        assert page.headers["x-content-type-options"] == "nosniff"
    page = client.get(f"/w/{wid}/site/")
    assert "HTMLFormElement.prototype.submit.call(clone)" in page.text
    assert "new XMLHttpRequest" not in page.text
    result = client.post(f"/w/{wid}/interest", data={"email":"public-boundary@example.test", "site_version":"1"})
    assert result.status_code == 201 and result.json()["received"] is True
