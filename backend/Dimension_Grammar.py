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
NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


def is_tolerance_operator(text: str) -> bool:
    t = text.strip()
    return t in TOLERANCE_OPERATORS or t == "±"


def is_number_token(text: str) -> bool:
    return bool(NUMBER_RE.match(text.strip()))


def normalize_decimal_text(text: str) -> str:
    t = (text or "").strip()
    if re.match(r"^\d+,\d+$", t):
        return t.replace(",", ".")
    return t


def parse_number_token_value(text: str):
    try:
        return float(normalize_decimal_text(text))
    except (TypeError, ValueError):
        return None


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
        if (g.get("char") or "").isdigit() or (g.get("char") or "") in {".", ","}
    ]
    digit_gaps = []
    for i in range(1, len(digit_glyphs)):
        digit_gaps.append(abs(_axis_coord(digit_glyphs[i], axis) - _axis_coord(digit_glyphs[i - 1], axis)))
    med_gap = median(digit_gaps) if digit_gaps else 8.0
    if axis == "vertical":
        # Be more sensitive in vertical stacks so nearby labels don't collapse
        # into one long numeric token (e.g. 54 + 20 + 8 -> 54208).
        split_threshold = max(1.6 * med_gap, med_gap + 2.0, 6.0)
    else:
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
        elif ch.isdigit() or ch in {".", ","}:
            if buf_chars and not is_number_token("".join(buf_chars) + ch):
                # Keep decimal separator attached to a leading integer token.
                current = "".join(buf_chars)
                if not (ch in {".", ","} and current.isdigit()):
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
    tokens = merge_parenthesis_suffix_tokens(tokens)
    tokens = merge_alpha_sequences(tokens)
    tokens = merge_symbol_number_prefix_tokens(tokens)
    return tokens


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


def merge_alpha_sequences(tokens):
    """Merge consecutive letter tokens into one lexical token (T H K -> THK)."""
    if not tokens:
        return tokens

    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        txt = (tok.get("text") or "")
        if tok.get("type") == "other" and len(txt) == 1 and txt.isalpha():
            j = i
            merged_chars = []
            merged_glyphs = []
            while j < len(tokens):
                cur = tokens[j]
                cur_txt = (cur.get("text") or "")
                if cur.get("type") == "other" and len(cur_txt) == 1 and cur_txt.isalpha():
                    merged_chars.append(cur_txt)
                    merged_glyphs.extend(cur.get("glyphs") or [])
                    j += 1
                else:
                    break

            if len(merged_chars) >= 2:
                merged_text = "".join(merged_chars)
                out.append({
                    "text": merged_text,
                    "glyphs": merged_glyphs,
                    "type": classify_lexeme(merged_text),
                })
                i = j
                continue

        out.append(tok)
        i += 1

    return out


def merge_symbol_number_prefix_tokens(tokens):
    """Merge prefix symbols like R/Ø with a following number token."""
    if not tokens:
        return tokens

    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        tok_text = (tok.get("text") or "").strip().upper()
        if tok_text in {"R", "Ø"} and i + 1 < len(tokens) and tokens[i + 1].get("type") == "number":
            nxt = tokens[i + 1]
            merged_text = f"{(tok.get('text') or '').strip()}{(nxt.get('text') or '').strip()}"
            out.append({
                "text": merged_text,
                "glyphs": (tok.get("glyphs") or []) + (nxt.get("glyphs") or []),
                "type": classify_lexeme(merged_text),
            })
            i += 2
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


def _token_axis_gap(tok1, tok2, axis="horizontal"):
    g1 = tok1.get("glyphs") or []
    g2 = tok2.get("glyphs") or []
    if not g1 or not g2:
        return float("inf")

    if axis == "vertical":
        top1 = min(g["bbox"][1] for g in g1)
        bottom1 = max(g["bbox"][3] for g in g1)
        top2 = min(g["bbox"][1] for g in g2)
        bottom2 = max(g["bbox"][3] for g in g2)
        # Tokens are already ordered by reading flow; use minimal edge distance.
        return min(abs(top2 - bottom1), abs(top1 - bottom2))

    right1 = max(g["bbox"][2] for g in g1)
    left1 = min(g["bbox"][0] for g in g1)
    right2 = max(g["bbox"][2] for g in g2)
    left2 = min(g["bbox"][0] for g in g2)
    return min(abs(left2 - right1), abs(left1 - right2))


def _median_digit_extent(tok1, tok2, axis="horizontal"):
    extents = []
    for tok in (tok1, tok2):
        for g in tok.get("glyphs") or []:
            ch = (g.get("char") or "").strip()
            if ch.isdigit():
                b = g["bbox"]
                extents.append(abs(b[3] - b[1]) if axis == "vertical" else abs(b[2] - b[0]))
    return median(extents) if extents else 6.0


