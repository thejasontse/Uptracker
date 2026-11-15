"""Metric extraction: parse a named value out of raw command/HTTP output.

Each metric defines a regular expression and a capture-group index. The matched
text is cast to a type (auto/int/float/str/bool). Extraction failures produce a
``None`` value with an explanatory error, which rules can still match via the
``missing`` operator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParsedValue:
    raw: str | None
    num: float | None
    txt: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def cast_value(text: str, cast: str) -> tuple[float | None, str | None]:
    """Return (num, txt) for *text* according to *cast*."""
    if cast == "int":
        return float(int(float(text))), text
    if cast == "float":
        return float(text), text
    if cast == "bool":
        low = text.strip().lower()
        b = low in ("1", "true", "yes", "on", "up", "ok")
        return (1.0 if b else 0.0), text
    if cast == "str":
        return None, text
    # auto: numeric if it parses, else string
    try:
        return float(text), text
    except ValueError:
        return None, text


def extract(raw_output: str, regex: str, group_index: int = 1, cast: str = "auto") -> ParsedValue:
    """Apply *regex* to *raw_output* and return the parsed metric value."""
    if raw_output is None:
        return ParsedValue(None, None, None, "no output")
    try:
        compiled = re.compile(regex)
    except re.error as exc:
        return ParsedValue(None, None, None, f"invalid regex: {exc}")
    match = compiled.search(raw_output)
    if not match:
        return ParsedValue(None, None, None, "no match")
    try:
        text = match.group(group_index)
    except (IndexError, re.error):
        return ParsedValue(None, None, None, f"capture group {group_index} missing")
    if text is None:
        return ParsedValue(None, None, None, f"capture group {group_index} empty")
    try:
        num, txt = cast_value(text, cast)
    except (ValueError, TypeError):
        return ParsedValue(text, None, text, f"cannot cast to {cast}")
    return ParsedValue(text, num, txt)
