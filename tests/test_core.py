import pytest

from takbridge import cot
from takbridge.core import Bridge
from takbridge.store import Store
from helpers import Clock, fixture, groklink_event, make_config


@pytest.fixture
def env():
    clock = Clock()
    cfg = make_config(outbound_per_minute=3)
    store = Store(":memory:", clock=clock)
    sent = []
    bridge = Bridge(cfg, store, send=lambda bot, data: sent.append((bot, data)) or True,
                    connected=lambda bot: True, clock=clock)
    # MITCH reports a position, so the bridge has seen him.
    bridge.handle_event("GROK-1", fixture("position.xml"))
    return bridge, store, clock, sent


def test_direct_chat_lands_in_inbox_and_webhook_body(env):
    bridge, store, clock, sent = env
    bodies = bridge.handle_event("GROK-1", fixture("direct_chat.xml"))
    assert len(bodies) == 1
    body = bodies[0]
    assert body["bot"] == "GROK-1" and body["kind"] == "chat"
    assert body["from_callsign"] == "MITCH"
    assert body["sender_position"]["lat"] == 35.0
    # The same message again (server resend) is stored once and fires nothing.
    assert bridge.handle_event("GROK-1", fixture("direct_chat.xml")) == []
    r = bridge.call("GROK-1", "inbox", {"peek": True})
    assert r["ok"] and [m["message_id"] for m in r["messages"]] == ["m-001"]
    assert bridge.call("GROK-1", "inbox")["messages"]
    assert bridge.call("GROK-1", "inbox")["messages"] == []  # marked read


def test_one_bot_cannot_read_another_bots_inbox(env):
    bridge, *_ = env
    bridge.handle_event("GROK-1", fixture("direct_chat.xml"))
    assert bridge.call("GROK-2", "inbox")["messages"] == []
    assert len(bridge.call("GROK-1", "inbox")["messages"]) == 1


def test_group_chat_and_unknown_events_are_counted_not_stored(env):
    bridge, *_ = env
    assert bridge.handle_event("GROK-1", fixture("group_chat.xml")) == []
    assert bridge.handle_event("GROK-1", b"<event") == []
    assert bridge.counters["group_chat"] == 1 and bridge.counters["unparsable"] == 1
    assert bridge.call("GROK-1", "inbox")["messages"] == []


def test_say_sends_to_known_recipient_only(env):
    bridge, store, clock, sent = env
    r = bridge.call("GROK-1", "say", {"to": "MITCH", "text": "hello"})
    assert r["ok"], r
    ev = cot.parse_event(sent[0][1])
    assert ev.find("marti/dest").get("callsign") == "MITCH"
    r = bridge.call("GROK-1", "say", {"to": "NOBODY", "text": "hello"})
    assert r == {"ok": False, "error": "NOBODY has not been seen on the TAK network"}
    assert len(sent) == 1


def test_mark_validates_and_returns_random_uid(env):
    bridge, store, clock, sent = env
    bad = [
        ({"lat": 135, "lon": -90}, "lat must be between -90 and 90"),
        ({"lat": "abc", "lon": -90}, "lat must be a number"),
        ({"lat": float("nan"), "lon": -90}, "lat must be a finite number"),
        ({"lat": 35, "lon": 181}, "lon must be between -180 and 180"),
        ({"lat": 35, "lon": -90, "name": "x" * 41}, "name is longer than 40 characters"),
        ({"lat": 35, "lon": -90, "remarks": "x" * 501}, "remarks is longer than 500"),
    ]
    for extra, why in bad:
        args = {"to": "MITCH", "name": "X", **extra}
        r = bridge.call("GROK-1", "mark", args)
        assert not r["ok"] and why in r["error"], (extra, r)
    r1 = bridge.call("GROK-1", "mark", {"to": "MITCH", "lat": 35.0045, "lon": -90, "name": "T"})
    r2 = bridge.call("GROK-1", "mark", {"to": "MITCH", "lat": 35.0045, "lon": -90, "name": "T"})
    assert r1["ok"] and r2["ok"] and r1["uid"] != r2["uid"]
    assert r1["uid"].startswith("takbridge-GROK-1-")


