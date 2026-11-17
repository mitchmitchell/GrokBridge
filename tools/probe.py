"""Phase 0/1 probe: connect to the TAK Server as one bot and log raw events.

Throwaway tooling, not part of the service. It opens one connection with
PyTAK's protocol_factory, using the bot's section of the real config file,
and writes every complete <event> it receives to stdout and, with --save,
one file per event in a capture folder (for phase 1 fixtures).

Usage:
    python tools/probe.py CONFIG BOT [--seconds N] [--save DIR] [--announce]

--announce sends the bot's provisional presence every 30 s, so you can see
whether ATAK lists it (phase 2 replaces that message with a captured shape).
Nothing else is ever sent.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytak

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from takbridge import cot  # noqa: E402
from takbridge.config import load  # noqa: E402

UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def summary(data: bytes) -> str:
    try:
        ev = cot.parse_event(data)
    except cot.CotParseError as exc:
        return f"unparsable ({exc})"
    cs = ev.find("contact")
    return (f"type={ev.type} uid={ev.uid} how={ev.how} "
            f"callsign={cs.get('callsign') if cs is not None else '-'} "
            f"detail=[{', '.join(c.tag for c in (ev.detail or []))}]")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("bot")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--save")
    ap.add_argument("--announce", action="store_true")
    a = ap.parse_args()

    cfg = load(a.config)
    bot = cfg.bot(a.bot)
    print(f"PyTAK {pytak.__version__ if hasattr(pytak, '__version__') else '?'}; "
          f"connecting to {cfg.cot_url} as {bot.callsign} (uid {bot.uid})", flush=True)
    reader, writer = await pytak.protocol_factory(cfg.pytak_section(bot))
    print("connected", flush=True)

    save = Path(a.save) if a.save else None
    if save:
        save.mkdir(parents=True, exist_ok=True)

    async def announce():
        while True:
            writer.write(cot.build_presence(
                bot_uid=bot.uid, callsign=bot.callsign, lat=cfg.lat, lon=cfg.lon,
                group=cfg.group, role=cfg.role, now=datetime.now(timezone.utc),
                stale_s=cfg.presence_stale_s))
            await writer.drain()
            await asyncio.sleep(30)

    if a.announce:
        asyncio.create_task(announce())

    buf, n, end = b"", 0, time.monotonic() + a.seconds
    while time.monotonic() < end:
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=max(0.1, end - time.monotonic()))
        except asyncio.TimeoutError:
            break
        if not chunk:
            print("server closed the connection", flush=True)
            break
        buf += chunk
        events, buf = cot.split_stream(buf)
        for data in events:
            n += 1
            print(f"[{n}] {summary(data)}", flush=True)
            print(data.decode("utf-8", "replace"), flush=True)
            if save:
                try:
                    kind = cot.parse_event(data).type or "none"
                except cot.CotParseError:
                    kind = "unparsable"
                (save / f"{n:03d}-{UNSAFE.sub('_', kind)}.xml").write_bytes(data)
    writer.close()
    print(f"done: {n} events", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
