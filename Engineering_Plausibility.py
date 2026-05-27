"""
Engineering plausibility constraints for dimension + tolerance binding.

Tolerance is a modifier on a nominal dimension — never a standalone semantic entity.
These rules reject impossible merges (e.g. 52.0 ±99) using drafting knowledge, not OCR.
"""

import re

# Golden standard tolerance values seen on mechanical drawings
VALID_TOLERANCES = (
    0.1, 0.2, 0.25, 0.5, 0.8,
    1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0,
)

MAX_ABSOLUTE_TOLERANCE = 10.0
MAX_TOLERANCE_RATIO = 0.5  # tolerance must be < 50% of nominal

# Local semantic anchor for SRC pairing (same drafting locality as THK binding).
SRC_ANCHOR_RADIUS_MM = 2.0
SRC_PAIR_MAX_GAP_MM = 6.0


def validate_tolerance_value(tolerance: float) -> bool:
    """Rule 1 & 4: tolerances are small, standard, rarely huge whole numbers."""
    tol = abs(float(tolerance))

    if tol > MAX_ABSOLUTE_TOLERANCE:
        return False

    for standard in VALID_TOLERANCES:
        if abs(tol - standard) < 0.02:
            return True

    # Typical decimal tolerances: ±0.1 … ±0.8
    if tol < 1.0 and abs(tol - round(tol, 2)) < 0.001:
        return True

    # Small integer tolerances: ±1 … ±4
    if 1.0 <= tol <= 4.0 and abs(tol - round(tol)) < 0.001:
        return True

    return False


def validate_dimension_entity(nominal, tolerance) -> bool:
    """
  Validate a nominal + tolerance pair before merge or export.

  Rules:
    - tolerance > 10 → reject
    - tolerance >= nominal → reject
    - tolerance > nominal * 0.5 → reject
    - non-standard huge values (±99, ±175) → reject
    """
    if tolerance is None:
        return True

    tol = abs(float(tolerance))

    if not validate_tolerance_value(tol):
        return False

    if tol > MAX_ABSOLUTE_TOLERANCE:
        return False

    if nominal is not None:
        n = abs(float(nominal))
        if n > 0:
            if tol >= n:
                return False
            if tol > n * MAX_TOLERANCE_RATIO:
                return False

    return True


def validate_tolerance_modifier_dict(tolerance_mod, nominal=None) -> bool:
    if not tolerance_mod or tolerance_mod.get("type") != "tolerance":
        return False
    plus = tolerance_mod.get("plus")
    if plus is None:
        return False
    return validate_dimension_entity(nominal, plus)


HORIZONTAL_ORIENTATIONS = frozenset({
    "horizontal",
    "unresolved_horizontal_fragment",
    "rotated",
})

VERTICAL_ORIENTATIONS = frozenset({
    "vertical",
    "unresolved_vertical_fragment",
})


def compatible_reading_orientation(parent_orientation, modifier_orientation) -> bool:
    """Merge only when modifier shares the parent's reading-direction family."""
    if parent_orientation == modifier_orientation:
        return True
    if parent_orientation in HORIZONTAL_ORIENTATIONS and modifier_orientation in HORIZONTAL_ORIENTATIONS:
        return True
    if parent_orientation in VERTICAL_ORIENTATIONS and modifier_orientation in VERTICAL_ORIENTATIONS:
        return True
    return False


def same_dimension_axis(parent_entity, modifier_candidate) -> bool:
    return parent_entity.get("dimension_axis") == modifier_candidate.get("dimension_axis")


# Quantity / multiplicity — NOT structured dimension pairs (e.g. "12 X 2nos").
INVALID_SRC_SUFFIXES = (
    "nos",
    "no.",
    "pcs",
    "pc",
    "qty",
    "quantity",
    "holes",
    "hole",
    "slots",
    "slot",
    "off",
    "ea",
    "each",
    "places",
    "place",
)

