"""Shared map items, speed and heading, and disconnects, against real traffic."""

from pathlib import Path

import pytest

from takbridge import cot
from takbridge.core import Bridge, distance_m
from takbridge.store import Store
from helpers import Clock, make_config

CAPTURED = Path(__file__).parent / "fixtures" / "captured"
BOT = "takbridge-GROK-1"


def cap(name: str) -> bytes:
    return (CAPTURED / name).read_bytes()


def item(name: str) -> cot.MapItem:
    what = cot.classify(cot.parse_event(cap(name)), BOT)
    assert isinstance(what, cot.MapItem), what
    return what


def test_route():
    r = item("route.xml")
    assert (r.kind, r.name, r.creator_callsign) == ("route", "Capture Route", "TEST1/TAK-TABLET")
    assert len(r.points) == 5
    assert (r.lat, r.lon) == tuple(r.points[0]) == (35.0, -90.0)
    assert [w["name"] for w in r.extra["waypoints"]] == ["Capture Route SP", "CP1", "CP2", "VDO"]
    assert r.extra["method"] == "Driving"


def test_range_and_bearing():
    r = item("range_bearing.xml")
    assert r.kind == "rab"
    assert r.extra["range_m"] == 179.1 and r.extra["bearing_deg"] == 115.46
    end = r.extra["end"]
    assert distance_m(r.lat, r.lon, *end) == pytest.approx(179.1, abs=0.5)


def test_circle():
    c = item("circle.xml")
    assert (c.kind, c.extra["shape"], c.extra["radius_m"]) == ("shape", "circle", 513.3)
    assert (c.lat, c.lon) == (35.0, -90.0)


def test_rectangle():
    r = item("rectangle.xml")
    assert (r.kind, r.extra["shape"], len(r.points)) == ("shape", "rectangle", 4)
    assert r.lat is not None  # the centre point is kept as the anchor


def test_marker_is_a_map_item():
    m = item("marker_first.xml")
    assert (m.kind, m.name, m.remarks) == ("marker", "Capture Marker", "Capture note")


@pytest.fixture
def bridge():
    clock = Clock(1_795_000_000.0)  # 2026-11-17, before the captured items go stale
    store = Store(":memory:", clock=clock)
    b = Bridge(make_config(), store, send=lambda *a: True, connected=lambda b: True,
               clock=clock)
    for name in ("route.xml", "range_bearing.xml", "circle.xml", "rectangle.xml",
                 "marker_first.xml"):
        assert b.handle_event("GROK-1", cap(name)) == []  # never wakes the bot
    return b, clock


def test_map_lists_and_filters(bridge):
    b, _ = bridge
    r = b.call("GROK-1", "map")
    assert r["ok"] and len(r["items"]) == 5
    routes = b.call("GROK-1", "map", {"kind": "route"})["items"]
    assert len(routes) == 1 and routes[0]["points"] == 5 and routes[0]["length_m"] > 0
    assert b.call("GROK-1", "map", {"name": "circle"})["items"][0]["radius_m"] == 513.3
    near = b.call("GROK-1", "map", {"near": [35.0, -90.0], "radius": 50})["items"]
    assert {i["kind"] for i in near} >= {"route", "rab", "shape"}
    assert all(i["distance_m"] <= 50 for i in near)
    assert not b.call("GROK-1", "map", {"kind": "bogus"})["ok"]


def test_mapitem_full_and_isolated(bridge):
    b, _ = bridge
    uid = b.call("GROK-1", "map", {"kind": "route"})["items"][0]["uid"]
    full = b.call("GROK-1", "mapitem", {"uid": uid})
    assert full["ok"] and len(full["item"]["points"]) == 5
    # Another bot's connection never received it.
    assert not b.call("GROK-2", "mapitem", {"uid": uid})["ok"]
    assert b.call("GROK-2", "map")["items"] == []


def test_items_expire_at_their_stale_time(bridge):
    b, clock = bridge
    clock.t += 3 * 86400
    # The drawn items carry a one-day stale time; the captured marker a year.
    assert [i["kind"] for i in b.call("GROK-1", "map")["items"]] == ["marker"]


def test_updates_replace_not_duplicate(bridge):
    b, _ = bridge
    b.handle_event("GROK-1", cap("route.xml"))
    assert len(b.call("GROK-1", "map", {"kind": "route"})["items"]) == 1


def test_where_has_speed_heading_and_online(bridge):
    b, _ = bridge
    b.handle_event("GROK-1", cap("position_atak_tablet.xml"))
    w = b.call("GROK-1", "where", {"callsign": "TEST1/TAK"})
    assert w["ok"] and w["speed_mps"] == 0.0 and w["course_deg"] == pytest.approx(317.3)
    assert w["online"] is True


def test_disconnect_notice_marks_contact_offline(bridge):
    b, _ = bridge
    b.handle_event("GROK-1", cap("position_atak_tablet.xml"))
    gone = (b"<event version='2.0' uid='x' type='t-x-d-d' how='h-g-i-g-o' "
            b"time='2026-11-17T20:25:45.975Z' start='2026-11-17T20:25:45.974Z' "
            b"stale='2026-11-17T20:26:05.974Z'><point lat='0' lon='0' hae='0' ce='9999999' "
            b"le='9999999'/><detail><link relation='p-p' uid='ANDROID-tablet' "
            b"type='a-f-G-U-C'/></detail></event>")
    b.handle_event("GROK-1", gone)
    assert b.call("GROK-1", "where", {"callsign": "TEST1/TAK"})["online"] is False
    assert b.call("GROK-1", "contacts")["contacts"][0]["online"] is False
    # Its next position report brings it back.
    b.handle_event("GROK-1", cap("position_atak_tablet.xml"))
    assert b.call("GROK-1", "where", {"callsign": "TEST1/TAK"})["online"] is True


