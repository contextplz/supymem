"""Native scene geometry and immutable derivation, with no model or browser calls."""
from __future__ import annotations

import importlib
from xml.etree import ElementTree as ET

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def modules():
    load_company_os_app()
    return {name: importlib.import_module("app.pagebuilder." + name) for name in ("models", "images", "qa")}


@pytest.fixture(autouse=True)
def archived_compiler(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


def direction(models):
    return models.DesignDirection(concept="Original object composition", palette_family="Navy white yellow",
        palette={"background": "#0B1A30", "text": "#FFFFFF", "primary": "#FFD54A",
                 "on_primary": "#0B1A30", "accent": "#FFD54A", "surface": "#14233E"},
        display_font="Georgia, serif", body_font="Arial, sans-serif", layout_archetype="Open composition",
        density="airy", motion="none", illustration_style="Crisp original contextual line work.",
        section_rhythm="Open sections with distinct conceptual illustrations.")


def draft(models, objects=None):
    return models.DraftImageAsset(id="hero_art", role="hero", section_id="hero",
        subject="Original contextual object illustration", prompt="Original conceptual composition with complete visible objects.",
        scene={"objects": objects or [{"kind": "leaf", "x": 600, "y": 450}]})


@pytest.mark.parametrize("subject", ["A shipping schedule for warehouse teams", "A hiking journal for families"])
def test_local_scene_compilation_requires_your_coding_tool(modules, monkeypatch, subject):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    models, images = modules["models"], modules["images"]
    source = draft(models)
    source.subject = subject
    with pytest.raises(ValueError, match="Settings > Keys and connections > Coding tools"):
        images.compile_asset(source, direction(models))


@pytest.mark.parametrize("kind", ["note_board", "filmstrip", "leaf", "barrel"])
def test_complete_native_objects_use_approved_navy_palette_and_external_alt(modules, kind):
    models, images, qa = (modules[key] for key in ("models", "images", "qa"))
    palette = direction(models)
    assert qa.contrast(palette.palette["text"], palette.palette["accent"]) < 4.5
    source = draft(models, [{"kind": kind, "x": 600, "y": 450, "scale": .9, "rotation": 17}])
    asset = images.compile_asset(source, palette)
    images.validate_derivation(asset, palette)
    assert asset.compiler_version == images.COMPILER_VERSION and asset.provenance == "model_scene_native_svg"
    assert asset.scene.model_dump() == source.scene.model_dump()
    assert (asset.width, asset.height, asset.path) == (1200, 900, "assets/hero_art.svg")
    assert images.compile_asset(source, palette).model_dump() == asset.model_dump()
    root = ET.fromstring(asset.svg)
    ns = "{" + images.SVG_NAMESPACE + "}"
    assert root.get("viewBox") == "0 0 1200 900" and "".join(root.itertext()) == ""
    assert root.find(ns + "title") is None and root.find(ns + "text") is None
    assert all(node.get("stroke") in {None, palette.palette["text"]} for node in root.iter())
    assert all(node.get("fill") != palette.palette["accent"] for node in root.iter() if node.get("stroke"))
    group = root.find(ns + "g")
    if kind == "note_board":
        assert len(group.findall(ns + "path")) == 6
    elif kind == "filmstrip":
        assert len([node for node in group.findall(ns + "rect") if node.get("width") == "22"]) == 12
        assert len([node for node in group.findall(ns + "rect") if node.get("width") == "166"]) == 3
    elif kind == "leaf":
        assert len(group.findall(ns + "path")) == 5 and group[0].get("d").endswith(" Z")
    else:
        assert len(group.findall(ns + "ellipse")) == 1 and len(group.findall(ns + "path")) == 3
    assert kind.replace("note_board", "board with note cards") in asset.alt
    packaged, files = images.package_images([asset])
    images.validate_derivation(packaged[0], palette)
    assert files[asset.path] == asset.svg


@pytest.mark.parametrize("objects", [
    [{"kind": "leaf", "x": 10, "y": 450}],
    [{"kind": "leaf", "x": 180, "y": 450, "rotation": 45}],
    [{"kind": "leaf", "x": 600, "y": 450}, {"kind": "barrel", "x": 601, "y": 450}],
])
def test_rotated_stroke_bounds_and_occlusion_are_repacked(modules, objects):
    models, images = modules["models"], modules["images"]
    source, palette = draft(models, objects), direction(models)
    asset = images.compile_asset(source, palette)
    assert asset.scene != source.scene
    assert images.compile_asset(source, palette) == asset
    images.validate_derivation(asset, palette)


def test_low_contrast_and_post_validation_pose_mutation_fail_closed(modules):
    models, images = (modules[key] for key in ("models", "images"))
    palette = direction(models)
    palette.palette["text"] = palette.palette["background"]
    with pytest.raises(ValueError, match="contrast_invalid"):
        images.compile_asset(draft(models), palette)
    source = draft(models)
    source.scene.objects[0].x = 0
    assert images.compile_asset(source, direction(models)).scene.objects[0].x == 600


@pytest.mark.parametrize("change", ["scene", "svg", "alt", "version", "provenance", "path", "dimensions", "palette"])
def test_exact_derivation_rejects_mutation_and_legacy_assets(modules, change):
    models, images = (modules[key] for key in ("models", "images"))
    palette = direction(models)
    asset = images.compile_asset(draft(models), palette)
    if change == "scene":
        asset.scene.objects[0].x += 20
    elif change == "svg":
        asset.svg = asset.svg.replace('r="9"', 'r="8"')
    elif change == "alt":
        asset.alt = "Invented description of a functioning product."
    elif change == "version":
        asset.compiler_version = "native_scene_other"
    elif change == "provenance":
        asset.provenance = "original_model_svg"
    elif change == "path":
        asset.path = "assets/unbound.svg"
    elif change == "dimensions":
        asset.width = 1199
    else:
        palette.palette["accent"] = "#FFC84D"
    with pytest.raises(ValueError, match="scene_(derivation|provenance)"):
        images.validate_derivation(asset, palette)


def test_invalid_stored_geometry_is_not_silently_normalized_during_verification(modules):
    models, images = modules["models"], modules["images"]
    palette = direction(models)
    asset = images.compile_asset(draft(models), palette)
    asset.scene.objects[0].x = 0
    with pytest.raises(ValueError, match="out_of_bounds"):
        images.validate_derivation(asset, palette)


def test_three_objects_derive_conservative_plural_alt_and_whole_plan_is_checked(modules):
    from tests.unit.pagebuilder.test_pipeline import fixture_plan
    models, images = (modules[key] for key in ("models", "images"))
    composition = draft(models, [{"kind": "leaf", "x": 220, "y": 230},
        {"kind": "leaf", "x": 610, "y": 230}, {"kind": "barrel", "x": 990, "y": 630, "scale": .7}])
    asset = images.compile_asset(composition, direction(models))
    assert asset.alt == "Original illustration of leaves and a barrel."
    data = fixture_plan()
    palette = models.DesignDirection.model_validate(data["direction"])
    data["assets"] = [images.compile_asset(models.DraftImageAsset(**{key: item[key] for key in
        ("id", "role", "section_id", "subject", "prompt")}, scene={"objects": [{"kind": "leaf", "x": 600, "y": 450}]}),
        palette).model_dump(mode="json") for item in data["assets"]]
    plan = models.PagePlan.model_validate(data)
    images.validate_compiled_assets(plan)
    plan.assets[1].scene = None
    with pytest.raises(ValueError, match="provenance_invalid"):
        images.validate_compiled_assets(plan)
