"""
Engineering dimension grammar — parse axis chains, not OCR characters.

Rule: ± on the same dimension axis is NEVER an independent object.
Pattern: NUMBER + (±|+) + NUMBER → one semantic dimension entity.
"""

import re
from statistics import median
from Filter_Engineering import is_nominal_dimension
from Engineering_Plausibility import can_reconstruct_src_pair, validate_dimension_entity

TOLERANCE_OPERATORS = frozenset({"±", "+", "-", "−", "\u00b1"})
NUMBER_RE = re.compile(r"^[\d.]+$")


def is_tolerance_operator(text: str) -> bool:
    t = text.strip()
    return t in TOLERANCE_OPERATORS or t == "±"


def is_number_token(text: str) -> bool:
    return bool(NUMBER_RE.match(text.strip()))


def classify_lexeme(text: str) -> str:
    t = text.strip()
    if is_tolerance_operator(t):
        return "operator"
    if is_number_token(t):
        return "number"
    if "±" in t:
        return "fused_tolerance"
    if is_nominal_dimension(t):
        return "nominal"
    return "other"


def _axis_coord(g, axis):
    return g.get("cy") if axis == "vertical" else g.get("cx")


def segment_glyph_chain_to_tokens(ordered_glyphs, axis="horizontal"):
    """
    Split sorted glyph chain into lexical tokens (number / operator / other).

    Engineering-specific: if the vector-cross operator `X` is missing as text,
    we often see two numeric values whose glyphs are split by a large geometric gap.
    Use that gap to split a would-be number token into two number tokens.
    """
    tokens = []
    buf_chars = []
    buf_glyphs = []

    # Estimate typical digit spacing on this chain.
    digit_glyphs = [
        g
        for g in ordered_glyphs
        if (g.get("char") or "").isdigit() or (g.get("char") or "") == "."
    ]
    digit_gaps = []
    for i in range(1, len(digit_glyphs)):
        digit_gaps.append(abs(_axis_coord(digit_glyphs[i], axis) - _axis_coord(digit_glyphs[i - 1], axis)))
    med_gap = median(digit_gaps) if digit_gaps else 8.0
    split_threshold = max(2.5 * med_gap, med_gap + 6.0, 10.0)

    def flush():
        if not buf_chars:
            return
        text = "".join(buf_chars)
        tokens.append({
            "text": text,
            "glyphs": list(buf_glyphs),
            "type": classify_lexeme(text),
        })

    for g in ordered_glyphs:
        ch = g["char"]
        if is_tolerance_operator(ch):
            flush()
            buf_chars = []
            buf_glyphs = []
            tokens.append({"text": ch, "glyphs": [g], "type": "operator"})
        elif ch.isdigit() or ch == ".":
            if buf_chars and not is_number_token("".join(buf_chars) + ch):
                flush()
                buf_chars = []
                buf_glyphs = []

            # If there is a large geometric gap between consecutive digits,
            # split into two number tokens (implicit operator case like "17 X 19").
            if buf_glyphs:
                prev_coord = _axis_coord(buf_glyphs[-1], axis)
                cur_coord = _axis_coord(g, axis)
                gap = abs(cur_coord - prev_coord)
                if gap > split_threshold and is_number_token("".join(buf_chars) + ch):
                    flush()
                    buf_chars = []
                    buf_glyphs = []

            buf_chars.append(ch)
            buf_glyphs.append(g)
        else:
            flush()
            buf_chars = []
            buf_glyphs = []
            tokens.append({"text": ch, "glyphs": [g], "type": classify_lexeme(ch)})

    flush()
    return merge_parenthesis_suffix_tokens(tokens)


