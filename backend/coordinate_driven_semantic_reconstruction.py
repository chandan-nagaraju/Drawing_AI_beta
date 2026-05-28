import fitz
import json
import math
import re
from pathlib import Path
from statistics import median
from Filter_Engineering import (
    is_dimension,
    is_grammar_operator,
    is_nominal_dimension,
    is_tolerance_modifier,
)
from Dimension_Grammar import apply_dimension_grammar_to_glyph_group, is_tolerance_operator
from Engineering_Plausibility import (
    SRC_ANCHOR_RADIUS_MM,
    SRC_PAIR_MAX_GAP_MM,
    can_merge_tolerance_modifier,
    can_reconstruct_src_pair,
    is_valid_src_dimension,
    validate_dimension_entity,
    validate_tolerance_modifier_dict,
)
from Export_Validation import prepare_entities_for_export
from pdf_geometry import bbox_center_inside, bbox_intersects
from Geometry_Primitives import extract_geometry_primitives


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


TOLERANCE_VALUE_RE = re.compile(r"[±+\-−]\s*([\d]+(?:[.,]\d+)?)")
NOMINAL_NUMBER_RE = re.compile(r"([\d]+(?:[.,]\d+)?)")


def normalize_decimal_text(text: str) -> str:
    return (text or "").replace(",", ".")


def normalize_font_family(font_name: str) -> str:
    name = (font_name or "").strip()
    if not name:
        return ""
    name = re.sub(
        r"[-,_\s]*(Bold|Italic|Oblique|MT|Regular|Medium|Black|Light|Semibold)+",
        "",
        name,
        flags=re.I,
    )
    return name.lower()


def infer_font_weight(font_name: str) -> str:
    name = (font_name or "").lower()
    if any(k in name for k in ("bold", "black", "heavy")):
        return "bold"
    if any(k in name for k in ("light", "thin")):
        return "light"
    return "regular"


def infer_normalized_token_type(text: str) -> str:
    t = (text or "").strip().upper()
    if t in {"±", "+", "-", "−", "\u00b1"}:
        return "PLUS_MINUS"
    if re.match(r"^\d+(?:[.,]\d+)?$", t):
        return "NUMERIC"
    if t == "THK":
        return "THK"
    if t == "R":
        return "R_PREFIX"
    if t == "Ø":
        return "DIAMETER_PREFIX"
    return "OTHER"


def normalize_operator_glyph_char(ch: str) -> str:
    t = (ch or "").strip()
    # Common PDF fallback for ± in some embedded fonts.
    if t in {"�", "\ufffd"}:
        return "±"
    return ch


