"""
Deterministic geometry primitive extraction from PyMuPDF page drawings.

This is a foundational symbolic layer for engineering scene understanding:
lines, circles, curves, polylines, and simple drafting markers.
"""

from __future__ import annotations

import math


def _bbox_from_points(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _center_from_bbox(b):
    return [(b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0]


def _distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _primitive_from_item(item, draw, seq_id):
    op = item[0]
    width = float(draw.get("width") or 0.0)
    dashed = str(draw.get("dashes") or "").strip() not in {"", "[] 0", "[]0"}

    if op == "l":
        p1, p2 = item[1], item[2]
        points = [(p1.x, p1.y), (p2.x, p2.y)]
        bbox = _bbox_from_points(points)
        return {
            "primitive_id": seq_id,
            "geometry_type": "line",
            "bbox": bbox,
            "center": _center_from_bbox(bbox),
            "length": _distance(points[0], points[1]),
            "dashed": dashed,
            "stroke_width": width,
            "raw_op": op,
        }

    if op == "re":
        rect = item[1]
        bbox = [rect.x0, rect.y0, rect.x1, rect.y1]
        return {
            "primitive_id": seq_id,
            "geometry_type": "rectangle",
            "bbox": bbox,
            "center": _center_from_bbox(bbox),
            "dashed": dashed,
            "stroke_width": width,
            "raw_op": op,
        }

    if op == "qu":
        q = item[1]
        points = [(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.ll.x, q.ll.y), (q.lr.x, q.lr.y)]
        bbox = _bbox_from_points(points)
        return {
            "primitive_id": seq_id,
            "geometry_type": "quad",
            "bbox": bbox,
            "center": _center_from_bbox(bbox),
            "dashed": dashed,
            "stroke_width": width,
            "raw_op": op,
        }

    if op in {"c", "v", "y"}:
        # Bezier curve operators.
        points = []
        for part in item[1:]:
            if hasattr(part, "x") and hasattr(part, "y"):
                points.append((part.x, part.y))
        if not points:
            return None
        bbox = _bbox_from_points(points)
        return {
            "primitive_id": seq_id,
            "geometry_type": "curve",
            "bbox": bbox,
            "center": _center_from_bbox(bbox),
            "dashed": dashed,
            "stroke_width": width,
            "raw_op": op,
        }

    return None


def _classify_drawing_group(draw, item_primitives, seq_id):
    """
    Group-level symbolic detector (useful for centerlines / arrowheads / circles).
    """
    rect = draw["rect"]
    bbox = [rect.x0, rect.y0, rect.x1, rect.y1]
    w = abs(rect.x1 - rect.x0)
    h = abs(rect.y1 - rect.y0)
    dashed = str(draw.get("dashes") or "").strip() not in {"", "[] 0", "[]0"}
    width = float(draw.get("width") or 0.0)
    fill = draw.get("fill")
    close_path = bool(draw.get("closePath"))

    geometry_type = "path"
    if dashed and len(item_primitives) > 0:
        geometry_type = "centerline"
    elif close_path and fill is not None and len(item_primitives) <= 3 and max(w, h) < 14:
        geometry_type = "arrowhead"
    elif w > 0 and h > 0 and (min(w, h) / max(w, h)) > 0.82 and len(item_primitives) <= 4:
        geometry_type = "circle_or_arc"
    elif len(item_primitives) == 1 and item_primitives[0]["geometry_type"] == "line":
        geometry_type = "line"

    return {
        "primitive_id": seq_id,
        "geometry_type": geometry_type,
        "bbox": bbox,
        "center": _center_from_bbox(bbox),
        "dashed": dashed,
        "stroke_width": width,
        "raw_op": "group",
    }


def extract_geometry_primitives(page, *, region_bbox=None, intersects_fn=None):
    """
    Build deterministic geometry primitives from page drawings.
    """
    primitives = []
    seq_id = 0

    for draw in page.get_drawings():
        rect = draw["rect"]
        draw_bbox = [rect.x0, rect.y0, rect.x1, rect.y1]
        if region_bbox and intersects_fn and not intersects_fn(draw_bbox, region_bbox):
            continue

        item_primitives = []
        for item in draw.get("items", []):
            seq_id += 1
            p = _primitive_from_item(item, draw, seq_id)
            if p:
                if not region_bbox or not intersects_fn or intersects_fn(p["bbox"], region_bbox):
                    item_primitives.append(p)
                    primitives.append(p)

        # Add one grouped symbolic primitive per drawing path.
        seq_id += 1
        group_p = _classify_drawing_group(draw, item_primitives, seq_id)
        if not region_bbox or not intersects_fn or intersects_fn(group_p["bbox"], region_bbox):
            primitives.append(group_p)

    return primitives
