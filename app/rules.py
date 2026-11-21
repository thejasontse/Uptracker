"""Rule evaluation and status derivation.

A rule compares a parsed metric value against a constant using one of the
supported operators. When several rules match, the metric takes the worst
(highest-rank) status among them. A metric with no matching rule has no status
of its own — the caller maps that to ``unknown``.

A node's overall status is the worst-ranked status across all enabled datasets
of a tracked node; when no dataset contributes a status the node is unknown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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


def rule_matches(rule: RuleRow, value: ParsedValue) -> bool:
    """True if *rule* applies to *value*."""
    op = rule.op
    if op == "missing":
        return not value.ok
    if op == "present":
        return value.ok
    if not value.ok:
        # A missing value cannot satisfy a value-comparison rule.
        return False
    num, text, cnum = _coerce(value, rule.value)
    if op == "gt":
        return num is not None and cnum is not None and num > cnum
    if op == "lt":
        return num is not None and cnum is not None and num < cnum
    if op == "gte":
        return num is not None and cnum is not None and num >= cnum
    if op == "lte":
        return num is not None and cnum is not None and num <= cnum
    if op == "eq":
        if num is not None and cnum is not None:
            return num == cnum or _text_num_equal(text, cnum)
        if cnum is not None:
            return _text_num_equal(text, cnum)
        return text == rule.value
    if op == "ne":
        if num is not None and cnum is not None:
            return num != cnum and not _text_num_equal(text, cnum)
        if cnum is not None:
            return not _text_num_equal(text, cnum)
        return text != rule.value
    if op == "contains":
        return rule.value in text
    if op == "regex":
        try:
            return re.search(rule.value, text) is not None
        except re.error:
            return False
    if op in ("is", "is_not"):
        want = _as_bool(rule.value)
        got = _as_bool(text)
        if got is None and num is not None:
            got = num != 0
        if want is None or got is None:
            return False
        return got == want if op == "is" else got != want
    if op == "between":
        lo, hi = _parse_range(rule.value)
        if num is None or lo is None or hi is None:
            return False
        return lo <= num <= hi
    return False


def evaluate_metric(rules: list[RuleRow], value: ParsedValue, ranks: dict[int, int]) -> int | None:
    """Return the status_level_id for a metric, or None if no rule matches.

    When several rules match, the worst (highest-rank) status among them wins.
    ``ranks`` maps status_level_id -> rank (higher is worse).
    """
    matched = [r.status_level_id for r in rules if rule_matches(r, value)]
    return worst_status(matched, ranks)


def worst_status(status_ids: list[int], ranks: dict[int, int]) -> int | None:
    """Return the status_level_id with the highest rank among *status_ids*.

    Returns None when *status_ids* is empty (the caller decides the fallback,
    which is ``unknown``).
    """
    if not status_ids:
        return None
    return max(status_ids, key=lambda sid: ranks.get(sid, 0))