def canonicalize_display_text(entity_type: str, text: str) -> str:
    t = normalize_decimal_text((text or "").strip())
    # Normalize broken plus-minus glyphs from mixed PDF encodings.
    t = t.replace("�", "±")
    t = t.replace("\u00b1", "±")
    # Normalize OCR / font fallback for diameter prefix.
    t = re.sub(r"^O(?=\s*\d)", "Ø", t, flags=re.I)
    t = re.sub(r"\s+", " ", t)
    # Normalize tolerance operator variants and spacing:
    #   34±0.2, 34 ±0.2, 34± 0.2, 34 ± 0.2 -> 34±0.2
    t = re.sub(r"\s*(?:\+/-|\+/\-|±)\s*", "±", t)
    t = re.sub(r"\s*[Xx×]\s*", "X", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*THK\b", r"\1THK", t, flags=re.I)
    t = re.sub(r"\bTHK\s*(\d+(?:\.\d+)?)\b", r"\1THK", t, flags=re.I)
    t = re.sub(r"(THK)+$", "THK", t, flags=re.I)
    t = re.sub(r"^R\s+", "R", t, flags=re.I)
    t = re.sub(r"^Ø\s+", "Ø", t, flags=re.I)
    return t


def classify_entity_type(text):
    t = text.strip().upper()
    if re.search(r"^R\s*\d", t):
        return "radius_dimension"
    if "Ø" in t or re.search(r"^O\s*\d", t):
        return "diameter_dimension"
    if "°" in t:
        return "angular_dimension"
    if "THK" in t:
        return "thickness_dimension"
    if "(REF)" in t:
        return "reference_dimension"
    return "linear_dimension"


def parse_tolerance_modifier(text):
    t = (text or "").strip()
    if not t:
        return None

    # Explicit tolerance token: ±0.2, +0.2, -0.2
    explicit = re.match(r"^[±+\-−]\s*([\d]+(?:[.,]\d+)?)$", t)
    if explicit:
        value = float(normalize_decimal_text(explicit.group(1)))
        return {
            "type": "tolerance",
            "value": f"±{format_nominal_for_display(value)}",
            "tolerance_type": "bilateral",
            "plus": value,
            "minus": -value,
        }

    # Implicit tolerance recovery: decimal like 0.2 / 0,2 that lost ± glyph.
    implicit = re.match(r"^([\d]+(?:[.,]\d+)?)$", t)
    if not implicit:
        return None
    t_norm = normalize_decimal_text(implicit.group(1))
    if "." not in t_norm:
        return None
    value = float(t_norm)
    if not (0 < value <= 1.0):
        return None
    return {
        "type": "tolerance",
        "value": f"±{format_nominal_for_display(value)}",
        "tolerance_type": "bilateral",
        "plus": value,
        "minus": -value,
        "implicit": True,
    }


def parse_nominal_value(text):
    without_tol = TOLERANCE_VALUE_RE.sub("", text).strip()
    without_tol = re.sub(r"^[RØO]\s*", "", without_tol, flags=re.I)
    without_tol = normalize_decimal_text(without_tol)
    match = NOMINAL_NUMBER_RE.search(without_tol)
    if not match:
        return None, without_tol
    try:
        return float(match.group(1)), without_tol.strip()
    except ValueError:
        return None, without_tol.strip()


def normalize_decimal_display(text):
    return (text or "").replace(",", ".")


def compute_limits(nominal_value, tolerance_mod):
    if nominal_value is None or tolerance_mod is None:
        return None
    return {
        "min": round(nominal_value + tolerance_mod["minus"], 6),
        "max": round(nominal_value + tolerance_mod["plus"], 6),
    }


def build_dimension_entity(candidate, modifiers=None):
    text = candidate["text"].strip()

    # Generic syntax entity reconstructed from axis tokens (e.g. "17 X 19" where `X` was not extracted).
    if candidate.get("entity_type") == "SRC_dimension":
        values = candidate.get("values") or []
        if len(values) >= 2 and not is_valid_src_dimension(values[0], values[1]):
            candidate = {**candidate, "entity_type": None}
        else:
            return {
                "page": candidate["page"],
                "entity_type": "SRC_dimension",
                "display_text": text,
                "nominal": None,
                "nominal_text": text,
                "modifiers": [],
                "limits": None,
                "orientation": candidate["orientation"],
                "dimension_axis": candidate.get("dimension_axis", "horizontal"),
                "axis_bucket": candidate.get("axis_bucket"),
                "reading_direction": candidate.get("reading_direction", "left_to_right"),
                "grammar_rule": candidate.get("grammar_rule"),
                "operator": candidate.get("operator"),
                "values": candidate.get("values"),
                "semantic_zone_id": candidate.get("semantic_zone_id"),
                "anchor_center": candidate.get("anchor_center"),
                "anchor_radius_mm": candidate.get("anchor_radius_mm"),
                "reconstructed_operator": candidate.get("reconstructed_operator"),
                "confidence": candidate.get("confidence"),
                "text_bbox": candidate["bbox"],
                "center": candidate["center"],
                "glyph_count": candidate["glyph_count"],
                "font_size": candidate.get("font_size"),
                "font_family": candidate.get("font_family"),
            }

    # Explicit operator reconstruction: NUMBER X NUMBER (or using ×) inside extracted text.
    # This creates a generic SRC_dimension without assuming slot/chamfer/etc semantics.
    src_match = re.match(
        r"^\s*(?P<v1>\d+(?:\.\d+)?)\s*(?P<op>[Xx×])\s*(?P<v2>\d+(?:\.\d+)?)(?P<tail>.*)$",
        text,
    )
    if src_match:
        v1 = float(src_match.group("v1"))
        v2 = float(src_match.group("v2"))
        op = "X"
        tail = src_match.group("tail") or ""
        if 0 < v1 < 5000 and 0 < v2 < 5000 and is_valid_src_dimension(v1, v2, tail):
            axis = candidate.get("dimension_axis", "horizontal")
            page = candidate.get("page", 1)
            # Caller may pass registry via candidate; without it, still enforce reading order in text.
            sep = "" if tail.startswith("(") else " "
            display_text = f"{format_nominal_for_display(v1)} {op} {format_nominal_for_display(v2)}{sep}{tail}".strip()
            return {
                "page": candidate["page"],
                "entity_type": "SRC_dimension",
                "display_text": display_text,
                "nominal": None,
                "nominal_text": display_text,
                "modifiers": [],
                "limits": None,
                "orientation": candidate["orientation"],
                "dimension_axis": candidate.get("dimension_axis", "horizontal"),
                "axis_bucket": candidate.get("axis_bucket"),
                "reading_direction": candidate.get("reading_direction", "left_to_right"),
                "grammar_rule": "EXPLICIT_X_OPERATOR",
                "operator": op,
                "values": [v1, v2],
                "reconstructed_operator": False,
                "confidence": 1.0,
                "text_bbox": candidate["bbox"],
                "center": candidate.get("center"),
                "glyph_count": candidate["glyph_count"],
                "font_size": candidate.get("font_size"),
                "font_family": candidate.get("font_family"),
            }

    if candidate.get("grammar_fused") and candidate.get("tolerance_value") is not None:
        nominal_value = candidate.get("nominal")
        nominal_text = candidate.get("nominal_text", text)
        tol_val = candidate["tolerance_value"]
        tol = {
            "type": "tolerance",
            "value": f"±{tol_val}".replace("±±", "±"),
            "tolerance_type": "bilateral",
            "plus": tol_val,
            "minus": -tol_val,
        }
        if not validate_tolerance_modifier_dict(tol, nominal_value):
            tol = None
        entity_modifiers = [tol] if tol else []
        display_text = candidate.get("text", text)
        limits = compute_limits(nominal_value, tol)
        return {
            "page": candidate["page"],
            "entity_type": classify_entity_type(display_text),
            "display_text": display_text,
            "nominal": nominal_value,
            "nominal_text": nominal_text,
            "modifiers": entity_modifiers,
            "limits": limits,
            "orientation": candidate["orientation"],
            "dimension_axis": candidate.get("dimension_axis", "horizontal"),
            "axis_bucket": candidate.get("axis_bucket"),
            "reading_direction": candidate.get("reading_direction", "left_to_right"),
            "grammar_rule": candidate.get("grammar_rule"),
            "text_bbox": candidate["bbox"],
            "center": candidate["center"],
            "glyph_count": candidate["glyph_count"],
            "font_size": candidate.get("font_size"),
            "font_family": candidate.get("font_family"),
        }

    tol = parse_tolerance_modifier(text)
    nominal_value, nominal_text = parse_nominal_value(text)
    entity_modifiers = list(modifiers or [])

    if tol and not validate_tolerance_modifier_dict(tol, nominal_value):
        tol = None

    if tol and not any(m["type"] == "tolerance" for m in entity_modifiers):
        entity_modifiers.append(tol)

    entity_modifiers = [
        m for m in entity_modifiers
        if m.get("type") != "tolerance" or validate_tolerance_modifier_dict(m, nominal_value)
    ]

    display_parts = [nominal_text or text]
    for mod in entity_modifiers:
        if mod["type"] == "tolerance":
            display_parts.append(mod["value"])
    display_text = " ".join(p for p in display_parts if p).strip()
    if tol and nominal_text:
        display_text = f"{nominal_text} {tol['value']}".strip()

    limits = compute_limits(nominal_value, next((m for m in entity_modifiers if m["type"] == "tolerance"), None))

    return {
        "page": candidate["page"],
        "entity_type": classify_entity_type(text),
        "display_text": display_text or text,
        "nominal": nominal_value,
        "nominal_text": nominal_text or text,
        "modifiers": entity_modifiers,
        "limits": limits,
        "orientation": candidate["orientation"],
        "dimension_axis": candidate.get("dimension_axis", "horizontal"),
        "axis_bucket": candidate.get("axis_bucket"),
        "reading_direction": candidate.get("reading_direction", "left_to_right"),
        "grammar_rule": candidate.get("grammar_rule"),
        "text_bbox": candidate["bbox"],
        "center": candidate["center"],
        "glyph_count": candidate["glyph_count"],
        "font_size": candidate.get("font_size"),
        "font_family": candidate.get("font_family"),
    }


def find_nearest_nominal_parent(modifier, nominals, tolerance_mod, max_dist=60):
    best = None
    best_score = float("inf")

    for nominal in nominals:
        # Tolerance is always a modifier of a numeric nominal dimension.
        if nominal.get("nominal") is None:
            continue
        if nominal["page"] != modifier["page"]:
            continue

        if not can_merge_tolerance_modifier(nominal, modifier, tolerance_mod):
            continue

        if not same_dimension_axis_line(nominal, modifier):
            continue

        axis = nominal.get("dimension_axis", "horizontal")
        d = distance(modifier["center"], nominal["center"])

        if axis == "vertical":
            if d > max_dist * 1.5:
                continue
            # Bottom → top: nominal below, tolerance above (smaller PDF y).
            dy = nominal["center"][1] - modifier["center"][1]
            if dy < 0:
                continue
            score = d
        else:
            if d > max_dist:
                continue
            dy = abs(modifier["center"][1] - nominal["center"][1])
            if dy > 22:
                continue
            # Left → right: tolerance must be to the right of nominal.
            dx = modifier["center"][0] - nominal["center"][0]
            if dx < 0:
                continue
            score = d
        if score < best_score:
            best_score = score
            best = nominal

    return best


def is_small_decimal_candidate(text: str) -> bool:
    t = normalize_decimal_text((text or "").strip())
    if not re.match(r"^0\.\d+$", t):
        return False
    try:
        v = float(t)
    except ValueError:
        return False
    return 0 < v <= 1.0


def _is_operator_token_text(text: str) -> bool:
    t = (text or "").strip()
    return t in {"±", "+", "-", "−", "\u00b1"}


def _forward_axis_delta(src, dst, axis) -> float:
    """
    Positive means dst is in forward reading direction from src.
    horizontal: left->right
    vertical: bottom->top (PDF y decreases upward)
    """
    if axis == "vertical":
        return float(src["center"][1]) - float(dst["center"][1])
    return float(dst["center"][0]) - float(src["center"][0])


def _cross_axis_delta(src, dst, axis) -> float:
    if axis == "vertical":
        return abs(float(src["center"][0]) - float(dst["center"][0]))
    return abs(float(src["center"][1]) - float(dst["center"][1]))


def _style_compatible(a, b, size_tol=1.8):
    fa = (a.get("font_family") or "").strip().lower()
    fb = (b.get("font_family") or "").strip().lower()
    if fa and fb and fa != fb:
        return False
    sa = float(a.get("font_size") or 0.0)
    sb = float(b.get("font_size") or 0.0)
    if sa > 0 and sb > 0:
        if abs(sa - sb) > size_tol:
            ratio = min(sa, sb) / max(sa, sb)
            # Allow superscript/subscript-like tolerance tokens (same family, smaller size).
            if ratio < 0.55:
                return False
    return True


def _plain_tolerance_token_value(text: str):
    t = normalize_decimal_text((text or "").strip())
    if not re.match(r"^\d+(?:\.\d+)?$", t):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    # Practical standalone tolerance token range.
    if not (0 < v <= 4.0):
        return None
    return v


def attach_strict_axis_tolerances(entities, raw_candidates):
    """
    Strict local pattern:
      NOMINAL -> OPERATOR(+/-/±) -> TOLERANCE_VALUE
    Constraints:
      - same page, same dimension_axis, same axis_bucket
      - forward direction only (never opposite)
      - compact spacing (operator must be immediately after nominal)
      - tolerance immediately after operator
    """
    if not entities or not raw_candidates:
        return

    op_tokens = []
    value_tokens = []
    for cand in raw_candidates:
        txt = (cand.get("text") or "").strip()
        if not txt:
            continue
        if _is_operator_token_text(txt):
            op_tokens.append(cand)
            continue
        if parse_tolerance_modifier(txt) or _plain_tolerance_token_value(txt) is not None:
            value_tokens.append(cand)

    if not op_tokens or not value_tokens:
        return

    for parent in entities:
        if parent.get("nominal") is None:
            continue
        if any(m.get("type") == "tolerance" for m in parent.get("modifiers", [])):
            continue

        axis = parent.get("dimension_axis", "horizontal")
        page = parent.get("page")
        if axis not in {"horizontal", "vertical"}:
            continue

        # Tight operator locality; "right after nominal", same axis line.
        max_nominal_to_op = 42.0 if axis == "horizontal" else 48.0
        max_cross = 10.0 if axis == "horizontal" else 12.0
        op_best = None
        op_best_score = float("inf")
        for op in op_tokens:
            if op.get("page") != page:
                continue
            if op.get("dimension_axis") != axis:
                continue
            if _forward_axis_delta(parent, op, axis) <= 0:
                continue
            fwd = _forward_axis_delta(parent, op, axis)
            cross = _cross_axis_delta(parent, op, axis)
            if fwd > max_nominal_to_op or cross > max_cross:
                continue
            if not _style_compatible(parent, op):
                continue
            score = fwd + 0.5 * cross
            if score < op_best_score:
                op_best_score = score
                op_best = op

        if not op_best:
            continue

        # Tight tolerance locality; immediate next to operator, same axis line.
        max_op_to_tol = 44.0 if axis == "horizontal" else 52.0
        tol_best = None
        tol_best_score = float("inf")
        for val in value_tokens:
            if val.get("page") != page:
                continue
            if val.get("dimension_axis") != axis:
                continue
            if _forward_axis_delta(op_best, val, axis) <= 0:
                continue
            fwd = _forward_axis_delta(op_best, val, axis)
            cross = _cross_axis_delta(op_best, val, axis)
            if fwd > max_op_to_tol or cross > max_cross:
                continue
            if not _style_compatible(parent, val):
                continue
            score = fwd + 0.5 * cross
            if score < tol_best_score:
                tol_best_score = score
                tol_best = val

        if not tol_best:
            continue

        op_text = (op_best.get("text") or "").strip()
        tol_text = normalize_decimal_text((tol_best.get("text") or "").strip())
        if op_text in {"\u00b1"}:
            op_text = "±"
        composed = f"{op_text}{tol_text}"
        tol = parse_tolerance_modifier(composed)
        if not tol:
            continue
        if not validate_dimension_entity(parent.get("nominal"), tol.get("plus")):
            continue

        parent["modifiers"].append(tol)
        parent["display_text"] = f"{parent['nominal_text']} {tol['value']}".strip()
        parent["limits"] = compute_limits(parent["nominal"], tol)
        merge_parts = [parent["text_bbox"]]
        if op_best.get("bbox"):
            merge_parts.append(op_best["bbox"])
        if tol_best.get("bbox"):
            merge_parts.append(tol_best["bbox"])
        parent["text_bbox"] = merge_bbox(merge_parts)
        parent["glyph_count"] += int(op_best.get("glyph_count", 1)) + int(tol_best.get("glyph_count", 1))
        cx = (parent["text_bbox"][0] + parent["text_bbox"][2]) / 2
        cy = (parent["text_bbox"][1] + parent["text_bbox"][3]) / 2
        parent["center"] = [cx, cy]


def find_nearest_nominal_for_implicit_decimal(modifier, nominals, tolerance_mod, max_dist=55):
    """
    Recover implicit tolerance where ± glyph was dropped (e.g. 50.7 + 0.2).
    Uses a slightly looser same-line tolerance than explicit ± binding.
    """
    best = None
    best_score = float("inf")
    for nominal in nominals:
        if nominal.get("nominal") is None:
            continue
        if nominal.get("page") != modifier.get("page"):
            continue

        d = distance(modifier["center"], nominal["center"])
        if d > max_dist * 1.2:
            continue

        dx_signed = modifier["center"][0] - nominal["center"][0]
        dx_abs = abs(dx_signed)
        dy_signed = nominal["center"][1] - modifier["center"][1]
        dy_abs = abs(dy_signed)

        horizontal_ok = (d <= max_dist) and (dx_signed >= 0) and (dy_abs <= 24)
        vertical_ok = (d <= max_dist * 1.2) and (dy_signed >= 0) and (dx_abs <= 24)
        if not (horizontal_ok or vertical_ok):
            continue

        horiz_score = d + 0.2 * dy_abs if horizontal_ok else float("inf")
        vert_score = d + 0.2 * dx_abs if vertical_ok else float("inf")
        score = min(horiz_score, vert_score)

        if score < best_score:
            best_score = score
            best = nominal
    return best


def attach_modifiers_to_dimensions(candidates):
    """
    STAGE 6 — Parent-child semantic binding.

    Tolerance tokens are modifiers attached to nearest nominal dimension.
    Standalone tolerance entities are never exported.
    """
    entities = []
    pending_modifiers = []
    pending_implicit_small_decimals = []
    raw_candidates = []

    for cand in candidates:
        text = cand["text"].strip()
        norm_text = normalize_decimal_text(text)
        raw_candidates.append(cand)

        if _is_operator_token_text(text):
            continue

        tol_probe = parse_tolerance_modifier(norm_text)
        # Explicit ± tokens and recovered small decimals can act as modifiers.
        if tol_probe and (
            is_tolerance_modifier(text)
            or norm_text.startswith(("+", "-", "±", "−"))
            or tol_probe.get("implicit")
        ):
            pending_modifiers.append(cand)
            continue

        if is_small_decimal_candidate(norm_text):
            pending_implicit_small_decimals.append(cand)
            continue

        if cand.get("grammar_fused"):
            entities.append(build_dimension_entity(cand))
            continue

        if not is_nominal_dimension(text):
            continue

        entities.append(build_dimension_entity(cand))

    # Strict deterministic operator binding first (axis + direction + locality).
    attach_strict_axis_tolerances(entities, raw_candidates)

    for mod in pending_modifiers:
        tol = parse_tolerance_modifier(mod["text"])
        if not tol:
            continue

        parent = find_nearest_nominal_parent(mod, entities, tol)
        if not parent or not can_merge_tolerance_modifier(parent, mod, tol):
            continue

        if any(m["type"] == "tolerance" for m in parent["modifiers"]):
            continue

        if not validate_dimension_entity(parent.get("nominal"), tol.get("plus")):
            continue

        parent["modifiers"].append(tol)
        parent["display_text"] = f"{parent['nominal_text']} {tol['value']}".strip()
        parent["limits"] = compute_limits(parent["nominal"], tol)
        parent["text_bbox"] = merge_bbox([parent["text_bbox"], mod["bbox"]])
        parent["glyph_count"] += mod["glyph_count"]
        cx = (parent["text_bbox"][0] + parent["text_bbox"][2]) / 2
        cy = (parent["text_bbox"][1] + parent["text_bbox"][3]) / 2
        parent["center"] = [cx, cy]

    # Implicit tolerance recovery:
    # if bare 0.2 appears tightly aligned to a nominal on same axis, attach as ±0.2.
    for mod in pending_implicit_small_decimals:
        t = normalize_decimal_text(mod["text"])
        try:
            val = float(t)
        except ValueError:
            continue
        tol = {
            "type": "tolerance",
            "value": f"±{format_nominal_for_display(val)}",
            "tolerance_type": "bilateral",
            "plus": val,
            "minus": -val,
            "implicit": True,
        }
        parent = find_nearest_nominal_for_implicit_decimal(mod, entities, tol, max_dist=55)
        if not parent:
            # Fallback for dropped ± glyph cases: attach to nearest nominal if local.
            best = None
            best_d = float("inf")
            for cand_parent in entities:
                if cand_parent.get("page") != mod.get("page"):
                    continue
                if cand_parent.get("nominal") is None:
                    continue
                if any(m.get("type") == "tolerance" for m in cand_parent.get("modifiers", [])):
                    continue
                d = distance(mod["center"], cand_parent["center"])
                if d < best_d and d <= 70:
                    best_d = d
                    best = cand_parent
            parent = best
        if not parent:
            continue
        if parent.get("nominal") is None:
            continue
        if any(m.get("type") == "tolerance" for m in parent["modifiers"]):
            continue
        if not validate_dimension_entity(parent.get("nominal"), tol.get("plus")):
            continue

        parent["modifiers"].append(tol)
        parent["display_text"] = f"{parent['nominal_text']} {tol['value']}".strip()
        parent["limits"] = compute_limits(parent["nominal"], tol)
        parent["text_bbox"] = merge_bbox([parent["text_bbox"], mod["bbox"]])
        parent["glyph_count"] += mod["glyph_count"]
        cx = (parent["text_bbox"][0] + parent["text_bbox"][2]) / 2
        cy = (parent["text_bbox"][1] + parent["text_bbox"][3]) / 2
        parent["center"] = [cx, cy]

    return [sanitize_dimension_entity(ent) for ent in entities]


def sanitize_dimension_entity(entity):
    """Final pass: drop invalid tolerance modifiers; rebuild display + limits."""
    valid_mods = []
    for mod in entity.get("modifiers", []):
        if mod.get("type") == "tolerance":
            if validate_tolerance_modifier_dict(mod, entity.get("nominal")):
                valid_mods.append(mod)
        else:
            valid_mods.append(mod)

    entity["modifiers"] = valid_mods
    tol = next((m for m in valid_mods if m["type"] == "tolerance"), None)

    entity["nominal_text"] = normalize_decimal_display(entity.get("nominal_text", ""))
    entity["display_text"] = normalize_decimal_display(entity.get("display_text", ""))

    if tol:
        entity["display_text"] = f"{entity['nominal_text']} {tol['value']}".strip()
        entity["limits"] = compute_limits(entity.get("nominal"), tol)
    else:
        entity["display_text"] = entity.get("nominal_text") or entity.get("display_text", "")
        entity["limits"] = None

    # Engineering formatting rule: radius dimension must display with leading `R`.
    if entity.get("entity_type") == "radius_dimension":
        nominal_text = (entity.get("nominal_text") or "").strip()
        if nominal_text and not nominal_text.upper().startswith("R"):
            entity["nominal_text"] = f"R{nominal_text}"

        # Rebuild display_text so it reflects the corrected nominal_text.
        tol = next((m for m in valid_mods if m.get("type") == "tolerance"), None)
        if tol:
            entity["display_text"] = f"{entity['nominal_text']} {tol['value']}".strip()
        else:
            entity["display_text"] = entity.get("nominal_text", "").strip()

    # Engineering formatting rule: diameter dimension must display with leading `Ø`.
    if entity.get("entity_type") == "diameter_dimension":
        nominal_text = (entity.get("nominal_text") or "").strip()
        nominal_text = re.sub(r"^[Oo]\s*", "Ø", nominal_text)
        if nominal_text and not nominal_text.startswith("Ø"):
            nominal_text = f"Ø{nominal_text}"
        entity["nominal_text"] = nominal_text
        tol = next((m for m in valid_mods if m.get("type") == "tolerance"), None)
        if tol:
            entity["display_text"] = f"{entity['nominal_text']} {tol['value']}".strip()
        else:
            entity["display_text"] = entity.get("nominal_text", "").strip()

    if entity.get("entity_type") == "thickness_dimension":
        nominal_text = (entity.get("nominal_text") or "").strip()
        nominal_text = re.sub(r"\s*THK\s*$", "", nominal_text, flags=re.I)
        entity["nominal_text"] = nominal_text
        if nominal_text:
            entity["display_text"] = f"{nominal_text} THK"
        entity["semantic_locked"] = True

    entity["display_text"] = canonicalize_display_text(
        entity.get("entity_type", ""),
        entity.get("display_text", ""),
    )

    return entity


def merge_bbox(bboxes):
    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[2] for b in bboxes)
    y1 = max(b[3] for b in bboxes)
    return [x0, y0, x1, y1]


