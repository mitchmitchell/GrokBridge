"""CoT codec: pure functions, no network or disk access (ground rule 4).

Parsing follows docs/TakChatSpec.odt in the ATAK-CIV SDK (P2P messages,
section 4.1; server routing with <marti>, section 3.2).

PROVISIONAL BUILDERS: build_chat, build_marker and build_presence are written
from that spec, not yet from traffic captured on the live TAK Server. Ground
rule 3 makes captured traffic the source of truth: phases 1, 2 and 5 replace
them with versions matched field by field to the captured fixtures in
tests/fixtures/captured/. Until then they are used only against the fake
TAK Server in the tests.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UNKNOWN = "9999999.0"
CHAT_TYPE = "b-t-f"
GROKLINK = "groklink"


class CotParseError(ValueError):
    pass


# ---------------------------------------------------------------- time ----


def cot_time(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def iso(dt: datetime | None) -> str | None:
    """Second-precision ISO 8601 UTC, as the bots see it."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- parsing ----


@dataclass
class Event:
    uid: str
    type: str
    how: str
    time: datetime | None
    stale: datetime | None
    lat: float | None
    lon: float | None
    detail: ET.Element | None
    raw: bytes = field(repr=False, default=b"")

    def find(self, path: str) -> ET.Element | None:
        return None if self.detail is None else self.detail.find(path)


def _float(v: str | None) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if abs(f) < 1e6 else None


def parse_event(data: bytes) -> Event:
    """Parse one <event>. Refuses DTDs and entity declarations outright."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise CotParseError("DTD not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CotParseError(str(exc)) from None
    if root.tag != "event":
        raise CotParseError(f"root is <{root.tag}>, not <event>")
    point = root.find("point")
    lat = lon = None
    if point is not None:
        lat, lon = _float(point.get("lat")), _float(point.get("lon"))
        if lat is not None and not -90 <= lat <= 90:
            lat = None
        if lon is not None and not -180 <= lon <= 180:
            lon = None
        if lat == 0 and lon == 0:
            lat = lon = None
    return Event(
        uid=root.get("uid", ""),
        type=root.get("type", ""),
        how=root.get("how", ""),
        time=parse_time(root.get("time")),
        stale=parse_time(root.get("stale")),
        lat=lat,
        lon=lon,
        detail=root.find("detail"),
        raw=data,
    )


def split_stream(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a TCP buffer into complete <event>...</event> blocks."""
    events = []
    end_tag = b"</event>"
    while True:
        end = buffer.find(end_tag)
        if end < 0:
            break
        end += len(end_tag)
        chunk = buffer[:end]
        start = chunk.find(b"<event")
        if start >= 0:
            events.append(chunk[start:])
        buffer = buffer[end:]
    return events, buffer


# ---------------------------------------------------------- classifying ----


@dataclass
class Position:
    uid: str
    callsign: str | None
    lat: float
    lon: float
    time: datetime | None


@dataclass
class DirectChat:
    message_id: str
    from_uid: str
    from_callsign: str | None
    to_uid: str
    text: str
    time: datetime | None


@dataclass
class GroupChat:
    chatroom: str


@dataclass
class GrokLink:
    event_uid: str
    from_uid: str
    raw_json: str
    time: datetime | None


@dataclass
class Other:
    type: str


def sender_uid(ev: Event) -> str | None:
    link = None
    if ev.detail is not None:
        for el in ev.detail.findall("link"):
            if el.get("relation") == "p-p" and el.get("uid"):
                link = el.get("uid")
                break
    return link


def classify(ev: Event, bot_uid: str):
    """What an incoming event means to one bot's connection."""
    groklink = ev.find(GROKLINK)
    if groklink is not None:
        return GrokLink(
            event_uid=ev.uid,
            from_uid=sender_uid(ev) or "",
            raw_json=(groklink.text or ""),
            time=ev.time,
        )

    if ev.type == CHAT_TYPE:
        chat = ev.find("__chat")
        remarks = ev.find("remarks")
        if chat is None or remarks is None:
            return Other(ev.type)
        if chat.get("id") != bot_uid:
            return GroupChat(chat.get("chatroom", ""))
        frm = sender_uid(ev)
        source = remarks.get("source", "")
        if not frm and source.startswith("BAO.F.ATAK."):
            frm = source[len("BAO.F.ATAK."):]
        return DirectChat(
            message_id=chat.get("messageId") or ev.uid,
            from_uid=frm or "",
            from_callsign=chat.get("senderCallsign"),
            to_uid=bot_uid,
            text=(remarks.text or ""),
            time=parse_time(remarks.get("time")) or ev.time,
        )

    contact = ev.find("contact")
    if ev.type.startswith("a-") and ev.lat is not None and ev.lon is not None:
        return Position(
            uid=ev.uid,
            callsign=contact.get("callsign") if contact is not None else None,
            lat=ev.lat,
            lon=ev.lon,
            time=ev.time,
        )
    return Other(ev.type)