def test_text_limits(env):
    bridge, *_ = env
    assert not bridge.call("GROK-1", "say", {"to": "MITCH", "text": "x" * 2001})["ok"]
    assert not bridge.call("GROK-1", "say", {"to": "MITCH", "text": "   "})["ok"]
    assert not bridge.call("GROK-1", "say", {"to": "MITCH", "text": "bell\x07"})["ok"]


def test_rate_limit_refuses_not_queues(env):
    bridge, store, clock, sent = env
    for _ in range(3):
        assert bridge.call("GROK-1", "say", {"to": "MITCH", "text": "x"})["ok"]
    r = bridge.call("GROK-1", "say", {"to": "MITCH", "text": "x"})
    assert not r["ok"] and "rate limit" in r["error"]
    assert len(sent) == 3
    # Other bots have their own budget; a minute later this one does too.
    assert bridge.call("GROK-2", "say", {"to": "MITCH", "text": "x"})["ok"]
    clock.t += 61
    assert bridge.call("GROK-1", "say", {"to": "MITCH", "text": "x"})["ok"]


def test_where_reports_age_and_staleness(env):
    bridge, store, clock, sent = env
    r = bridge.call("GROK-1", "where", {"callsign": "MITCH"})
    assert r["ok"] and r["lat"] == 35.0 and r["age_s"] == 0 and r["stale"] is False
    clock.t += 601
    assert bridge.call("GROK-1", "where", {"callsign": "MITCH"})["stale"] is True
    assert not bridge.call("GROK-1", "where", {"callsign": "BRAVO"})["ok"]


def test_contacts_excludes_bots_and_status(env):
    bridge, *_ = env
    r = bridge.call("GROK-1", "contacts")
    assert [c["callsign"] for c in r["contacts"]] == ["MITCH"]
    assert bridge.call("GROK-1", "status") == {"ok": True, "bot": "GROK-1", "connected": True,
                                               "sent_last_minute": 0}


def test_unknown_tool_and_bot(env):
    bridge, *_ = env
    assert bridge.call("GROK-1", "rm") == {"ok": False, "error": "unknown command rm"}
    assert bridge.call("EVIL", "inbox") == {"ok": False, "error": "unknown bot EVIL"}


# ------------------------------------------------------------ snapshots --

def _request(bridge, sid, text="Which route?", n=1):
    summary = {"counts": {"route": 1}, "detail": "sending"}
    return bridge.handle_event("GROK-1", groklink_event(
        {"v": 1, "msg": "request", "snapshot_id": sid, "text": text, "summary": summary},
        uid=f"GrokLink.ANDROID-abc123.{sid}.{n}"))


def _detail(bridge, sid, part, parts, items, bot="GROK-1"):
    return bridge.handle_event(bot, groklink_event(
        {"v": 1, "msg": "detail", "snapshot_id": sid, "part": part, "parts": parts,
         "items": items}, uid=f"GrokLink.ANDROID-abc123.{sid}.p{part}"))


ROUTE = {"uid": "r1", "kind": "route", "name": "Route A", "lat": 35.001, "lon": -90.0,
         "points": [[35, -90], [35.01, -90]], "remarks": "", "extra": {}}
MARK = {"uid": "m1", "kind": "marker", "name": "Crossing", "lat": 35.1, "lon": -90.0,
        "remarks": "ford here", "extra": {}}


def test_request_fires_webhook_with_summary_detail_does_not(env):
    bridge, *_ = env
    bodies = _request(bridge, "s1")
    assert len(bodies) == 1 and bodies[0]["kind"] == "request"
    assert bodies[0]["summary"]["counts"] == {"route": 1}
    assert bodies[0]["from_callsign"] == "MITCH"
    assert _detail(bridge, "s1", 1, 2, [ROUTE]) == []
    assert [m["kind"] for m in bridge.call("GROK-1", "inbox")["messages"]] == ["request"]


def test_parts_out_of_order_and_missing(env):
    bridge, *_ = env
    _detail(bridge, "s1", 2, 2, [MARK])     # detail before its request
    _request(bridge, "s1")
    r = bridge.call("GROK-1", "sitrep")
    assert r["ok"] and r["parts_received"] == 1 and r["complete"] is False
    assert r["summary"]["detail"] == "sending"
    _detail(bridge, "s1", 1, 2, [ROUTE])
    r = bridge.call("GROK-1", "sitrep", {"from": "MITCH"})
    assert r["parts_received"] == 2 and r["complete"] is True
    items = bridge.call("GROK-1", "items", {"kind": "route"})
    assert [i["uid"] for i in items["items"]] == ["r1"]
    near = bridge.call("GROK-1", "items", {"near": [35.0, -90.0], "radius": 500})
    assert [i["uid"] for i in near["items"]] == ["r1"]
    full = bridge.call("GROK-1", "item", {"uid": "m1"})
    assert full["item"]["remarks"] == "ford here"
    assert not bridge.call("GROK-1", "item", {"uid": "zz"})["ok"]


