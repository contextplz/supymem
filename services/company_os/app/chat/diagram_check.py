"""Validate model-authored SVG before storing or returning it."""

from __future__ import annotations

import json
import logging
import math
import re

from app.chat.diagram_render import render_spec, spec_from_svg

try:
    from defusedxml.ElementTree import fromstring
except ImportError:
    from xml.etree.ElementTree import fromstring


log = logging.getLogger("app.chat.diagram")
_NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
_BLOCKED = re.compile(
    r"\b(?:approv\w*|confirm\w*|money|credits?|cost\w*|spend\w*|pay\w*|billing|"
    r"send\w*|sent|errors?|fail\w*|retry|brief\w*|short\w*|concise|brevity)\b|[$€£₹]", re.I)
_BRIEF = re.compile(r"\b(?:brief\w*|short\w*|concise|brevity)\b", re.I)
EXPLICIT = re.compile(r"\b(?:draw|diagram\w*|flowchart\w*|visuali[sz]e|sketch|map\s+(?:it\s+)?out)\b", re.I)


def diagrams_allowed(question: str, *, body: str = "", kind: str = "text", actions=None, preferences=None) -> bool:
    # `body` is accepted for old callers but ignored: the founder's question decides, not Charlie's prose.
    # Scanning prose for "send"/"cost"/"short" dropped nearly every diagram as reply_policy in production.
    from app.chat.navigation_map import IN_CHAT_VIEWS  # read-only views that replaced page links still allow a diagram
    if kind != "text" or any(a.get("confirmation") or (a.get("action") and not (a["action"] in IN_CHAT_VIEWS and not a.get("payload")))
                             or a.get("quote_id") for a in actions or [] if isinstance(a, dict)):
        return False
    if EXPLICIT.search(question or ""):
        return True
    return not _BLOCKED.search(question or "") and not _BRIEF.search(json.dumps(preferences or [], ensure_ascii=False))


def _tag(element) -> str:
    return element.tag.removeprefix("{http://www.w3.org/2000/svg}")


def _number(element, name: str, default: str = "0") -> float:
    value = element.get(name, default)
    if not re.fullmatch(_NUMBER, value):
        raise ValueError("invalid_coordinates")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid_coordinates")
    return number


def _contains(outer, inner) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _crosses(start, end, box) -> bool:
    lower, upper = 0.0, 1.0
    for axis in (0, 1):
        delta = end[axis] - start[axis]
        if delta == 0:
            if not box[axis] < start[axis] < box[axis + 2]:
                return False
        else:
            bounds = sorted(((box[axis] - start[axis]) / delta, (box[axis + 2] - start[axis]) / delta))
            lower, upper = max(lower, bounds[0]), min(upper, bounds[1])
    return lower < upper and upper > 0 and lower < 1


def _path_points(path: str) -> list[tuple[float, float]]:
    # ponytail: straight connectors only, add curve intersection checks before accepting curves.
    tokens = re.findall(rf"[A-Za-z]|{_NUMBER}", path)
    if re.sub(rf"[MmLlHhVv]|{_NUMBER}|[\s,]", "", path):
        raise ValueError("unsupported_arrow_path")
    points, command, index = [], "", 0
    while index < len(tokens):
        if tokens[index].isalpha():
            command, index = tokens[index], index + 1
        count = 1 if command.upper() in {"H", "V"} else 2
        if command.upper() not in {"M", "L", "H", "V"} or index + count > len(tokens):
            raise ValueError("invalid_arrow_path")
        values = [float(value) for value in tokens[index:index + count]]
        index += count
        if not all(math.isfinite(value) for value in values) or (not points and command.upper() != "M"):
            raise ValueError("invalid_arrow_path")
        previous = points[-1] if points else (0, 0)
        if command.upper() == "H":
            point = (values[0] + (previous[0] if command.islower() else 0), previous[1])
        elif command.upper() == "V":
            point = (previous[0], values[0] + (previous[1] if command.islower() else 0))
        else:
            point = tuple(value + (previous[axis] if command.islower() else 0) for axis, value in enumerate(values))
        if points and command.upper() == "M":
            raise ValueError("disconnected_arrow_path")
        points.append(point)
        if command.upper() == "M":
            command = "l" if command.islower() else "L"
    if len(points) < 2:
        raise ValueError("invalid_arrow_path")
    return points


