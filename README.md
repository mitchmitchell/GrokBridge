# tak-bridge

Lets AI bots take part in a TAK network as ordinary chat contacts. Each bot
appears in ATAK's contact list; when someone messages it, the bridge stores
the message and calls the bot's webhook; the bot answers by running `tak`
commands over SSH: reply, place a marker, look up positions and the markers,
routes and shapes people share. The bridge never talks to any LLM.

Checked live against TAK Server 5.8-RELEASE-103 with ATAK-CIV 5.8.0.10,
ATAK-CIV 5.6.0.12 (Play Store) and WinTAK-CIV 5.8.0.150.

## How it fits together

```
ATAK ──chat / shared items──▶ TAK Server ──TLS 8089──▶ tak-bridged ──webhook──▶ bot
ATAK ◀──reply / marker─────── TAK Server ◀──────────── tak-bridged ◀──ssh tak── bot
```

* `tak-bridged` holds one TLS connection per bot (PyTAK), announces the bot
  every 30 s, stores what arrives in SQLite, and serves a local control
  socket.
* `tak` talks to that socket. Over SSH the bot reaches it only through
  `takbridge-gate`, which fixes the bot's identity from the SSH key it used.

## Install (phase 7)

On the TAK Server host, as root. The bot's webhook URL works like a
password, so it and the bot's SSH public key go in a root-only file, never
in this repository:

    sudo install -m 600 /dev/null /root/takbridge-secrets.env
    sudo nano /root/takbridge-secrets.env
        BOT_WEBHOOK_URL=https://...
        BOT_SSH_KEY=ssh-ed25519 AAAA... comment
    sudo SECRETS=/root/takbridge-secrets.env bash deploy/install.sh

It creates the users `takbridge` (service) and `takbot` (bot login), group
`takadmin`, installs into `/opt/takbridge`, writes `/etc/takbridge/`, sets
up the SSH gate and starts the `tak-bridged` systemd unit. It does not touch
the TAK Server's configuration, users or groups, sshd's configuration, or the
firewall. Read the script first; every step is commented.

TAK Server side (done in its admin console, not by the script): issue the
bot a client certificate, add its user to the group it should share with
(for example `family`), and make that group active for the bot.

## Adding a bot

1. Issue a client certificate for the bot's callsign on the TAK Server and
   put it in the right group (see above).
2. Add a `[bot:CALLSIGN]` section to `/etc/takbridge/takbridge.ini` with its
   `webhook_url` and certificate paths (see `config.example.ini`).
3. Add its SSH key to `/home/takbot/.ssh/authorized_keys`:

       command="/opt/takbridge/venv/bin/takbridge-gate CALLSIGN",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty,no-user-rc ssh-ed25519 AAAA...

4. `sudo systemctl restart tak-bridged`

To remove a bot, delete its config section and its key line, and restart.

## The webhook

On each new direct message the bridge POSTs JSON to the bot's webhook:

```json
{"bot": "GROK-1", "kind": "chat", "message_id": "e71f84c2-...",
 "from_callsign": "AE5HO/TAK-TABLET", "from_uid": "ANDROID-...",
 "text": "Mark the trailhead 500 m north of me.", "time": "2026-11-17T18:31:47Z",
 "sender_position": {"lat": 35.0, "lon": -90.0, "time": "2026-11-17T18:31:30Z",
                     "age_s": 17, "speed_mps": 0.0, "course_deg": 305.1}}
```

`kind` is `chat` for ATAK chat and `request` for the GrokLink example
plugin. The header `X-TakBridge-Signature` is the hex HMAC-SHA256 of the
exact body bytes with the shared `webhook_secret`. Verify it before acting:

```python
import hashlib, hmac
ok = hmac.compare_digest(
    hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), signature)
```

A failed call is retried three times; the message stays unread in the inbox
either way. `tools/webhook_receiver.py` is a working example receiver.
`deploy/helper-skill.md` is a ready-to-use instruction set for the bot's own
agent: how to read vs. create map items, every command with JSON examples,
and worked examples.

## The tak commands

