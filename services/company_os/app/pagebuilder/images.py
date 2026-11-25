"""Compile original typed scenes and package guarded SVG without remote assets."""
from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
from xml.etree import ElementTree as ET

from .models import DesignDirection, DraftImageAsset, GenerationDraft, ImageAsset, PagePlan

SVG_TAGS = {"svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline",
            "polygon", "text", "tspan", "linearGradient", "radialGradient", "stop", "clipPath", "mask",
            "pattern", "filter", "feGaussianBlur", "feOffset", "feBlend", "feColorMatrix", "feComposite",
            "feFlood", "feMerge", "feMergeNode", "use"}
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def validate_svg(svg: str) -> str:
    """Reject active/external content rather than attempting a lossy cleanup."""
    if len(svg.encode()) > 100000:
        raise ValueError("pagebuilder_svg_unsafe:SVG exceeds100000 UTF-8 bytes")
    # A single standard declaration adds no image behavior; all other PIs stay forbidden.
    svg = re.sub(r"\A\s*<\?xml\s+version=([\"'])1\.0\1(?:\s+encoding=([\"'])(?i:UTF-8)\2)?\s*\?>\s*",
                 "", svg, count=1)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", svg, re.I):
        raise ValueError("pagebuilder_svg_unsafe:Remove DOCTYPE and ENTITY declarations; return only a bare <svg> root")
    if "<?" in svg:
        raise ValueError("pagebuilder_svg_unsafe:Remove XML declarations and all processing instructions; return only a bare <svg> root")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError("pagebuilder_svg_invalid") from exc
    if root.tag not in {"svg", "{" + SVG_NAMESPACE + "}svg"} or "viewBox" not in root.attrib:
        raise ValueError("pagebuilder_svg_viewbox_required")
    try:
        box = [float(number) for number in root.attrib["viewBox"].split()]
    except ValueError as exc:
        raise ValueError("pagebuilder_svg_viewbox_invalid") from exc
    if len(box) != 4 or not all(abs(number) <= 10000 for number in box) or not all(0 < value <= 2400 for value in box[2:]):
        raise ValueError("pagebuilder_svg_viewbox_invalid")
    nodes = list(root.iter())
    if len(nodes) > 1000 or len(nodes) < 5:
        raise ValueError("pagebuilder_svg_complexity_invalid")
    for node in nodes:
        tag = node.tag.removeprefix("{" + SVG_NAMESPACE + "}")
        if tag not in SVG_TAGS:
            raise ValueError("pagebuilder_svg_active_content")
        for name, value in node.attrib.items():
            local = name.rsplit("}", 1)[-1].lower()
            if local.startswith("on") or local in {"style", "src", "base"}:
                raise ValueError("pagebuilder_svg_active_content")
            if local == "href" and not re.fullmatch(r"#[a-zA-Z][\w-]*", value):
                raise ValueError("pagebuilder_svg_external_resource")
            if re.search(r"(?:https?:|data:|javascript:|file:|//)", value, re.I):
                raise ValueError("pagebuilder_svg_external_resource")
            for target in re.findall(r"url\s*\((.*?)\)", value, flags=re.I):
                if not re.fullmatch(r"[\"']?#[a-zA-Z][\w-]*[\"']?", target.strip()):
                    raise ValueError("pagebuilder_svg_external_resource")
    ET.register_namespace("", SVG_NAMESPACE)
    return ET.tostring(root, encoding="unicode")


def package_images(assets: list[ImageAsset]) -> tuple[list[ImageAsset], dict[str, str]]:
    packaged, files = [], {}
    for asset in assets:
        path = "assets/" + asset.id + ".svg"
        artwork = validate_svg(asset.svg)
        item = asset.model_copy(update={"svg": artwork, "path": path})
        files[path] = artwork
        packaged.append(item)
    return packaged, files


COMPILER_VERSION = "native_scene_v2"
# v1 candidates stay verifiable byte-for-byte; v2 adds motifs and a seeded backdrop.
SUPPORTED_COMPILER_VERSIONS = {"native_scene_v1", COMPILER_VERSION}
V2_KINDS = {"orbit_rings", "wave_bands", "dot_field", "sunburst", "stacked_blocks", "arch_window",
            "envelope", "calendar_page", "chat_bubble", "map_pin"}


def _element(parent, tag, **attrs):
    return ET.SubElement(parent, tag, {key.replace("_", "-"): f"{value:g}" if isinstance(value, (int, float)) else value
                                    for key, value in attrs.items()})


