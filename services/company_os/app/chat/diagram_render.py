"""Deterministic SVG for chat diagrams: the model sends nodes and edges, we own the geometry."""

from __future__ import annotations

import math
import re
import textwrap
from xml.sax.saxutils import escape, quoteattr

WIDTH, LEFT, RIGHT, GAP_X, GAP_Y, TOP = 680, 40, 640, 20, 56, 40
MAX_NODES, MAX_PER_ROW = 12, 4
COLORS = ("teal", "purple", "blue", "amber", "coral", "gray")
_MARKER = ('<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" '
           'orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" '
           'stroke-linecap="round" stroke-linejoin="round"/></marker>')


def _clean(value, limit: int) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f  ]", " ", str(value or ""))).strip()[:limit]


def _ranks(ids: list[str], edges: list[tuple[str, str]]):
    """Longest-path layering; edges that close a cycle are returned separately as back edges."""
    out = {node: [] for node in ids}
    for source, target in edges:
        out[source].append(target)
    state, back = {}, set()

    def visit(node):  # ponytail: recursion is fine at MAX_NODES=12
        state[node] = 1
        for target in out[node]:
            if state.get(target) == 1:
                back.add((node, target))
            elif target not in state:
                visit(target)
        state[node] = 2

    for node in ids:
        if node not in state:
            visit(node)
    forward = [edge for edge in edges if edge not in back and edge[0] != edge[1]]
    rank = {node: 0 for node in ids}
    for _ in ids:  # Bellman-Ford style relaxation over a DAG of <= 12 nodes
        for source, target in forward:
            rank[target] = max(rank[target], rank[source] + 1)
    return rank, [edge for edge in edges if edge in back or edge[0] == edge[1]]


def render_spec(title: str, nodes, edges) -> str:
    """Return a light-theme SVG for a nodes/edges spec, or raise ValueError('invalid_spec'|'too_wide')."""
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise ValueError("invalid_spec")
    spec, ids = {}, []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError("invalid_spec")
        key = _clean(node.get("id") or f"n{index}", 40)
        label = _clean(node.get("label"), 100)
        if not label or key in spec:
            raise ValueError("invalid_spec")
        color = node.get("color") if node.get("color") in COLORS else "teal"
        spec[key] = {"label": label, "detail": _clean(node.get("detail"), 120), "color": color}
        ids.append(key)
    links = []
    for edge in edges if isinstance(edges, list) else []:
        pair = (_clean(edge.get("from"), 40), _clean(edge.get("to"), 40)) if isinstance(edge, dict) else None
        if pair and pair[0] in spec and pair[1] in spec and pair[0] != pair[1] and pair not in links:
            links.append(pair)
    rank, back = _ranks(ids, links)
    rows = [[node for node in ids if rank[node] == level] for level in range(max(rank.values()) + 1)]
    if any(len(row) > MAX_PER_ROW for row in rows):
        raise ValueError("too_wide")

    boxes, row_top, row_bottom, y = {}, [], [], TOP
    for row in rows:
        width = min(280, (RIGHT - LEFT - GAP_X * (len(row) - 1)) / len(row))
        start = WIDTH / 2 - (width * len(row) + GAP_X * (len(row) - 1)) / 2
        wrapped = {}
        for node in row:
            # Same glyph estimate as diagram_check: 7px per 14px char, 6.5px per 12px char, 12px padding each side.
            wrapped[node] = (textwrap.wrap(spec[node]["label"], max(4, math.floor((width - 24) / 7)), break_long_words=True),
                             textwrap.wrap(spec[node]["detail"], max(4, math.floor((width - 24) / 6.5)), break_long_words=True))
        height = max(28 + 18 * len(main) + (4 + 16 * len(sub) if sub else 0) for main, sub in wrapped.values())
        for column, node in enumerate(row):
            boxes[node] = (start + column * (width + GAP_X), y, width, height, *wrapped[node])
        row_top.append(y)
        row_bottom.append(y + height)
        y += height + GAP_Y
    canvas = row_bottom[-1] + 40

    def n(value):
        return f"{value:.1f}".rstrip("0").rstrip(".")

    out = [f'<svg width="100%" viewBox="0 0 {WIDTH} {n(canvas)}" role="img">',
           f"<title>{escape(_clean(title, 80).replace('_', ' ') or 'Diagram')}</title>",
           f"<desc>{escape(' then '.join(spec[node]['label'] for node in ids))}</desc>", f"<defs>{_MARKER}</defs>"]
    for node in ids:
        left, top, width, height, main, sub = boxes[node]
        center = left + width / 2
        prompt = re.sub(r"['\"\\()<>]", "", f"Tell me more about {spec[node]['label']}")[:200]
        out.append(f'<g class="node c-{spec[node]["color"]}" onclick={quoteattr(f"sendPrompt({chr(39)}{prompt}{chr(39)})")}>'
                   f'<rect x="{n(left)}" y="{n(top)}" width="{n(width)}" height="{n(height)}" rx="8" stroke-width="0.5"/>')
        for index, line in enumerate(main):
            out.append(f'<text class="th" x="{n(center)}" y="{n(top + 23 + 18 * index)}" text-anchor="middle" '
                       f'dominant-baseline="central">{escape(line)}</text>')
        for index, line in enumerate(sub):
            out.append(f'<text class="ts" x="{n(center)}" y="{n(top + 14 + 18 * len(main) + 12 + 16 * index)}" '
                       f'text-anchor="middle" dominant-baseline="central">{escape(line)}</text>')
        out.append("</g>")
    for source, target in links:
        sx, sy = boxes[source][0] + boxes[source][2] / 2, row_bottom[rank[source]]
        tx, ty = boxes[target][0] + boxes[target][2] / 2, row_top[rank[target]]
        lane = sy + 14 + 8 * (rows[rank[source]].index(source) % 3)  # one bus per source inside the row gap
        if rank[target] == rank[source] + 1:
            d = f"M{n(sx)} {n(sy)}V{n(lane)}H{n(tx)}V{n(ty - 4)}"
        else:  # skips go down the left margin, loops back up the right margin
            side = 16 if (source, target) not in back else WIDTH - 16
            d = f"M{n(sx)} {n(sy)}V{n(lane)}H{side}V{n(ty - 14)}H{n(tx)}V{n(ty - 4)}"
        out.append(f'<path class="arr" d="{d}" fill="none" marker-end="url(#arrow)"/>')
    out.append("</svg>")
    return "".join(out)