def merge_parenthesis_suffix_tokens(tokens):
    """Attach parenthesis suffixes like (REF) to the preceding numeric token."""
    if not tokens:
        return tokens

    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok.get("type") == "number"
            and i + 1 < len(tokens)
            and (tokens[i + 1].get("text") or "").strip() == "("
        ):
            j = i + 1
            suffix_text = ""
            suffix_glyphs = list(tok.get("glyphs", []))
            while j < len(tokens):
                part = tokens[j].get("text") or ""
                suffix_text += part
                suffix_glyphs.extend(tokens[j].get("glyphs", []))
                if part.strip() == ")":
                    j += 1
                    break
                j += 1

            if ")" in suffix_text:
                merged = dict(tok)
                merged["text"] = f"{tok['text']}{suffix_text}"
                merged["glyphs"] = suffix_glyphs
                out.append(merged)
                i = j
                continue

        out.append(tok)
        i += 1

    return out


def _merge_token_glyphs(*token_list):
    glyphs = []
    for tok in token_list:
        glyphs.extend(tok["glyphs"])
    return glyphs


def _bbox_from_glyphs(glyphs, merge_bbox_fn):
    return merge_bbox_fn([g["bbox"] for g in glyphs])


def fuse_tolerance_triple(nominal_tok, operator_tok, tolerance_tok, merge_bbox_fn):
    """Grammar: NUMBER ± NUMBER → single dimension."""
    nominal_text = nominal_tok["text"].strip()
    tol_value_text = tolerance_tok["text"].strip()

    try:
        nominal_val = float(nominal_text)
        tol_val = float(tol_value_text)
    except ValueError:
        return None

    if not validate_dimension_entity(nominal_val, tol_val):
        return None

    op_display = "±"
    display_text = f"{nominal_text} {op_display}{tol_value_text}"
    glyphs = _merge_token_glyphs(nominal_tok, operator_tok, tolerance_tok)

    return {
        "text": display_text,
        "glyphs": glyphs,
        "bbox": _bbox_from_glyphs(glyphs, merge_bbox_fn),
        "grammar_fused": True,
        "grammar_rule": "NUMBER_OPERATOR_NUMBER",
        "nominal": nominal_val,
        "nominal_text": nominal_text,
        "tolerance_value": tol_val,
    }


def fuse_inline_tolerance_token(token, merge_bbox_fn):
    """Token already contains '99±0.25' style text."""
    text = token["text"].strip()
    match = re.match(r"^([\d.]+)\s*±\s*([\d.]+)$", text)
    if not match:
        match = re.match(r"^([\d.]+)±([\d.]+)$", text)
    if not match:
        return None

    nominal_text, tol_text = match.group(1), match.group(2)
    try:
        nominal_val = float(nominal_text)
        tol_val = float(tol_text)
    except ValueError:
        return None

    if not validate_dimension_entity(nominal_val, tol_val):
        return None

    display_text = f"{nominal_text} ±{tol_text}"
    return {
        "text": display_text,
        "glyphs": token["glyphs"],
        "bbox": _bbox_from_glyphs(token["glyphs"], merge_bbox_fn),
        "grammar_fused": True,
        "grammar_rule": "INLINE_TOLERANCE",
        "nominal": nominal_val,
        "nominal_text": nominal_text,
        "tolerance_value": tol_val,
    }


def fuse_nominal_token(token, merge_bbox_fn):
    text = token["text"].strip()
    if is_tolerance_operator(text):
        return None
    if not is_nominal_dimension(text) and not is_number_token(text):
        return None

    return {
        "text": text,
        "glyphs": token["glyphs"],
        "bbox": _bbox_from_glyphs(token["glyphs"], merge_bbox_fn),
        "grammar_fused": False,
        "grammar_rule": "NOMINAL_ONLY",
    }


