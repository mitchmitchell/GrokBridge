"""Make captured CoT safe to commit to a public repository.

Replaces every latitude/longitude with fixed test values, device and user
uids with stable placeholders, and drops phone numbers, email addresses,
server node ids and endpoint addresses. Everything else (element order,
attribute names, types, how, detail structure) is kept, because that shape
is what the fixtures are for.

Usage: python tools/sanitize.py IN.xml OUT.xml [--map OLD=NEW ...]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TEST_LAT, TEST_LON = "35.0000000", "-90.0000000"

RULES = [
    # coordinates (point and any lat/lon-like attribute)
    (re.compile(r'\blat="-?[0-9.]+"'), f'lat="{TEST_LAT}"'),
    (re.compile(r'\blon="-?[0-9.]+"'), f'lon="{TEST_LON}"'),
    (re.compile(r"\blat='-?[0-9.]+'"), f"lat='{TEST_LAT}'"),
    (re.compile(r"\blon='-?[0-9.]+'"), f"lon='{TEST_LON}'"),
    # personal data
    (re.compile(r'\s(phone|email|emailAddress)="[^"]*"'), ""),
    # TAK Server node id in flow tags
    (re.compile(r"TAK-Server-[0-9a-f]{32}"), "TAK-Server-00000000000000000000000000000000"),
    # IP:port endpoints (keep the "*:-1:stcp" form)
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}:\d+"), "192.0.2.1:4242"),
    # Windows SIDs used as WinTAK uids
    (re.compile(r"S-1-5-21-\d+-\d+-\d+-\d+"), "WINTAK-pc"),
]


def sanitize(text: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        text = text.replace(old, new)
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
