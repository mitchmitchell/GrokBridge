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


@pytest.mark.parametrize("name", ["server_protocol_offer.xml", "server_t-x-g-c.xml"])
def test_server_control_messages_are_ignored(name):
    assert isinstance(cot.classify(cot.parse_event(cap(name)), BOT), cot.Other)


def test_server_disconnect_notice():
    what = cot.classify(cot.parse_event(cap("server_t-x-d-d.xml")), BOT)
    assert isinstance(what, cot.Disconnected) and what.uid == "takbridge-GROK-1"


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


@pytest.mark.parametrize("name,from_uid,callsign,text", [
    ("chat_direct_from_tablet.xml", "ANDROID-tablet", "TEST1/TAK", "capture test one"),
    ("chat_direct_from_phone.xml", "ANDROID-phone", "TEST1/TAK", "capture test two"),
])
def test_real_direct_chats_to_the_bot(name, from_uid, callsign, text):
    what = cot.classify(cot.parse_event(cap(name)), BOT)
    assert isinstance(what, cot.DirectChat)
    assert (what.from_uid, what.from_callsign, what.text) == (from_uid, callsign, text)
    assert what.message_id  # ATAK's messageId, used to drop duplicates


def test_real_all_chat_rooms_message_is_not_for_the_bot():
    assert isinstance(cot.classify(cot.parse_event(cap("chat_all_rooms.xml")), BOT),
                      cot.GroupChat)


def test_real_groklink_request_from_the_plugin():
    what = cot.classify(cot.parse_event(cap("groklink_request_text_only.xml")), BOT)
    assert isinstance(what, cot.GrokLink) and what.from_uid == "ANDROID-tablet"
    assert cot.parse_groklink(what.raw_json, 262144, 200) == {
        "v": 1, "msg": "request", "text": "Hello"}


@pytest.mark.parametrize("name", ["marker_first.xml", "marker_update.xml"])
def test_real_markers_are_map_items_not_contacts(name):
    what = cot.classify(cot.parse_event(cap(name)), BOT)
    assert isinstance(what, cot.MapItem)
    assert what.name == "Capture Marker" and what.remarks == "Capture note"


def test_inbound_round_trip_with_real_traffic():
    cfg = make_config()
    store = Store(":memory:")
    bridge = Bridge(cfg, store, send=lambda b, d: True, connected=lambda b: True)
    bridge.handle_event("GROK-1", cap("position_atak_tablet.xml"))
    bridge.handle_event("GROK-1", cap("marker_first.xml"))
    bodies = bridge.handle_event("GROK-1", cap("chat_direct_from_tablet.xml"))
    assert [b["text"] for b in bodies] == ["capture test one"]
    assert bodies[0]["sender_position"]["lat"] == 35.0
    bodies = bridge.handle_event("GROK-1", cap("groklink_request_text_only.xml"))
    assert bodies[0]["kind"] == "request" and bodies[0]["data"]["text"] == "Hello"
    # The marker is not a contact, so it cannot be a recipient.
    assert [c["callsign"] for c in bridge.call("GROK-1", "contacts")["contacts"]] == ["TEST1/TAK"]
    assert not bridge.call("GROK-1", "say", {"to": "Capture Marker", "text": "x"})["ok"]


def test_marker_builder_matches_captured_marker_shape():
    real = cot.parse_event(cap("marker_first.xml"))
    ours = cot.parse_event(cot.build_marker(
        bot_uid=BOT, bot_callsign="GROK-1", to_callsign="TEST1/TAK", uid="takbridge-GROK-1-x",
        lat=35, lon=-90, name="Trailhead", remarks="note", now=real.time))
    assert (ours.type, ours.how) == (real.type, real.how)
    # _flow-tags_ is added by the server; precisionlocation says where ATAK
    # got the altitude (terrain data), and the bot sets no altitude.
    real_tags = [c.tag for c in real.detail if c.tag not in ("_flow-tags_", "precisionlocation")]
    ours_tags = [c.tag for c in ours.detail if c.tag != "marti"]
    assert sorted(set(ours_tags)) == sorted(set(real_tags))
    assert ours.find("link").get("parent_callsign") == "GROK-1"
    assert ours.find("marti/dest").get("callsign") == "TEST1/TAK"


def test_chat_builder_matches_captured_direct_chat_shape():
    real = cot.parse_event(cap("chat_direct_from_tablet.xml"))
    ours = cot.parse_event(cot.build_chat(
        bot_uid=BOT, bot_callsign="GROK-1", to_uid="ANDROID-tablet", to_callsign="TEST1/TAK",
        text="hi", message_id="m1", lat=35, lon=-90, now=real.time))
    assert (ours.type, ours.how) == (real.type, real.how)
    for el in ("__chat", "__chat/chatgrp", "link", "remarks"):
        r, o = real.find(el), ours.find(el)
        assert o is not None, el
        assert sorted(o.attrib) == sorted(r.attrib), el
    # Seen from the tablet, our reply is a direct chat from GROK-1.
    what = cot.classify(ours, "ANDROID-tablet")
    assert isinstance(what, cot.DirectChat) and what.from_callsign == "GROK-1"


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
