"""CSS casing must not change semantic names or admit invisible approved copy."""
from pathlib import Path
import sys
import struct

import pytest
from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("domain", ["equipment.example", "gardening.example"])
def test_renderer_requires_loopback_authority(tmp_path, domain):
    from functools import partial
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    from threading import Thread

    load_company_os_app()
    from app.pagebuilder.rendering import _Handler

    (tmp_path / "index.html").write_text(domain)
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=tmp_path)) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            client.request("GET", "/")
            with client.getresponse() as response:
                assert (response.status, response.read()) == (200, domain.encode())
            client.request("HEAD", "/", headers={"Host": f"{domain}:{server.server_port}"})
            with client.getresponse() as response:
                assert response.status == 403
            client.request("POST", "/", headers={"Origin": f"http://{domain}"})
            with client.getresponse() as response:
                assert response.status == 403
        finally:
            client.close()
            server.shutdown()


@pytest.fixture(scope="module")
def renderer():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        executable = (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                      if sys.platform == "darwin" else Path(pw.firefox.executable_path if sys.platform == "linux" else pw.chromium.executable_path))
        if not executable.exists():
            pytest.skip("isolated browser is not installed in this unit-test environment")
    load_company_os_app()
    from app.pagebuilder.rendering import render_page
    return render_page


def test_visible_dom_copy_preserves_semantic_case_and_excludes_hidden_text(renderer, monkeypatch):
    import time
    from app.pagebuilder.rendering import _Handler
    original_get = _Handler.do_GET

    def delayed_lazy_image(handler):
        if handler.path == "/assets/late.svg":
            time.sleep(2.25)
        return original_get(handler)

    # A real lazy image must finish its bounded load before decode/DOM checks.
    monkeypatch.setattr(_Handler, "do_GET", delayed_lazy_image)
    result = renderer("""<!doctype html><html lang="en"><head><title>Semantic DOM contract</title>
        <style>h1,a{text-transform:uppercase}a{display:inline-block;padding:16px}body{font-size:16px}</style>
        </head><body><main><h1>Nightshift</h1><p>A synthetic renderer contract for preserving approved copy.</p>
        <div style="opacity:0"><span>Invisible product promise</span></div>
        <a id="hero_cta" href="#waitlist">Explore early access<span aria-hidden="true"> →</span></a>
        <section id="waitlist"><h2>Early access</h2></section>
        <div style="height:9000px" aria-hidden="true"></div>
        <img id="late_art" src="assets/late.svg" loading="lazy" width="200" height="150" alt="Synthetic lazy-image contract">
        </main></body></html>""", {"assets/late.svg": '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="#224433"/></svg>'})
    for observation in result["observations"].values():
        assert "Nightshift" in observation["visibleText"]
        assert "Invisible product promise" not in observation["visibleText"]
        assert observation["visibleControls"][0]["name"] == "Explore early access"
        assert {action["mode"] for action in observation["actions"]} == {"pointer", "keyboard"}
        assert all(action["passed"] for action in observation["actions"])
        assert observation["brokenImages"] == 0
        assert observation["visibleImages"][0]["id"] == "late_art"
    assert not result["passed"]  # One image: this is a contract fixture, not a gallery page.
    assert struct.unpack(">II", result["social_preview_png"][16:24]) == (1200, 630)


def rendering_fixture(renderer, css="", before_cta=""):
    artwork = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 900"><rect width="1200" height="900" fill="#224433"/></svg>'
    return renderer("""<!doctype html><html lang="en"><head><title>Native rendering checks</title><style>
        *{box-sizing:border-box}body{margin:0;font-size:16px;color:#111;background:linear-gradient(#eee,#ddd)}
        main{background:white;padding:16px}h1{font-size:32px}a{display:inline-block;padding:16px;color:#111;background:#eee;border-radius:999px}
        .image-box{width:240px;max-width:100%;overflow:hidden}img{display:block;width:100%;height:auto}
        .ink-surface{background:rgba(0,0,0,.1)}#disclosure{color:rgba(0,0,0,.8)}
        """ + css + """</style></head><body><main><section id="hero"><h1>A synthetic concept</h1>
        <div class="ink-surface"><p id="disclosure" aria-hidden="true">A proposed concept with readable disclosure text.</p></div>
        """ + before_cta + """<div class="hero-action"><a id="hero_cta" href="#waitlist">Explore early access</a></div>
        <div class="image-box"><img id="hero_art" src="assets/hero.svg" width="1200" height="900" alt="Synthetic hero artwork"></div>
        </section><section id="context"><p>Two original drawings support this synthetic rendering contract.</p>
        <div class="image-box"><img id="context_art" src="assets/context.svg" width="1200" height="900" alt="Synthetic contextual artwork"></div>
        </section><div style="height:900px" aria-hidden="true"></div><section id="waitlist"><h2>Early access</h2>
        <a id="waitlist_cta" href="#hero">Request early access</a></section></main></body></html>""",
        {"assets/hero.svg": artwork, "assets/context.svg": artwork})