def enrich_glyph_metrics(glyph):
    x0, y0, x1, y1 = glyph["bbox"]
    glyph["w"] = max(x1 - x0, 0.1)
    glyph["h"] = max(y1 - y0, 0.1)
    return glyph


def adaptive_thresholds(glyphs):
    if not glyphs:
        return {"line_tol": 5, "h_gap_max": 20, "v_gap_max": 20, "size_tol": 1, "col_tol": 3}

    widths = [g["w"] for g in glyphs]
    heights = [g["h"] for g in glyphs]
    med_w = median(widths) or 5
    med_h = median(heights) or 5

    return {
        "line_tol": max(0.5 * med_h, 2),
        "col_tol": max(0.35 * med_w, 2),
        # Cap chain-link gaps so dense drawings don't collapse distant labels
        # into giant axis chains.
        "h_gap_max": min(max(1.5 * med_w, 5), 28),
        "v_gap_max": min(max(1.2 * med_h, 5), 14),
        "size_tol": max(0.35 * med_h, 0.5),
        "max_chain_len": 12,
    }


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[x]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def same_text_group(a, b, size_tol):
    fam_a = a.get("font_family") or normalize_font_family(a.get("font", ""))
    fam_b = b.get("font_family") or normalize_font_family(b.get("font", ""))
    if fam_a and fam_b and fam_a != fam_b:
        # Keep operator inference style-agnostic (bold ± should still fuse).
        a_type = a.get("normalized_type") or infer_normalized_token_type(a.get("char", ""))
        b_type = b.get("normalized_type") or infer_normalized_token_type(b.get("char", ""))
        if "PLUS_MINUS" not in {a_type, b_type}:
            return False
    return abs(a["size"] - b["size"]) <= size_tol