def parse_dimension_grammar(tokens, merge_bbox_fn):
    """
    Apply engineering grammar to a token chain on one dimension axis.
    Never returns standalone operator entities.
    """
    fused = []
    i = 0

    while i < len(tokens):
        tok = tokens[i]

        if tok["type"] == "fused_tolerance" or ("±" in tok["text"] and tok["type"] != "operator"):
            entity = fuse_inline_tolerance_token(tok, merge_bbox_fn)
            if entity:
                fused.append(entity)
            i += 1
            continue

        # NUMBER + (±|+) + NUMBER
        if (
            i + 2 < len(tokens)
            and tokens[i]["type"] == "number"
            and tokens[i + 1]["type"] == "operator"
            and tokens[i + 2]["type"] == "number"
        ):
            op = tokens[i + 1]["text"]
            if op in TOLERANCE_OPERATORS or op in {"+", "-", "−"}:
                entity = fuse_tolerance_triple(tokens[i], tokens[i + 1], tokens[i + 2], merge_bbox_fn)
                if entity:
                    fused.append(entity)
                    i += 3
                    continue

        # NUMBER + operator (±) — wait for tolerance on next pass; orphan op skipped below
        if tok["type"] == "number":
            if i + 1 < len(tokens) and tokens[i + 1]["type"] == "operator":
                if i + 2 < len(tokens) and tokens[i + 2]["type"] == "number":
                    i += 1
                    continue
            entity = fuse_nominal_token(tok, merge_bbox_fn)
            if entity:
                fused.append(entity)
            i += 1
            continue

        # ± alone or orphan operator — never export
        if tok["type"] == "operator":
            i += 1
            continue

        if tok["type"] in ("nominal", "other") and is_nominal_dimension(tok["text"]):
            entity = fuse_nominal_token(tok, merge_bbox_fn)
            if entity:
                fused.append(entity)

        i += 1

    return fused


def _try_fuse_src_dimension_between_numbers(num1_tok, num2_tok, merge_bbox_fn):
    """
    Implicit operator reconstruction:
      NUMBER NUMBER (split by large digit gap) → "N X M" as SRC_dimension
    """
    t1 = (num1_tok.get("text") or "").strip()
    t2 = (num2_tok.get("text") or "").strip()
    if not (re.match(r"^\d+$", t1) and re.match(r"^\d+$", t2)):
        return None

    # Strong constraint: reject dates / big numbers.
    v1 = int(t1)
    v2 = int(t2)
    if not (0 < v1 < 500 and 0 < v2 < 500):
        return None

    if not can_reconstruct_src_pair(v1, v2, glyph_count=len(_merge_token_glyphs(num1_tok, num2_tok))):
        return None

    glyphs = _merge_token_glyphs(num1_tok, num2_tok)
    bbox = _bbox_from_glyphs(glyphs, merge_bbox_fn)

    return {
        "entity_type": "SRC_dimension",
        "text": f"{t1} X {t2}",
        "glyphs": glyphs,
        "bbox": bbox,
        "operator": "X",
        "values": [v1, v2],
        "grammar_fused": True,
        "grammar_rule": "IMPLICIT_X_BETWEEN_NUMBERS",
    }


def apply_dimension_grammar_to_glyph_group(group, axis, sort_fn, merge_bbox_fn):
    """
    STEP: axis chain → tokenize → grammar parse → semantic dimension(s).
    """
    if not group:
        return []

    ordered = sort_fn(group, axis)
    if not ordered:
        return []

    has_operator = any(is_tolerance_operator(g["char"]) for g in ordered)
    tokens = segment_glyph_chain_to_tokens(ordered, axis=axis)

    if has_operator:
        parsed = parse_dimension_grammar(tokens, merge_bbox_fn)
        if parsed:
            return parsed
        # ± on axis but grammar failed — do not export fragments
        return []

    # No ± operator: try implicit X reconstruction on axis.
    number_tokens = [t for t in tokens if t["type"] == "number"]
    if len(number_tokens) >= 2:
        fused = _try_fuse_src_dimension_between_numbers(number_tokens[0], number_tokens[1], merge_bbox_fn)
        if fused:
            return [fused]

    # Fallback: export the full chain as a raw candidate string.
    text = "".join(g["char"] for g in ordered).strip()
    if not text:
        return []

    return [{
        "text": text,
        "glyphs": ordered,
        "bbox": merge_bbox_fn([g["bbox"] for g in ordered]),
        "grammar_fused": False,
        "grammar_rule": "CHAIN_STRING",
    }]
