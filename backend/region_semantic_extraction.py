"""
Region-scoped semantic extraction for interactive viewer.

Glyphs and geometry are filtered to a user-selected PDF bbox before reconstruction.
"""

from __future__ import annotations

from typing import Any, Sequence

import fitz

from coordinate_driven_semantic_reconstruction import process_page_semantic
from pdf_geometry import normalize_bbox


def _center_of_bbox(bbox: Sequence[float]) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


def _point_to_bbox_distance(x: float, y: float, bbox: Sequence[float]) -> float:
    x0, y0, x1, y1 = bbox
    dx = max(x0 - x, 0.0, x - x1)
    dy = max(y0 - y, 0.0, y - y1)
    return (dx * dx + dy * dy) ** 0.5


def extract_region_semantics(
    pdf_path: str,
    page_number: int,
    region_bbox: Sequence[float],
    *,
    glyph_mode: str = "intersects",
) -> dict:
    """
    Run semantic reconstruction on one page limited to region_bbox (PDF points).

    glyph_mode:
      - "intersects": glyph bbox overlaps selection (default, inclusive)
      - "center": glyph center must lie inside selection (stricter)
    """
    region = normalize_bbox(region_bbox)
    doc = fitz.open(pdf_path)
    try:
        if page_number < 1 or page_number > doc.page_count:
            raise ValueError(f"page_number must be 1..{doc.page_count}")
        page = doc[page_number - 1]
        entities = process_page_semantic(
            page,
            page_number,
            region_bbox=region,
            glyph_mode=glyph_mode,
            user_selected_region=True,
        )
        return {
            "page": page_number,
            "region_bbox": region,
            "page_width": page.rect.width,
            "page_height": page.rect.height,
            "entity_count": len(entities),
            "entities": entities,
        }
    finally:
        doc.close()


def snap_semantic_entity(
    pdf_path: str,
    page_number: int,
    x: float,
    y: float,
    *,
    radius: float = 24.0,
    glyph_mode: str = "intersects",
) -> dict[str, Any]:
    """
    Find nearest semantic entity around a hover point.

    Strategy:
      1) Extract semantics from a small region around the point.
      2) Pick nearest entity to pointer by bbox-distance.
    """
    region_bbox = [x - radius, y - radius, x + radius, y + radius]
    result = extract_region_semantics(
        pdf_path,
        page_number,
        region_bbox,
        glyph_mode=glyph_mode,
    )
    entities = result.get("entities", [])
    if not entities:
        return {
            "page": page_number,
            "point": [x, y],
            "radius": radius,
            "region_bbox": region_bbox,
            "candidate_count": 0,
            "selected": None,
        }

    best = None
    best_dist = float("inf")
    for ent in entities:
        bbox = ent.get("text_bbox")
        if not bbox or len(bbox) != 4:
            continue
        d = _point_to_bbox_distance(x, y, bbox)
        if d < best_dist:
            best = ent
            best_dist = d

    if not best:
        return {
            "page": page_number,
            "point": [x, y],
            "radius": radius,
            "region_bbox": region_bbox,
            "candidate_count": len(entities),
            "selected": None,
        }

    bx, by = _center_of_bbox(best["text_bbox"])
    selected = {**best, "center": [bx, by], "snap_distance": round(best_dist, 3)}
    return {
        "page": page_number,
        "point": [x, y],
        "radius": radius,
        "region_bbox": region_bbox,
        "candidate_count": len(entities),
        "selected": selected,
    }
