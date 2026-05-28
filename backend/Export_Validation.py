"""
Final semantic validation before JSON export.

Filters title-block/metadata noise, removes unresolved fragments,
fuses vertical digit chains, and assigns export confidence.
"""

import re
from statistics import median

# Geometry distance above this (PDF units) is weak dimension–geometry coupling.
SRC_MAX_GEOMETRY_DISTANCE = 18.0

DATE_PATTERN = re.compile(
    r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}$|^\d{1,2}[./]\d{2}$",
)
LONG_NUMERIC_ID = re.compile(r"^\d{6,}$")
INCOMPLETE_TEXT = re.compile(
    r"^\d+\($|^\($|^[^(]*\($|^[A-Za-z]{0,2}\($|^\d+\.\s*$",
)
REFERENCE_PATTERN = re.compile(r"^\d+(?:\.\d+)?\s*\(REF\)$", re.I)


def in_excluded_drawing_zone(center, page_rect):
    """Title block, revision strip, and sheet-edge metadata zones."""
    if not center or not page_rect:
        return False

    x, y = center[0], center[1]
    w, h = page_rect.width, page_rect.height

    if y > h * 0.86:
        return True
    if x > w * 0.60 and y > h * 0.68:
        return True
    if y < h * 0.05:
        return True
    return False