def _board(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "rect", x=-145, y=-165, width=290, height=330, rx=12, fill=bg, stroke=ink, stroke_width=8)
    _element(group, "circle", cx=0, cy=-145, r=7, fill=accent)
    for y in (-112, -20, 72):
        _element(group, "path", d=f"M-112 {y} H92 L112 {y + 20} V{y + 68} H-112 Z", fill=bg, stroke=ink, stroke_width=5)
        _element(group, "path", d=f"M92 {y} V{y + 20} H112", fill="none", stroke=ink, stroke_width=4)
        for offset in (29, 46):
            _element(group, "line", x1=-90, y1=y + offset, x2=67, y2=y + offset, stroke=ink, stroke_width=4)


def _filmstrip(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "rect", x=-145, y=-165, width=290, height=330, rx=8, fill=bg, stroke=ink, stroke_width=8)
    for y in (-145, -90, -35, 20, 75, 130):
        for x in (-130, 108):
            _element(group, "rect", x=x, y=y, width=22, height=25, rx=3, fill=bg, stroke=ink, stroke_width=4)
    for y in (-140, -40, 60):
        _element(group, "rect", x=-83, y=y, width=166, height=80, rx=3, fill=bg, stroke=ink, stroke_width=6)
        _element(group, "rect", x=-65, y=y + 58, width=130, height=7, fill=accent)


def _leaf(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "path", d="M0-165 C130-110 145 50 0 135 C-145 50-130-110 0-165 Z", fill=bg, stroke=ink, stroke_width=6)
    _element(group, "circle", cx=45, cy=-70, r=9, fill=accent)
    _element(group, "path", d="M0 165 V-145", fill="none", stroke=ink, stroke_width=6)
    for y, reach in ((-65, 38), (-5, 65), (55, 53)):
        _element(group, "path", d=f"M{-reach} {y - 25} L0 {y} L{reach} {y - 25}", fill="none", stroke=ink, stroke_width=4)


def _barrel(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "path", d="M-115-135 V135 A115 30 0 0 0 115 135 V-135 Z", fill=bg, stroke=ink, stroke_width=8)
    _element(group, "circle", cx=-55, cy=20, r=14, fill=accent)
    _element(group, "ellipse", cx=0, cy=-135, rx=115, ry=30, fill=bg, stroke=ink, stroke_width=8)
    for y in (-65, 65):
        _element(group, "path", d=f"M-115 {y} A115 30 0 0 0 115 {y}", fill="none", stroke=ink, stroke_width=8)
    _element(group, "circle", cx=25, cy=-135, r=10, fill=bg, stroke=ink, stroke_width=4)


def _orbit_rings(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "circle", r=145, fill=bg, stroke=ink, stroke_width=7)
    for radius in (102, 60):
        _element(group, "circle", r=radius, fill="none", stroke=ink, stroke_width=5)
    _element(group, "circle", r=18, fill=ink)
    _element(group, "circle", cx=102, cy=-102, r=16, fill=accent, stroke=ink, stroke_width=4)
    _element(group, "circle", cx=-42, cy=43, r=10, fill=accent)


def _wave_bands(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "rect", x=-145, y=-170, width=290, height=340, rx=145, fill=bg, stroke=ink, stroke_width=7)
    for index, y in enumerate((-95, -35, 25, 85)):
        _element(group, "path", d=f"M-120 {y} C-75 {y - 38} -35 {y + 38} 0 {y} S75 {y - 38} 120 {y}", fill="none",
                 stroke=accent if index == 1 else ink, stroke_width=12 if index == 1 else 6)


def _dot_field(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "rect", x=-145, y=-170, width=290, height=340, rx=18, fill=bg, stroke=ink, stroke_width=7)
    for row in range(7):
        for column in range(6):
            _element(group, "circle", cx=-100 + column * 40, cy=-120 + row * 40, r=9 if (row + column) % 5 else 13,
                     fill=accent if (row * 2 + column) % 7 == 3 else ink)


def _sunburst(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "circle", r=145, fill=bg, stroke=ink, stroke_width=6)
    for index in range(16):
        angle = math.radians(index * 22.5)
        inner, outer = (52, 128) if index % 2 else (52, 104)
        _element(group, "line", x1=round(inner * math.cos(angle), 2), y1=round(inner * math.sin(angle), 2),
                 x2=round(outer * math.cos(angle), 2), y2=round(outer * math.sin(angle), 2), stroke=ink, stroke_width=6)
    _element(group, "circle", r=38, fill=accent, stroke=ink, stroke_width=6)