def horizontal_link(a, b, thresholds):
    dx = b["cx"] - a["cx"]
    dy = abs(b["cy"] - a["cy"])
    if dx <= 0:
        return False
    if dy > thresholds["line_tol"]:
        return False
    if dx > thresholds["h_gap_max"]:
        return False
    return same_text_group(a, b, thresholds["size_tol"])


def vertical_axis_link(a, b, thresholds):
    """Glyphs on the same vertical dimension column (same axis, any vertical gap)."""
    dx = abs(b["cx"] - a["cx"])
    if dx > thresholds["col_tol"]:
        return False
    dy = abs(b["cy"] - a["cy"])
    if dy <= 0 or dy > thresholds["v_gap_max"]:
        return False
    return same_text_group(a, b, thresholds["size_tol"])


def is_numeric_dimension_chain(text):
    stripped = text.strip()
    if not stripped:
        return False
    digit_ratio = sum(ch.isdigit() for ch in stripped) / len(stripped)
    return digit_ratio >= 0.6 or bool(re.search(r"^[RØ±]|THK|°", stripped, re.I))


def split_chain_by_spacing(group, axis):
    if len(group) < 3:
        return [group]

    ordered = sort_group_by_axis(group, axis)
    gaps = []
    for i in range(1, len(ordered)):
        if axis == "vertical":
            gaps.append(abs(ordered[i - 1]["cy"] - ordered[i]["cy"]))
        else:
            gaps.append(ordered[i]["cx"] - ordered[i - 1]["cx"])

    med_gap = median(gaps) or 1
    chunks = [[ordered[0]]]
    chunk_start_coord = ordered[0]["cy"] if axis == "vertical" else ordered[0]["cx"]
    max_span = 120 if axis == "vertical" else 220
    for i in range(1, len(ordered)):
        gap = gaps[i - 1]
        cur_coord = ordered[i]["cy"] if axis == "vertical" else ordered[i]["cx"]
        span = abs(cur_coord - chunk_start_coord)
        if gap > 2.2 * med_gap or span > max_span:
            chunks.append([ordered[i]])
            chunk_start_coord = cur_coord
        else:
            chunks[-1].append(ordered[i])
    return chunks


def split_oversized_chain(group, axis, thresholds):
    chunks = split_chain_by_spacing(group, axis)
    max_len = thresholds["max_chain_len"]
    final = []
    for chunk in chunks:
        if len(chunk) <= max_len:
            final.append(chunk)
            continue
        token = chain_to_token(chunk, axis)
        if is_numeric_dimension_chain(token):
            for i in range(0, len(chunk), max_len):
                final.append(chunk[i : i + max_len])
        else:
            for g in chunk:
                final.append([g])
    return final


def build_axis_components(glyphs, axis, thresholds):
    """Merge glyphs only along the given dimension axis within one axis bucket."""
    n = len(glyphs)
    uf = UnionFind(n)
    link_fn = horizontal_link if axis == "horizontal" else vertical_axis_link

    for i in range(n):
        for j in range(i + 1, n):
            a, b = glyphs[i], glyphs[j]
            if link_fn(a, b, thresholds) or link_fn(b, a, thresholds):
                uf.union(i, j)

    components = {}
    for i in range(n):
        root = uf.find(i)
        components.setdefault(root, []).append(glyphs[i])

    return list(components.values())


def detect_glyph_dimension_axis(glyph, all_glyphs, thresholds):
    """STEP 1 — Classify each glyph's dimension axis from bbox + neighbors."""
    w, h = glyph["w"], glyph["h"]

    v_neighbors = count_aligned_neighbors(glyph, all_glyphs, "vertical", thresholds)
    h_neighbors = count_aligned_neighbors(glyph, all_glyphs, "horizontal", thresholds)

    if h > w * 1.15 and v_neighbors >= h_neighbors:
        return "vertical"
    if w > h * 1.15 and h_neighbors > v_neighbors:
        return "horizontal"
    if v_neighbors > h_neighbors and v_neighbors > 0:
        return "vertical"
    if h_neighbors > 0:
        return "horizontal"
    return "horizontal"


def axis_bucket_key(glyph, axis, thresholds):
    """Separate dimension columns (vertical) or rows (horizontal)."""
    if axis == "vertical":
        return int(round(glyph["cx"] / max(thresholds["col_tol"], 1)))
    return int(round(glyph["cy"] / max(thresholds["line_tol"], 1)))


def assign_glyph_axes(glyphs, thresholds):
    for glyph in glyphs:
        axis = detect_glyph_dimension_axis(glyph, glyphs, thresholds)
        glyph["dimension_axis"] = axis
        glyph["axis_bucket"] = axis_bucket_key(glyph, axis, thresholds)


def reading_direction_for_axis(axis):
    if axis == "vertical":
        return "bottom_to_top"
    if axis == "angled":
        return "along_vector"
    return "left_to_right"


def sort_group_by_axis(group, axis):
    """STEP 2 — Sort along reading axis (drafting convention)."""
    if axis == "vertical":
        # Bottom → top (larger PDF y = lower on sheet = read first)
        return sorted(group, key=lambda g: -g["cy"])
    if axis == "angled":
        return sorted(group, key=lambda g: (g["cx"], g["cy"]))
    # Horizontal: left → right
    return sorted(group, key=lambda g: g["cx"])


def chain_to_token(group, axis):
    """STEP 3 — Merge tokens only along the dimension axis reading order."""
    ordered = sort_group_by_axis(group, axis)
    text = "".join(g["char"] for g in ordered)
    if axis == "vertical":
        text = re.sub(r"(\d)(±)", r"\1 \2", text)
    return text.strip()


def count_aligned_neighbors(glyph, glyphs, axis, thresholds):
    count = 0
    for other in glyphs:
        if other is glyph:
            continue
        if axis == "vertical":
            if vertical_axis_link(glyph, other, thresholds):
                count += 1
        else:
            if horizontal_link(glyph, other, thresholds) or horizontal_link(other, glyph, thresholds):
                count += 1
    return count


def classify_singleton_fragment(glyph, glyphs, thresholds):
    axis = glyph.get("dimension_axis", "horizontal")
    if axis == "vertical":
        return "unresolved_vertical_fragment"
    if count_aligned_neighbors(glyph, glyphs, "horizontal", thresholds) > 0:
        return "unresolved_horizontal_fragment"
    return "isolated_glyph"


