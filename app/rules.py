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

OPS = ["gt", "lt", "gte", "lte", "eq", "ne", "contains", "regex", "missing", "present"]


@dataclass
class RuleRow:
    id: int
    op: str
    value: str
    status_level_id: int
    priority: int


def _coerce(value: ParsedValue, compare: str) -> tuple[float | None, str, float | None]:
    """Return (num, text, compare_num) for a comparison."""
    text = value.txt if value.txt is not None else (value.raw or "")
    compare_num: float | None
    try:
        compare_num = float(compare)
    except (TypeError, ValueError):
        compare_num = None
    return value.num, text, compare_num


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
            return num == cnum
        return text == rule.value
    if op == "ne":
        if num is not None and cnum is not None:
            return num != cnum
        return text != rule.value
    if op == "contains":
        return rule.value in text
    if op == "regex":
        try:
            return re.search(rule.value, text) is not None
        except re.error:
            return False
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
