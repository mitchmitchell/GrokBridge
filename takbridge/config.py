"""Configuration: one INI file, readable only by the service user.

INI (configparser) rather than TOML so the bridge runs on any Python 3.10+
and matches how PyTAK itself is configured. See config.example.ini.
"""

from __future__ import annotations

import configparser
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

CALLSIGN_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,40}$")

# PyTAK TLS settings a bot section may carry (names from pytak.constants).
TLS_KEYS = (
    "PYTAK_TLS_CLIENT_CERT",
    "PYTAK_TLS_CLIENT_KEY",
    "PYTAK_TLS_CLIENT_CAFILE",
    "PYTAK_TLS_CLIENT_PASSWORD",
    "PYTAK_TLS_DONT_CHECK_HOSTNAME",
    "PYTAK_TLS_DONT_VERIFY",
    "PYTAK_TLS_SERVER_EXPECTED_HOSTNAME",
)


@dataclass
class Bot:
    callsign: str
    webhook_url: str
    uid: str
    tls: dict = field(default_factory=dict)
    token_sha256: str | None = None  # phase 8 (MCP)


@dataclass
class Limits:
    chat_chars: int = 2000
    name_chars: int = 40
    remarks_chars: int = 500
    outbound_per_minute: int = 20
    stale_after_s: int = 600
    request_bytes: int = 256 * 1024
    request_items: int = 200
    snapshots_kept: int = 5


@dataclass
class Config:
    cot_url: str
    lat: float
    lon: float
    group: str = "Cyan"
    role: str = "Team Member"
    presence_interval_s: int = 30
    presence_stale_s: int = 120
    db_path: str = "/var/lib/takbridge/takbridge.db"
    socket_path: str = "/run/takbridge/takbridge.sock"
    webhook_secret: str = ""
    webhook_timeout_s: float = 10.0
    webhook_retries: int = 3
    bot_login_user: str = "takbot"
    admin_group: str = "takadmin"
    limits: Limits = field(default_factory=Limits)
    bots: dict[str, Bot] = field(default_factory=dict)

    def bot(self, callsign: str) -> Bot:
        try:
            return self.bots[callsign]
        except KeyError:
            raise KeyError(f"unknown bot {callsign}") from None

    def pytak_section(self, bot: Bot) -> configparser.SectionProxy:
        """A PyTAK config section for one bot's connection."""
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # keep PyTAK's upper-case keys
        values = {"COT_URL": self.cot_url, "PYTAK_NO_HELLO": "1"}
        values.update(bot.tls)
        cp[bot.callsign] = values
        return cp[bot.callsign]


def _bot_uid(callsign: str) -> str:
    # Stable across restarts, so ATAK keeps one contact per bot.
    return "takbridge-" + re.sub(r"[^A-Za-z0-9_.\-]", "_", callsign)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load(path: str | Path) -> Config:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    with open(path, encoding="utf-8") as f:
        cp.read_file(f)
    return parse(cp)


def loads(text: str) -> Config:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read_string(text)
    return parse(cp)


def parse(cp: configparser.ConfigParser) -> Config:
    if "bridge" not in cp:
        raise ValueError("config: missing [bridge] section")
    b = cp["bridge"]

    def need(key: str) -> str:
        v = b.get(key, "").strip()
        if not v:
            raise ValueError(f"config: [bridge] {key} is required")
        return v

    cfg = Config(
        cot_url=need("cot_url"),
        lat=float(need("lat")),
        lon=float(need("lon")),
    )
    if not (-90 <= cfg.lat <= 90 and -180 <= cfg.lon <= 180):
        raise ValueError("config: lat/lon out of range")
    cfg.group = b.get("group", cfg.group)
    cfg.role = b.get("role", cfg.role)
    cfg.presence_interval_s = b.getint("presence_interval_s", cfg.presence_interval_s)
    cfg.presence_stale_s = b.getint("presence_stale_s", cfg.presence_stale_s)
    if cfg.presence_stale_s <= cfg.presence_interval_s:
        raise ValueError("config: presence_stale_s must be longer than presence_interval_s")
    cfg.db_path = b.get("db_path", cfg.db_path)
    cfg.socket_path = b.get("socket_path", cfg.socket_path)
    cfg.webhook_secret = need("webhook_secret")
    if len(cfg.webhook_secret) < 16:
        raise ValueError("config: webhook_secret must be at least 16 characters")
    cfg.webhook_timeout_s = b.getfloat("webhook_timeout_s", cfg.webhook_timeout_s)
    cfg.webhook_retries = b.getint("webhook_retries", cfg.webhook_retries)
    cfg.bot_login_user = b.get("bot_login_user", cfg.bot_login_user)
    cfg.admin_group = b.get("admin_group", cfg.admin_group)

    if "limits" in cp:
        lim = cp["limits"]
        for name in Limits.__dataclass_fields__:
            if name in lim:
                setattr(cfg.limits, name, lim.getint(name))

    for section in cp.sections():
        if not section.startswith("bot:"):
            continue
        callsign = section[4:].strip()
        if not CALLSIGN_RE.match(callsign):
            raise ValueError(f"config: bad bot callsign {callsign!r}")
        s = cp[section]
        url = s.get("webhook_url", "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"config: [{section}] webhook_url must be http(s)")
        tls = {k: s[k] for k in TLS_KEYS if k in s}
        cfg.bots[callsign] = Bot(
            callsign=callsign,
            webhook_url=url,
            uid=s.get("uid", _bot_uid(callsign)),
            tls=tls,
            token_sha256=s.get("token_sha256") or None,
        )
    if not cfg.bots:
        raise ValueError("config: no [bot:CALLSIGN] sections")
    return cfg