class DirectionalChainRegistry:
    """
    Deterministic single-pass chain reconstruction.

    - Horizontal: left → right only
    - Vertical: bottom → top only
    - Consumed glyphs/candidates cannot join another chain
  """

    def __init__(self):
        self._consumed_glyphs = set()
        self._consumed_candidates = set()
        self._src_pairs = set()

    def glyph_key(self, glyph, page=1):
        return (page, round(glyph["cx"], 1), round(glyph["cy"], 1), glyph["char"])

    def glyph_consumed(self, glyph, page=1):
        return self.glyph_key(glyph, page) in self._consumed_glyphs

    def consume_glyphs(self, glyphs, page=1):
        for g in glyphs:
            self._consumed_glyphs.add(self.glyph_key(g, page))

    def any_glyph_consumed(self, glyphs, page=1):
        return any(self.glyph_consumed(g, page) for g in glyphs)

    def candidate_key(self, candidate):
        bb = candidate.get("bbox") or candidate.get("text_bbox")
        if not bb:
            return (candidate.get("page"), candidate.get("text", ""))
        return (
            candidate.get("page"),
            round((bb[0] + bb[2]) / 2, 1),
            round((bb[1] + bb[3]) / 2, 1),
            candidate.get("text", ""),
        )

    def candidate_consumed(self, candidate):
        return self.candidate_key(candidate) in self._consumed_candidates

    def consume_candidate(self, candidate):
        self._consumed_candidates.add(self.candidate_key(candidate))

    def register_src_pair(self, page, v1, v2, axis):
        """
        Register SRC in locked reading order only.
        Rejects mirrored duplicate (28 X 17 after 17 X 28).
        """
        key = (page, axis, float(v1), float(v2))
        mirror_key = (page, axis, float(v2), float(v1))
        if key in self._src_pairs or mirror_key in self._src_pairs:
            return False
        self._src_pairs.add(key)
        return True

    def mirrored_src_seen(self, page, v1, v2, axis):
        return (page, axis, float(v2), float(v1)) in self._src_pairs


def same_dimension_axis_line(parent, modifier, axis_tolerance=12):
    axis = parent.get("dimension_axis")
    if axis != modifier.get("dimension_axis"):
        return False
    if axis == "vertical":
        return abs(parent["center"][0] - modifier["center"][0]) <= axis_tolerance
    return abs(parent["center"][1] - modifier["center"][1]) <= axis_tolerance


def reconstruct_dimension_chain(glyphs, registry=None):
    """
    STAGE 5 — Axis-aware dimension reconstruction.

    1) detect dimension axis per glyph (horizontal / vertical / angled)
    2) bucket by axis column/row (separate dimension lines)
    3) merge glyphs ONLY along that axis
    4) sort by drafting reading direction (L→R or bottom→top)
  """
    if not glyphs:
        return []

    registry = registry or DirectionalChainRegistry()
    glyphs = [enrich_glyph_metrics(dict(g)) for g in glyphs]
    thresholds = adaptive_thresholds(glyphs)
    assign_glyph_axes(glyphs, thresholds)

    page = glyphs[0].get("page", 1) if glyphs else 1
    used_glyph_ids = set()
    chains = []
    chain_id = 0

    for axis in ("horizontal", "vertical"):
        axis_glyphs = [
            g for g in glyphs
            if g["dimension_axis"] == axis and not registry.glyph_consumed(g, page)
        ]
        buckets = {}
        for g in axis_glyphs:
            buckets.setdefault(g["axis_bucket"], []).append(g)

        for bucket_id, bucket_glyphs in buckets.items():
            components = build_axis_components(bucket_glyphs, axis, thresholds)

            for group in components:
                has_tol_op = any(is_tolerance_operator(g["char"]) for g in group)
                sub_groups = (
                    [group]
                    if has_tol_op
                    else split_oversized_chain(group, axis, thresholds)
                )

                for sub_group in sub_groups:
                    # Single directional traversal: sort once, reconstruct once.
                    sub_group = sort_group_by_axis(sub_group, axis)
                    glyph_ids = tuple((g["cx"], g["cy"], g["char"]) for g in sub_group)
                    if any(gid in used_glyph_ids for gid in glyph_ids):
                        continue
                    if registry.any_glyph_consumed(sub_group, page):
                        continue

                    fused_list = apply_dimension_grammar_to_glyph_group(
                        sub_group, axis, sort_group_by_axis, merge_bbox
                    )

                    if not fused_list and has_tol_op:
                        for gid in glyph_ids:
                            used_glyph_ids.add(gid)
                        continue

                    if not fused_list:
                        token = chain_to_token(sub_group, axis)
                        if is_grammar_operator(token) or is_tolerance_operator(token):
                            continue
                        if len(sub_group) > 1 and not is_numeric_dimension_chain(token):
                            continue
                        fused_list = [{
                            "text": token,
                            "glyphs": sub_group,
                            "bbox": merge_bbox([g["bbox"] for g in sub_group]),
                            "grammar_fused": False,
                            "grammar_rule": "CHAIN_STRING",
                        }]

                    for fused in fused_list:
                        fused_glyphs = fused["glyphs"]
                        fused_ids = tuple((g["cx"], g["cy"], g["char"]) for g in fused_glyphs)
                        if any(gid in used_glyph_ids for gid in fused_ids):
                            continue
                        if registry.any_glyph_consumed(fused_glyphs, page):
                            continue

                        values = fused.get("values") or []
                        if fused.get("entity_type") == "SRC_dimension" and len(values) >= 2:
                            if not registry.register_src_pair(page, values[0], values[1], axis):
                                registry.consume_glyphs(fused_glyphs, page)
                                continue

                        for gid in fused_ids:
                            used_glyph_ids.add(gid)
                        registry.consume_glyphs(fused_glyphs, page)

                        orientation = axis if len(fused_glyphs) > 1 else classify_singleton_fragment(
                            fused_glyphs[0], glyphs, thresholds
                        )

                        chain_id += 1
                        chains.append({
                            "glyphs": fused_glyphs,
                            "text": fused["text"],
                            "orientation": orientation,
                            "dimension_axis": axis,
                            "axis_bucket": bucket_id,
                            "reading_direction": reading_direction_for_axis(axis),
                            "direction_locked": True,
                            "chain_id": chain_id,
                            "glyph_count": len(fused_glyphs),
                            "bbox": fused["bbox"],
                            "grammar_fused": fused.get("grammar_fused", False),
                            "grammar_rule": fused.get("grammar_rule"),
                            "entity_type": fused.get("entity_type"),
                            "operator": fused.get("operator"),
                            "values": fused.get("values"),
                            "nominal": fused.get("nominal"),
                            "nominal_text": fused.get("nominal_text"),
                            "tolerance_value": fused.get("tolerance_value"),
                        })

    for g in glyphs:
        gid = (g["cx"], g["cy"], g["char"])
        if gid in used_glyph_ids or registry.glyph_consumed(g, page):
            continue

        if is_tolerance_operator(g["char"]) or is_grammar_operator(g["char"]):
            used_glyph_ids.add(gid)
            registry.consume_glyphs([g], page)
            axis = g["dimension_axis"]
            chains.append({
                "glyphs": [g],
                "text": g["char"],
                "orientation": classify_singleton_fragment(g, glyphs, thresholds),
                "dimension_axis": axis,
                "axis_bucket": g["axis_bucket"],
                "reading_direction": reading_direction_for_axis(axis),
                "glyph_count": 1,
                "bbox": g["bbox"],
                "grammar_fused": False,
                "grammar_rule": "OPERATOR_SINGLETON",
            })
            continue

        used_glyph_ids.add(gid)
        registry.consume_glyphs([g], page)
        axis = g["dimension_axis"]
        chains.append({
            "glyphs": [g],
            "text": g["char"],
            "orientation": classify_singleton_fragment(g, glyphs, thresholds),
            "dimension_axis": axis,
            "axis_bucket": g["axis_bucket"],
            "reading_direction": reading_direction_for_axis(axis),
            "glyph_count": 1,
            "bbox": g["bbox"],
            "grammar_fused": False,
        })

    return chains


def deduplicate_entities(entities):
    # Safety net: drop mirrored SRC pairs (28 X 17 vs 17 X 28).
    src_seen = set()
    filtered = []
    for ent in entities:
        if ent.get("entity_type") != "SRC_dimension":
            filtered.append(ent)
            continue
        vals = ent.get("values") or []
        if len(vals) >= 2:
            axis = ent.get("dimension_axis", "horizontal")
            mirror_key = (ent["page"], axis, float(vals[1]), float(vals[0]))
            forward_key = (ent["page"], axis, float(vals[0]), float(vals[1]))
            if mirror_key in src_seen:
                continue
            if forward_key in src_seen:
                continue
            src_seen.add(forward_key)
            src_seen.add(mirror_key)
        filtered.append(ent)
    entities = filtered

    best = {}
    orientation_rank = {
        "horizontal": 5,
        "vertical": 5,
        "rotated": 4,
        "unresolved_vertical_fragment": 2,
        "unresolved_horizontal_fragment": 2,
        "isolated_glyph": 1,
    }

    for ent in entities:
        key = (ent["page"], round(ent["center"][0], 1), round(ent["center"][1], 1), ent["display_text"])
        rank = orientation_rank.get(ent["orientation"], 0)
        has_tol = 1 if ent.get("modifiers") else 0
        score = (has_tol, rank, ent["glyph_count"], len(ent["display_text"]))
        if key not in best or score > best[key]["_score"]:
            ent["_score"] = score
            best[key] = ent

    deduped = []
    for item in best.values():
        item.pop("_score", None)
        deduped.append(item)
    return deduped