def test_repair_fallback_passes_real_browser_and_copy_checks(renderer, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.pagebuilder.pipeline import fallback_html, fallback_plan
    from app.pagebuilder.models import PageInput
    from app.pagebuilder.qa import check_page
    inputs = PageInput(venture_id="ven_fallback", name="Coffee Shop", idea="A coffee shop with online table reservations.",
        waitlist_url="https://gateway.supymem.com/public/waitlists/ven_fallback")
    plan = fallback_plan(inputs)
    html = fallback_html(plan, inputs)
    files = {"index.html": html, **{asset.path: asset.svg for asset in plan.assets}}
    assert check_page(html, files, inputs, plan)["passed"]
    rendered = renderer(html, files)
    assert rendered["passed"], rendered["failures"]
    assert {"desktop.png", "mobile.png"} <= rendered["screenshots"].keys()
    for observation in rendered["observations"].values():
        assert observation["initialHeroCta"]["withinViewport"] and observation["initialHeroCta"]["visible"]
        assert len(observation["visibleImages"]) == 2


def test_solid_compositing_and_below_fold_waitlist_preserve_valid_layout(renderer):
    result = rendering_fixture(renderer)
    assert result["passed"], result["failures"]
    for observed in result["observations"].values():
        assert observed["contrastSkipped"] == 0 and observed["contrastChecked"] >= 6
        assert observed["contrastUnresolved"] == observed["contrastFailures"] == []
        assert observed["imageClippingFailures"] == [] and observed["checks"]["image_clipping"]
        assert observed["initialHeroCta"]["visible"] and observed["initialHeroCta"]["withinViewport"]
        assert observed["initialHeroCta"]["hitCoverage"]
        assert observed["checks"]["hero_cta_initial"] and observed["pageHeight"] > observed["viewport"]["height"]
        assert all(action["passed"] for action in observed["actions"])


@pytest.mark.parametrize("case,check", [("gradient", "contrast"), ("opacity", "contrast"), ("filter", "contrast"),
    ("clipping", "image_clipping"), ("cover", "image_clipping"), ("transform", "image_clipping"),
    ("late_hero", "hero_cta_initial"), ("clipped_hero", "hero_cta_initial")])
def test_actual_browser_rejects_unresolved_contrast_cropped_art_and_late_hero(renderer, case, check):
    css = {"gradient": ".ink-surface{background:linear-gradient(#1C1A1A,#2C2520)}#disclosure{color:#A34A28;font-size:14.4px}",
        "opacity": ".ink-surface{background:white;opacity:.1}#disclosure{color:black}",
        "filter": ".ink-surface{background:white;filter:opacity(.1)}#disclosure{color:black}",
        "clipping": "#hero .image-box{height:100px;display:flex;align-items:center}#hero_art{flex-shrink:0}",
        "cover": "#hero_art{height:100px;object-fit:cover}",
        "transform": "#hero .image-box{height:100px;transform:scale(.5);transform-origin:left top}",
        "clipped_hero": ".hero-action{height:10px;overflow:hidden}",
        "late_hero": ""}[case]
    result = rendering_fixture(renderer, css, '<div style="height:900px"></div>' if case == "late_hero" else "")
    assert not result["passed"]
    for viewport, observed in result["observations"].items():
        assert "render_" + check + ":" + viewport in result["failures"]
        assert not observed["checks"][check]
        if check == "contrast":
            diagnostic = next(item for item in observed["contrastUnresolved"] if item["id"] == "disclosure")
            assert diagnostic["reason"] == {"gradient": "background_image", "opacity": "group_opacity", "filter": "visual_effect"}[case]
            assert diagnostic["text"] == "A proposed concept with readable disclosure text."
            assert observed["contrastSkipped"] > 0 and observed["contrastChecked"] > 0
        elif check == "image_clipping":
            if case == "clipping":
                assert observed["imageClippingFailures"] == [{"id": "hero_art",
                    "ancestor": {"tag": "div", "id": "", "className": "image-box"}, "clipX": False, "clipY": True}]
            else:
                assert observed["imageClippingFailures"][0]["id"] == "hero_art"
                assert observed["imageClippingFailures"][0]["reason"] == ("object_fit_cover" if case == "cover" else "transformed_clip_ancestor")
        elif case == "late_hero":
            assert observed["initialHeroCta"]["top"] > observed["viewport"]["height"]
            assert not observed["initialHeroCta"]["withinViewport"]
            assert all(action["passed"] for action in observed["actions"])
        else:
            assert observed["initialHeroCta"]["withinViewport"] and observed["initialHeroCta"]["visible"]
            assert not observed["initialHeroCta"]["hitCoverage"]


def test_job_session_reuses_one_browser_across_preflight_and_two_renders(renderer, monkeypatch):
    from app.pagebuilder import rendering
    launch = rendering._launch_browser
    launches = []
    def counted(pw):
        launches.append(1)
        return launch(pw)
    monkeypatch.setattr(rendering, "_launch_browser", counted)
    with rendering.renderer_session():
        rendering.require_renderer()
        assert rendering_fixture(renderer)["passed"]
        assert rendering_fixture(renderer)["passed"]
    assert launches == [1]
    assert rendering._SESSION.get() is None