def test_items_limit_sets_more(env):
    bridge, *_ = env
    _request(bridge, "s1")
    _detail(bridge, "s1", 1, 1, [dict(MARK, uid=f"m{i}") for i in range(5)])
    r = bridge.call("GROK-1", "items", {"limit": 2})
    assert len(r["items"]) == 2 and r["more"] is True


def test_only_five_snapshots_kept(env):
    bridge, store, clock, sent = env
    for i in range(7):
        clock.t += 1
        _request(bridge, f"s{i}")
        _detail(bridge, f"s{i}", 1, 1, [ROUTE])
    n = store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert n == 5
    assert store.db.execute(
        "SELECT COUNT(*) FROM items WHERE snapshot_id IN ('s0','s1')").fetchone()[0] == 0
    assert bridge.call("GROK-1", "sitrep")["snapshot_id"] == "s6"


def test_a_bot_cannot_read_snapshots_sent_to_another(env):
    bridge, *_ = env
    _request(bridge, "s1")
    _detail(bridge, "s1", 1, 1, [ROUTE])
    for tool, args in [("sitrep", {}), ("items", {}), ("item", {"uid": "r1"}),
                       ("sitrep", {"from": "MITCH"})]:
        r = bridge.call("GROK-2", tool, args)
        assert not r["ok"], (tool, r)


def test_oversized_and_bad_requests_are_rejected(env):
    bridge, *_ = env
    assert bridge.handle_event("GROK-1", groklink_event("{not json")) == []
    too_many = {"v": 1, "msg": "detail", "snapshot_id": "s", "part": 1, "parts": 1,
                "items": [MARK] * 201}
    assert bridge.handle_event("GROK-1", groklink_event(too_many)) == []
    assert bridge.counters["groklink_rejected"] == 2


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/hook", True),
    ("http://127.0.0.1:8765/takbridge", True),
    ("http://grokbot.snowy-pangolin.ts.net:8080/x", True),
    ("http://100.75.230.48:8080/x", True),
    ("http://192.168.30.94/x", True),
    ("http://example.com/hook", False),
    ("http://8.8.8.8/hook", False),
    ("ftp://example.com/x", False),
])
def test_webhook_url_must_be_https_off_the_tailnet(url, ok):
    if ok:
        assert make_config(webhook=url).bots["GROK-1"].webhook_url == url
    else:
        with pytest.raises(ValueError):
            make_config(webhook=url)


def test_webhook_headers_from_config():
    from takbridge import config
    base = ("[bridge]\ncot_url = tcp://h:1\nlat = 1\nlon = 1\n"
            "webhook_secret = 0123456789abcdef0123\n[bot:B]\nwebhook_url = https://x.example/h\n")
    cfg = config.loads(base + "webhook_header_Authorization = Bearer abc  \n")
    assert cfg.bots["B"].webhook_headers == {"Authorization": "Bearer abc"}
    assert "abc" not in repr(cfg.bots["B"])  # never shown in logs
    for bad in ("webhook_header_Content-Type = x\n", "webhook_header_Bad Name = x\n",
                "webhook_header_X-Signature = \n"):
        with pytest.raises(ValueError):
            config.loads(base + bad)


def test_webhook_sender_position_has_age(env):
    bridge, store, clock, sent = env
    clock.t += 42  # the position was seen 42 s before the chat
    body = bridge.handle_event("GROK-1", fixture("direct_chat.xml"))[0]
    assert body["sender_position"]["age_s"] == 42


def test_chat_does_not_make_an_old_position_look_fresh(env):
    bridge, store, clock, sent = env
    clock.t += 600
    bridge.handle_event("GROK-1", fixture("direct_chat.xml"))  # MITCH chats; no new fix
    w = bridge.call("GROK-1", "where", {"callsign": "MITCH"})
    assert w["age_s"] == 600 and w["online"] is True
