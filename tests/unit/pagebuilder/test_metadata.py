"""Controller image metadata uses real raster bytes and leaves host binding explicit."""
from __future__ import annotations

import binascii
import importlib
import struct
import zlib

import pytest

from tests.company_os_harness import load_company_os_app


def png(width=1200, height=630):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", binascii.crc32(kind + value))
    pixels = (b"\x00" + b"\x42\x63\x40" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def metadata():
    load_company_os_app()
    return importlib.import_module("app.pagebuilder.metadata")


@pytest.mark.parametrize("topic", ['Factory "maintenance" scheduling', "Family pottery & weekends"])
def test_social_metadata_replaces_svg_and_leaves_no_invented_canonical_host(metadata, monkeypatch, topic):
    from html import escape

    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    source = ('<!doctype html><html><head><meta property="og:image" content="assets/hero.svg">'
              '<meta property="og:image:alt" content="old"><meta property="og:url" content="https://invented.test">'
              '<meta name="twitter:image" content="assets/hero.svg"></head><body>Approved copy</body></html>')
    html, manifest = metadata.add_social_preview(source, png(), topic)
    assert "assets/hero.svg" not in html and "invented.test" not in html
    assert html.count('property="og:image"') == html.count('name="twitter:image"') == 1
    assert escape(topic, quote=True) in html and "Approved copy" in html
    assert manifest["path"] == "assets/social-preview.png"
    assert manifest["canonical_url_binding"] == "required_before_final_source_qa"


@pytest.mark.parametrize("image", [b"not-png", png(390, 844)])
@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("topic", ["Factory maintenance scheduling", "Family pottery weekends"])
def test_social_metadata_refuses_invalid_or_wrong_size_image(metadata, monkeypatch, image, mode, topic):
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    message = "social_preview_invalid" if mode == "hosted" else "Settings > Keys and connections > Coding tools"
    with pytest.raises(ValueError, match=message):
        metadata.add_social_preview("<head></head>", image, topic)


@pytest.mark.parametrize("topic", ["Factory maintenance scheduling", "Family pottery weekends"])
@pytest.mark.parametrize("field,tag", [("headline", "h1"), ("subhead", "p"), ("button", "a")])
def test_local_copy_edit_leaves_replacements_to_your_tool(metadata, monkeypatch, topic, field, tag):
    from app.pagebuilder.qa import edit_applied, literal_copy_edit

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    before = f'<html><body><{tag}>{topic}</{tag}><form><input name="email"><button>Join</button></form></body></html>'.encode()
    instruction = f'Change the {field} to "Choose your next step"'
    copy = {"cta" if field == "button" else field: topic}
    assert literal_copy_edit(before, instruction, copy) is None
    after = before.replace(topic.encode(), b"Choose your next step")
    assert edit_applied(before, after, instruction)
    assert literal_copy_edit(before, f'Change the {field} to "{topic}"', copy) == ("cta" if field == "button" else field, topic, before)


@pytest.mark.parametrize("topic", ["Factory maintenance scheduling", "Family pottery weekends"])
def test_local_control_checker_does_not_repair_tool_source(metadata, monkeypatch, topic):
    from app.pagebuilder.qa import pin_control_names
    from core.static_qa_contracts import Action, Contract

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    contract = Contract(profile="static_landing_v1", product_name=topic,
        required_text=["Preview only. This page does not collect signups."],
        actions=[Action(id="waitlist_cta", kind="anchor", role="link", name="Get in touch", target="waitlist")])
    source = f'<section id="waitlist"><h2>{topic}</h2><a id="waitlist_cta" href="/old" aria-label="Old label">Get in touch</a></section>'
    assert pin_control_names(source, contract) == source


@pytest.mark.parametrize("topic", ['Factory "maintenance" scheduling', "Family pottery & weekends"])
def test_local_social_metadata_checks_tool_output_without_rewriting_it(metadata, monkeypatch, topic):
    from html import escape

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    required = metadata.social_preview_requirements(topic)
    tags = "".join(f'<meta {"property" if name.startswith("og:") else "name"}="{name}" content="{escape(value, quote=True)}">'
        for name, value in required.items())
    source = f'<html><head>{tags}</head><body>{escape(topic)}</body></html>'
    html, manifest = metadata.add_social_preview(source, png(), topic)
    assert html == source
    assert manifest["path"] == required["og:image"] == required["twitter:image"]
    assert manifest["canonical_url_binding"] == "required_before_final_source_qa"


@pytest.mark.parametrize("topic", ["Factory maintenance scheduling", "Family pottery weekends"])
@pytest.mark.parametrize("defect", ["missing", "wrong_image", "duplicate", "canonical", "missing_head"])
def test_local_social_metadata_needs_tool_repair_not_internal_replacement(metadata, monkeypatch, topic, defect):
    from html import escape

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    required = metadata.social_preview_requirements(topic)
    tags = "".join(f'<meta {"property" if name.startswith("og:") else "name"}="{name}" content="{escape(value, quote=True)}">'
        for name, value in required.items())
    tags = {"missing": "", "wrong_image": tags.replace("social-preview.png", "hero.svg"),
        "duplicate": tags + tags, "canonical": tags + '<meta property="og:url" content="https://invented.test">',
        "missing_head": tags}[defect]
    source = tags if defect == "missing_head" else f"<html><head>{tags}</head><body>{topic}</body></html>"
    with pytest.raises(ValueError, match="Settings > Keys and connections > Coding tools"):
        metadata.add_social_preview(source, png(), topic)
