"""The bridge's logic, shared by every way in (tak command, SSH gate, MCP).

Inbound: handle_event() turns one CoT event from a bot's TAK connection into
stored contacts, inbox entries and snapshot detail, and returns the inbox
entries whose webhook should fire.

Outbound: call() runs one tool for one bot and returns the JSON object the
tak command prints. Every result carries "ok".
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from . import cot, validate
from .config import Config
from .errors import ToolError
from .store import Store

log = logging.getLogger("takbridge.core")

TOOLS = ("inbox", "say", "mark", "where", "contacts", "status", "map", "mapitem",
         "sitrep", "items", "item")
ITEM_KINDS = ("marker", "contact", "route", "shape", "rab", "bloodhound", "track", "other")
MAP_KINDS = ("marker", "route", "shape", "rab")


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Bridge:
    def __init__(self, config: Config, store: Store,
                 send: Callable[[str, bytes], bool],
                 connected: Callable[[str], bool],
                 clock: Callable[[], float] = time.time,
                 reconnect: Callable[[str], None] = lambda bot: None):
        """send(bot, event_bytes) queues an event on that bot's connection and
        returns False if it cannot; connected(bot) reports the link state;
        reconnect(bot) asks that bot's link to drop and start a fresh TAK
        session (used when TAK resets our groups)."""
        self.cfg = config
        self.store = store
        self.send = send
        self.connected = connected
        self.clock = clock
        self.reconnect = reconnect
        self.counters: dict[str, int] = {}

    def _count(self, what: str) -> None:
        self.counters[what] = self.counters.get(what, 0) + 1

    # ============================================================ inbound ==

    def handle_event(self, bot: str, data: bytes) -> list[dict]:
        """Process one raw event received on a bot's connection."""
        b = self.cfg.bot(bot)
        try:
            ev = cot.parse_event(data)
        except cot.CotParseError as exc:
            self._count("unparsable")
            log.debug("bot %s: unparsable event: %s", bot, exc)
            return []
        if ev.uid in {x.uid for x in self.cfg.bots.values()}:
            return []  # our own presence or messages echoed back

        what = cot.classify(ev, b.uid)
        if isinstance(what, cot.Position):
            self.store.see_contact(what.uid, what.callsign, what.lat, what.lon,
                                   cot.iso(what.time), what.speed_mps, what.course_deg)
            self._count("position")
            return []
        if isinstance(what, cot.Disconnected):
            if what.uid == b.uid:
                # TAK told us THIS bot was disconnected. The server sends this
                # when it resets our groups, and it may leave the TCP socket
                # half-open: 'connected' still reads true and our sends still
                # leave the socket, but the server no longer routes them (the
                # replies that silently stopped arriving). Trust the notice,
                # not the socket: drop and reconnect to get fresh groups.
                log.warning("bot %s: TAK sent a disconnect/group-reset notice for "
                            "this bot; reconnecting for a fresh session", bot)
                self._count("self_disconnect")
                self.reconnect(bot)
                return []
            self.store.mark_disconnected(what.uid)
            c = self.store.contact_by_uid(what.uid)
            log.info("bot %s: %s disconnected from the TAK Server", bot,
                     (c or {}).get("callsign") or what.uid)
            self._count("disconnected")
            return []
        if isinstance(what, cot.DirectChat):
            if what.from_uid:
                self.store.see_contact(what.from_uid, what.from_callsign, None, None, None)
            entry = self._store_entry(bot, what.message_id, "chat", what.from_uid,
                                      what.from_callsign, what.text, None, what.time)
            # Who and which message, never the text (activity log).
            log.info("bot %s: chat from %s (message %s)%s", bot, what.from_callsign,
                     what.message_id, "" if entry else ", duplicate ignored")
            return [entry] if entry else []
        if isinstance(what, cot.GrokLink):
            return self._handle_groklink(bot, what)
        if isinstance(what, cot.MapItem):
            # Markers, routes, shapes and range lines shared in the group or
            # sent to the bot. Stored per bot (each bot's connection has its
            # own view) for the map and mapitem tools. They never wake the
            # bot: ATAK re-sends shared items often (captured: every ~15 s),
            # and the server strips the routing that would say "for this bot".
            is_new = self.store.map_item(bot, what.uid) is None
            self.store.put_map_item(bot, what)
            if is_new:
                log.info("bot %s: new shared %s %r from %s", bot, what.kind, what.name,
                         what.creator_callsign)
            else:
                log.debug("bot %s: update to %s %r", bot, what.kind, what.name)
            self._count("map_item")
            return []
        self._count("group_chat" if isinstance(what, cot.GroupChat) else "other")
        return []

    def _handle_groklink(self, bot: str, g: cot.GrokLink) -> list[dict]:
        lim = self.cfg.limits
        if not g.from_uid:
            log.info("bot %s: groklink event %s has no sender link; dropped", bot, g.event_uid)
            return []
        try:
            data = cot.parse_groklink(g.raw_json, lim.request_bytes, lim.request_items)
        except ValueError as exc:
            log.info("bot %s: groklink from %s rejected: %s", bot, g.from_uid, exc)
            self._count("groklink_rejected")
            return []
        sender = self.store.contact_by_uid(g.from_uid)
        callsign = sender["callsign"] if sender else None

        if data["msg"] == "detail":
            try:
                part, parts = int(data["part"]), int(data["parts"])
                sid = validate.text(data["snapshot_id"], "snapshot_id", 100)
            except (TypeError, ValueError, ToolError) as exc:
                log.info("bot %s: bad detail from %s: %s", bot, g.from_uid, exc)
                return []
            if not 1 <= part <= parts <= 10000:
                log.info("bot %s: bad part numbers %s/%s", bot, part, parts)
                return []
            self.store.add_detail_part(bot, g.from_uid, sid, part, parts,
                                       data.get("items") or [], keep=lim.snapshots_kept)
            self._count("detail_part")
            return []  # detail never creates inbox entries or fires the webhook

        text = data["text"].strip()[: lim.chat_chars]
        sid = data.get("snapshot_id")
        if isinstance(sid, str) and sid:
            summary = data.get("summary") if isinstance(data.get("summary"), dict) else None
            self.store.upsert_snapshot(bot, g.from_uid, sid[:100], summary=summary,
                                       keep=lim.snapshots_kept)
        entry = self._store_entry(bot, g.event_uid, "request", g.from_uid, callsign,
                                  text, data, g.time)
        log.info("bot %s: plugin request from %s (message %s)%s", bot, callsign or g.from_uid,
                 g.event_uid, "" if entry else ", duplicate ignored")
        return [entry] if entry else []

    def _store_entry(self, bot, message_id, kind, from_uid, from_callsign, text, data,
                     when) -> dict | None:
        row = self.store.add_inbox(bot=bot, message_id=message_id, kind=kind,
                                   from_uid=from_uid, from_callsign=from_callsign,
                                   text=text, data=data, time=cot.iso(when))
        if row is None:
            return None  # duplicate (the server resent it)
        self._count(kind)
        return self.webhook_body(bot, message_id, kind, from_uid, from_callsign, text,
                                 data, cot.iso(when))

    def webhook_body(self, bot, message_id, kind, from_uid, from_callsign, text, data,
                     when) -> dict:
        body = {
            "bot": bot, "kind": kind, "message_id": message_id,
            "from_callsign": from_callsign, "from_uid": from_uid,
            "text": text, "time": when,
        }
        pos = self.store.contact_by_uid(from_uid) if from_uid else None
        if pos and pos.get("lat") is not None:
            body["sender_position"] = self._position(pos)
            # Seconds since the bridge last heard from that device, so a bot
            # can say "2 minutes ago" without converting time zones.
            body["sender_position"]["age_s"] = self._pos_age(pos)
        if data is not None:
            if isinstance(data.get("summary"), dict):
                body["summary"] = data["summary"]
            body["data"] = data
        return body

    # =========================================================== outbound ==

    def call(self, bot: str, tool: str, args: dict | None = None) -> dict:
        """Run one tool as one bot. Never raises for a refused request."""
        args = args or {}
        try:
            if bot not in self.cfg.bots:
                raise ToolError(f"unknown bot {bot}")
            if tool not in TOOLS:
                raise ToolError(f"unknown command {tool}")
            if not isinstance(args, dict):
                raise ToolError("arguments must be an object")
            result = getattr(self, "_t_" + tool)(bot, args)
            log.info("bot %s: %s ok%s", bot, tool, self._log_target(tool, args))
            return {"ok": True, **result}
        except ToolError as exc:
            log.info("bot %s: %s refused%s: %s", bot, tool, self._log_target(tool, args), exc)
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _log_target(tool: str, args) -> str:
        """Who a command was about, for the activity log. Never message
        text, marker names or coordinates."""
        if not isinstance(args, dict):
            return ""
        who = args.get("to") if tool in ("say", "mark") else (
            args.get("callsign") if tool == "where" else None)
        return f" ({str(who)[:40]})" if isinstance(who, str) and who else ""

    # --- helpers

    def _pos_age(self, c: dict) -> int:
        """Seconds since the position itself was reported (not since the
        device was last heard from, e.g. by chat)."""
        seen = c.get("pos_seen") or c["last_seen"]
        return max(0, int(self.clock() - seen))

    @staticmethod
    def _position(c: dict) -> dict:
        p = {"lat": c["lat"], "lon": c["lon"], "time": c["time"]}
        if c.get("speed_mps") is not None:
            p["speed_mps"] = round(c["speed_mps"], 2)
        if c.get("course_deg") is not None:
            p["course_deg"] = round(c["course_deg"], 1)
        return p

    def _recipient(self, value) -> dict:
        cs = validate.callsign(value, "to")
        c = self.store.contact_by_callsign(cs)
        if c is None or c["uid"] in {b.uid for b in self.cfg.bots.values()}:
            raise ToolError(f"{cs} has not been seen on the TAK network")
        return c

    def _rate_check(self, bot: str) -> None:
        if self.store.outbound_count(bot) >= self.cfg.limits.outbound_per_minute:
            raise ToolError(
                f"rate limit: at most {self.cfg.limits.outbound_per_minute} messages per minute")

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def _sender(self, bot: str, args: dict) -> str:
        """The user whose snapshots to read: --from CALLSIGN or the latest sender."""
        frm = args.get("from")
        if frm:
            c = self.store.contact_by_callsign(validate.callsign(frm, "from"))
            if c is None:
                raise ToolError(f"{frm} has not been seen on the TAK network")
            return c["uid"]
        uid = self.store.latest_sender(bot)
        if uid is None:
            raise ToolError("no snapshots have been sent to this bot")
        return uid

    def _snapshot(self, bot: str, sender_uid: str) -> dict:
        snap = self.store.latest_snapshot(bot, sender_uid)
        if snap is None:
            raise ToolError("no snapshots from that user")
        return snap

    # --- tools

    def _t_inbox(self, bot, args):
        return {"messages": self.store.inbox(bot, peek=bool(args.get("peek")))}

    def _t_say(self, bot, args):
        lim = self.cfg.limits
        text = validate.text(args.get("text"), "text", lim.chat_chars)
        to = self._recipient(args.get("to"))
        self._rate_check(bot)
        b = self.cfg.bot(bot)
        mid = str(uuid.uuid4())
        data = cot.build_chat(bot_uid=b.uid, bot_callsign=b.callsign, to_uid=to["uid"],
                              to_callsign=to["callsign"], text=text, message_id=mid,
                              lat=self.cfg.lat, lon=self.cfg.lon, now=self._now())
        if not self.send(bot, data):
            raise ToolError("the TAK Server connection is down; not sent")
        self.store.log_outbound(bot, "say", to["callsign"], mid)
        return {"message_id": mid}

    def _t_mark(self, bot, args):
        lim = self.cfg.limits
        lat = validate.lat(args.get("lat"))
        lon = validate.lon(args.get("lon"))
        name = validate.text(args.get("name"), "name", lim.name_chars)
        remarks = validate.text(args.get("remarks"), "remarks", lim.remarks_chars,
                                required=False)
        to = self._recipient(args.get("to"))
        self._rate_check(bot)
        b = self.cfg.bot(bot)
        uid = f"takbridge-{b.callsign}-{uuid.uuid4()}"
        data = cot.build_marker(bot_uid=b.uid, bot_callsign=b.callsign,
                                to_callsign=to["callsign"], uid=uid,
                                lat=lat, lon=lon, name=name, remarks=remarks, now=self._now())
        if not self.send(bot, data):
            raise ToolError("the TAK Server connection is down; not sent")
        self.store.log_outbound(bot, "mark", to["callsign"], uid)
        return {"uid": uid}

    def _t_where(self, bot, args):
        cs = validate.callsign(args.get("callsign"))
        c = self.store.contact_by_callsign(cs)
        if c is None or c.get("lat") is None:
            raise ToolError(f"no position known for {cs}")
        age = self._pos_age(c)
        return {"callsign": cs, **self._position(c), "age_s": age,
                "stale": age > self.cfg.limits.stale_after_s,
                "online": c.get("disconnected_at") is None}

    def _t_contacts(self, bot, args):
        now = self.clock()
        own = {b.uid for b in self.cfg.bots.values()}
        return {"contacts": [
            {"callsign": c["callsign"], "last_seen_s": max(0, int(now - c["last_seen"])),
             "online": c.get("disconnected_at") is None}
            for c in self.store.contacts(exclude_uids=own)]}

    def _t_map(self, bot, args):
        """Shared map items this bot has received, newest first, or nearest
        first with near/radius."""
        kind = args.get("kind")
        if kind is not None and kind not in MAP_KINDS:
            raise ToolError(f"kind must be one of {', '.join(MAP_KINDS)}")
        limit = validate.positive_int(args.get("limit", 50), "limit", 500)
        name = args.get("name")
        if name is not None:
            name = validate.text(name, "name", 100).lower()
        center = rad = None
        if args.get("near") is not None:
            near = args["near"]
            if not isinstance(near, (list, tuple)) or len(near) != 2:
                raise ToolError("near must be [lat, lon]")
            center = (validate.lat(near[0]), validate.lon(near[1]))
            rad = validate.radius(args.get("radius", 1000))
        rows = []
        for it in self.store.map_items(bot):
            if kind and it["kind"] != kind:
                continue
            if name and name not in (it["name"] or "").lower():
                continue
            brief = {k: it[k] for k in ("uid", "kind", "name", "lat", "lon",
                                         "creator_callsign")}
            brief["has_notes"] = bool(it["remarks"])
            if it["kind"] == "route":
                brief["length_m"] = round(sum(
                    distance_m(*a, *b) for a, b in zip(it["points"], it["points"][1:])), 1)
                brief["points"] = len(it["points"])
            for k in ("radius_m", "range_m", "bearing_deg", "shape"):
                if k in it["extra"]:
                    brief[k] = it["extra"][k]
            if center is not None:
                if it["lat"] is None or it["lon"] is None:
                    continue
                d = distance_m(center[0], center[1], it["lat"], it["lon"])
                if d > rad:
                    continue
                brief["distance_m"] = round(d, 1)
            rows.append(brief)
        if center is not None:
            rows.sort(key=lambda r: r["distance_m"])
        return {"items": rows[:limit], "more": len(rows) > limit}

    def _t_mapitem(self, bot, args):
        uid = validate.text(args.get("uid"), "uid", 200)
        it = self.store.map_item(bot, uid)
        if it is None:
            raise ToolError(f"no shared map item {uid}")
        del it["updated_at"]
        return {"item": it}

    def _t_status(self, bot, args):
        return {"bot": bot, "connected": bool(self.connected(bot)),
                "sent_last_minute": self.store.outbound_count(bot)}

    def _t_sitrep(self, bot, args):
        sender = self._sender(bot, args)
        snap = self._snapshot(bot, sender)
        c = self.store.contact_by_uid(sender)
        return {
            "from_callsign": c["callsign"] if c else None,
            "snapshot_id": snap["snapshot_id"],
            "age_s": max(0, int(self.clock() - snap["created_at"])),
            "summary": snap["summary"],
            "parts": snap["parts"],
            "parts_received": snap["parts_received"],
            "complete": bool(snap["parts"]) and snap["parts_received"] >= snap["parts"],
        }

    def _t_items(self, bot, args):
        sender = self._sender(bot, args)
        snap = self._snapshot(bot, sender)
        kind = args.get("kind")
        if kind is not None and kind not in ITEM_KINDS:
            raise ToolError(f"kind must be one of {', '.join(ITEM_KINDS)}")
        limit = validate.positive_int(args.get("limit", 50), "limit", 500)
        near = args.get("near")
        center = rad = None
        if near is not None:
            if not isinstance(near, (list, tuple)) or len(near) != 2:
                raise ToolError("near must be [lat, lon]")
            center = (validate.lat(near[0]), validate.lon(near[1]))
            rad = validate.radius(args.get("radius", 1000))
        rows = []
        for it in self.store.items(bot, sender, snap["snapshot_id"]):
            if kind and it["kind"] != kind:
                continue
            if center is not None:
                if it["lat"] is None or it["lon"] is None:
                    continue
                d = distance_m(center[0], center[1], it["lat"], it["lon"])
                if d > rad:
                    continue
                it["distance_m"] = round(d, 1)
            rows.append(it)
        if center is not None:
            rows.sort(key=lambda r: r["distance_m"])
        more = len(rows) > limit
        return {"snapshot_id": snap["snapshot_id"], "items": rows[:limit], "more": more}

    def _t_item(self, bot, args):
        uid = validate.text(args.get("uid"), "uid", 200)
        sender = self._sender(bot, args)
        snap = self._snapshot(bot, sender)
        it = self.store.item(bot, sender, snap["snapshot_id"], uid)
        if it is None:
            raise ToolError(f"no item {uid} in the latest snapshot")
        return {"snapshot_id": snap["snapshot_id"], "item": it}
