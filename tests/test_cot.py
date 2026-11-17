from datetime import datetime, timezone

import pytest

from takbridge import cot
from helpers import fixture, groklink_event

BOT = "takbridge-GROK-1"
NOW = datetime(2026, 11, 16, 21, 0, 0, tzinfo=timezone.utc)


def test_direct_chat_fixture_parses():
    what = cot.classify(cot.parse_event(fixture("direct_chat.xml")), BOT)
    assert isinstance(what, cot.DirectChat)
    assert what.message_id == "m-001"
    assert what.from_uid == "ANDROID-abc123"
    assert what.from_callsign == "MITCH"
    assert what.text == "Mark the trailhead 500 m north of me."
    assert cot.iso(what.time) == "2026-11-16T21:00:05Z"


def test_chat_for_someone_else_or_a_room_is_group_chat():
    what = cot.classify(cot.parse_event(fixture("group_chat.xml")), BOT)
    assert isinstance(what, cot.GroupChat)
    what = cot.classify(cot.parse_event(fixture("direct_chat.xml")), "takbridge-OTHER")
    assert isinstance(what, cot.GroupChat)


def test_position_fixture_parses():
    what = cot.classify(cot.parse_event(fixture("position.xml")), BOT)
    assert isinstance(what, cot.Position)
    assert (what.uid, what.callsign, what.lat, what.lon) == ("ANDROID-abc123", "MITCH", 35.0, -90.0)


def test_dtd_and_garbage_are_refused():
    with pytest.raises(cot.CotParseError):
        cot.parse_event(b"<!DOCTYPE x [<!ENTITY a 'b'>]><event uid='x'/>")
    with pytest.raises(cot.CotParseError):
        cot.parse_event(b"<event")
    with pytest.raises(cot.CotParseError):
        cot.parse_event(b"<notevent/>")


def test_split_stream_keeps_partial_tail():
    a, b = fixture("position.xml").strip(), fixture("group_chat.xml").strip()
    events, rest = cot.split_stream(a + b[:50])
    assert len(events) == 1 and rest == b[:50]
    events, rest = cot.split_stream(rest + b[50:])
    assert len(events) == 1 and rest == b""


@pytest.mark.parametrize("text", ["plain", 'q"uo\'tes', "<b>tags</b> & amps", "multi\nline"])
def test_built_chat_parses_back(text):
    data = cot.build_chat(bot_uid=BOT, bot_callsign="GROK-1", to_uid="ANDROID-abc123",
                          to_callsign="MITCH", text=text, message_id="x1",
                          lat=35, lon=-90, now=NOW)
    ev = cot.parse_event(data)
    # Seen from the recipient's side it is a direct chat from the bot.
    what = cot.classify(ev, "ANDROID-abc123")
    assert isinstance(what, cot.DirectChat)
    assert what.text == text
    assert what.from_uid == BOT
    assert what.from_callsign == "GROK-1"
    assert ev.find("marti/dest").get("callsign") == "MITCH"


def test_built_marker_escapes_and_targets_one_recipient():
    data = cot.build_marker(bot_uid=BOT, to_callsign="MITCH", uid="takbridge-GROK-1-u1",
                            lat=35.0045, lon=-90, name='Trail <head> & "x"',
                            remarks="a<b", now=NOW)
    ev = cot.parse_event(data)
    assert ev.find("contact").get("callsign") == 'Trail <head> & "x"'
    assert ev.find("remarks").text == "a<b"
    assert [d.get("callsign") for d in ev.find("marti").findall("dest")] == ["MITCH"]
    assert (ev.lat, ev.lon) == (35.0045, -90.0)


def test_presence_stale_is_after_time():
    ev = cot.parse_event(cot.build_presence(bot_uid=BOT, callsign="GROK-1", lat=35, lon=-90,
                                            group="Cyan", role="Team Member", now=NOW,
                                            stale_s=120))
    assert (ev.stale - ev.time).total_seconds() == 120
    assert ev.find("contact").get("callsign") == "GROK-1"


def test_groklink_event_classified_and_checked():
    what = cot.classify(cot.parse_event(groklink_event({"v": 1, "text": "hi"})), BOT)
    assert isinstance(what, cot.GrokLink) and what.from_uid == "ANDROID-abc123"
    data = cot.parse_groklink(what.raw_json, 1000, 2)
    assert data["msg"] == "request"
    for bad, why in [("{", "does not parse"), ("[]", "not an object"),
                     ('{"v":2,"text":"x"}', "version"),
                     ('{"v":1,"msg":"detail"}', "no snapshot_id"),
                     ('{"v":1,"text":"x","items":[1,2,3]}', "more than 2 items"),
                     ('{"v":1,"text":"' + "x" * 2000 + '"}', "over 1000 bytes")]:
        with pytest.raises(ValueError, match=why):
            cot.parse_groklink(bad, 1000, 2)