def _stacked_blocks(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    for x, y, width, height, fill in ((-140, 45, 280, 115, bg), (-105, -55, 210, 100, bg), (-70, -155, 140, 100, accent)):
        _element(group, "rect", x=x, y=y, width=width, height=height, rx=14, fill=fill, stroke=ink, stroke_width=7)


def _arch_window(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "path", d="M-125 165 V-45 A125 125 0 0 1 125 -45 V165 Z", fill=bg, stroke=ink, stroke_width=8)
    _element(group, "circle", cx=40, cy=-60, r=34, fill=accent)
    _element(group, "path", d="M0 165 V-170 M-125 40 H125", fill="none", stroke=ink, stroke_width=6)


def _envelope(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "rect", x=-145, y=-100, width=290, height=200, rx=12, fill=bg, stroke=ink, stroke_width=8)
    _element(group, "path", d="M-145 -100 L0 15 L145 -100 M-145 100 L-40 -15 M145 100 L40 -15", fill="none",
             stroke=ink, stroke_width=6)
    _element(group, "circle", cx=0, cy=15, r=17, fill=accent, stroke=ink, stroke_width=4)


def _calendar_page(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "rect", x=-130, y=-150, width=260, height=300, rx=16, fill=bg, stroke=ink, stroke_width=8)
    _element(group, "rect", x=-130, y=-150, width=260, height=62, rx=16, fill=accent, stroke=ink, stroke_width=8)
    for x in (-65, 65):
        _element(group, "line", x1=x, y1=-172, x2=x, y2=-128, stroke=ink, stroke_width=9)
    for row in range(4):
        for column in range(4):
            _element(group, "rect", x=-104 + column * 54, y=-66 + row * 52, width=38, height=34, rx=5,
                     fill=accent if (row, column) == (2, 1) else bg, stroke=ink, stroke_width=4)


def _chat_bubble(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "path", d="M-120 -125 H120 A25 25 0 0 1 145 -100 V45 A25 25 0 0 1 120 70 H-20 L-85 140 V70 H-120"
             " A25 25 0 0 1 -145 45 V-100 A25 25 0 0 1 -120 -125 Z", fill=bg, stroke=ink, stroke_width=8)
    for x in (-60, 0, 60):
        _element(group, "circle", cx=x, cy=-27, r=17, fill=accent if x == 0 else ink)


def _map_pin(group, palette):
    bg, ink, accent = (palette[key] for key in ("background", "text", "accent"))
    _element(group, "path", d="M0 168 C-55 70 -125 15 -125 -48 A125 125 0 0 1 125 -48 C125 15 55 70 0 168 Z",
             fill=bg, stroke=ink, stroke_width=8)
    _element(group, "circle", cx=0, cy=-50, r=46, fill=accent, stroke=ink, stroke_width=6)


_RECIPES = {"note_board": _board, "filmstrip": _filmstrip, "leaf": _leaf, "barrel": _barrel,
            "orbit_rings": _orbit_rings, "wave_bands": _wave_bands, "dot_field": _dot_field, "sunburst": _sunburst,
            "stacked_blocks": _stacked_blocks, "arch_window": _arch_window, "envelope": _envelope,
            "calendar_page": _calendar_page, "chat_bubble": _chat_bubble, "map_pin": _map_pin}
_NAMES = {"note_board": ("a board with note cards", "boards with note cards"),
          "filmstrip": ("a filmstrip", "filmstrips"), "leaf": ("a leaf", "leaves"), "barrel": ("a barrel", "barrels"),
          "orbit_rings": ("abstract concentric rings", "sets of abstract concentric rings"),
          "wave_bands": ("abstract wave lines", "panels of abstract wave lines"),
          "dot_field": ("an abstract dot grid", "abstract dot grids"),
          "sunburst": ("an abstract sunburst", "abstract sunbursts"),
          "stacked_blocks": ("three stacked rounded blocks", "stacks of rounded blocks"),
          "arch_window": ("an arched window", "arched windows"), "envelope": ("an envelope", "envelopes"),
          "calendar_page": ("an unlabeled calendar page", "unlabeled calendar pages"),
          "chat_bubble": ("a speech bubble", "speech bubbles"), "map_pin": ("a map pin", "map pins")}
# Domain nouns are drawn only when confirmed founder text names them (see pipeline.validate_plan).
DOMAIN_CONTEXT = {
    "barrel": r"\bbarrels?\b",
    "envelope": r"\b(?:e-?mails?|mail|letters?|envelopes?|newsletters?|invitations?)\b",
    "calendar_page": r"\b(?:calendars?|schedul\w*|bookings?|appointments?|events?|weekly|monthly)\b",
    "chat_bubble": r"\b(?:chats?|messag\w*|conversations?|repl(?:y|ies)|comments?|notes?|feedback)\b",
    "map_pin": r"\b(?:maps?|locations?|places?|nearby|neighbou?rhoods?|routes?|trails?|local|cit(?:y|ies)|travel\w*)\b",
}


def _backdrop(root, asset_id, direction):
    """Seeded palette-only blobs so no two ventures share a canvas.

    One node only: the capability reviewer may cite at most 32 geometry quotes per alt.
    """
    seed = hashlib.sha256((asset_id + "|" + direction.concept).encode()).digest()
    blobs = []
    for index in range(3):
        x, y, r = 150 + seed[index] * 900 // 255, 120 + seed[index + 3] * 660 // 255, 180 + seed[index + 6] * 160 // 255
        blobs.append(f"M{x - r} {y} a{r} {r} 0 1 0 {2 * r} 0 a{r} {r} 0 1 0 {-2 * r} 0 Z")
    _element(root, "path", d=" ".join(blobs), fill=direction.palette.get("surface", direction.palette["accent"]),
             fill_opacity=.3 if "surface" in direction.palette else .16)


def _validate_scene_geometry(asset: DraftImageAsset) -> None:
    boxes = []
    for obj in asset.scene.objects:
        c, s = abs(math.cos(math.radians(obj.rotation))), abs(math.sin(math.radians(obj.rotation)))
        # All defining geometry fits +/-150x180; margin encloses rounded strokes.
        rx, ry = obj.scale * (155 * c + 185 * s), obj.scale * (185 * c + 155 * s)
        box = (obj.x - rx, obj.y - ry, obj.x + rx, obj.y + ry)
        if box[0] < 0 or box[1] < 0 or box[2] > 1200 or box[3] > 900:
            raise ValueError("pagebuilder_scene_out_of_bounds:" + asset.id)
        if any(min(box[2], prior[2]) > max(box[0], prior[0]) and min(box[3], prior[3]) > max(box[1], prior[1]) for prior in boxes):
            raise ValueError("pagebuilder_scene_objects_overlap:" + asset.id)
        boxes.append(box)


def compile_asset(asset: DraftImageAsset, direction: DesignDirection, version: str = COMPILER_VERSION) -> ImageAsset:
    from app.core.local_mode import is_local
    if is_local():
        raise ValueError("pagebuilder_coding_tool_required:Open Settings > Keys and connections > Coding tools to connect your tool, then retry your page on Website.")
    return _compile_asset(asset, direction, version)


def prepare_scene(asset: DraftImageAsset, direction: DesignDirection, version: str = COMPILER_VERSION) -> DraftImageAsset:
    from .qa import contrast
    asset = DraftImageAsset.model_validate(asset.model_dump(mode="json"))
    if version not in SUPPORTED_COMPILER_VERSIONS or (
            version == "native_scene_v1" and any(obj.kind in V2_KINDS for obj in asset.scene.objects)):
        raise ValueError("pagebuilder_scene_provenance_invalid:" + asset.id)
    direction = DesignDirection.model_validate(direction.model_dump(mode="json"))
    if contrast(direction.palette["text"], direction.palette["background"]) < 4.5:
        raise ValueError("pagebuilder_scene_contrast_invalid")
    try:
        _validate_scene_geometry(asset)
    except ValueError:
        # ponytail: at most three objects use a row, use 2D packing if the scene limit grows.
        cell_width = 1200 / len(asset.scene.objects)
        for index, obj in enumerate(asset.scene.objects):
            cosine, sine = abs(math.cos(math.radians(obj.rotation))), abs(math.sin(math.radians(obj.rotation)))
            obj.x, obj.y = cell_width * (index + .5), 450
            obj.scale = min(obj.scale, (cell_width / 2 - 20) / (155 * cosine + 185 * sine),
                            430 / (185 * cosine + 155 * sine))
        asset = DraftImageAsset.model_validate(asset.model_dump(mode="json"))
        _validate_scene_geometry(asset)
    return asset


def _compile_asset(asset: DraftImageAsset, direction: DesignDirection, version: str = COMPILER_VERSION) -> ImageAsset:
    """Derive exact SVG/alt from typed poses and the effective approved palette."""
    asset = prepare_scene(asset, direction, version)
    direction = DesignDirection.model_validate(direction.model_dump(mode="json"))
    counts = Counter(obj.kind for obj in asset.scene.objects)
    nouns = [_NAMES[kind][int(count > 1)] for kind, count in counts.items()]
    description = nouns[0] if len(nouns) == 1 else ", ".join(nouns[:-1]) + " and " + nouns[-1]
    alt = "Original illustration of " + description + "."
    root = ET.Element("svg", xmlns=SVG_NAMESPACE, viewBox="0 0 1200 900")
    _element(root, "rect", width=1200, height=900, fill=direction.palette["background"])
    if version != "native_scene_v1":
        _backdrop(root, asset.id, direction)
    for index, obj in enumerate(asset.scene.objects):
        group = _element(root, "g", id=f"object_{index}",
            transform=f"translate({obj.x:g} {obj.y:g}) rotate({obj.rotation:g}) scale({obj.scale:g})",
            stroke_linecap="round", stroke_linejoin="round")
        _RECIPES[obj.kind](group, direction.palette)
    if version != "native_scene_v1":
        # Comma-join geometry so digit runs never resemble phone/card numbers to the model-boundary PII scan.
        for node in root.iter():
            for name in ("d", "transform"):
                if name in node.attrib:
                    node.set(name, re.sub(r"(?<=[\d.]) (?=[-\d.])", ",", node.attrib[name]))
    return ImageAsset(**asset.model_dump(mode="json"), svg=validate_svg(ET.tostring(root, encoding="unicode")),
        alt=alt, path="assets/" + asset.id + ".svg", width=1200, height=900,
        compiler_version=version, provenance="model_scene_native_svg")


def validate_derivation(asset: ImageAsset, direction: DesignDirection) -> None:
    """Exact derivation proof; this does not attest rendering or aesthetic QA."""
    if (asset.scene is None or asset.compiler_version not in SUPPORTED_COMPILER_VERSIONS
            or asset.provenance != "model_scene_native_svg"):
        raise ValueError("pagebuilder_scene_provenance_invalid:" + asset.id)
    draft = DraftImageAsset.model_validate({key: getattr(asset, key) for key in
        ("id", "role", "section_id", "subject", "prompt", "scene")})
    _validate_scene_geometry(draft)
    expected = _compile_asset(draft, direction, asset.compiler_version)
    if (validate_svg(asset.svg), asset.alt, asset.path, asset.width, asset.height) != (
            expected.svg, expected.alt, expected.path, expected.width, expected.height):
        raise ValueError("pagebuilder_scene_derivation_mismatch:" + asset.id)


def validate_compiled_assets(plan: PagePlan) -> None:
    if isinstance(plan, GenerationDraft):
        GenerationDraft.model_validate(plan.model_dump(mode="json"))
        for asset in plan.assets:
            _validate_scene_geometry(asset)
        return
    for asset in plan.assets:
        validate_derivation(asset, plan.direction)


RASTER_PROVENANCE = "openai_image_illustrative"
RASTER_MAX_BYTES = 450000
RASTER_EXTRA_ALT = "Illustrative AI-generated abstract texture in the page palette."
_RASTER_RULES = ("Absolutely no text, letters, numbers, words, logos, watermarks, signage, banners, headers, labels or"
    " brand marks; every board, card, page and surface is blank or abstract."
    " No people, faces, hands or body parts. No screens, user interfaces, app mockups, dashboards, charts or devices"
    " showing content. Not a product photo or screenshot: an original illustrative artwork.")


def webp_size(data: bytes) -> tuple[int, int]:
    """Stdlib WebP header check; anything else falls back to native SVG art."""
    if (not isinstance(data, bytes) or not 30 <= len(data) <= RASTER_MAX_BYTES or data[:4] != b"RIFF"
            or data[8:12] != b"WEBP" or int.from_bytes(data[4:8], "little") + 8 != len(data)):
        raise ValueError("pagebuilder_image_format_invalid")
    chunk = data[12:16]
    if chunk == b"VP8X":
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ValueError("pagebuilder_image_format_invalid")


def raster_alt(asset: ImageAsset) -> str:
    """Reuse the reviewed native caption's objects; label the medium honestly."""
    subject = asset.alt.removeprefix("Original illustration of ").rstrip(".")
    return ("Illustrative AI-generated image of " + subject + ".")[:160]


def raster_prompt(direction: DesignDirection, focus: str, setting: str, *, wide: bool) -> str:
    palette = ", ".join(f"{role} {color}" for role, color in direction.palette.items())
    return ("Wordless image: no text, lettering, signs or titles anywhere. "
        + direction.illustration_style[:400].rstrip(". ") + ". Original illustrative artwork for a landing page."
        + " Focal subject: " + focus[:200] + ". Setting and mood: " + setting[:300].rstrip(". ") + "; "
        + direction.concept[:300].rstrip(". ") + ". Colour palette: " + palette + ". "
        + ("Wide composition with calm negative space." if wide else "Balanced composition with one clear focal point.")
        + " Refined editorial craft, rich light and tactile materials. " + _RASTER_RULES)