def parse_groklink(raw_json: str, max_bytes: int, max_items: int) -> dict:
    """Parse and size-check a groklink payload. Raises ValueError with why."""
    if len(raw_json.encode("utf-8")) > max_bytes:
        raise ValueError(f"groklink payload over {max_bytes} bytes")
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"groklink JSON does not parse: {exc.msg}") from None
    if not isinstance(data, dict):
        raise ValueError("groklink JSON is not an object")
    if data.get("v") != 1:
        raise ValueError("groklink version is not 1")
    msg = data.get("msg", "request")
    if msg not in ("request", "detail"):
        raise ValueError(f"groklink msg {msg!r} unknown")
    data["msg"] = msg
    items = data.get("items")
    if items is not None:
        if not isinstance(items, list):
            raise ValueError("groklink items is not a list")
        if len(items) > max_items:
            raise ValueError(f"groklink message has more than {max_items} items")
    if msg == "request" and not isinstance(data.get("text"), str):
        raise ValueError("groklink request has no text")
    if msg == "detail":
        for key in ("snapshot_id", "part", "parts"):
            if key not in data:
                raise ValueError(f"groklink detail has no {key}")
    return data


# ------------------------------------------------------------- building ----


def _event(uid: str, type_: str, how: str, lat: float, lon: float,
           now: datetime, stale: datetime) -> tuple[ET.Element, ET.Element]:
    ev = ET.Element("event", {
        "version": "2.0", "uid": uid, "type": type_, "how": how,
        "time": cot_time(now), "start": cot_time(now), "stale": cot_time(stale),
    })
    ET.SubElement(ev, "point", {
        "lat": f"{lat:.7f}", "lon": f"{lon:.7f}",
        "hae": UNKNOWN, "ce": UNKNOWN, "le": UNKNOWN,
    })
    return ev, ET.SubElement(ev, "detail")


def _bytes(ev: ET.Element) -> bytes:
    # ElementTree escapes &, <, > and quotes in text and attributes.
    return ET.tostring(ev, encoding="utf-8", xml_declaration=False)


def _marti(detail: ET.Element, to_callsign: str) -> None:
    # Without this the TAK Server sends the message to everyone (spec 3.2).
    marti = ET.SubElement(detail, "marti")
    ET.SubElement(marti, "dest", {"callsign": to_callsign})


def build_chat(*, bot_uid: str, bot_callsign: str, to_uid: str, to_callsign: str,
               text: str, message_id: str, lat: float, lon: float,
               now: datetime) -> bytes:
    """PROVISIONAL (see module docstring). A P2P chat per TakChatSpec 4.1."""
    ev, detail = _event(f"GeoChat.{bot_uid}.{to_callsign}.{message_id}", CHAT_TYPE,
                        "h-g-i-g-o", lat, lon, now, now + timedelta(days=1))
    chat = ET.SubElement(detail, "__chat", {
        "parent": "RootContactGroup", "groupOwner": "false",
        "messageId": message_id, "chatroom": to_callsign, "id": to_uid,
        "senderCallsign": bot_callsign,
    })
    ET.SubElement(chat, "chatgrp", {"uid0": bot_uid, "uid1": to_uid, "id": to_uid})
    ET.SubElement(detail, "link", {"uid": bot_uid, "type": "a-f-G-U-C", "relation": "p-p"})
    remarks = ET.SubElement(detail, "remarks", {
        "source": f"BAO.F.ATAK.{bot_uid}", "to": to_uid, "time": cot_time(now),
    })
    remarks.text = text
    _marti(detail, to_callsign)
    return _bytes(ev)


def build_marker(*, bot_uid: str, to_callsign: str, uid: str, lat: float, lon: float,
                 name: str, remarks: str, now: datetime, stale_s: int = 86400) -> bytes:
    """PROVISIONAL (see module docstring). A point marker for one recipient."""
    ev, detail = _event(uid, "a-u-G", "h-g-i-g-o", lat, lon, now,
                        now + timedelta(seconds=stale_s))
    ET.SubElement(detail, "contact", {"callsign": name})
    ET.SubElement(detail, "link", {"uid": bot_uid, "type": "a-f-G-U-C", "relation": "p-p"})
    r = ET.SubElement(detail, "remarks")
    r.text = remarks
    _marti(detail, to_callsign)
    return _bytes(ev)


def build_presence(*, bot_uid: str, callsign: str, lat: float, lon: float,
                   group: str, role: str, now: datetime, stale_s: int) -> bytes:
    """PROVISIONAL (see module docstring). Announces the bot as a contact."""
    ev, detail = _event(bot_uid, "a-f-G-U-C", "m-g", lat, lon, now,
                        now + timedelta(seconds=stale_s))
    ET.SubElement(detail, "takv", {
        "platform": "tak-bridge", "device": "tak-bridge", "os": "linux", "version": "0.1",
    })
    # endpoint "*:-1:stcp" means "reach me through the TAK Server stream".
    ET.SubElement(detail, "contact", {"callsign": callsign, "endpoint": "*:-1:stcp"})
    ET.SubElement(detail, "__group", {"name": group, "role": role})
    ET.SubElement(detail, "uid", {"Droid": callsign})
    return _bytes(ev)
