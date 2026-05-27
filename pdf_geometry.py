"""PDF bbox helpers (PyMuPDF / PDF.js compatible: origin top-left, y down)."""

from typing import List, Sequence


def normalize_bbox(bbox: Sequence[float]) -> List[float]:
    x0, y0, x1, y1 = bbox
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def bbox_intersects(a: Sequence[float], b: Sequence[float]) -> bool:
    ax0, ay0, ax1, ay1 = normalize_bbox(a)
    bx0, by0, bx1, by1 = normalize_bbox(b)
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0


def bbox_center_inside(inner: Sequence[float], region: Sequence[float]) -> bool:
    ix0, iy0, ix1, iy1 = inner
    cx = (ix0 + ix1) / 2
    cy = (iy0 + iy1) / 2
    rx0, ry0, rx1, ry1 = normalize_bbox(region)
    return rx0 <= cx <= rx1 and ry0 <= cy <= ry1
