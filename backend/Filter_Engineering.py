import re


def is_tolerance_modifier(text):
    """Standalone ± token — modifier only, not a dimension entity."""
    t = text.strip()
    return bool(re.match(r"^±", t))


def is_grammar_operator(text):
    """± + - are grammar operators, never standalone export entities."""
    t = text.strip()
    return t in {"±", "+", "-", "−", "\u00b1"} or t == "±"


def is_nominal_dimension(text):
    """Dimension candidate (nominal or combined nominal+tolerance in one token)."""
    if is_tolerance_modifier(text):
        return False

    patterns = [
        r"^R\s*\d",
        r"^Ø\s*\d",
        r"^\d",
        r"°",
        r"THK",
        r"\d+\.\d+",
    ]

    for p in patterns:
        if re.search(p, text, re.I):
            return True

    return False


def is_dimension(text):
    """Broad filter for pipeline inclusion (nominals + combined + modifiers)."""
    return is_nominal_dimension(text) or is_tolerance_modifier(text)