def test_store_migrates_an_old_contacts_table(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE contacts (uid TEXT PRIMARY KEY, callsign TEXT, lat REAL,"
                " lon REAL, time TEXT, last_seen REAL NOT NULL)")
    con.commit()
    con.close()
    s = Store(str(db))
    cols = {r["name"] for r in s.db.execute("PRAGMA table_info(contacts)")}
    assert {"speed_mps", "course_deg", "disconnected_at", "pos_seen"} <= cols


def test_self_disconnect_notice_triggers_reconnect():
    cfg = make_config()
    store = Store(":memory:")
    calls = []
    b = Bridge(cfg, store, send=lambda *a: True, connected=lambda b: True,
               reconnect=lambda bot: calls.append(bot))

    def ddnotice(link_uid):
        return (f"<event version='2.0' uid='x' type='t-x-d-d' how='h-g-i-g-o' "
                f"time='2026-11-18T05:00:00Z' start='2026-11-18T05:00:00Z' "
                f"stale='2026-11-18T05:00:20Z'><point lat='0' lon='0' hae='0' ce='9' le='9'/>"
                f"<detail><link relation='p-p' uid='{link_uid}' type='a-f-G-U-C'/></detail></event>"
                ).encode()

    # a notice for THIS bot's own uid -> reconnect
    b.handle_event("GROK-1", ddnotice("takbridge-GROK-1"))
    assert calls == ["GROK-1"] and b.counters.get("self_disconnect") == 1
    # a notice for someone else -> mark offline, no reconnect
    b.handle_event("GROK-1", cap("position_atak_tablet.xml"))
    b.handle_event("GROK-1", ddnotice("ANDROID-tablet"))
    assert calls == ["GROK-1"]  # unchanged
    assert b.call("GROK-1", "where", {"callsign": "TEST1/TAK"})["online"] is False


def _mk_bridge():
    store = Store(":memory:")
    sent = []
    b = Bridge(make_config(), store, send=lambda bot, d: sent.append(d) or True,
               connected=lambda b: True)
    b.handle_event("GROK-1", cap("position_atak_tablet.xml"))  # TEST1/TAK seen
    return b, sent


def test_rab_builds_sends_and_parses_back():
    from takbridge import cot
    b, sent = _mk_bridge()
    r = b.call("GROK-1", "rab", {"to": "TEST1/TAK", "lat": 35.0, "lon": -90.0,
                                 "bearing": 30, "range": 1000, "name": "Bearing A"})
    assert r["ok"], r
    ev = cot.parse_event(sent[-1])
    assert ev.type == "u-rb-a"
    assert ev.find("range").get("value") == "1000.0" or ev.find("range").get("value") == "1000"
    assert ev.find("bearing").get("value").startswith("30")
    assert ev.find("marti/dest").get("callsign") == "TEST1/TAK"
    what = cot.classify(ev, "takbridge-GROK-1")
    assert what.kind == "rab" and what.extra["range_m"] == 1000.0


def test_rab_from_a_persons_position():
    b, sent = _mk_bridge()
    r = b.call("GROK-1", "rab", {"to": "TEST1/TAK", "from": "TEST1/TAK",
                                 "bearing": 90, "range": 500, "name": "B"})
    assert r["ok"], r  # start taken from TEST1/TAK's last position


def test_circle_builds_and_parses_back():
    from takbridge import cot
    b, sent = _mk_bridge()
    r = b.call("GROK-1", "circle", {"to": "TEST1/TAK", "lat": 35.0, "lon": -90.0,
                                    "radius": 300, "name": "Area 1"})
    assert r["ok"], r
    ev = cot.parse_event(sent[-1])
    assert ev.type == "u-d-c-c"
    what = cot.classify(ev, "takbridge-GROK-1")
    assert what.kind == "shape" and what.extra["shape"] == "circle" and what.extra["radius_m"] == 300.0
    assert ev.find("marti/dest").get("callsign") == "TEST1/TAK"


def test_rectangle_builds_with_four_corners():
    from takbridge import cot
    b, sent = _mk_bridge()
    r = b.call("GROK-1", "rectangle", {"to": "TEST1/TAK", "lat": 35.0, "lon": -90.0,
                                       "lat2": 35.01, "lon2": -89.99, "name": "Box"})
    assert r["ok"], r
    ev = cot.parse_event(sent[-1])
    assert ev.type == "u-d-r"
    assert len([l for l in ev.detail.findall("link") if l.get("point")]) == 4
    assert cot.classify(ev, "takbridge-GROK-1").extra["shape"] == "rectangle"


def test_shape_validation_and_bad_point():
    b, _ = _mk_bridge()
    assert not b.call("GROK-1", "rab", {"to": "TEST1/TAK", "lat": 35, "lon": -90,
                                        "bearing": 999, "range": 100, "name": "x"})["ok"]
    assert not b.call("GROK-1", "circle", {"to": "TEST1/TAK", "radius": 100, "name": "x"})["ok"]
    assert not b.call("GROK-1", "rab", {"to": "NOBODY", "lat": 35, "lon": -90,
                                        "bearing": 10, "range": 100, "name": "x"})["ok"]
