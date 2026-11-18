"""The tak command. Prints one JSON object; exit 0 on success, 1 on failure.

Two forms:
    tak say --to MITCH Marked the trailhead.
    echo '{"cmd":"say","to":"MITCH","text":"Marked the trailhead."}' | tak

The JSON-on-stdin form is the one bots should use over SSH: SSH loses the
quoting of the bot's own shell, while stdin passes text through untouched.

Over SSH the bot is set by the gate from the key used. Run locally, an admin
picks the bot with --bot NAME; the service refuses callers who are neither.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import control

DEFAULT_SOCKET = "/run/takbridge/takbridge.sock"


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # print JSON, not usage text
        raise _ArgError(message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="tak", description="Talk to the TAK network as a bot.")
    p.add_argument("--bot", help="bot callsign (admins, local use only)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("inbox", help="unread messages; marks them read")
    s.add_argument("--peek", action="store_true", help="do not mark read")

    s = sub.add_parser("say", help="direct chat message")
    s.add_argument("--to", required=True)
    s.add_argument("text", nargs="+", help="words after the options form the message")

    s = sub.add_parser("mark", help="point marker for one person")
    s.add_argument("--to", required=True)
    s.add_argument("--lat", required=True)
    s.add_argument("--lon", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--remarks", default="")

    s = sub.add_parser("rab", help="range-and-bearing line for one person")
    s.add_argument("--to", required=True)
    s.add_argument("--lat")
    s.add_argument("--lon")
    s.add_argument("--from", dest="from_", help="start at this person's position instead of --lat/--lon")
    s.add_argument("--bearing", required=True, help="degrees true, 0-360")
    s.add_argument("--range", dest="range_", required=True, help="metres")
    s.add_argument("--name", required=True)
    s.add_argument("--remarks", default="")

    s = sub.add_parser("circle", help="circular area for one person")
    s.add_argument("--to", required=True)
    s.add_argument("--lat")
    s.add_argument("--lon")
    s.add_argument("--from", dest="from_", help="centre on this person's position")
    s.add_argument("--radius", required=True, help="metres")
    s.add_argument("--name", required=True)
    s.add_argument("--remarks", default="")

    s = sub.add_parser("rectangle", help="rectangular area (two opposite corners)")
    s.add_argument("--to", required=True)
    s.add_argument("--lat", required=True)
    s.add_argument("--lon", required=True)
    s.add_argument("--lat2", required=True)
    s.add_argument("--lon2", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--remarks", default="")

    s = sub.add_parser("where", help="last known position")
    s.add_argument("callsign")

    sub.add_parser("contacts", help="callsigns seen")
    sub.add_parser("status", help="this bot's connection")

    s = sub.add_parser("map", help="markers, routes, shapes and range lines shared with this bot")
    s.add_argument("--kind", choices=["marker", "route", "shape", "rab"])
    s.add_argument("--name", help="only items whose name contains this text")
    s.add_argument("--near", nargs=2, metavar=("LAT", "LON"))
    s.add_argument("--radius", help="metres around --near (default 1000)")
    s.add_argument("--limit")

    s = sub.add_parser("mapitem", help="one shared map item in full")
    s.add_argument("uid")

    s = sub.add_parser("sitrep", help="latest map summary from a user")
    s.add_argument("--from", dest="from_")

    s = sub.add_parser("items", help="brief list of items in the latest snapshot")
    s.add_argument("--from", dest="from_")
    s.add_argument("--kind")
    s.add_argument("--near", nargs=2, metavar=("LAT", "LON"))
    s.add_argument("--radius")
    s.add_argument("--limit")

    s = sub.add_parser("item", help="one item in full")
    s.add_argument("uid")
    s.add_argument("--from", dest="from_")
    return p


def args_to_request(ns: argparse.Namespace) -> tuple[str, dict]:
    cmd = ns.cmd
    a: dict = {}
    if cmd == "inbox":
        a = {"peek": ns.peek}
    elif cmd == "say":
        a = {"to": ns.to, "text": " ".join(ns.text)}
    elif cmd == "mark":
        a = {"to": ns.to, "lat": ns.lat, "lon": ns.lon, "name": ns.name, "remarks": ns.remarks}
    elif cmd == "rab":
        a = {"to": ns.to, "bearing": ns.bearing, "range": ns.range_, "name": ns.name,
             "remarks": ns.remarks}
        if ns.from_:
            a["from"] = ns.from_
        else:
            a["lat"], a["lon"] = ns.lat, ns.lon
    elif cmd == "circle":
        a = {"to": ns.to, "radius": ns.radius, "name": ns.name, "remarks": ns.remarks}
        if ns.from_:
            a["from"] = ns.from_
        else:
            a["lat"], a["lon"] = ns.lat, ns.lon
    elif cmd == "rectangle":
        a = {"to": ns.to, "lat": ns.lat, "lon": ns.lon, "lat2": ns.lat2, "lon2": ns.lon2,
             "name": ns.name, "remarks": ns.remarks}
    elif cmd == "where":
        a = {"callsign": ns.callsign}
    elif cmd == "map":
        for k in ("kind", "name", "radius", "limit"):
            if getattr(ns, k) is not None:
                a[k] = getattr(ns, k)
        if ns.near:
            a["near"] = ns.near
    elif cmd == "mapitem":
        a = {"uid": ns.uid}
    elif cmd in ("sitrep", "items", "item"):
        if ns.from_:
            a["from"] = ns.from_
        if cmd == "items":
            for k in ("kind", "radius", "limit"):
                if getattr(ns, k) is not None:
                    a[k] = getattr(ns, k)
            if ns.near:
                a["near"] = ns.near
        if cmd == "item":
            a["uid"] = ns.uid
    return cmd, a


def stdin_request(text: str) -> tuple[str, dict]:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _ArgError(f"stdin is not JSON: {exc.msg}") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("cmd"), str):
        raise _ArgError('stdin JSON needs a "cmd" field')
    cmd = obj.pop("cmd")
    obj.pop("bot", None)  # the bot is never chosen by the request
    return cmd, obj


def run(argv: list[str], *, bot: str | None = None, stdin=None,
        socket_path: str | None = None) -> tuple[int, dict]:
    """Parse, call the service, return (exit code, JSON object)."""
    socket_path = socket_path or os.environ.get("TAKBRIDGE_SOCKET", DEFAULT_SOCKET)
    try:
        ns = build_parser().parse_args(argv)
        if bot is None:
            bot = ns.bot or os.environ.get("TAKBRIDGE_BOT")
        if not bot:
            raise _ArgError("no bot: use --bot NAME")
        if ns.cmd is None:
            stream = stdin if stdin is not None else sys.stdin
            if stream is None or (hasattr(stream, "isatty") and stream.isatty()):
                raise _ArgError("no command (see tak --help)")
            cmd, args = stdin_request(stream.read(70000))
        else:
            cmd, args = args_to_request(ns)
    except _ArgError as exc:
        return 1, {"ok": False, "error": str(exc)}
    try:
        reply = control.request(socket_path, bot, cmd, args)
    except (OSError, ValueError) as exc:
        return 1, {"ok": False, "error": f"cannot reach tak-bridged: {exc}"}
    return (0 if reply.get("ok") else 1), reply


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] in (["-h"], ["--help"]):
        build_parser().print_help()
        return 0
    code, out = run(argv)
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
