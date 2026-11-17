"""SSH gate: the forced command for every bot key.

authorized_keys line (one per bot, set up in phase 7):
    command="/opt/takbridge/bin/takbridge-gate GROK-1",no-port-forwarding,
    no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... grok-1

The bot name comes from that line, never from what the bot sends. The
command the bot asked for (SSH_ORIGINAL_COMMAND) is split with shlex, never
run by a shell, and must be "tak" plus one allowed subcommand. Shell
metacharacters are refused outright: free text belongs in the JSON-on-stdin
form, which this gate passes through untouched.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

from . import cli
from .config import CALLSIGN_RE

ALLOWED = {"inbox", "say", "mark", "where", "contacts", "status", "map", "mapitem",
           "sitrep", "items", "item"}
METACHARS = re.compile(r"[;&|`$<>\\(){}\n\r\x00]")


class GateError(Exception):
    pass


def parse(original: str | None) -> list[str]:
    """SSH_ORIGINAL_COMMAND -> argv for tak (without the leading 'tak')."""
    if not original or not original.strip():
        raise GateError("no command; this login only runs tak")
    if METACHARS.search(original):
        raise GateError("shell metacharacters are not allowed; send text as JSON on stdin")
    try:
        argv = shlex.split(original, posix=True)
    except ValueError as exc:
        raise GateError(f"cannot parse command: {exc}") from None
    if not argv or argv[0] != "tak":
        raise GateError("this login only runs tak")
    rest = argv[1:]
    if any(a == "--bot" or a.startswith("--bot=") for a in rest):
        raise GateError("--bot is not allowed over SSH")
    if rest and rest[0] not in ALLOWED:
        raise GateError(f"unknown tak command {rest[0]!r}")
    return rest


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not CALLSIGN_RE.match(argv[0]):
        print(json.dumps({"ok": False, "error": "gate misconfigured"}))
        return 1
    bot = argv[0]
    try:
        tak_argv = parse(os.environ.get("SSH_ORIGINAL_COMMAND"))
    except GateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    code, out = cli.run(tak_argv, bot=bot, stdin=sys.stdin)
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