def _arrow_crosses(points, box) -> bool:
    for index, (start, end) in enumerate(zip(points, points[1:])):
        length = math.dist(start, end)
        if not length:
            continue
        start_inside = _contains(box, (*start, *start))
        end_inside = _contains(box, (*end, *end))
        trim_start = min(6, length) if index == 0 and start_inside and not end_inside else 0
        trim_end = min(6, length) if index == len(points) - 2 and end_inside and not start_inside else 0
        if trim_start + trim_end >= length:
            continue
        trimmed_start = tuple(start[axis] + (end[axis] - start[axis]) * trim_start / length for axis in (0, 1))
        trimmed_end = tuple(end[axis] - (end[axis] - start[axis]) * trim_end / length for axis in (0, 1))
        if _crosses(trimmed_start, trimmed_end, box):
            return True
    return False


def check_diagram(svg: str) -> list[str]:
    """Return reason codes only, never SVG or parser exceptions."""
    if not isinstance(svg, str):
        return ["invalid_svg"]
    try:
        if len(svg.encode("utf-8")) > 64 * 1024:
            return ["oversize"]
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", svg, re.I):
            return ["entity_declaration"]
        root = fromstring(svg)
    except Exception as exc:
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, None, "diagram.validate")
        return ["bad_xml"]
    if _tag(root) != "svg":
        return ["invalid_root"]
    try:
        viewbox = root.get("viewBox", "").split()
        if len(viewbox) != 4 or [float(value) for value in viewbox[:3]] != [0, 0, 680]:
            return ["invalid_viewbox"]
        height = float(viewbox[3])
        if not math.isfinite(height) or height <= 0:
            return ["invalid_viewbox"]
    except ValueError:
        return ["invalid_viewbox"]
    reasons = set()
    parents = {child: parent for parent in root.iter() for child in parent}
    if root.get("role") != "img" or [_tag(child) for child in list(root)[:2]] != ["title", "desc"]:
        reasons.add("missing_accessibility")
    rects, texts, arrows = [], [], []
    for element in root.iter():
        tag = _tag(element)
        if tag == "svg" and element is not root:
            reasons.add("unsafe_element")
        if tag not in {"svg", "title", "desc", "defs", "marker", "g", "rect", "text", "line", "path"}:
            reasons.add("unsafe_element")
        for name, value in element.attrib.items():
            if name.lower().startswith("on"):
                if not (name == "onclick" and tag == "g" and "node" in element.get("class", "").split()
                        and re.fullmatch(r"sendPrompt\('[^'\\\r\n<>]*'\)", value)):
                    reasons.add("unsafe_attribute")
            elif name in {"transform", "style", "dx", "dy", "rotate", "textLength", "lengthAdjust"} or "href" in name.lower() or re.search(r"url\(", value, re.I) and not (
                    name == "marker-end" and value == "url(#arrow)"):
                reasons.add("unsafe_attribute")
            if name == "font-size" and value not in {"12", "14", "12px", "14px"}:
                reasons.add("unsupported_text_size")
        ancestor = element
        in_defs = False
        while ancestor in parents:
            ancestor = parents[ancestor]
            in_defs |= _tag(ancestor) == "defs"
        if in_defs and tag not in {"rect", "text"}:
            continue
        try:
            if tag == "rect":
                left, top = _number(element, "x"), _number(element, "y")
                width, tall = _number(element, "width"), _number(element, "height")
                box = (left, top, left + width, top + tall)
                if width <= 0 or tall <= 0 or not _contains((0, 0, 680, height), box):
                    reasons.add("out_of_canvas")
                rects.append((element, box))
            elif tag == "text":
                texts.append(element)
            elif tag in {"line", "path"} and "arr" in element.get("class", "").split():
                points = ([_number(element, "x1"), _number(element, "y1")],
                          [_number(element, "x2"), _number(element, "y2")]) if tag == "line" else _path_points(element.get("d", ""))
                if any(not (0 <= point[0] <= 680 and 0 <= point[1] <= height) for point in points):
                    reasons.add("out_of_canvas")
                arrows.append(points)
        except (ValueError, OverflowError):
            reasons.add("invalid_coordinates")
    groups = {element for element, _ in rects if "group" in element.get("class", "").split()
              or "group" in parents.get(element, root).get("class", "").split()}
    for index, (element, box) in enumerate(rects):
        for other, other_box in rects[index + 1:]:
            if (element in groups and _contains(box, other_box)
                    or other in groups and _contains(other_box, box)):
                continue
            if (min(box[2], other_box[2]) - max(box[0], other_box[0]) > 3
                    and min(box[3], other_box[3]) - max(box[1], other_box[1]) > 3):
                reasons.add("rect_overlap")
    for text in texts:
        try:
            from app.chat.blockers import humanize_codes
            from app.secrets.detect import looks_like_secret
            label = "".join(text.itertext())
            if humanize_codes(label, include_failures=True) != label or looks_like_secret(label):
                reasons.add("unsafe_label")
            horizontal, vertical = _number(text, "x"), _number(text, "y")
            if not (0 <= horizontal <= 680 and 0 <= vertical <= height):
                reasons.add("out_of_canvas")
            # ponytail: approximate glyph widths, use font metrics if observed labels still misfit.
            width = len(label) * (6.5 if "ts" in text.get("class", "").split()
                or text.get("font-size") in {"12", "12px"} else 7)
            anchor = text.get("text-anchor", "start")
            left = horizontal - (width / 2 if anchor == "middle" else width if anchor == "end" else 0)
            if left < 0 or left + width > 680:
                reasons.add("out_of_canvas")
            ancestor, candidates = text, []
            while ancestor in parents and not candidates:
                ancestor = parents[ancestor]
                if _tag(ancestor) == "g":
                    candidates = [box for element, box in rects if parents.get(element) is ancestor]
            if not candidates:
                candidates = [box for _, box in rects if _contains(box, (horizontal, vertical, horizontal, vertical))]
            if candidates:
                container = min(candidates, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
                tolerant_container = (container[0] - 4, container[1] - 3, container[2] + 4, container[3] + 3)
                if not _contains(tolerant_container, (left, vertical, left + width, vertical)):
                    reasons.add("text_overflow")
        except ValueError:
            reasons.add("invalid_coordinates")
    for points in arrows:
        for element, box in rects:
            if element not in groups and _arrow_crosses(points, box):
                reasons.add("arrow_through_box")
    return sorted(reasons)


# Raw model SVG with only layout or markup problems is rebuilt from its labels; these are never salvaged.
_UNSALVAGEABLE = {"invalid_svg", "oversize", "entity_declaration", "bad_xml", "invalid_root", "unsafe_label"}


def _diagram_svg(part: dict) -> tuple[str | None, list[str]]:
    """Return (final svg, reasons). Specs are rendered; messy SVG is rebuilt deterministically."""
    title = part.get("title", "")
    try:
        if part.get("type") == "show_diagram" and "nodes" in part:
            svg = render_spec(title, part.get("nodes"), part.get("edges"))
        else:
            svg = part.get("svg")
            reasons = check_diagram(svg)
            if not reasons or part.get("type") != "show_diagram" or _UNSALVAGEABLE.intersection(reasons):
                return svg, reasons
            spec = spec_from_svg(fromstring(svg))
            if not spec:
                return None, reasons
            svg = render_spec(title, *spec)
            log.info("chat_diagram_rebuilt %s", json.dumps({"reasons": reasons}, separators=(",", ":")))
    except ValueError as exc:
        return None, [str(exc) if str(exc) in {"invalid_spec", "too_wide"} else "invalid_spec"]
    return svg, check_diagram(svg)


def checked_parts(parts: list, *, question: str = "", body: str = "", kind: str = "text", actions=None, preferences=None,
                  drop_reasons: list | None = None) -> list[dict]:
    """Fail closed on invalid diagrams; retain only prose and at most two safe SVGs."""
    if not isinstance(parts, list):
        return []
    allowed = diagrams_allowed(question, body=body, kind=kind, actions=actions, preferences=preferences)
    result, count = [], 0
    diagram_count = sum(isinstance(part, dict) and part.get("type") in {"diagram", "show_diagram"} for part in parts)
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            result.append({"type": "text", "text": part["text"]})
        elif part.get("type") in {"diagram", "show_diagram"}:
            # Model titles like "Customer journey" are normalised, not dropped.
            title = "_".join(re.findall(r"[a-z0-9]+", str(part.get("title", "")).lower()))[:60].strip("_")
            title = title if re.match(r"[a-z]", title) else f"diagram_{title}".strip("_")
            svg, reasons = _diagram_svg(part) if allowed and count < 2 else (None, [])
            if not allowed or count >= 2:
                reasons = [*reasons, "reply_policy"]
            if drop_reasons is not None:
                drop_reasons.append({"part_index": index, "reasons": reasons})
            if reasons:
                try:
                    log.warning("chat_diagram_dropped %s", json.dumps({"reasons": reasons, "part_index": index,
                        "part_count": len(parts), "diagram_count": diagram_count, "accepted_count": count}, separators=(",", ":")))
                except Exception as exc:
                    from app.chat.recovery import log_founder_exception
                    log_founder_exception(exc, None, "diagram.validation_telemetry")
            else:
                result.append({"type": "diagram", "title": title, "svg": svg})
                count += 1
    return result