def is_metadata_noise(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if DATE_PATTERN.match(t):
        return True
    if LONG_NUMERIC_ID.match(t):
        return True
    if INCOMPLETE_TEXT.match(t):
        return True
    if len(t) >= 5 and t.isdigit():
        return True
    # Watermark / PLM style long numbers with few unique digits
    if len(t) >= 7 and sum(ch.isdigit() for ch in t) / len(t) > 0.85:
        return True
    if re.match(r"^\d+[A-Za-z]{1,4}$", t) and not t.upper().startswith("R"):
        return True
    if re.match(r"^[0-9]{1,2}[A-Za-z]$", t):
        return True
    if re.match(r"^0\d+$", t) and len(t) <= 3:
        return True
    if re.match(r"^0\d{2,}$", t):
        return True
    return False


def is_orphan_keyword(text: str) -> bool:
    t = (text or "").strip().upper()
    return t in {"THK", "±", "+", "-", "REF"}


def compute_export_confidence(entity) -> float:
    score = 1.0
    entity_type = entity.get("entity_type")

    if entity.get("reconstructed_operator"):
        score *= 0.92

    glyph_count = entity.get("glyph_count") or 0
    if glyph_count <= 1:
        score *= 0.45
    elif glyph_count <= 2:
        score *= 0.65

    geom_dist = entity.get("distance")
    if geom_dist is not None:
        if entity_type in {"radius_dimension", "diameter_dimension"}:
            # Radius/diameter callouts are often offset from arc center by leader geometry.
            if geom_dist > 180:
                score *= 0.5
            elif geom_dist > 120:
                score *= 0.75
        else:
            if geom_dist > SRC_MAX_GEOMETRY_DISTANCE:
                score *= 0.35
            elif geom_dist > 12:
                score *= 0.75

    if entity.get("entity_type") == "SRC_dimension" and geom_dist and geom_dist > 12:
        score *= 0.55

    orientation = entity.get("orientation") or ""
    if orientation.startswith("unresolved"):
        return 0.0
    if orientation == "isolated_glyph":
        score *= 0.4

    return round(max(0.0, min(1.0, score)), 3)


def should_export_entity(entity, page_rect, min_confidence=0.35, skip_zone_filter=False) -> bool:
    text = (entity.get("display_text") or entity.get("nominal_text") or "").strip()
    orientation = entity.get("orientation") or ""

    # Drop control / non-printable garbage fragments early.
    if any(ord(ch) < 32 for ch in text):
        return False

    if orientation.startswith("unresolved"):
        return False

    if is_orphan_keyword(text):
        return False

    if entity.get("entity_type") == "thickness_dimension" and entity.get("nominal") is None:
        return False

    center = entity.get("center")
    if not skip_zone_filter and center and in_excluded_drawing_zone(center, page_rect):
        return False

    if is_metadata_noise(text) and entity.get("entity_type") not in (
        "SRC_dimension",
        "radius_dimension",
        "diameter_dimension",
        "thickness_dimension",
    ):
        return False

    if entity.get("entity_type") == "SRC_dimension":
        geom_dist = entity.get("distance")
        if geom_dist is not None and geom_dist > SRC_MAX_GEOMETRY_DISTANCE:
            return False

        # Decimal-vs-SRC final guard for implicit operator reconstruction.
        # If compact NN + N slipped through grammar as SRC, reject at export.
        values = entity.get("values") or []
        rule = (entity.get("grammar_rule") or "").upper()
        if len(values) == 2 and "IMPLICIT_X_BETWEEN_NUMBERS" in rule:
            try:
                left = int(values[0])
                right = int(values[1])
            except Exception:
                left = right = None
            if left is not None and right is not None:
                if left >= 10 and 0 <= right <= 9:
                    return False

    confidence = compute_export_confidence(entity)
    entity["confidence"] = confidence
    return confidence >= min_confidence


def fuse_vertical_digit_chains(entities):
    """
    Merge single-digit vertical fragments on the same axis bucket into one number (e.g. 1,9,2 -> 192).
    """
    fragments = []
    kept = []

    for ent in entities:
        text = (ent.get("display_text") or "").strip()
        if (
            ent.get("dimension_axis") == "vertical"
            and len(text) == 1
            and text.isdigit()
            and "unresolved" in (ent.get("orientation") or "")
        ):
            fragments.append(ent)
        else:
            kept.append(ent)

    if len(fragments) < 2:
        return entities

    def fusion_cluster_key(ent):
        center = ent.get("center")
        if not center:
            bb = ent.get("text_bbox") or ent.get("bbox")
            if bb and len(bb) == 4:
                center = [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]
            else:
                center = [0, 0]
        return (ent.get("page"), round(center[0] / 10))

    groups = {}
    for ent in fragments:
        key = fusion_cluster_key(ent)
        groups.setdefault(key, []).append(ent)

    used_ids = set()
    fused = []

    for group in groups.values():
        if len(group) < 2:
            continue

        group.sort(key=lambda e: -((e.get("center") or [0, 0])[1]))

        chains = []
        current = [group[0]]

        for i in range(1, len(group)):
            prev, cur = current[-1], group[i]
            pb = prev.get("text_bbox") or prev.get("bbox")
            cb = cur.get("text_bbox") or cur.get("bbox")
            if not pb or not cb:
                chains.append(current)
                current = [cur]
                continue

            line_dx = abs((prev.get("center") or [0, 0])[0] - (cur.get("center") or [0, 0])[0])
            gap = pb[1] - cb[3]

            gaps = []
            for j in range(1, len(current)):
                a = current[j - 1]
                b = current[j]
                ab = a.get("text_bbox") or a.get("bbox")
                bb = b.get("text_bbox") or b.get("bbox")
                if ab and bb:
                    gaps.append(abs(ab[1] - bb[3]))

            med_gap = median(gaps) if gaps else gap
            max_gap = max(med_gap * 2.5, med_gap + 8, 12)

            if line_dx <= 8 and 0 < gap <= max_gap:
                current.append(cur)
            else:
                chains.append(current)
                current = [cur]

        chains.append(current)

        for chain in chains:
            if len(chain) < 2:
                continue

            digits = [c.get("display_text", "").strip() for c in chain]
            if not all(d.isdigit() for d in digits):
                continue

            text = "".join(digits)
            if len(text) < 2:
                continue

            bboxes = [c.get("text_bbox") or c.get("bbox") for c in chain]
            bboxes = [b for b in bboxes if b]
            if not bboxes:
                continue

            merged_bbox = [
                min(b[0] for b in bboxes),
                min(b[1] for b in bboxes),
                max(b[2] for b in bboxes),
                max(b[3] for b in bboxes),
            ]
            cx = (merged_bbox[0] + merged_bbox[2]) / 2
            cy = (merged_bbox[1] + merged_bbox[3]) / 2

            try:
                nominal = float(text)
            except ValueError:
                nominal = None

            fused.append({
                "page": chain[0].get("page"),
                "entity_type": "linear_dimension",
                "display_text": text,
                "nominal": nominal,
                "nominal_text": text,
                "modifiers": [],
                "limits": None,
                "orientation": "vertical",
                "dimension_axis": "vertical",
                "axis_bucket": chain[0].get("axis_bucket"),
                "reading_direction": "bottom_to_top",
                "grammar_rule": "VERTICAL_DIGIT_FUSION",
                "text_bbox": merged_bbox,
                "bbox": merged_bbox,
                "center": [cx, cy],
                "glyph_count": sum(c.get("glyph_count", 1) for c in chain),
                "direction_locked": True,
            })

            for c in chain:
                used_ids.add(id(c))

    if not fused:
        return entities

    out = [e for e in kept if id(e) not in used_ids]
    out.extend(fused)
    return out


def merge_reference_suffix_entities(entities):
    """Merge numeric + (REF) fragments into reference_dimension."""
    numerics = []
    refs = []
    kept = []

    for ent in entities:
        t = (ent.get("display_text") or "").strip().upper()
        if t in {"(REF)", "REF"} or t == "REF":
            refs.append(ent)
        elif re.match(r"^\d+(?:\.\d+)?$", (ent.get("display_text") or "").strip()):
            numerics.append(ent)
        else:
            kept.append(ent)

    if not refs:
        return entities

    used = set()
    merged = []

    for ref in refs:
        rc = ref.get("center")
        if not rc:
            continue
        best = None
        best_d = float("inf")
        for num in numerics:
            if id(num) in used:
                continue
            nc = num.get("center")
            if not nc:
                continue
            d = ((rc[0] - nc[0]) ** 2 + (rc[1] - nc[1]) ** 2) ** 0.5
            if d < best_d and d < 40:
                best_d = d
                best = num
        if not best:
            continue

        used.add(id(best))
        used.add(id(ref))
        val = (best.get("display_text") or "").strip()
        display = f"{val}(REF)"
        bbox = best.get("text_bbox") or best.get("bbox")
        rb = ref.get("text_bbox") or ref.get("bbox")
        if bbox and rb:
            bbox = [
                min(bbox[0], rb[0]),
                min(bbox[1], rb[1]),
                max(bbox[2], rb[2]),
                max(bbox[3], rb[3]),
            ]
        try:
            nominal = float(val)
        except ValueError:
            nominal = None

        merged.append({
            **best,
            "entity_type": "reference_dimension",
            "display_text": display,
            "nominal_text": display,
            "nominal": nominal,
            "grammar_rule": "REFERENCE_SUFFIX_MERGE",
            "text_bbox": bbox,
            "bbox": bbox,
            "center": [
                (bbox[0] + bbox[2]) / 2 if bbox else best.get("center", [0, 0])[0],
                (bbox[1] + bbox[3]) / 2 if bbox else best.get("center", [0, 0])[1],
            ],
        })

    if not merged:
        return entities

    out = [e for e in kept if id(e) not in used]
    out.extend(merged)
    return out


def prepare_entities_for_export(entities, page_rect, skip_zone_filter=False):
    """Final cleanup pass before writing JSON."""
    entities = fuse_vertical_digit_chains(entities)
    entities = merge_reference_suffix_entities(entities)

    exportable = []
    for ent in entities:
        if should_export_entity(ent, page_rect, skip_zone_filter=skip_zone_filter):
            exportable.append(ent)

    return exportable
