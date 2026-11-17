"""tak-bridged: the long-running service.

One TAK Server connection per configured bot (opened by PyTAK's
protocol_factory, kept up by PyTAK's supervise_with_reconnect), a presence
announcement per bot on a timer, the local control socket, and webhook calls
for new inbox entries.

Usage: tak-bridged [CONFIG]   (default /etc/takbridge/takbridge.ini, or
                               $TAKBRIDGE_CONFIG)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import pytak

from . import control, cot, webhook
from .config import Bot, Config, load
from .core import Bridge
from .store import Store

log = logging.getLogger("takbridge")

READ_CHUNK = 65536
MAX_BUFFER = 1024 * 1024  # larger than any accepted event (256 KB requests)
FATAL_RETRY_S = 60


class BotLink:
    """One bot's TAK Server connection."""

    def __init__(self, svc: "Service", bot: Bot):
        self.svc = svc
        self.bot = bot
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.connected = False

    def send(self, data: bytes) -> bool:
        if not self.connected:
            return False
        try:
            self.queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            return False

    async def run(self) -> None:
        section = self.svc.cfg.pytak_section(self.bot)
        while True:
            try:
                await pytak.supervise_with_reconnect(section, self._run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a configuration fault: keep the others up
                log.error("bot %s: connection failed (%s: %s); retrying in %ss",
                          self.bot.callsign, type(exc).__name__, exc, FATAL_RETRY_S)
                await asyncio.sleep(FATAL_RETRY_S)

    async def _run_once(self) -> None:
        reader, writer = await pytak.protocol_factory(self.svc.cfg.pytak_section(self.bot))
        log.info("bot %s: connected to %s", self.bot.callsign, self.svc.cfg.cot_url)
        self.connected = True
        while not self.queue.empty():  # drop anything queued while down
            self.queue.get_nowait()
        tasks = [
            asyncio.create_task(self._reader(reader)),
            asyncio.create_task(self._writer(writer)),
            asyncio.create_task(self._presence()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception():
                    raise t.exception()
            raise ConnectionResetError("connection closed")
        finally:
            self.connected = False
            for t in tasks:
                t.cancel()
            writer.close()
            log.warning("bot %s: disconnected", self.bot.callsign)

    async def _reader(self, reader: asyncio.StreamReader) -> None:
        buf = b""
        while True:
            chunk = await reader.read(READ_CHUNK)
            if not chunk:
                raise ConnectionResetError("server closed the connection")
            buf += chunk
            events, buf = cot.split_stream(buf)
            if len(buf) > MAX_BUFFER:
                log.warning("bot %s: dropped %d bytes without an event end",
                            self.bot.callsign, len(buf))
                buf = b""
            for data in events:
                for body in self.svc.bridge.handle_event(self.bot.callsign, data):
                    self.svc.fire_webhook(self.bot, body)

    async def _writer(self, writer: asyncio.StreamWriter) -> None:
        while True:
            data = await self.queue.get()
            writer.write(data)
            await writer.drain()

    async def _presence(self) -> None:
        cfg = self.svc.cfg
        while True:
            data = cot.build_presence(
                bot_uid=self.bot.uid, callsign=self.bot.callsign, lat=cfg.lat, lon=cfg.lon,
                group=cfg.group, role=cfg.role, now=datetime.now(timezone.utc),
                stale_s=cfg.presence_stale_s)
            self.queue.put_nowait(data)
            await asyncio.sleep(cfg.presence_interval_s)


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.links = {cs: BotLink(self, b) for cs, b in cfg.bots.items()}
        self.bridge = Bridge(cfg, self.store,
                             send=lambda bot, data: self.links[bot].send(data),
                             connected=lambda bot: self.links[bot].connected)
        self._webhooks: set[asyncio.Task] = set()

    def fire_webhook(self, bot: Bot, body: dict) -> None:
        t = asyncio.create_task(webhook.notify(
            bot.webhook_url, body, self.cfg.webhook_secret,
            timeout=self.cfg.webhook_timeout_s, retries=self.cfg.webhook_retries,
            headers=bot.webhook_headers))
        self._webhooks.add(t)
        t.add_done_callback(self._webhooks.discard)

    async def run(self) -> None:
        server = await control.serve(
            self.cfg.socket_path, self.bridge.call,
            control.make_authorizer(self.cfg.bot_login_user, self.cfg.admin_group))
        log.info("control socket at %s; %d bot(s)", self.cfg.socket_path, len(self.links))
        tasks = [asyncio.create_task(link.run()) for link in self.links.values()]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            server.close()
            self.store.close()
            log.info("stopped; counters %s", self.bridge.counters)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else os.environ.get("TAKBRIDGE_CONFIG", "/etc/takbridge/takbridge.ini")
    logging.basicConfig(
        level=os.environ.get("TAKBRIDGE_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load(path)
    except (OSError, ValueError) as exc:
        log.error("cannot load config %s: %s", path, exc)
        return 2
    asyncio.run(Service(cfg).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
