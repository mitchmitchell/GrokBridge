import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from takbridge import webhook
from helpers import SECRET


class Receiver:
    """A tiny HTTP server that fails the first `fail` calls."""

    def __init__(self, fail=0):
        self.calls = []
        self.fail = fail
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.calls.append((body, self.headers.get(webhook.SIGNATURE_HEADER)))
                code = 500 if len(outer.calls) <= outer.fail else 204
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/hook"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_signature_round_trip():
    body = webhook.encode({"bot": "GROK-1", "text": "héllo"})
    sig = webhook.sign(body, SECRET)
    assert webhook.verify(body, SECRET, sig)
    assert not webhook.verify(body + b" ", SECRET, sig)
    assert not webhook.verify(body, "wrong-secret-xxxxxxxx", sig)


def test_delivers_signed_body():
    r = Receiver()
    ok = asyncio.run(webhook.notify(r.url, {"bot": "GROK-1", "message_id": "m1"}, SECRET,
                                    retries=0))
    r.close()
    assert ok
    body, sig = r.calls[0]
    assert json.loads(body) == {"bot": "GROK-1", "message_id": "m1"}
    assert webhook.verify(body, SECRET, sig)


def test_retries_then_succeeds():
    r = Receiver(fail=2)
    ok = asyncio.run(webhook.notify(r.url, {"bot": "GROK-1"}, SECRET, retries=3, backoff=0.01))
    r.close()
    assert ok and len(r.calls) == 3


def test_gives_up_after_retries():
    r = Receiver(fail=99)
    ok = asyncio.run(webhook.notify(r.url, {"bot": "GROK-1"}, SECRET, retries=3, backoff=0.01))
    r.close()
    assert not ok and len(r.calls) == 4


def test_unreachable_host_returns_false_quickly():
    ok = asyncio.run(webhook.notify("http://127.0.0.1:9/none", {"bot": "GROK-1"}, SECRET,
                                    timeout=1, retries=1, backoff=0.01))
    assert not ok


def test_extra_headers_are_sent():
    seen = {}
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            # Header names are case-insensitive; urllib normalises their case.
            seen.update({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ok = asyncio.run(webhook.notify(f"http://127.0.0.1:{httpd.server_port}/x", {"bot": "B"},
                                    SECRET, retries=0,
                                    headers={"Authorization": "Bearer k1"}))
    httpd.shutdown()
    assert ok and seen["authorization"] == "Bearer k1"
    assert seen[webhook.SIGNATURE_HEADER.lower()]
