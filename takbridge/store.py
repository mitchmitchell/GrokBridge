"""SQLite store: inbox, contacts, outbound log, snapshots and their items.

Every query that returns bot data takes the bot's callsign, so one bot can
never read another's inbox or snapshots.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot           TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- chat | request | item
    from_uid      TEXT NOT NULL,
    from_callsign TEXT,
    text          TEXT NOT NULL,
    data          TEXT,                   -- JSON, requests and items only
    time          TEXT,                   -- ISO 8601 UTC, sender's clock
    received_at   REAL NOT NULL,
    read          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (bot, message_id)
);
CREATE TABLE IF NOT EXISTS contacts (
    uid       TEXT PRIMARY KEY,
    callsign  TEXT,
    lat       REAL,
    lon       REAL,
    time      TEXT,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_callsign ON contacts (callsign);
CREATE TABLE IF NOT EXISTS outbound (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bot     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    to_callsign TEXT NOT NULL,
    uid     TEXT,
    sent_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbound_bot_time ON outbound (bot, sent_at);
CREATE TABLE IF NOT EXISTS snapshots (
    bot          TEXT NOT NULL,
    sender_uid   TEXT NOT NULL,
    snapshot_id  TEXT NOT NULL,
    summary      TEXT,
    parts        INTEGER,
    created_at   REAL NOT NULL,
    PRIMARY KEY (bot, sender_uid, snapshot_id)
);
CREATE TABLE IF NOT EXISTS snapshot_parts (
    bot TEXT NOT NULL, sender_uid TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    part INTEGER NOT NULL,
    PRIMARY KEY (bot, sender_uid, snapshot_id, part)
);
CREATE TABLE IF NOT EXISTS items (
    bot TEXT NOT NULL, sender_uid TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    uid       TEXT NOT NULL,
    kind      TEXT,
    name      TEXT,
    lat       REAL,
    lon       REAL,
    has_notes INTEGER NOT NULL DEFAULT 0,
    data      TEXT NOT NULL,
    PRIMARY KEY (bot, sender_uid, snapshot_id, uid)
);
"""


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ inbox --

    def add_inbox(self, *, bot: str, message_id: str, kind: str, from_uid: str,
                  from_callsign: str | None, text: str, data: dict | None,
                  time: str | None) -> int | None:
        """Store one entry. Returns its row id, or None if already stored."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO inbox (bot, message_id, kind, from_uid, from_callsign,"
            " text, data, time, received_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (bot, message_id, kind, from_uid, from_callsign, text,
             json.dumps(data, separators=(",", ":")) if data is not None else None,
             time or _iso_now(), self.clock()),
        )
        return cur.lastrowid if cur.rowcount else None

    def inbox(self, bot: str, peek: bool = False) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM inbox WHERE bot=? AND read=0 ORDER BY id", (bot,)).fetchall()
        if rows and not peek:
            self.db.execute(
                f"UPDATE inbox SET read=1 WHERE bot=? AND id IN ({','.join('?' * len(rows))})",
                (bot, *[r["id"] for r in rows]))
        return [self._entry(r) for r in rows]

    @staticmethod
    def _entry(r: sqlite3.Row) -> dict:
        e = {
            "message_id": r["message_id"],
            "kind": r["kind"],
            "from_callsign": r["from_callsign"],
            "from_uid": r["from_uid"],
            "text": r["text"],
            "time": r["time"],
        }
        if r["data"] is not None:
            e["data"] = json.loads(r["data"])
        return e

    # --------------------------------------------------------- contacts --

    def see_contact(self, uid: str, callsign: str | None, lat: float | None,
                    lon: float | None, time: str | None) -> None:
        self.db.execute(
            "INSERT INTO contacts (uid, callsign, lat, lon, time, last_seen)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET"
            " callsign=COALESCE(excluded.callsign, contacts.callsign),"
            " lat=COALESCE(excluded.lat, contacts.lat),"
            " lon=COALESCE(excluded.lon, contacts.lon),"
            " time=COALESCE(excluded.time, contacts.time),"
            " last_seen=excluded.last_seen",
            (uid, callsign, lat, lon, time, self.clock()))

    def contact_by_callsign(self, callsign: str) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM contacts WHERE callsign=? ORDER BY last_seen DESC LIMIT 1",
            (callsign,)).fetchone()
        return dict(r) if r else None

    def contact_by_uid(self, uid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM contacts WHERE uid=?", (uid,)).fetchone()
        return dict(r) if r else None

    def contacts(self, exclude_uids: set[str] = frozenset()) -> list[dict]:
        rows = self.db.execute(
            "SELECT uid, callsign, last_seen FROM contacts WHERE callsign IS NOT NULL"
            " ORDER BY callsign").fetchall()
        return [dict(r) for r in rows if r["uid"] not in exclude_uids]

    # --------------------------------------------------------- outbound --

    def outbound_count(self, bot: str, window_s: float = 60.0) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM outbound WHERE bot=? AND sent_at>?",
            (bot, self.clock() - window_s)).fetchone()[0]

    def log_outbound(self, bot: str, kind: str, to_callsign: str, uid: str | None) -> None:
        self.db.execute(
            "INSERT INTO outbound (bot, kind, to_callsign, uid, sent_at) VALUES (?,?,?,?,?)",
            (bot, kind, to_callsign, uid, self.clock()))

    # -------------------------------------------------------- snapshots --

    def upsert_snapshot(self, bot: str, sender_uid: str, snapshot_id: str, *,
                        summary: dict | None = None, parts: int | None = None,
                        keep: int = 5) -> None:
        self.db.execute(
            "INSERT INTO snapshots (bot, sender_uid, snapshot_id, summary, parts, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(bot, sender_uid, snapshot_id) DO UPDATE SET"
            " summary=COALESCE(excluded.summary, snapshots.summary),"
            " parts=COALESCE(excluded.parts, snapshots.parts)",
            (bot, sender_uid, snapshot_id,
             json.dumps(summary, separators=(",", ":")) if summary is not None else None,
             parts, self.clock()))
        self._expire(bot, sender_uid, keep)

    def _expire(self, bot: str, sender_uid: str, keep: int) -> None:
        old = self.db.execute(
            "SELECT snapshot_id FROM snapshots WHERE bot=? AND sender_uid=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (bot, sender_uid, keep)).fetchall()
        for r in old:
            for table in ("items", "snapshot_parts", "snapshots"):
                self.db.execute(
                    f"DELETE FROM {table} WHERE bot=? AND sender_uid=? AND snapshot_id=?",
                    (bot, sender_uid, r["snapshot_id"]))

    def add_detail_part(self, bot: str, sender_uid: str, snapshot_id: str, part: int,
                        parts: int, items: list[dict], keep: int = 5) -> None:
        self.upsert_snapshot(bot, sender_uid, snapshot_id, parts=parts, keep=keep)
        self.db.execute(
            "INSERT OR IGNORE INTO snapshot_parts VALUES (?,?,?,?)",
            (bot, sender_uid, snapshot_id, part))
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("uid"), str):
                continue
            lat, lon = it.get("lat"), it.get("lon")
            self.db.execute(
                "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
                (bot, sender_uid, snapshot_id, it["uid"], it.get("kind"), it.get("name"),
                 lat if isinstance(lat, (int, float)) else None,
                 lon if isinstance(lon, (int, float)) else None,
                 1 if it.get("remarks") else 0,
                 json.dumps(it, separators=(",", ":"))))

    def latest_snapshot(self, bot: str, sender_uid: str) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM snapshots WHERE bot=? AND sender_uid=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (bot, sender_uid)).fetchone()
        if not r:
            return None
        received = self.db.execute(
            "SELECT COUNT(*) FROM snapshot_parts WHERE bot=? AND sender_uid=? AND snapshot_id=?",
            (bot, sender_uid, r["snapshot_id"])).fetchone()[0]
        return {
            "snapshot_id": r["snapshot_id"],
            "summary": json.loads(r["summary"]) if r["summary"] else None,
            "parts": r["parts"],
            "parts_received": received,
            "created_at": r["created_at"],
        }

    def latest_sender(self, bot: str) -> str | None:
        r = self.db.execute(
            "SELECT sender_uid FROM snapshots WHERE bot=? ORDER BY created_at DESC, rowid DESC"
            " LIMIT 1", (bot,)).fetchone()
        return r["sender_uid"] if r else None

    def items(self, bot: str, sender_uid: str, snapshot_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT uid, kind, name, lat, lon, has_notes FROM items"
            " WHERE bot=? AND sender_uid=? AND snapshot_id=? ORDER BY rowid",
            (bot, sender_uid, snapshot_id)).fetchall()
        return [dict(r, has_notes=bool(r["has_notes"])) for r in rows]

    def item(self, bot: str, sender_uid: str, snapshot_id: str, uid: str) -> dict | None:
        r = self.db.execute(
            "SELECT data FROM items WHERE bot=? AND sender_uid=? AND snapshot_id=? AND uid=?",
            (bot, sender_uid, snapshot_id, uid)).fetchone()
        return json.loads(r["data"]) if r else None
