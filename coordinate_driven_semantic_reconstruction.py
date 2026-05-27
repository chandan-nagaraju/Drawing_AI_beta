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


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


TOLERANCE_VALUE_RE = re.compile(r"±\s*([\d.]+)")
NOMINAL_NUMBER_RE = re.compile(r"([\d.]+)")


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
    match = TOLERANCE_VALUE_RE.search(text)
    if not match:
        return None

    value = float(match.group(1))
    return {
        "type": "tolerance",
        "value": f"±{match.group(1)}",
        "tolerance_type": "bilateral",
        "plus": value,
        "minus": -value,
    }


def parse_nominal_value(text):
    without_tol = TOLERANCE_VALUE_RE.sub("", text).strip()
    without_tol = re.sub(r"^[RØ]\s*", "", without_tol, flags=re.I)
    match = NOMINAL_NUMBER_RE.search(without_tol)
    if not match:
        return None, without_tol
    try:
        return float(match.group(1)), without_tol.strip()
    except ValueError:
        return None, without_tol.strip()


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
            if dy > 12:
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


def attach_modifiers_to_dimensions(candidates):
    """
    STAGE 6 — Parent-child semantic binding.

    Tolerance tokens are modifiers attached to nearest nominal dimension.
    Standalone tolerance entities are never exported.
    """
    entities = []
    pending_modifiers = []

    for cand in candidates:
        text = cand["text"].strip()

        if is_grammar_operator(text) or is_tolerance_operator(text):
            continue

        if is_tolerance_modifier(text):
            pending_modifiers.append(cand)
            continue

        if cand.get("grammar_fused"):
            entities.append(build_dimension_entity(cand))
            continue

        if not is_nominal_dimension(text):
            continue

        entities.append(build_dimension_entity(cand))

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
        "h_gap_max": max(1.8 * med_w, 6),
        "v_gap_max": max(1.8 * med_h, 6),
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
    if a["font"] != b["font"]:
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
    for i in range(1, len(ordered)):
        gap = gaps[i - 1]
        if gap > 2.2 * med_gap:
            chunks.append([ordered[i]])
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


def reconstruct_src_dimensions_local(
    candidates,
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

            page = left.get("page", 1)
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
        disp = (e.get("display_text") or "").upper().strip()
        nom = (e.get("nominal_text") or "").upper().strip()
        if e.get("entity_type") == "thickness_dimension" and ((disp == "THK") or (nom == "THK")):
            thickness_tokens.append(e)

    numeric_entities = [
        e for e in entities
        if e.get("nominal") is not None and e.get("entity_type") != "thickness_dimension"
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
                    text = ch.get("c", "")
                    if not text.strip():
                        continue

                    x0, y0, x1, y1 = ch["bbox"]
                    glyph = {
                        "page": page_index,
                        "char": text,
                        "bbox": [x0, y0, x1, y1],
                        "cx": (x0 + x1) / 2,
                        "cy": (y0 + y1) / 2,
                        "font": span.get("font", ""),
                        "size": span.get("size", 0),
                    }
                    if _glyph_in_region(glyph, region_bbox, glyph_mode):
                        glyphs.append(glyph)

    return glyphs


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
        "distance": ent.get("distance"),
        "glyph_count": ent["glyph_count"],
    }


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

    chain_registry = DirectionalChainRegistry()
    chains = reconstruct_dimension_chain(glyphs, registry=chain_registry)

    candidates = []
    for chain in chains:
        word = chain["text"].strip()
        if not word:
            continue
        if is_grammar_operator(word) or is_tolerance_operator(word):
            continue
        if not chain.get("grammar_fused") and not is_dimension(word):
            continue

        bbox = chain["bbox"]
        center_x = (bbox[0] + bbox[2]) / 2
        center_y = (bbox[1] + bbox[3]) / 2

        candidates.append({
            "page": page_index,
            "text": word,
            "bbox": bbox,
            "center": [center_x, center_y],
            "glyph_count": chain["glyph_count"],
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
        })

    candidates = reconstruct_src_dimensions_local(candidates, registry=chain_registry)
    entities = attach_modifiers_to_dimensions(candidates)
    entities = deduplicate_entities(entities)
    entities = fuse_thickness_keywords(entities, distance_mm=2.0)

    page_drawings = []
    for d in page.get_drawings():
        rect = d["rect"]
        geom_bbox = [rect.x0, rect.y0, rect.x1, rect.y1]
        if region_bbox:
            if not bbox_intersects(geom_bbox, region_bbox):
                continue
        center_x = (rect.x0 + rect.x1) / 2
        center_y = (rect.y0 + rect.y1) / 2
        page_drawings.append({
            "page": page_index,
            "bbox": geom_bbox,
            "center": [center_x, center_y],
            "items": d.get("items"),
            "fill": d.get("fill"),
            "color": d.get("color"),
        })

    for ent in entities:
        nearest = None
        nearest_dist = float("inf")
        for geom in page_drawings:
            dist = distance(ent["center"], geom["center"])
            if dist < nearest_dist:
                nearest_dist = dist
                nearest = geom
        ent["distance"] = nearest_dist
        ent["nearest_geometry_bbox"] = nearest["bbox"] if nearest else None

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
    pdf_path="X6C22514.pdf",
    output_path="vector_relationships.json",
):
    doc = fitz.open(pdf_path)
    all_results = []

    for page_num, page in enumerate(doc):
        page_index = page_num + 1
        all_results.extend(process_page_semantic(page, page_index))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=4, ensure_ascii=False)

    versioned_path = next_versioned_output_path(output_path)
    with open(versioned_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=4, ensure_ascii=False)

    return all_results