def _try_fuse_decimal_between_numbers(num1_tok, num2_tok, axis, merge_bbox_fn):
    """
    Decimal-priority reconstruction for compact splits (e.g. "60" + "8" -> "60.8").

    This runs BEFORE implicit SRC reconstruction to avoid grammar collisions like:
      60.8 misread as 60 X 8
      96.2 misread as 96 X 2
    """
    t1 = (num1_tok.get("text") or "").strip()
    t2 = (num2_tok.get("text") or "").strip()
    if not (re.match(r"^\d+$", t1) and re.match(r"^\d+$", t2)):
        return None

    # Strong decimal pattern: integer part + single decimal digit.
    if len(t1) < 1 or len(t2) != 1:
        return None

    # Engineering decimal style preference (reduce false positives like 1.7 from "1 7").
    if len(t1) < 2:
        return None

    gap = _token_axis_gap(num1_tok, num2_tok, axis=axis)
    med_extent = _median_digit_extent(num1_tok, num2_tok, axis=axis)
    # Missing decimal separators from CAD/PDF exports can create wider-than-expected gaps.
    # Keep this permissive enough to recover 60.8 / 96.2 before SRC inference.
    compact_threshold = max(1.35 * med_extent, 7.0)
    if gap > compact_threshold:
        return None

    glyphs = _merge_token_glyphs(num1_tok, num2_tok)
    bbox = _bbox_from_glyphs(glyphs, merge_bbox_fn)
    text = f"{t1}.{t2}"

    try:
        nominal = float(text)
    except ValueError:
        return None

    return {
        "entity_type": "linear_dimension",
        "text": text,
        "glyphs": glyphs,
        "bbox": bbox,
        "grammar_fused": True,
        "grammar_rule": "IMPLICIT_DECIMAL_FUSION",
        "nominal": nominal,
        "nominal_text": text,
    }


def fuse_tolerance_triple(nominal_tok, operator_tok, tolerance_tok, merge_bbox_fn):
    """Grammar: NUMBER ± NUMBER → single dimension."""
    nominal_text_raw = nominal_tok["text"].strip()
    tol_value_text_raw = tolerance_tok["text"].strip()
    nominal_text = normalize_decimal_text(nominal_text_raw)
    tol_value_text = normalize_decimal_text(tol_value_text_raw)

    nominal_val = parse_number_token_value(nominal_text_raw)
    tol_val = parse_number_token_value(tol_value_text_raw)
    if nominal_val is None or tol_val is None:
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
        match = re.match(r"^([\d,]+)\s*±\s*([\d,]+)$", text)
    if not match:
        match = re.match(r"^([\d,]+)±([\d,]+)$", text)
    if not match:
        return None

    nominal_text, tol_text = normalize_decimal_text(match.group(1)), normalize_decimal_text(match.group(2))
    nominal_val = parse_number_token_value(nominal_text)
    tol_val = parse_number_token_value(tol_text)
    if nominal_val is None or tol_val is None:
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
    text = normalize_decimal_text(token["text"].strip())
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


def _try_fuse_src_dimension_between_numbers(num1_tok, num2_tok, axis, merge_bbox_fn):
    """
    Implicit operator reconstruction:
      NUMBER NUMBER (split by large digit gap) → "N X M" as SRC_dimension
    """
    t1 = (num1_tok.get("text") or "").strip()
    t2 = (num2_tok.get("text") or "").strip()
    if not (re.match(r"^\d+$", t1) and re.match(r"^\d+$", t2)):
        return None

    gap = _token_axis_gap(num1_tok, num2_tok, axis=axis)
    med_extent = _median_digit_extent(num1_tok, num2_tok, axis=axis)

    # Decimal-priority guard: compact NN + N is more likely a missed decimal separator.
    if _try_fuse_decimal_between_numbers(num1_tok, num2_tok, axis, merge_bbox_fn):
        return None

    # Additional guard: do not reconstruct implicit SRC on compact NN + N patterns.
    # Example false positives we must avoid:
    #   60.8 -> 60 X 8
    #   96.2 -> 96 X 2
    if len(t1) >= 2 and len(t2) == 1:
        compact_src_threshold = max(0.9 * med_extent, 4.5)
        if gap <= compact_src_threshold:
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
        decimal_fused = _try_fuse_decimal_between_numbers(number_tokens[0], number_tokens[1], axis, merge_bbox_fn)
        if decimal_fused:
            return [decimal_fused]

        fused = _try_fuse_src_dimension_between_numbers(number_tokens[0], number_tokens[1], axis, merge_bbox_fn)
        if fused:
            return [fused]

    # If no SRC/decimal fuse succeeded, prefer token-wise numeric parsing over
    # collapsing an entire long chain into one value (prevents missing dimensions
    # in dense vertical columns).
    parsed_tokens = parse_dimension_grammar(tokens, merge_bbox_fn)
    # Use token-wise fallback only for short chains; large chains are often noisy
    # multi-label columns and should not explode into many false nominals.
    if parsed_tokens and len(ordered) <= 6:
        return parsed_tokens

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