# Allowed non-numeric tail on right operand (e.g. "45°").
VALID_SRC_TAIL_RE = re.compile(r"^[\s()°]*$")


def _is_decimal_value(value) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return abs(v - round(v)) > 1e-6


def is_plausible_src_dimension(left, right, tail: str = "") -> bool:
    """
    Engineering plausibility for SRC_dimension (real fabrication drawings).

    Reject parser hallucinations like 2 X 1, 3 X 4, 5 X 2.
    Require at least one strong operand: >=10, decimal, or angle tail.
    """
    try:
        left_v = float(left)
        right_v = float(right)
    except (TypeError, ValueError):
        return False

    tail_s = (tail or "").strip()
    if "°" in tail_s:
        return True

    left_strong = left_v >= 10 or _is_decimal_value(left)
    right_strong = right_v >= 10 or _is_decimal_value(right)

    if not (left_strong or right_strong):
        return False

    # Both single-digit integers (<10) — almost never real SRC on fab drawings.
    if left_v < 10 and right_v < 10:
        if not _is_decimal_value(left) and not _is_decimal_value(right):
            return False

    return True


def is_valid_src_dimension(left, right, tail: str = "") -> bool:
    """
    Grammar + plausibility validation for SRC_dimension (NUMBER X NUMBER).

    Valid:   17 X 28, 20 X 45°, 12 X 0.5, 11 X 17
    Invalid: 12 X 2nos, 2 X Ø10, 2 X 1, 3 X 4
    """
    if not is_plausible_src_dimension(left, right, tail):
        return False

    try:
        float(left)
        float(right)
    except (TypeError, ValueError):
        return False

    right_s = str(right).strip()
    tail_s = (tail or "").strip()
    combined = f"{right_s}{tail_s}".lower()

    for word in INVALID_SRC_SUFFIXES:
        if word in combined:
            return False

    if "Ø" in combined or "ø" in combined:
        return False

    if re.search(r"\bM\d", combined, re.I):
        return False

    # Right operand must be dimension-like (starts with number).
    if not re.match(r"^\d", right_s):
        return False

    # Reject alphabetic words on the right (except degree handled via tail).
    right_letters = re.sub(r"[\d.\s°±\-+]", "", right_s, flags=re.I)
    if right_letters:
        return False

    if tail_s and not VALID_SRC_TAIL_RE.match(tail_s):
        tail_letters = re.sub(r"[\d.\s()°±\-+]", "", tail_s, flags=re.I)
        if tail_letters:
            return False

    return True


def can_reconstruct_src_pair(
    left,
    right,
    tail: str = "",
    orientation: str | None = None,
    glyph_count: int | None = None,
) -> bool:
    """
    Full SRC reconstruction gate: grammar + plausibility + fragment rejection.
    """
    if orientation and str(orientation).startswith("unresolved"):
        return False

    if not is_valid_src_dimension(left, right, tail):
        return False

    # Tiny fragment pairs without strong operands are usually hallucinations.
    try:
        left_v = float(left)
        right_v = float(right)
    except (TypeError, ValueError):
        return False

    if glyph_count is not None and glyph_count <= 4:
        if left_v < 10 and right_v < 10 and not _is_decimal_value(left) and not _is_decimal_value(right):
            return False

    return True


def can_merge_tolerance_modifier(parent_entity, modifier_candidate, tolerance_mod) -> bool:
    """
    Merge dimension + tolerance only when ALL hold:
      1. Same dimension axis (column/row)
      2. Same orientation family
      3. Engineering-valid tolerance value
    """
    if not validate_tolerance_modifier_dict(tolerance_mod, parent_entity.get("nominal")):
        return False

    if not same_dimension_axis(parent_entity, modifier_candidate):
        return False

    return compatible_reading_orientation(
        parent_entity.get("orientation"),
        modifier_candidate.get("orientation"),
    )
