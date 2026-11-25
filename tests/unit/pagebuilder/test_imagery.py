"""Illustrative artwork metering/fallback, safe CSS motion and the per-page cost cap, offline."""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.pagebuilder.test_pipeline import fixture_draft, fixture_html, fixture_input, reviewed_call, stub_render


@pytest.fixture(autouse=True)
def hosted_generation(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


@pytest.fixture(scope="module")
def modules():
    load_company_os_app()
    return {name: importlib.import_module("app.pagebuilder." + name)
            for name in ("models", "pipeline", "images", "qa", "api", "runtime")}


def webp(width=1536, height=1024, pad=0):
    body = (b"WEBP" + b"VP8X" + (10).to_bytes(4, "little") + b"\0" * 4
            + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little") + b"\0" * pad)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def raster_html(plan, inputs, imagery):
    html = fixture_html(plan, inputs)
    for image in imagery:
        asset = next(asset for asset in plan.assets if asset.id == image.replaces)
        html = html.replace(f'<img id="{asset.id}" src="{asset.path}" alt="{asset.alt}" width="1200" height="900">',
            f'<img id="{image.id}" src="{image.path}" alt="{image.alt}" width="{image.width}" height="{image.height}">')
    return html


def build(modules, image_call, html_for):
    models, pipeline = modules["models"], modules["pipeline"]
    inputs = models.PageInput.model_validate(fixture_input())
    plan = pipeline.prepare_plan(inputs, lambda *_: json.dumps(fixture_draft()))
    _, files = modules["images"].package_images(plan.assets)
    def call(stage, prompt):
        return json.dumps(fixture_draft()) if stage == "brief_direction_plan" else html_for(plan, inputs, prompt)
    return pipeline.generate_page(inputs, reviewed_call(call), render=stub_render, image_call=image_call)


def test_webp_header_is_parsed_and_non_webp_is_rejected(modules):
    images = modules["images"]
    assert images.webp_size(webp(1536, 1024)) == (1536, 1024)
    for bad in (b"\x89PNG" + b"\0" * 40, webp()[:-1], webp(pad=images.RASTER_MAX_BYTES)):
        with pytest.raises(ValueError, match="pagebuilder_image_format_invalid"):
            images.webp_size(bad)


def test_artwork_replaces_native_slots_with_honest_alt_contract_and_manifest(modules):
    calls = []
    def image_call(index, prompt, size):
        calls.append((index, size))
        assert "no text" in prompt.casefold() and "no people" in prompt.casefold()
        return {"data": webp(*map(int, size.split("x"))), "model": "gpt-image-2.5-flare"}
    def html_for(plan, inputs, _prompt):
        slots = [asset for asset in plan.assets if asset.role in {"hero", "section"}]
        return raster_html(plan, inputs, [modules["models"].RasterImage(id="artwork_" + str(index), replaces=asset.id,
            section_id=asset.section_id, path=f"assets/artwork_{index}.webp", alt=modules["images"].raster_alt(asset),
            width=1536 if index == 1 else 1024, height=1024, sha256="0" * 64, operation="image_" + str(index),
            model="gpt-image-2.5-flare") for index, asset in enumerate(slots, 1)])
    built = build(modules, image_call, html_for)
    assert calls == [(1, "1536x1024"), (2, "1024x1024")]
    assert [image.id for image in built.imagery] == ["artwork_1", "artwork_2"]
    assert [image.replaces for image in built.imagery] == ["garden_hero", "planting_art"]
    assert all(image.alt.startswith("Illustrative AI-generated image of ") for image in built.imagery)
    assert built.files["assets/artwork_1.webp"] == webp(1536, 1024)
    assert built.qa["passed"] and [visual["id"] for visual in built.qa["contract"]["visuals"]] == ["artwork_1", "artwork_2"]
    assert [item["operation"] for item in built.manifest["illustrative_imagery"]] == ["image_1", "image_2"]
    assert built.manifest["image_fallbacks"] == []


@pytest.mark.parametrize("code", ["pagebuilder_outcome_uncertain", "pagebuilder_provider_rejected", "pagebuilder_invalid_output"])
def test_failed_or_unknown_artwork_never_redials_and_keeps_native_svg(modules, code):
    from app.core.jobs import FatalError
    calls = []
    def image_call(index, prompt, size):
        calls.append(index)
        raise FatalError(code)
    built = build(modules, image_call, lambda plan, inputs, _prompt: fixture_html(plan, inputs))
    assert calls == [1] and built.imagery == [] and built.qa["passed"]
    assert built.manifest["image_fallbacks"] == [{"operation": "image_1", "fallback": "native_svg", "reason": code}]
    assert [visual["id"] for visual in built.qa["contract"]["visuals"]] == ["garden_hero", "planting_art"]


def test_partial_artwork_keeps_first_image_and_native_second_slot(modules):
    from app.core.jobs import RetryableError
    def image_call(index, prompt, size):
        if index == 2:
            raise RetryableError("pagebuilder_provider_rate_limited")
        return {"data": webp(), "model": "gpt-image-2.5-flare"}
    def html_for(plan, inputs, _prompt):
        hero = next(asset for asset in plan.assets if asset.role == "hero")
        image = modules["models"].RasterImage(id="artwork_1", replaces=hero.id, section_id="hero", path="assets/artwork_1.webp",
            alt=modules["images"].raster_alt(hero), width=1536, height=1024, sha256="0" * 64, operation="image_1", model="x" * 3)
        return raster_html(plan, inputs, [image])
    built = build(modules, image_call, html_for)
    assert [image.id for image in built.imagery] == ["artwork_1"] and built.qa["passed"]
    assert [visual["id"] for visual in built.qa["contract"]["visuals"]] == ["artwork_1", "planting_art"]


def test_lease_and_authority_failures_still_abort_the_build(modules):
    from app.core.jobs import FatalError
    def image_call(*_):
        raise FatalError("pagebuilder_lease_lost")
    with pytest.raises(FatalError, match="pagebuilder_lease_lost"):
        build(modules, image_call, lambda plan, inputs, _prompt: fixture_html(plan, inputs))


def test_qa_pins_artwork_alt_placement_and_presence(modules):
    models, pipeline, qa, images = (modules[key] for key in ("models", "pipeline", "qa", "images"))
    inputs = models.PageInput.model_validate(fixture_input())
    plan = pipeline.prepare_plan(inputs, lambda *_: json.dumps(fixture_draft()))
    _, files = images.package_images(plan.assets)
    hero = plan.assets[0]
    image = models.RasterImage(id="artwork_1", replaces=hero.id, section_id="hero", path="assets/artwork_1.webp",
        alt=images.raster_alt(hero), width=1536, height=1024, sha256="0" * 64, operation="image_1", model="gpt-image-2.5-flare")
    files[image.path] = webp()
    good = raster_html(plan, inputs, [image])
    assert qa.check_page(good, files, inputs, plan, [image])["passed"]
    assert "image_not_pinned_or_alt_invalid" in qa.check_page(
        good.replace(image.alt, "A photo of happy customers"), files, inputs, plan, [image])["failures"]
    assert "contextual_image_missing:artwork_1" in qa.check_page(fixture_html(plan, inputs), files, inputs, plan, [image])["failures"]
    tag = good[good.index('<img id="artwork_1"'):good.index(">", good.index('<img id="artwork_1"')) + 1]
    moved = good.replace(tag, "", 1).replace('<section id="planting">', '<section id="planting">' + tag, 1)
    assert "image_wrong_section:artwork_1" in qa.check_page(moved, files, inputs, plan, [image])["failures"]


@pytest.mark.parametrize("css,expected", [
    ("@keyframes up{from{opacity:0;transform:translateY(12px)}}@media (prefers-reduced-motion: no-preference){h1{animation:up .7s ease .1s both}}"
     "a{transition:background-color .2s}a:hover{transform:translateY(-2px)}", []),
    ("h1{animation:up .7s ease both}", ["motion_not_reduced_motion_guarded"]),
    ("@media (prefers-reduced-motion: no-preference){img{animation:float 3s ease-in-out infinite}}", ["motion_unbounded"]),
    ("@media (prefers-reduced-motion: no-preference){img{animation:float 3s ease-in-out 2 alternate}}", ["motion_too_long"]),
    ("@media (prefers-reduced-motion: no-preference){h2{animation:up 1s both;animation-timeline:view()}}", ["motion_scroll_driven"]),
    ("@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}", []),
])
def test_css_motion_must_be_reduced_motion_guarded_and_bounded(modules, css, expected):
    models, pipeline, qa, images = (modules[key] for key in ("models", "pipeline", "qa", "images"))
    inputs = models.PageInput.model_validate(fixture_input())
    plan = pipeline.prepare_plan(inputs, lambda *_: json.dumps(fixture_draft()))
    _, files = images.package_images(plan.assets)
    result = qa.check_page(fixture_html(plan, inputs).replace("</style>", css + "</style>"), files, inputs, plan)
    assert sorted(failure for failure in result["failures"] if failure.startswith("motion_")) == expected
    inline = fixture_html(plan, inputs).replace("<h1>", '<h1 style="animation:up 1s">', 1)
    assert "motion_not_reduced_motion_guarded" in qa.check_page(inline, files, inputs, plan)["failures"]
    assert "prefers-reduced-motion: no-preference" in pipeline.build_spec(plan, inputs)


def test_image_quote_keeps_worst_case_page_under_five_dollars(modules, monkeypatch):
    api = modules["api"]
    from app.credits.estimate import estimate_llm_micro
    monkeypatch.setattr(api, "MAX_PAGE_MICRO", 5_000_000)
    route = SimpleNamespace(api_key_ref=api.OPENAI_KEY_REF)
    text = estimate_llm_micro("gpt-6.1-sol", prompt_tokens=api.MAX_PROMPT_TOKENS, max_tokens=api.MAX_OUTPUT_TOKENS)
    image = api.image_quote(route, text)
    assert image["model"] == "gpt-image-2.5-flare" and image["calls"] == 3
    assert image["call_ceiling_micro"] <= 130_000  # $0.13 worst case per high-quality artwork
    assert text * api.CALLS_PER_VARIANT + image["call_ceiling_micro"] * image["calls"] < 5_000_000
    assert api.calls_per_variant({"image": image}) == 13 and api.calls_per_variant({"image": None}) == 10
    assert api.image_quote(SimpleNamespace(api_key_ref="workspace:byok"), text) is None
    monkeypatch.setattr(api, "MAX_PAGE_MICRO", text * api.CALLS_PER_VARIANT + 1)
    assert api.image_quote(route, text) is None  # artwork never pushes a page over the hard cap
    monkeypatch.setattr(api, "MAX_PAGE_MICRO", 5_000_000)
    monkeypatch.setenv("PAGEBUILDER_IMAGES", "0")
    assert api.image_quote(route, text) is None


def test_image_receipts_are_validated_against_the_image_quote(modules, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    runtime = modules["runtime"]
    from app.core.jobs import FatalError
    image = {"provider": "openai", "model": "gpt-image-2.5-flare", "provider_identity": "openai:pagebuilder_images",
             "call_ceiling_micro": 120_000}
    quote = {"image": image, "payer": "platform_credits", "account_id": "acct", "provider": "openai", "model": "gpt-6.1-sol",
             "provider_identity": "openai:sol", "call_ceiling_micro": 200_000, "ceiling_micro": 2_360_000, "call_budget": 13}
    doc = {"_runtime": {"quote": quote, "calls_used": 0, "operations": {}}}
    usage = {"provider": "openai", "model": "gpt-image-2.5-flare", "provider_identity": "openai:pagebuilder_images",
             "account_id": "acct", "source": "platform", "estimate_micro": 100_000}
    runtime.validate_usage(None, doc, None, usage)
    for change in ({"estimate_micro": 120_001}, {"source": "byok"}, {"account_id": "other"}):
        with pytest.raises(FatalError):
            runtime.validate_usage(None, doc, None, {**usage, **change})
    monkeypatch.setattr(runtime, "validate_inputs", lambda *_: None)
    doc["_runtime"]["operations"] = {"variant_0:image_1": {"state": "uncertain", "usage": {"estimate_micro": 2_200_000}}}
    with pytest.raises(FatalError, match="pagebuilder_ceiling_exhausted"):
        runtime.before_call(None, doc, None)  # an unknown artwork outcome still holds its ceiling


@pytest.mark.parametrize("kind", ["orbit_rings", "wave_bands", "dot_field", "sunburst", "stacked_blocks", "arch_window",
                                  "envelope", "calendar_page", "chat_bubble", "map_pin"])
def test_v2_motifs_compile_deterministically_with_seeded_backdrop(modules, kind):
    models, images = modules["models"], modules["images"]
    direction = models.DesignDirection.model_validate(fixture_draft()["direction"])
    draft = models.DraftImageAsset(id="hero_art", role="hero", section_id="hero", subject="An abstract motif test",
        prompt="Original abstract composition test prompt.", scene={"objects": [{"kind": kind, "x": 600, "y": 450, "scale": 1.5}]})
    asset = images.compile_asset(draft, direction)
    assert asset.compiler_version == "native_scene_v2" and images.compile_asset(draft, direction) == asset
    images.validate_derivation(asset, direction)
    other = images.compile_asset(draft, direction.model_copy(update={"concept": "A different venture concept"}))
    assert other.svg != asset.svg and other.alt == asset.alt
    with pytest.raises(ValueError, match="pagebuilder_scene_provenance_invalid"):
        images.compile_asset(draft, direction, "native_scene_v1")


def test_v1_candidates_still_verify_byte_for_byte(modules):
    models, images = modules["models"], modules["images"]
    direction = models.DesignDirection.model_validate(fixture_draft()["direction"])
    draft = models.DraftImageAsset(id="hero_art", role="hero", section_id="hero", subject="A leaf for compatibility",
        prompt="Original leaf composition test prompt.", scene={"objects": [{"kind": "leaf", "x": 600, "y": 450}]})
    legacy = images.compile_asset(draft, direction, "native_scene_v1")
    images.validate_derivation(legacy, direction)
    assert "fill-opacity" not in legacy.svg and "fill-opacity" in images.compile_asset(draft, direction).svg


def test_domain_motifs_need_the_confirmed_noun(modules):
    pipeline = modules["pipeline"]
    draft = fixture_draft()
    draft["assets"][1]["scene"]["objects"][0]["kind"] = "envelope"
    with pytest.raises(ValueError, match="pagebuilder_scene_context_unconfirmed"):
        pipeline.compile_draft_plan(fixture_input(), draft)
    confirmed = {**fixture_input(), "idea": fixture_input()["idea"] + " Families would share a weekly garden email."}
    assert pipeline.compile_draft_plan(confirmed, draft).assets[1].scene.objects[0].kind == "envelope"
    abstract = fixture_draft()
    abstract["assets"][1]["scene"]["objects"][0]["kind"] = "orbit_rings"
    assert pipeline.compile_draft_plan(fixture_input(), abstract).assets[1].alt.endswith("abstract concentric rings.")


def test_v2_geometry_never_trips_the_model_boundary_pii_scan(modules):
    """A live capability review was refused when space-separated SVG path numbers looked like phone/card numbers."""
    from app.boundary import Segment
    from app.boundary.detectors import pii
    models, images = modules["models"], modules["images"]
    for concept in ("A calm night-shift handoff board", "Margin notes for readers", "Afterimage film essay notebook"):
        direction = models.DesignDirection.model_validate({**fixture_draft()["direction"], "concept": concept})
        svgs = [images.compile_asset(models.DraftImageAsset(id=asset_id, role="hero", section_id="hero",
            subject="A motif scanning subject", prompt="Original motif scanning prompt for safety.",
            scene={"objects": [{"kind": kind, "x": 330, "y": 450, "scale": 1.4, "rotation": 7},
                               {"kind": kind, "x": 900, "y": 430, "scale": .9, "rotation": -12}]}), direction).svg
            for kind in sorted(images._RECIPES) for asset_id in ("hero_art", "section_art")]
        assert pii.scan(Segment(text="\n".join(svgs))) == []
    assert pii.scan(Segment(text="M-20 141 C300 64 900 210 1220 141"))  # the space-separated form tripped it


def test_founder_preview_fits_the_largest_allowed_artwork_set(modules):
    preview = importlib.import_module("app.api.pagebuilder_api")
    inlined = modules["pipeline"].RASTER_BUNDLE_BYTES * 4 // 3 + 250_000 + 200_000  # base64 art, max HTML, SVG/favicon slack
    assert inlined < preview.PREVIEW_DOCUMENT_MAX_BYTES


def test_image_prompts_omit_the_venture_name_and_quoted_phrases(modules):
    wordless = modules["pipeline"]._wordless
    assert wordless('Nightshift\'s "Handoff" board on a NIGHTSHIFT wall', "Nightshift") == "Handoff board on a wall"
    assert modules["images"].raster_prompt(modules["models"].DesignDirection.model_validate(fixture_draft()["direction"]),
        "a leaf", "a garden", wide=True).startswith("Wordless image: no text")


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_optional_images_use_saved_provider_key_and_endpoint(tmp_path, monkeypatch, domain):
    import base64
    import httpx
    from tests.integration.test_postgres_auth_security import service_modules

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    with service_modules("company_os"):
        from app.core.providers_local import save_settings
        from app.pagebuilder import local, runtime

        save_settings(provider="openai", base_url="https://provider.example.invalid/v1",
            api_key="synthetic-saved-image-key", models={"chat": "gpt-6.1-sol"})
        route = local.route("workspace_" + domain)
        quote = local.image_quote(route)
        assert quote and quote["provider_identity"].endswith(":images")
        def post(url, *, headers, json, timeout):
            assert url == "https://provider.example.invalid/v1/images/generations"
            assert headers["Authorization"] == "Bearer synthetic-saved-image-key"
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "data": [{"b64_json": base64.b64encode(webp()).decode()}],
                "usage": {"input_tokens": 10, "output_tokens": 100, "total_tokens": 110}})
        monkeypatch.setattr(httpx, "post", post)
        result = runtime._image_response("Original artwork for " + domain, "1536x1024", quote["model"], 30,
            workspace_id="workspace_" + domain)
        assert base64.b64decode(result.choices[0].message.content) == webp()