def spec_from_svg(root) -> tuple[list[dict], list[dict]] | None:
    """Recover labelled boxes and arrows from a parsed model SVG; everything else is discarded."""
    from app.chat.diagram_check import _path_points, _tag  # local: diagram_check imports this module

    parents = {child: parent for parent in root.iter() for child in parent}

    def hidden(element):
        while element in parents:
            element = parents[element]
            if _tag(element) == "defs":
                return True
        return False

    def number(element, name):
        try:
            value = float(element.get(name, "0"))
        except ValueError:
            return None
        return value if math.isfinite(value) else None

    texts = [text for text in root.iter() if _tag(text) == "text" and not hidden(text)]
    boxes = []
    for rect in root.iter():
        if _tag(rect) != "rect" or hidden(rect) or "group" in rect.get("class", "").split():
            continue
        x, y, w, h = (number(rect, key) for key in ("x", "y", "width", "height"))
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        parent = parents.get(rect)
        owned = [text for text in texts if parents.get(text) is parent] if parent is not None and _tag(parent) == "g" else []
        owned = owned or [text for text in texts if x <= (number(text, "x") or -1) <= x + w and y <= (number(text, "y") or -1) <= y + h]
        main = " ".join(_clean("".join(t.itertext()), 80) for t in owned if "ts" not in t.get("class", "").split())
        sub = " ".join(_clean("".join(t.itertext()), 120) for t in owned if "ts" in t.get("class", "").split())
        if main or sub:
            boxes.append(((x, y, x + w, y + h), main or sub, sub if main else ""))
    if not boxes:
        return None
    boxes.sort(key=lambda box: (box[0][1], box[0][0]))
    nodes = [{"id": f"n{index}", "label": label, "detail": detail} for index, (_, label, detail) in enumerate(boxes)]

    def nearest(point):
        return min(range(len(boxes)), key=lambda index: math.hypot(
            max(boxes[index][0][0] - point[0], 0, point[0] - boxes[index][0][2]),
            max(boxes[index][0][1] - point[1], 0, point[1] - boxes[index][0][3])))

    edges = []
    for element in root.iter():
        if _tag(element) not in {"line", "path"} or hidden(element) or not (
                "arr" in element.get("class", "").split() or element.get("marker-end")):
            continue
        try:
            points = ([(float(element.get("x1")), float(element.get("y1"))), (float(element.get("x2")), float(element.get("y2")))]
                      if _tag(element) == "line" else _path_points(element.get("d", "")))
        except (TypeError, ValueError):
            continue
        source, target = nearest(points[0]), nearest(points[-1])
        if source != target:
            edges.append({"from": f"n{source}", "to": f"n{target}"})
    return nodes, edges
