"""Parsing and formatting of GOC registration numbers such as ``01-11200``."""

from __future__ import annotations

import re

DEFAULT_PREFIX = "01-"
DEFAULT_WIDTH = 5

_GOC_RE = re.compile(r"^\s*([A-Za-z0-9]+)\s*-\s*(\d+)\s*$")


class InvalidGocNumber(ValueError):
    pass


def parse(goc: str) -> tuple[str, int, int]:
    """Return ``(prefix, number, width)`` for a GOC number string.

    ``"01-11200"`` -> ``("01-", 11200, 5)``.
    """
    if goc is None:
        raise InvalidGocNumber("empty GOC number")
    m = _GOC_RE.match(str(goc))
    if not m:
        raise InvalidGocNumber(f"not a GOC number: {goc!r}")
    digits = m.group(2)
    return f"{m.group(1)}-", int(digits), len(digits)


def fmt(number: int, prefix: str = DEFAULT_PREFIX, width: int = DEFAULT_WIDTH) -> str:
    """Format a sequence number back into GOC form, preserving leading zeros."""
    return f"{prefix}{number:0{width}d}"


def normalise(goc: str) -> str:
    """Canonical string form (removes stray spaces, keeps zeros/hyphen)."""
    prefix, num, width = parse(goc)
    return fmt(num, prefix, max(width, DEFAULT_WIDTH))


def is_valid(goc: str) -> bool:
    try:
        parse(goc)
        return True
    except InvalidGocNumber:
        return False
