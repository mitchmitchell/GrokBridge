"""End to end: the real service against the fake TAK Server.

The acceptance round trip from the plan, with spec-derived messages in place
of captured ones: MITCH messages GROK-1, the webhook fires, the bot reads
its inbox, looks MITCH up, places a marker and replies.
"""

import asyncio
import json

from takbridge import cli, webhook
from takbridge.service import Service
from fake_tak import FakeTakServer
from helpers import SECRET, fixture, groklink_event, make_config
from test_webhook import Receiver


def test_round_trip(tmp_path):
    hook = Receiver()

    async def scenario():
        fake = FakeTakServer()
        await fake.start()
        sock = str(tmp_path / "tb.sock")
        cfg = make_config(cot_url=f"tcp://127.0.0.1:{fake.port}", db=str(tmp_path / "tb.db"),
                          socket_path=sock, webhook=hook.url, extra_bots=())
        svc = Service(cfg)
        run = asyncio.create_task(svc.run())
        try:
            await fake.wait_connected()

            # Presence: the bot announces itself as a contact.
            pres = await fake.wait_for(lambda e: e.type == "a-f-G-U-C")
            assert b"callsign=\"GROK-1\"" in pres

            # MITCH reports his position, then sends a direct message.
            await fake.push(fixture("position.xml"))
            await fake.push(fixture("direct_chat.xml"))
            for _ in range(200):
                if hook.calls:
                    break
                await asyncio.sleep(0.02)
            body, sig = hook.calls[0]
            assert webhook.verify(body, SECRET, sig)
            payload = json.loads(body)
            assert payload["kind"] == "chat" and payload["from_callsign"] == "MITCH"
            assert payload["sender_position"]["lat"] == 35.0

            async def tak(*argv):
                return await asyncio.to_thread(cli.run, list(argv), bot="GROK-1",
                                               socket_path=sock)

            code, out = await tak("inbox")
            assert code == 0 and out["messages"][0]["message_id"] == "m-001"
            code, out = await tak("where", "MITCH")
            assert code == 0 and out["lat"] == 35.0 and out["stale"] is False
            code, out = await tak("mark", "--to", "MITCH", "--lat", "35.0045", "--lon",
                                  "-90.0", "--name", "Trailhead")
            assert code == 0 and out["uid"].startswith("takbridge-GROK-1-")
            marker = await fake.wait_for(lambda e: e.uid == out["uid"])
            assert b"Trailhead" in marker and b'<dest callsign="MITCH"' in marker
            code, out = await tak("say", "--to", "MITCH", "Marked", "Trailhead", "500", "m",
                                  "north", "of", "you.")
            assert code == 0
            chat = await fake.wait_for(lambda e: e.type == "b-t-f")
            assert b"Marked Trailhead 500 m north of you." in chat

            # A refused command, as in the plan's example.
            code, out = await tak("mark", "--to", "MITCH", "--lat", "135", "--lon", "-90",
                                  "--name", "X")
            assert code == 1 and out == {"ok": False, "error": "lat must be between -90 and 90"}

            # A plugin request with a summary, then its detail.
            await fake.push(groklink_event({"v": 1, "msg": "request", "snapshot_id": "s1",
                                            "text": "Which route?",
                                            "summary": {"detail": "sending"}}))
            await fake.push(groklink_event({"v": 1, "msg": "detail", "snapshot_id": "s1",
                                            "part": 1, "parts": 1,
                                            "items": [{"uid": "r1", "kind": "route",
                                                       "name": "Route A", "lat": 35.0,
                                                       "lon": -90.0}]},
                                           uid="GrokLink.ANDROID-abc123.2"))
            for _ in range(200):
                if len(hook.calls) >= 2:
                    break
                await asyncio.sleep(0.02)
            assert json.loads(hook.calls[1][0])["kind"] == "request"
            await asyncio.sleep(0.1)
            code, out = await tak("sitrep")
            assert code == 0 and out["complete"] is True
            assert len(hook.calls) == 2  # detail fired no webhook

            # Status, then the server drops: the bridge reports it and reconnects.
            code, out = await tak("status")
            assert out["connected"] is True
            await fake.stop()
            await asyncio.sleep(0.2)
            code, out = await tak("status")
            assert out["connected"] is False
            code, out = await tak("say", "--to", "MITCH", "hi")
            assert code == 1 and "connection is down" in out["error"]
        finally:
            run.cancel()
            try:
                await run
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(scenario())
    finally:
        hook.close()
