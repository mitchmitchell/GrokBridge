"""Argument checks shared by every way into the bridge (tak, SSH, MCP)."""

from __future__ import annotations

import math

from .config import CALLSIGN_RE, Limits
from .errors import ToolError


def coord(value, name: str, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number") from None
    if not math.isfinite(v):
        raise ToolError(f"{name} must be a finite number")
    if not lo <= v <= hi:
        raise ToolError(f"{name} must be between {lo:g} and {hi:g}")
    return v


def lat(value) -> float:
    return coord(value, "lat", -90, 90)


def lon(value) -> float:
    return coord(value, "lon", -180, 180)


def text(value, name: str, max_chars: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ToolError(f"{name} must be text")
    v = value.strip()
    if required and not v:
        raise ToolError(f"{name} is empty")
    if len(v) > max_chars:
        raise ToolError(f"{name} is longer than {max_chars} characters")
    # Control characters other than newline and tab have no place in chat.
    if any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise ToolError(f"{name} contains control characters")
    return v


def callsign(value, name: str = "callsign") -> str:
    if not isinstance(value, str) or not CALLSIGN_RE.match(value.strip()):
        raise ToolError(f"{name} is not a valid callsign")
    return value.strip()


def positive_int(value, name: str, maximum: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a whole number") from None
    if not 1 <= v <= maximum:
        raise ToolError(f"{name} must be between 1 and {maximum}")
    return v


def radius(value) -> float:
    v = coord(value, "radius", 0, 20_000_000)
    if v == 0:
        raise ToolError("radius must be more than 0")
    return v


__all__ = ["lat", "lon", "text", "callsign", "positive_int", "radius", "Limits"]
