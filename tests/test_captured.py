"""The codec against real traffic captured from the TAK Server (phase 0/1)."""

from pathlib import Path

import pytest

from takbridge import cot
from takbridge.core import Bridge
from takbridge.store import Store
from helpers import make_config

CAPTURED = Path(__file__).parent / "fixtures" / "captured"
BOT = "takbridge-GROK-1"


def cap(name: str) -> bytes:
    return (CAPTURED / name).read_bytes()


@pytest.mark.parametrize("name,uid,type_", [
    ("position_atak_tablet.xml", "ANDROID-tablet", "a-f-G-U-C"),
    ("position_atak_phone.xml", "ANDROID-phone", "a-f-G-U-U-S"),
    ("position_wintak.xml", "WINTAK-pc", "a-f-G-U-C-I"),
])
def test_real_positions_parse(name, uid, type_):
    ev = cot.parse_event(cap(name))
    assert ev.type == type_
    what = cot.classify(ev, BOT)
    assert isinstance(what, cot.Position)
    assert what.uid == uid
    # Trailing space in the tablet's callsign is trimmed; "/" is kept.
    assert what.callsign == "TEST1/TAK"
    assert (what.lat, what.lon) == (35.0, -90.0)


@pytest.mark.parametrize("name", ["server_protocol_offer.xml", "server_t-x-d-d.xml",
                                  "server_t-x-g-c.xml"])
def test_server_control_messages_are_ignored(name):
    assert isinstance(cot.classify(cot.parse_event(cap(name)), BOT), cot.Other)


def test_callsign_with_slash_is_a_valid_recipient():
    cfg = make_config()
    store = Store(":memory:")
    sent = []
    bridge = Bridge(cfg, store, send=lambda b, d: sent.append(d) or True,
                    connected=lambda b: True)
    bridge.handle_event("GROK-1", cap("position_atak_tablet.xml"))
    r = bridge.call("GROK-1", "where", {"callsign": "TEST1/TAK"})
    assert r["ok"], r
    r = bridge.call("GROK-1", "say", {"to": "TEST1/TAK", "text": "hello"})
    assert r["ok"], r
    assert cot.parse_event(sent[0]).find("marti/dest").get("callsign") == "TEST1/TAK"


def test_presence_has_the_fields_real_atak_positions_carry():
    """Our provisional presence vs. the captured ATAK position: same
    top-level detail elements that make a contact (contact with a stream
    endpoint, __group, takv, uid)."""
    real = cot.parse_event(cap("position_atak_tablet.xml"))
    ours = cot.parse_event(cot.build_presence(
        bot_uid=BOT, callsign="GROK-1", lat=35, lon=-90, group="Cyan",
        role="Team Member", now=real.time, stale_s=120))
    for tag in ("contact", "__group", "takv", "uid"):
        assert real.find(tag) is not None and ours.find(tag) is not None, tag
    assert ours.find("contact").get("endpoint") == real.find("contact").get("endpoint")
    assert ours.type == real.type
