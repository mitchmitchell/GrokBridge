"""Shared test helpers: config, fixtures, a fake clock, a groklink event."""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

from takbridge import config

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-secret-0123456789abcdef"


def fixture(name: str) -> bytes:
    return (FIXTURES / "spec" / name).read_bytes()


def make_config(cot_url="tcp://127.0.0.1:1", db=":memory:", socket_path="/tmp/x.sock",
                webhook="http://127.0.0.1:1/hook", extra_bots=("GROK-2",), **limits):
    lines = [
        "[bridge]", f"cot_url = {cot_url}", "lat = 35.0", "lon = -90.0",
        "presence_interval_s = 1", "presence_stale_s = 5",
        f"db_path = {db}", f"socket_path = {socket_path}",
        f"webhook_secret = {SECRET}", "webhook_retries = 1",
        "[limits]",
        *[f"{k} = {v}" for k, v in limits.items()],
        "[bot:GROK-1]", f"webhook_url = {webhook}",
    ]
    for b in extra_bots:
        lines += [f"[bot:{b}]", f"webhook_url = {webhook}"]
    return config.loads("\n".join(lines) + "\n")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def groklink_event(payload: dict | str, *, uid="GrokLink.ANDROID-abc123.1",
                   sender="ANDROID-abc123") -> bytes:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return (
        f"<event version='2.0' uid='{uid}' type='t-x-groklink' how='h-e' "
        "time='2026-11-16T21:00:05.000Z' start='2026-11-16T21:00:05.000Z' "
        "stale='2026-11-16T21:02:05.000Z'>"
        "<point lat='35.0' lon='-90.0' hae='9999999.0' ce='9999999.0' le='9999999.0'/>"
        f"<detail><groklink>{escape(text)}</groklink>"
        f"<link uid='{sender}' relation='p-p' type='a-f-G-U-C'/></detail></event>"
    ).encode("utf-8")