Every command prints one JSON object with `"ok"`; failures add `"error"`
and exit non-zero. Over SSH, send the request as JSON on stdin; SSH loses
the quoting of the bot's own shell, while stdin passes text untouched:

    echo '{"cmd":"say","to":"AE5HO/TAK-TABLET","text":"On my way."}' | ssh takbot@zodiac tak

| Command | Result |
| --- | --- |
| `inbox [--peek]` | Unread messages; marks them read unless `--peek` |
| `say --to CALLSIGN TEXT` | Direct chat to that person only |
| `mark --to CALLSIGN --lat N --lon N --name TEXT [--remarks TEXT]` | Point marker on that person's map only |
| `rab --to CALLSIGN (--lat N --lon N \| --from CALLSIGN) --bearing DEG --range M --name TEXT` | Range-and-bearing line |
| `circle --to CALLSIGN (--lat N --lon N \| --from CALLSIGN) --radius M --name TEXT` | Circular area |
| `rectangle --to CALLSIGN --lat N --lon N --lat2 N --lon2 N --name TEXT` | Rectangular area (two corners) |
| `where CALLSIGN` | Last position, speed, heading, age, stale, online |
| `contacts` | Callsigns seen, last seen, online |
| `status` | This bot's connection |
| `map [--kind marker\|route\|shape\|rab] [--name TEXT] [--near LAT LON --radius M] [--limit N]` | Shared map items, brief |
| `mapitem UID` | One shared map item in full (route points and waypoints, circle radius, range and bearing with end point) |
| `sitrep`, `items`, `item` | Snapshots from the GrokLink plugin (frozen example) |

Examples (live output, coordinates shortened):

    $ tak where AE5HO/TAK-TABLET
    {"ok": true, "callsign": "AE5HO/TAK-TABLET", "lat": 35.0, "lon": -90.0, "time": "...",
     "speed_mps": 0.0, "course_deg": 305.1, "age_s": 92, "stale": false, "online": true}

    $ tak map
    {"ok": true, "items": [
      {"uid": "...", "kind": "route", "name": "Capture Route", "points": 5, "length_m": 1852.1, ...},
      {"uid": "...", "kind": "rab", "name": "Capture Range", "range_m": 179.1, "bearing_deg": 115.46, ...},
      {"uid": "...", "kind": "shape", "name": "Capture Circle", "shape": "circle", "radius_m": 268.3, ...}],
     "more": false}

    $ tak mark --to AE5HO/TAK-TABLET --lat 135 --lon -90 --name X
    {"ok": false, "error": "lat must be between -90 and 90"}

Limits: chat 2000 characters, marker name 40, remarks 500, 20 outgoing
messages per bot per minute (refused, not queued), recipients must have been
seen on the network.

## What a bot can see

Exactly what the TAK Server sends its connection, which the TAK groups
decide: positions of users in its groups, chat addressed to it, and markers,
routes, shapes and range lines shared in its groups or sent to it. Shared
items cannot be told apart from items sent to the bot alone (the server
strips that routing), so they never wake the bot; it looks them up with
`map`. Items disappear at the stale time ATAK gave them. A bot never sees
another bot's inbox, map items or snapshots.

What a bot sends goes only to the named person: replies and markers carry a
`<marti><dest callsign=…/>` element, so the server delivers them to that
user alone (checked: they did not reach other devices in the group).

## Updating

After reviewing a new GrokBridge commit, as root:

    sudo bash /opt/takbridge/src/deploy/update.sh <commit>

It reinstalls the code at that commit and restarts the service; the
configuration, certificates, database and SSH gate stay as they are.

## The log

`journalctl -u tak-bridged` shows connects and disconnects, each chat and
plugin request (sender and message id), new shared map items, devices
leaving, every `tak` command with its result and who it was about, webhook
deliveries, and a summary every 15 minutes of what arrived. It never records
message text, marker names or coordinates. Set `TAKBRIDGE_LOG=DEBUG` in the
unit for more.

## Running and stopping

    sudo systemctl status tak-bridged
    sudo journalctl -u tak-bridged -f
    sudo systemctl stop tak-bridged

## Development

    python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
    .venv/bin/python -m pytest

`tests/fixtures/captured/` holds real traffic from the live server,
sanitized with `tools/sanitize.py`; it is the reference for every message
format. `tools/probe.py` connects as a bot and saves raw events, for new
captures.
