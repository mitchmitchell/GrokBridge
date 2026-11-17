"""Make captured CoT safe to commit to a public repository.

Moves every coordinate so the first real position in the file lands on
35, -90 while keeping the distances between points (a route's shape and a
range line's length stay true); replaces device and user uids with stable
placeholders; drops phone numbers, email addresses, server node ids and
endpoint addresses. Everything else (element order, attribute names, types,
how, detail structure) is kept, because that shape is what the fixtures are
for.

Usage: python tools/sanitize.py IN.xml OUT.xml [--map OLD=NEW ...]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TEST_LAT, TEST_LON = 35.0, -90.0

NUM = r"-?[0-9]+(?:\.[0-9]+)?"
LAT_ATTR = re.compile(r"""\blat=(["'])(%s)\1""" % NUM)
LON_ATTR = re.compile(r"""\blon=(["'])(%s)\1""" % NUM)
POINT_ATTR = re.compile(r"""\bpoint=(["'])(%s),(%s)((?:,%s)?)\1""" % (NUM, NUM, NUM))

RULES = [
    (re.compile(r'\s(phone|email|emailAddress)="[^"]*"'), ""),
    (re.compile(r"TAK-Server-[0-9a-f]{32}"), "TAK-Server-00000000000000000000000000000000"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}:\d+"), "192.0.2.1:4242"),
    (re.compile(r"S-1-5-21-\d+-\d+-\d+-\d+"), "WINTAK-pc"),
]


def _anchor(text: str) -> tuple[float, float] | None:
    """The first non-zero position in the file."""
    lats = [float(m.group(2)) for m in LAT_ATTR.finditer(text)]
    lons = [float(m.group(2)) for m in LON_ATTR.finditer(text)]
    for la, lo in zip(lats, lons):
        if la != 0 or lo != 0:
            return la, lo
    m = POINT_ATTR.search(text)
    if m:
        return float(m.group(2)), float(m.group(3))
    return None


def _shift(text: str) -> str:
    anchor = _anchor(text)
    if anchor is None:
        return text
    a_lat, a_lon = anchor

    def lat(v: str) -> str:
        f = float(v)
        return v if f == 0 else f"{TEST_LAT + (f - a_lat):.7f}"

    def lon(v: str) -> str:
        f = float(v)
        return v if f == 0 else f"{TEST_LON + (f - a_lon):.7f}"

    text = LAT_ATTR.sub(lambda m: f"lat={m.group(1)}{lat(m.group(2))}{m.group(1)}", text)
    text = LON_ATTR.sub(lambda m: f"lon={m.group(1)}{lon(m.group(2))}{m.group(1)}", text)
    return POINT_ATTR.sub(
        lambda m: f"point={m.group(1)}{lat(m.group(2))},{lon(m.group(3))}{m.group(4)}{m.group(1)}",
        text)


def sanitize(text: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        text = text.replace(old, new)
    text = _shift(text)
    for pattern, repl in RULES:
        text = pattern.sub(repl, text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--map", action="append", default=[], help="OLD=NEW literal replacement")
    a = ap.parse_args()
    mapping = dict(m.split("=", 1) for m in a.map)
    out = sanitize(Path(a.src).read_text(encoding="utf-8"), mapping)
    Path(a.dst).write_text(out, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