def next_versioned_output_path(output_path):
    base = Path(output_path)
    stem = base.stem
    suffix = base.suffix or ".json"
    parent = base.parent if str(base.parent) else Path(".")

    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def mm_to_points(mm: float) -> float:
    """
    Convert mm to PDF coordinate units.

    PyMuPDF "user space" in this repo behaves closer to ~300 DPI units for our drawings,
    so use 300 units/inch to make the 2mm threshold meaningful.
    """
    units_per_inch = 300.0
    return mm * units_per_inch / 25.4


def format_nominal_for_display(nominal) -> str:
    """Clean nominal formatting for engineering text display."""
    if nominal is None:
        return ""
    try:
        n = float(nominal)
    except (TypeError, ValueError):
        return str(nominal)

    if abs(n - round(n)) < 1e-6:
        return str(int(round(n)))

    return f"{n:.4f}".rstrip("0").rstrip(".")


def entity_center_from_bbox(ent):
    c = ent.get("center")
    if c and len(c) == 2:
        return c
    bb = ent.get("bbox") or ent.get("text_bbox")
    if bb and len(bb) == 4:
        return [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]
    return None


def _line_axis_compatible(geom, axis):
    if geom.get("geometry_type") not in {"line", "centerline"}:
        return False
    bbox = geom.get("bbox") or []
    if len(bbox) != 4:
        return False
    w = abs(bbox[2] - bbox[0])
    h = abs(bbox[3] - bbox[1])
    # Dimension ownership should align with chain axis.
    if axis == "vertical":
        return h >= max(10.0, 1.5 * w)
    return w >= max(10.0, 1.5 * h)


def nearest_owner_primitive(candidate, page_primitives, axis):
    center = entity_center_from_bbox(candidate)
    if not center or not page_primitives:
        return None, float("inf")

    compatible = [g for g in page_primitives if _line_axis_compatible(g, axis)]
    if not compatible:
        return None, float("inf")

    best = min(compatible, key=lambda g: distance(center, g["center"]))
    return best, distance(center, best["center"])


