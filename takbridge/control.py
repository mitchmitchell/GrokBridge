"""Local control socket between the tak command and the service.

One JSON request per connection, one JSON reply:
    {"bot": "GROK-1", "tool": "say", "args": {...}}  ->  {"ok": true, ...}

Who may call is decided by the caller's Unix identity (SO_PEERCRED), not by
anything in the request:
  * the bot login user: only through the SSH gate, which sets "bot" from the
    key the bot logged in with; that user can run nothing else;
  * root, the service user, or a member of the admin group: any bot (testing).
Everyone else is refused. The socket file is also mode 0660.
"""

from __future__ import annotations

import asyncio
import grp
import json
import logging
import os
import pwd
import socket
import struct
from typing import Callable

log = logging.getLogger("takbridge.control")

MAX_REQUEST = 64 * 1024


def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def make_authorizer(bot_login_user: str, admin_group: str) -> Callable[[int | None], bool]:
    def allowed(uid: int | None) -> bool:
        if uid is None:
            return False
        if uid in (0, os.getuid()):
            return True
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            return False
        if name == bot_login_user:
            return True
        try:
            g = grp.getgrnam(admin_group)
        except KeyError:
            return False
        return name in g.gr_mem or pwd.getpwuid(uid).pw_gid == g.gr_gid
    return allowed


async def serve(path: str, handle: Callable[[str, str, dict], dict],
                allowed: Callable[[int | None], bool]) -> asyncio.AbstractServer:
    """Start the socket server. handle(bot, tool, args) -> reply dict."""

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            uid = peer_uid(writer)
            if not allowed(uid):
                log.warning("control: refused caller uid=%s", uid)
                reply = {"ok": False, "error": "not allowed"}
            else:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if len(line) > MAX_REQUEST:
                    reply = {"ok": False, "error": "request too large"}
                else:
                    try:
                        req = json.loads(line)
                        reply = handle(str(req.get("bot", "")), str(req.get("tool", "")),
                                       req.get("args") or {})
                    except (json.JSONDecodeError, AttributeError):
                        reply = {"ok": False, "error": "bad request"}
            writer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError) as exc:
            log.debug("control: client error %s", exc)
        finally:
            writer.close()

    if os.path.exists(path):
        os.unlink(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    server = await asyncio.start_unix_server(on_client, path=path, limit=MAX_REQUEST + 1)
    os.chmod(path, 0o660)
    return server


def request(path: str, bot: str, tool: str, args: dict, timeout: float = 15.0) -> dict:
    """Blocking client used by the tak command."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(json.dumps({"bot": bot, "tool": tool, "args": args}).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf)
