import asyncio
import io
import json
import threading

import pytest

from takbridge import cli, control, sshgate
from takbridge.core import Bridge
from takbridge.store import Store
from helpers import fixture, make_config


# ------------------------------------------------------------------ gate --

@pytest.mark.parametrize("cmd,argv", [
    ("tak inbox", ["inbox"]),
    ("tak inbox --peek", ["inbox", "--peek"]),
    ("tak say --to MITCH Marked Trailhead 500 m north of you.",
     ["say", "--to", "MITCH", "Marked", "Trailhead", "500", "m", "north", "of", "you."]),
    ("tak mark --to MITCH --lat 35.0045 --lon -90 --name 'Trail head'",
     ["mark", "--to", "MITCH", "--lat", "35.0045", "--lon", "-90", "--name", "Trail head"]),
    ("tak", []),  # JSON on stdin
])
def test_gate_allows_tak_commands(cmd, argv):
    assert sshgate.parse(cmd) == argv


@pytest.mark.parametrize("cmd,why", [
    (None, "no command"),
    ("bash", "only runs tak"),
    ("/bin/sh -c id", "only runs tak"),
    ("tak inbox; rm -rf /", "metacharacters"),
    ("tak say --to MITCH $(id)", "metacharacters"),
    ("tak say --to MITCH `id`", "metacharacters"),
    ("tak inbox | nc evil 1", "metacharacters"),
    ("tak inbox && id", "metacharacters"),
    ("tak say --to MITCH hi > /tmp/x", "metacharacters"),
    ("tak rm -rf", "unknown tak command"),
    ("tak --bot GROK-2 inbox", "--bot is not allowed"),
    ("tak inbox --bot=GROK-2", "--bot is not allowed"),
    ("tak say 'unterminated", "cannot parse"),
])
def test_gate_rejects_everything_else(cmd, why):
    with pytest.raises(sshgate.GateError, match=why):
        sshgate.parse(cmd)


def test_gate_main_refuses_bad_bot_argument(capsys):
    assert sshgate.main(["GROK-1; id"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "gate misconfigured"


# --------------------------------------------------- cli over the socket --

@pytest.fixture
def served(tmp_path):
    """A real control socket in a background loop, every caller allowed."""
    sock = str(tmp_path / "tb.sock")
    cfg = make_config(socket_path=sock)
    store = Store(":memory:")
    sent = []
    bridge = Bridge(cfg, store, send=lambda b, d: sent.append((b, d)) or True,
                    connected=lambda b: True)
    bridge.handle_event("GROK-1", fixture("position.xml"))
    bridge.handle_event("GROK-1", fixture("direct_chat.xml"))
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def start():
        await control.serve(sock, bridge.call, allowed=lambda uid: True)
        ready.set()

    t = threading.Thread(target=lambda: (loop.run_until_complete(start()), loop.run_forever()),
                         daemon=True)
    t.start()
    ready.wait(5)
    yield sock, sent
    loop.call_soon_threadsafe(loop.stop)


def test_cli_args_form(served):
    sock, sent = served
    code, out = cli.run(["--bot", "GROK-1", "inbox", "--peek"], socket_path=sock)
    assert code == 0 and out["messages"][0]["text"].startswith("Mark the trailhead")
    code, out = cli.run(["say", "--to", "MITCH", "Marked", "it."], bot="GROK-1",
                        socket_path=sock)
    assert code == 0 and out["ok"]
    assert b"Marked it." in sent[-1][1]


def test_cli_stdin_json_form_keeps_text_intact(served):
    sock, sent = served
    text = 'Marked "Trail head" & <ok>; $(not run)'
    stdin = io.StringIO(json.dumps({"cmd": "say", "to": "MITCH", "text": text,
                                    "bot": "GROK-2"}))
    code, out = cli.run([], bot="GROK-1", stdin=stdin, socket_path=sock)
    assert code == 0, out
    assert sent[-1][0] == "GROK-1"  # the "bot" field in stdin is ignored
    from takbridge import cot
    assert cot.parse_event(sent[-1][1]).find("remarks").text == text


def test_cli_failure_is_json_with_nonzero_exit(served):
    sock, _ = served
    code, out = cli.run(["--bot", "GROK-1", "mark", "--to", "MITCH", "--lat", "135",
                         "--lon", "-90", "--name", "X"], socket_path=sock)
    assert code == 1 and out == {"ok": False, "error": "lat must be between -90 and 90"}
    code, out = cli.run(["--bot", "GROK-1", "frobnicate"], socket_path=sock)
    assert code == 1 and "invalid choice" in out["error"]
    code, out = cli.run(["inbox"], socket_path=sock)
    assert code == 1 and out["error"] == "no bot: use --bot NAME"


def test_cli_reports_missing_service(tmp_path):
    code, out = cli.run(["--bot", "GROK-1", "status"], socket_path=str(tmp_path / "none"))
    assert code == 1 and out["error"].startswith("cannot reach tak-bridged")


def test_socket_refuses_unauthorised_callers(tmp_path):
    sock = str(tmp_path / "deny.sock")

    async def go():
        server = await control.serve(sock, lambda b, t, a: {"ok": True},
                                     allowed=lambda uid: False)
        out = await asyncio.to_thread(control.request, sock, "GROK-1", "inbox", {})
        server.close()
        return out

    assert asyncio.run(go()) == {"ok": False, "error": "not allowed"}


def test_authorizer_allows_self_and_refuses_strangers():
    import os
    allowed = control.make_authorizer("no-such-user-xyz", "no-such-group-xyz")
    assert allowed(os.getuid())
    assert not allowed(None)
    assert not allowed(4_000_000)  # no such user


@pytest.mark.parametrize("cmd", ["rab", "circle", "rectangle"])
def test_gate_allows_new_map_commands(cmd):
    assert sshgate.parse(f"tak {cmd}") == [cmd]
