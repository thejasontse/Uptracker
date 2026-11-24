"""Rule evaluation and status derivation.

A rule compares a parsed metric value against a constant using one of the
supported operators. When several rules match, the metric takes the worst
(highest-rank) status among them. A metric with no matching rule has no status
of its own — the caller maps that to ``unknown``.

A node's overall status is the worst-ranked status across all enabled datasets
of a tracked node; when no dataset contributes a status the node is unknown.

Comparison lives in ONE place: :func:`compare`, a table-driven evaluator whose
dispatch map covers every operator in :data:`OPS`. It returns
``(matched, error_reason)`` so callers that only need the boolean
(:func:`rule_matches`, used by the collector) stay simple while interactive
tooling (the rules editor's "Test rules") can report *why* a rule failed to
evaluate instead of silently treating it as no-match.

The client mirror of this module is ``admin/js/rulesEngine.js`` — keep the two
in sync when changing operator semantics or error wording.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .parser import ParsedValue

# Operators that do not require a comparison value.
_NO_VALUE_OPS = {"missing", "present"}

OPS = ["gt", "lt", "gte", "lte", "eq", "ne", "contains", "regex", "missing", "present",
       "is", "is_not", "between"]

# Words interpreted as boolean ``True`` by the ``is``/``is_not`` operators.
_TRUE_WORDS = {"1", "true", "yes", "on", "up", "ok"}


def _as_bool(text: str) -> bool | None:
    """Normalise *text* to a tri-state boolean, or None if not recognisable."""
    low = (text or "").strip().lower()
    if low in _TRUE_WORDS:
        return True
    if low in ("0", "false", "no", "off", "down"):
        return False
    return None


def _parse_range(compare: str) -> tuple[float | None, float | None]:
    """Parse a ``min,max`` range string into two floats (either may be None).

    The UI hint says "min, max", but users habitually type a plain space
    instead of the comma ("80 90"), which silently matched nothing. Accept any
    run of commas and/or whitespace as the separator so both spellings work."""
    parts = [p for p in re.split(r"[,\s]+", (compare or "").strip()) if p]
    if len(parts) != 2:
        return None, None
    out: list[float | None] = []
    for p in parts:
        try:
            out.append(float(p.strip()))
        except (TypeError, ValueError):
            out.append(None)
    return out[0], out[1]


@dataclass
class RuleRow:
    id: int
    op: str
    value: str
    status_level_id: int
    priority: int


def _leading_num(text: str) -> float | None:
    """Return the leading numeric prefix of *text* as a float, or None.

    ``"5%"`` -> 5.0, ``"85 % "`` -> 85.0, ``"abc"`` -> None. Lets comparison
    operators work on text-captured values that carry a unit suffix."""
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _coerce(value: ParsedValue, compare: str) -> tuple[float | None, str, float | None]:
    """Return (num, text, compare_num) for a comparison.

    When the metric value has no parsed number (e.g. cast ``str``, or a regex
    that captured "5%" so auto-cast failed), fall back to its leading numeric
    prefix so gt/lt/gte/lte/between still evaluate percentage-style text."""
    text = value.txt if value.txt is not None else (value.raw or "")
    num = value.num if value.num is not None else _leading_num(text)
    compare_num: float | None
    try:
        compare_num = float(compare)
    except (TypeError, ValueError):
        compare_num = None
    return num, text, compare_num


def _text_num_equal(text: str, compare_num: float) -> bool:
    """True when *text*'s leading numeric prefix equals *compare_num*.

    A rule value like ``85%`` parses to 85.0 via float() but never equals the
    raw string, and a metric cast to ``str`` (or captured with a unit, e.g.
    "85 %") has no num at all. Comparing the leading numeric prefixes keeps eq/ne
    working for percentage-style metrics instead of silently falling through to
    a string compare that can never match."""
    lead = _leading_num(text)
    return lead is not None and lead == compare_num


# ---------------------------------------------------------------------------
# Comparison engine (single source of truth; mirrored by rulesEngine.js)
# ---------------------------------------------------------------------------
# Each handler: (num, text, cnum, rule_value) -> (matched, error | None).
# ``num``/``text`` come from _coerce (never both unusable for a given op),
# ``cnum`` is the rule value parsed as float or None.

def _op_gt(num, text, cnum, rv):
    return num > cnum, None


def _op_lt(num, text, cnum, rv):
    return num < cnum, None


def _op_gte(num, text, cnum, rv):
    return num >= cnum, None


def _op_lte(num, text, cnum, rv):
    return num <= cnum, None


def _op_eq(num, text, cnum, rv):
    if cnum is not None:
        return (num == cnum if num is not None else False) or _text_num_equal(text, cnum), None
    return text == rv, None


def _op_ne(num, text, cnum, rv):
    if cnum is not None:
        equal = (num == cnum if num is not None else False) or _text_num_equal(text, cnum)
        return not equal, None
    return text != rv, None


def _op_contains(num, text, cnum, rv):
    return rv in text, None


def _op_regex(num, text, cnum, rv):
    try:
        return re.search(rv, text) is not None, None
    except re.error as exc:
        return False, f"invalid regex: {exc}"


def _op_is(num, text, cnum, rv):
    want = _as_bool(rv)
    if want is None:
        return False, f"cannot interpret '{rv}' as true/false"
    got = _as_bool(text)
    if got is None and num is not None:
        got = num != 0
    if got is None:
        return False, f"cannot interpret value '{text}' as true/false"
    return got == want, None


def _op_is_not(num, text, cnum, rv):
    matched, err = _op_is(num, text, cnum, rv)
    return (not matched, None) if err is None else (False, err)


def _op_between(num, text, cnum, rv):
    lo, hi = _parse_range(rv)
    if lo is None or hi is None:
        return False, "expected two numbers 'min, max'"
    if num is None:
        return False, f"value '{text}' is not numeric"
    return lo <= num <= hi, None


# Ops whose *rule value* must parse as a number.
_NUMERIC_RULE_OPS = {"gt", "lt", "gte", "lte"}

# Handler table: every op in OPS maps to exactly one callable.
_HANDLERS: dict[str, Callable[[float | None, str, float | None, str], tuple[bool, str | None]]] = {
    "gt": _op_gt, "lt": _op_lt, "gte": _op_gte, "lte": _op_lte,
    "eq": _op_eq, "ne": _op_ne,
    "contains": _op_contains, "regex": _op_regex,
    "is": _op_is, "is_not": _op_is_not,
    "between": _op_between,
}


def compare(op: str, rule_value: str, value: ParsedValue) -> tuple[bool, str | None]:
    """Evaluate one operator against *value*; return ``(matched, error)``.

    ``error`` is a human-readable reason when the comparison could not be
    performed (bad regex, non-numeric threshold, unrecognisable boolean word);
    it is ``None`` whenever the comparison ran, matched or not. An unknown op,
    a missing value vs a value-comparison rule, and no-value ops never produce
    errors — they simply do not match."""
    if op == "missing":
        return (not value.ok), None
    if op == "present":
        return value.ok, None
    if not value.ok:
        # A missing value cannot satisfy a value-comparison rule.
        return False, None
    handler = _HANDLERS.get(op)
    if handler is None:
        return False, f"unknown operator '{op}'"
    num, text, cnum = _coerce(value, rule_value)
    if op in _NUMERIC_RULE_OPS and cnum is None:
        return False, f"rule value '{rule_value}' is not numeric"
    if op in _NUMERIC_RULE_OPS and num is None:
        return False, f"value '{text}' is not numeric"
    return handler(num, text, cnum, rule_value)


def rule_matches(rule: RuleRow, value: ParsedValue) -> bool:
    """True if *rule* applies to *value* (error details dropped — collector)."""
    matched, _ = compare(rule.op, rule.value, value)
    return matched


def evaluate_metric(rules: list[RuleRow], value: ParsedValue, ranks: dict[int, int]) -> int | None:
    """Return the status_level_id for a metric, or None if no rule matches.

    When several rules match, the worst (highest-rank) status among them wins.
    ``ranks`` maps status_level_id -> rank (higher is worse).
    """
    matched = [r.status_level_id for r in rules if rule_matches(r, value)]
    return worst_status(matched, ranks)


def evaluate_metric_detailed(
    rules: list[RuleRow], value: ParsedValue, ranks: dict[int, int]
) -> tuple[int | None, list[tuple[RuleRow, str]]]:
    """Like :func:`evaluate_metric` but also returns the rules that failed to
    evaluate as ``(rule, error_reason)`` pairs, for interactive tooling."""
    matched_ids: list[int] = []
    errors: list[tuple[RuleRow, str]] = []
    for r in rules:
        matched, err = compare(r.op, r.value, value)
        if err is not None:
            errors.append((r, err))
        elif matched:
            matched_ids.append(r.status_level_id)
    return worst_status(matched_ids, ranks), errors


def worst_status(status_ids: list[int], ranks: dict[int, int]) -> int | None:
    """Return the status_level_id with the highest rank among *status_ids*.

    Returns None when *status_ids* is empty (the caller decides the fallback,
    which is ``unknown``).
    """
    if not status_ids:
        return None
    return max(status_ids, key=lambda sid: ranks.get(sid, 0))