def reconstruct_src_dimensions_local(
    candidates,
    page_primitives_by_page=None,
    registry=None,
    anchor_radius_mm=SRC_ANCHOR_RADIUS_MM,
    pair_max_gap_mm=SRC_PAIR_MAX_GAP_MM,
):
    """
    Reconstruct missing `X` between numeric pairs inside a local semantic zone.

    Never searches globally across the page — only adjacent nominals on the same
    dimension axis bucket, with line alignment + operator-scale gap + grammar validation.
    """
    if not candidates:
        return []

    registry = registry or DirectionalChainRegistry()
    numeric_re = re.compile(r"^\d+(\.\d+)?$")
    numeric = []
    for cand in candidates:
        if cand.get("entity_type") == "SRC_dimension":
            continue
        if registry.candidate_consumed(cand):
            continue
        text = (cand.get("text") or "").strip()
        if not numeric_re.match(text):
            continue
        if len(text) > 6:
            continue
        numeric.append(cand)

    if len(numeric) < 2:
        return candidates

    anchor_radius_pt = mm_to_points(anchor_radius_mm)
    max_pair_gap_pt = mm_to_points(pair_max_gap_mm)

    groups = {}
    for cand in numeric:
        key = (cand["page"], cand.get("dimension_axis"), cand.get("axis_bucket"))
        groups.setdefault(key, []).append(cand)

    used_ids = set()
    fused = []
    zone_id = 0

    for group in groups.values():
        if len(group) < 2:
            continue

        axis = group[0].get("dimension_axis", "horizontal")
        if axis == "vertical":
            group.sort(key=lambda e: -entity_center_from_bbox(e)[1])
        else:
            group.sort(key=lambda e: entity_center_from_bbox(e)[0])

        zone_id += 1
        zone_center = entity_center_from_bbox(group[len(group) // 2])

        # Forward-only directional pairing (never reverse traversal).
        for i in range(len(group) - 1):
            left = group[i]
            right = group[i + 1]
            if id(left) in used_ids or id(right) in used_ids:
                continue
            if registry.candidate_consumed(left) or registry.candidate_consumed(right):
                continue

            ca = entity_center_from_bbox(left)
            cb = entity_center_from_bbox(right)
            if not ca or not cb:
                continue

            la, ra = left["bbox"], right["bbox"]
            if axis == "vertical":
                line_offset = abs(ca[0] - cb[0])
                gap = la[1] - ra[3]
            else:
                line_offset = abs(ca[1] - cb[1])
                gap = ra[0] - la[2]

            if line_offset > anchor_radius_pt:
                continue
            if gap <= 0 or gap > max_pair_gap_pt:
                continue

            # Geometry ownership gate:
            # both numeric tokens must belong to the same local dimension line primitive.
            page = left.get("page", 1)
            page_prims = (page_primitives_by_page or {}).get(page, [])
            owner_l, dist_l = nearest_owner_primitive(left, page_prims, axis)
            owner_r, dist_r = nearest_owner_primitive(right, page_prims, axis)
            owner_radius_pt = mm_to_points(anchor_radius_mm * 1.4)
            if not owner_l or not owner_r:
                continue
            if owner_l.get("primitive_id") != owner_r.get("primitive_id"):
                continue
            if dist_l > owner_radius_pt or dist_r > owner_radius_pt:
                continue

            w_left = la[2] - la[0]
            w_right = ra[2] - ra[0]
            min_operator_gap = 0.35 * max(w_left, w_right, 1.0)
            if gap < min_operator_gap:
                continue

            try:
                v1 = float(left["text"])
                v2 = float(right["text"])
            except ValueError:
                continue

            pair_orientation = left.get("orientation") or right.get("orientation")
            total_glyphs = left.get("glyph_count", 0) + right.get("glyph_count", 0)
            if not can_reconstruct_src_pair(
                v1,
                v2,
                orientation=pair_orientation,
                glyph_count=total_glyphs,
            ):
                continue

            if not registry.register_src_pair(page, v1, v2, axis):
                continue

            display = f"{format_nominal_for_display(v1)} X {format_nominal_for_display(v2)}"
            merged_bbox = merge_bbox([la, ra])
            anchor_center = [
                (merged_bbox[0] + merged_bbox[2]) / 2,
                (merged_bbox[1] + merged_bbox[3]) / 2,
            ]
            confidence = round(max(0.55, 1.0 - (gap / max_pair_gap_pt) * 0.35), 3)

            fused.append({
                "page": left["page"],
                "text": display,
                "bbox": merged_bbox,
                "center": anchor_center,
                "glyph_count": left.get("glyph_count", 0) + right.get("glyph_count", 0),
                "orientation": left.get("orientation", axis),
                "dimension_axis": axis,
                "axis_bucket": left.get("axis_bucket"),
                "reading_direction": reading_direction_for_axis(axis),
                "direction_locked": True,
                "grammar_fused": True,
                "grammar_rule": "LOCAL_ANCHOR_PAIR",
                "entity_type": "SRC_dimension",
                "operator": "X",
                "values": [v1, v2],
                "semantic_zone_id": zone_id,
                "anchor_center": anchor_center,
                "anchor_radius_mm": anchor_radius_mm,
                "reconstructed_operator": True,
                "confidence": confidence,
            })
            used_ids.add(id(left))
            used_ids.add(id(right))
            registry.consume_candidate(left)
            registry.consume_candidate(right)

    if not fused:
        return candidates

    remaining = [c for c in candidates if id(c) not in used_ids]
    remaining.extend(fused)
    return remaining


def fuse_thickness_keywords(entities, distance_mm=2.0):
    """
    Bind standalone thickness keyword entities (THK) to nearby numeric nominals.

    Rule:
      If a `THK` token exists within `distance_mm` (all directions) of a numeric dimension,
      rewrite that numeric entity as: "<N> THK" and drop the standalone THK token.
    """
    if not entities:
        return []

    max_dist = mm_to_points(distance_mm)

    thickness_tokens = []
    for e in entities:
        if e.get("semantic_locked"):
            continue
        disp = (e.get("display_text") or "").upper().strip()
        nom = (e.get("nominal_text") or "").upper().strip()
        if e.get("entity_type") == "thickness_dimension" and ((disp == "THK") or (nom == "THK")):
            thickness_tokens.append(e)

    numeric_entities = [
        e for e in entities
        if e.get("nominal") is not None
        and e.get("entity_type") != "thickness_dimension"
        and not e.get("semantic_locked")
        and "THK" not in ((e.get("display_text") or "").upper())
        and "THK" not in ((e.get("nominal_text") or "").upper())
    ]

    if not thickness_tokens:
        return entities

    if not numeric_entities:
        # Drop unbound standalone THK for cleaner output.
        return [e for e in entities if not (e.get("entity_type") == "thickness_dimension" and e.get("nominal") is None)]

    def entity_center(ent):
        c = ent.get("center")
        if c and len(c) == 2:
            return c
        bb = ent.get("text_bbox") or ent.get("bbox")
        if bb and len(bb) == 4:
            return [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]
        return None

    used_numeric = set()
    used_thk = set()
    attached_any = False

    for thk in thickness_tokens:
        thk_center = entity_center(thk)
        if not thk_center:
            continue

        best = None
        best_d = float("inf")

        for num in numeric_entities:
            num_id = id(num)
            if num_id in used_numeric:
                continue
            num_center = entity_center(num)
            if not num_center:
                continue

            d = distance(thk_center, num_center)
            if d <= max_dist and d < best_d:
                best = num
                best_d = d

        if best is None:
            continue

        used_numeric.add(id(best))
        used_thk.add(id(thk))
        attached_any = True

        best["entity_type"] = "thickness_dimension"
        best["nominal_text"] = format_nominal_for_display(best.get("nominal"))
        best["display_text"] = f"{best['nominal_text']} THK"
        best["semantic_locked"] = True

        # Expand bbox/center to cover both tokens.
        if thk.get("text_bbox") and best.get("text_bbox"):
            best["text_bbox"] = merge_bbox([best["text_bbox"], thk["text_bbox"]])
        if thk.get("bbox") and best.get("bbox"):
            best["bbox"] = merge_bbox([best["bbox"], thk["bbox"]])
        if best.get("text_bbox"):
            bx = best["text_bbox"]
            best["center"] = [(bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2]

    # Remove fused THK tokens + optionally any remaining unbound THK tokens.
    out = []
    for e in entities:
        if id(e) in used_thk:
            continue
        if e.get("entity_type") == "thickness_dimension" and e.get("nominal") is None:
            continue
        out.append(e)

    return out


def attach_symbol_prefixes_to_dimensions(entities, glyphs, max_gap_pt=16.0):
    """
    Recover symbol-prefixed dimensions when prefix glyphs are detached from numeric tokens.
    Example: R + 10 -> R10, Ø + 8 -> Ø8
    """
    if not entities or not glyphs:
        return entities

    symbol_glyphs = []
    for g in glyphs:
        ch = (g.get("char") or "").strip()
        if not ch:
            continue
        up = ch.upper()
        if up in {"R", "Ø", "O"}:
            symbol_glyphs.append((up, g))

    if not symbol_glyphs:
        return entities

    numeric_entities = []
    for ent in entities:
        if ent.get("nominal") is None:
            continue
        if ent.get("entity_type") in {"radius_dimension", "diameter_dimension", "thickness_dimension"}:
            continue
        bb = ent.get("text_bbox") or ent.get("bbox")
        if not bb:
            continue
        numeric_entities.append(ent)

    if not numeric_entities:
        return entities

    used_symbols = set()
    for idx, (sym, sg) in enumerate(symbol_glyphs):
        sb = sg.get("bbox")
        if not sb:
            continue
        sx0, sy0, sx1, sy1 = sb
        syc = (sy0 + sy1) / 2.0

        best = None
        best_score = float("inf")
        for ent in numeric_entities:
            eb = ent.get("text_bbox") or ent.get("bbox")
            if not eb:
                continue
            ex0, ey0, ex1, ey1 = eb
            eh = max(ey1 - ey0, 1.0)
            eyc = (ey0 + ey1) / 2.0

            # Prefix symbol should be on the left side of the numeric text.
            if sx0 >= ex0:
                continue
            # Allow slight overlap due tight CAD kerning (R50, Ø8).
            gap = max(0.0, ex0 - sx1)
            if gap > max_gap_pt:
                continue
            if abs(syc - eyc) > max(0.8 * eh, 10.0):
                continue

            score = gap + abs(syc - eyc) * 0.25
            if score < best_score:
                best_score = score
                best = ent

        if not best:
            continue

        used_symbols.add(idx)
        nominal_text = format_nominal_for_display(best.get("nominal"))
        if sym == "R":
            best["entity_type"] = "radius_dimension"
            best["nominal_text"] = f"R{nominal_text}"
            best["display_text"] = best["nominal_text"]
        else:
            best["entity_type"] = "diameter_dimension"
            best["nominal_text"] = f"Ø{nominal_text}"
            best["display_text"] = best["nominal_text"]

        if best.get("text_bbox"):
            best["text_bbox"] = merge_bbox([best["text_bbox"], [sx0, sy0, sx1, sy1]])
            best["bbox"] = best.get("text_bbox")
            tb = best["text_bbox"]
            best["center"] = [(tb[0] + tb[2]) / 2.0, (tb[1] + tb[3]) / 2.0]
        best["glyph_count"] = int(best.get("glyph_count") or 0) + 1
        best["grammar_rule"] = best.get("grammar_rule") or "PREFIX_SYMBOL_ATTACH"

    return entities


def _glyph_in_region(glyph, region_bbox, glyph_mode="intersects"):
    if not region_bbox:
        return True
    if glyph_mode == "center":
        return bbox_center_inside(glyph["bbox"], region_bbox)
    return bbox_intersects(glyph["bbox"], region_bbox)


def _extract_page_glyphs(page, page_index, region_bbox=None, glyph_mode="intersects"):
    glyphs = []
    text_data = page.get_text("rawdict")

    for block in text_data.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    text = normalize_operator_glyph_char(ch.get("c", ""))
                    if not text.strip():
                        continue

                    x0, y0, x1, y1 = ch["bbox"]
                    glyph = {
                        "page": page_index,
                        "char": text,
                        "normalized_char": text,
                        "bbox": [x0, y0, x1, y1],
                        "cx": (x0 + x1) / 2,
                        "cy": (y0 + y1) / 2,
                        "font": span.get("font", ""),
                        "font_family": normalize_font_family(span.get("font", "")),
                        "font_weight": infer_font_weight(span.get("font", "")),
                        "normalized_type": infer_normalized_token_type(text),
                        "size": span.get("size", 0),
                    }
                    if _glyph_in_region(glyph, region_bbox, glyph_mode):
                        glyphs.append(glyph)

    return glyphs


def extract_all_direction_span_entities(page, page_index, region_bbox=None):
    """
    Fallback extractor for non-axis-aligned dimension text.
    Captures rotated labels (e.g. R6 at arbitrary angles) and direct THK spans.
    """
    text_data = page.get_text("dict")
    entities = []

    for block in text_data.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            line_dir = line.get("dir") or (1.0, 0.0)
            dx, dy = float(line_dir[0]), float(line_dir[1])
            angle = math.degrees(math.atan2(dy, dx))
            angle_mod = abs(angle) % 180.0
            near_horizontal = angle_mod <= 12.0 or angle_mod >= 168.0
            near_vertical = abs(angle_mod - 90.0) <= 12.0
            rotated = not (near_horizontal or near_vertical)

            for span in line.get("spans", []):
                raw_text = (span.get("text") or "").strip()
                if not raw_text:
                    continue
                if len(raw_text) > 24:
                    continue

                bbox = span.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                span_bbox = [bbox[0], bbox[1], bbox[2], bbox[3]]
                if region_bbox and not bbox_intersects(span_bbox, region_bbox):
                    continue

                norm = normalize_decimal_text(raw_text).upper()
                has_digit = any(ch.isdigit() for ch in norm)
                if not has_digit:
                    continue

                # Keep this pass high-precision to avoid metadata pollution.
                looks_radius = bool(re.match(r"^R\s*\d", norm))
                looks_diameter = "Ø" in norm
                looks_thk = "THK" in norm
                looks_tol = "±" in norm
                if not (looks_radius or looks_diameter or looks_thk or (rotated and looks_tol)):
                    continue

                candidate = {
                    "page": page_index,
                    "text": raw_text,
                    "orientation": "rotated",
                    "dimension_axis": "angled",
                    "axis_bucket": round(((span_bbox[0] + span_bbox[2]) / 2) / 10),
                    "reading_direction": "angle_locked",
                    "glyph_count": max(1, len(raw_text.replace(" ", ""))),
                    "bbox": span_bbox,
                    "center": [(span_bbox[0] + span_bbox[2]) / 2, (span_bbox[1] + span_bbox[3]) / 2],
                    "grammar_fused": False,
                    "grammar_rule": "ALL_DIRECTION_SPAN_FALLBACK",
                }
                entities.append(build_dimension_entity(candidate))

    return entities


def extract_nearby_thickness_entities(page, page_index, region_bbox=None):
    """
    Build explicit thickness entities from nearby numeric + THK spans.
    Handles layouts where '3' and 'THK' are split into separate text spans.
    """
    text_data = page.get_text("dict")
    number_spans = []
    thk_spans = []

    for block in text_data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                if not text:
                    continue
                bbox = span.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                b = [bbox[0], bbox[1], bbox[2], bbox[3]]
                if region_bbox and not bbox_intersects(b, region_bbox):
                    continue

                t_upper = text.upper()
                if "THK" in t_upper:
                    thk_spans.append({"text": text, "bbox": b})
                elif re.match(r"^\d+(?:[.,]\d+)?$", text):
                    number_spans.append({"text": text, "bbox": b})

    entities = []
    for thk in thk_spans:
        tb = thk["bbox"]
        tc = [(tb[0] + tb[2]) / 2.0, (tb[1] + tb[3]) / 2.0]
        best = None
        best_score = float("inf")
        for num in number_spans:
            nb = num["bbox"]
            nc = [(nb[0] + nb[2]) / 2.0, (nb[1] + nb[3]) / 2.0]
            dx = abs(nc[0] - tc[0])
            dy = abs(nc[1] - tc[1])
            d = distance(nc, tc)
            if d > 26:
                continue
            if dx > 22 or dy > 22:
                continue
            score = d + 0.2 * min(dx, dy)
            if score < best_score:
                best_score = score
                best = num

        if not best:
            continue

        try:
            nominal_val = float(normalize_decimal_text(best["text"]))
        except Exception:
            nominal_val = None
        if nominal_val is None:
            continue

        merged = merge_bbox([best["bbox"], tb])
        display = f"{format_nominal_for_display(nominal_val)} THK"
        entities.append({
            "page": page_index,
            "entity_type": "thickness_dimension",
            "display_text": display,
            "nominal": nominal_val,
            "nominal_text": format_nominal_for_display(nominal_val),
            "modifiers": [],
            "limits": None,
            "orientation": "rotated",
            "dimension_axis": "angled",
            "axis_bucket": round(((merged[0] + merged[2]) / 2) / 10),
            "reading_direction": "angle_locked",
            "grammar_rule": "NEARBY_THK_SPAN_FALLBACK",
            "text_bbox": merged,
            "bbox": merged,
            "center": [(merged[0] + merged[2]) / 2.0, (merged[1] + merged[3]) / 2.0],
            "glyph_count": max(4, len(display.replace(" ", ""))),
        })

    return entities


def _entity_to_export_record(ent):
    return {
        "page": ent["page"],
        "entity_type": ent["entity_type"],
        "display_text": ent["display_text"],
        "nominal": ent["nominal"],
        "nominal_text": ent["nominal_text"],
        "modifiers": ent["modifiers"],
        "limits": ent["limits"],
        "orientation": ent["orientation"],
        "dimension_axis": ent.get("dimension_axis"),
        "axis_bucket": ent.get("axis_bucket"),
        "reading_direction": ent.get("reading_direction"),
        "grammar_rule": ent.get("grammar_rule"),
        "operator": ent.get("operator"),
        "values": ent.get("values"),
        "semantic_zone_id": ent.get("semantic_zone_id"),
        "anchor_center": ent.get("anchor_center"),
        "anchor_radius_mm": ent.get("anchor_radius_mm"),
        "reconstructed_operator": ent.get("reconstructed_operator"),
        "confidence": ent.get("confidence"),
        "chain_id": ent.get("chain_id"),
        "direction_locked": ent.get("direction_locked"),
        "text_bbox": ent["text_bbox"],
        "nearest_geometry_bbox": ent.get("nearest_geometry_bbox"),
        "nearest_geometry_type": ent.get("nearest_geometry_type"),
        "nearest_geometry_id": ent.get("nearest_geometry_id"),
        "distance": ent.get("distance"),
        "glyph_count": ent["glyph_count"],
    }


def choose_nearest_geometry_for_entity(entity, page_primitives):
    """
    Geometry-aware nearest primitive selection.
    Radius / diameter entities should prefer arc-like primitives over generic lines.
    """
    if not page_primitives:
        return None, float("inf")

    entity_type = entity.get("entity_type")
    preferred_types = set()
    if entity_type in {"radius_dimension", "diameter_dimension"}:
        preferred_types = {"circle_or_arc", "curve"}
    elif entity_type in {"linear_dimension", "SRC_dimension", "thickness_dimension"}:
        preferred_types = {"line", "centerline"}

    best = None
    best_score = float("inf")
    best_dist = float("inf")

    if preferred_types:
        preferred = [g for g in page_primitives if g.get("geometry_type") in preferred_types]
        if preferred:
            pref_best = min(preferred, key=lambda g: distance(entity["center"], g["center"]))
            pref_dist = distance(entity["center"], pref_best["center"])
            # For radius/diameter anchoring, prefer arc-like primitives if reasonably local.
            if entity_type in {"radius_dimension", "diameter_dimension"} and pref_dist <= 160.0:
                return pref_best, pref_dist

    for geom in page_primitives:
        dist = distance(entity["center"], geom["center"])
        penalty = 0.0
        gtype = geom.get("geometry_type")
        if preferred_types and gtype not in preferred_types:
            penalty = 40.0
        score = dist + penalty
        if score < best_score:
            best_score = score
            best_dist = dist
            best = geom

    return best, best_dist


def process_page_semantic(
    page,
    page_index,
    *,
    region_bbox=None,
    glyph_mode="intersects",
    user_selected_region=False,
):
    """Reconstruct exportable dimension entities for one PDF page (optional region clip)."""
    glyphs = _extract_page_glyphs(page, page_index, region_bbox, glyph_mode)
    page_drawings = extract_geometry_primitives(
        page,
        region_bbox=region_bbox,
        intersects_fn=bbox_intersects if region_bbox else None,
    )

    chain_registry = DirectionalChainRegistry()
    chains = reconstruct_dimension_chain(glyphs, registry=chain_registry)

    candidates = []
    for chain in chains:
        word = chain["text"].strip()
        if not word:
            continue

        bbox = chain["bbox"]
        center_x = (bbox[0] + bbox[2]) / 2
        center_y = (bbox[1] + bbox[3]) / 2

        candidate = {
            "page": page_index,
            "text": word,
            "bbox": bbox,
            "center": [center_x, center_y],
            "glyph_count": chain["glyph_count"],
            "font_size": median([g.get("size", 0) for g in chain.get("glyphs", [])]) if chain.get("glyphs") else None,
            "font_family": (chain.get("glyphs", [{}])[0].get("font_family") if chain.get("glyphs") else ""),
            "orientation": chain["orientation"],
            "dimension_axis": chain.get("dimension_axis", "horizontal"),
            "axis_bucket": chain.get("axis_bucket"),
            "reading_direction": chain.get("reading_direction", "left_to_right"),
            "grammar_fused": chain.get("grammar_fused", False),
            "grammar_rule": chain.get("grammar_rule"),
            "nominal": chain.get("nominal"),
            "nominal_text": chain.get("nominal_text"),
            "tolerance_value": chain.get("tolerance_value"),
            "entity_type": chain.get("entity_type"),
            "operator": chain.get("operator"),
            "values": chain.get("values"),
            "chain_id": chain.get("chain_id"),
            "direction_locked": chain.get("direction_locked"),
        }

        # Keep explicit operator tokens as candidates so strict tolerance matcher
        # can bind NOMINAL -> OPERATOR -> VALUE locally on the same axis.
        if is_grammar_operator(word) or is_tolerance_operator(word):
            candidates.append(candidate)
            continue
        if not chain.get("grammar_fused") and not is_dimension(word):
            continue

        candidates.append(candidate)

    candidates = reconstruct_src_dimensions_local(
        candidates,
        page_primitives_by_page={page_index: page_drawings},
        registry=chain_registry,
    )
    entities = attach_modifiers_to_dimensions(candidates)
    entities.extend(extract_all_direction_span_entities(page, page_index, region_bbox=region_bbox))
    entities.extend(extract_nearby_thickness_entities(page, page_index, region_bbox=region_bbox))
    entities = [sanitize_dimension_entity(ent) for ent in entities]
    entities = attach_symbol_prefixes_to_dimensions(entities, glyphs)
    entities = deduplicate_entities(entities)
    entities = fuse_thickness_keywords(entities, distance_mm=8.0)

    for ent in entities:
        nearest, nearest_dist = choose_nearest_geometry_for_entity(ent, page_drawings)
        ent["distance"] = nearest_dist
        ent["nearest_geometry_bbox"] = nearest["bbox"] if nearest else None
        ent["nearest_geometry_type"] = nearest.get("geometry_type") if nearest else None
        ent["nearest_geometry_id"] = nearest.get("primitive_id") if nearest else None
        if nearest and ent.get("entity_type") in {"radius_dimension", "diameter_dimension"}:
            ent["anchor_center"] = nearest.get("center")
            if ent.get("anchor_radius_mm") is None and ent.get("nominal") is not None:
                ent["anchor_radius_mm"] = ent.get("nominal")

    entities = prepare_entities_for_export(
        entities,
        page.rect,
        skip_zone_filter=user_selected_region,
    )

    if region_bbox:
        filtered = []
        for ent in entities:
            tb = ent.get("text_bbox")
            if tb and bbox_intersects(tb, region_bbox):
                filtered.append(ent)
        entities = filtered

    return [_entity_to_export_record(ent) for ent in entities]


def run_coordinate_driven_semantic_reconstruction(
    pdf_path="../X6C22514.pdf",
    output_path="../outputs/vector_relationships.json",
    versioned_only=True,
):
    doc = fitz.open(pdf_path)
    all_results = []

    for page_num, page in enumerate(doc):
        page_index = page_num + 1
        all_results.extend(process_page_semantic(page, page_index))

    if versioned_only:
        target_path = next_versioned_output_path(output_path)
    else:
        target_path = Path(output_path)

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=4, ensure_ascii=False)

    return all_results
